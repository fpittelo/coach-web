"""Tests for the Google OIDC login → callback → session flow (issue #65).

Google is mocked with respx (token endpoint + JWKS); ID tokens are signed
with a test RSA keypair. Covers AC1 (login flow) and the 403 whitelist
rejection at callback time (AC2).
"""

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from coach_web.auth.tokens import verify_session_token

OWNER_EMAIL = "frederic.pitteloud@gmail.com"
SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
CLIENT_ID = "test-client-id"
ISSUER = "https://accounts.google.com"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - public URL
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


def _id_token_claims(nonce: str, email: str = OWNER_EMAIL) -> dict[str, Any]:
    """Return valid Google ID-token claims bound to the login state/nonce."""
    import time

    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "google-user-123",
        "email": email,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        "nonce": nonce,
    }


def _login_state(client: TestClient) -> str:
    """Drive /auth/login and return the state bound to the client's cookie jar."""
    login = client.get("/auth/login", follow_redirects=False)
    assert login.status_code == 302
    return parse_qs(urlparse(login.headers["location"]).query)["state"][0]


def _state_cookie_cleared(response: Any) -> bool:
    """Return True when the response clears the single-use state cookie."""
    return any('cw_oidc_state=""' in cookie for cookie in response.headers.get_list("set-cookie"))


def _mock_google_token_exchange(
    google_test_keys: Any, id_token: str, status_code: int = 200
) -> None:
    """Mock the Google token endpoint and JWKS endpoint."""
    respx.post(GOOGLE_TOKEN_URL).mock(
        return_value=(
            Response(status_code, json={"id_token": id_token})
            if status_code == 200
            else Response(status_code, text="upstream error")
        )
    )
    respx.get(GOOGLE_JWKS_URL).mock(return_value=Response(200, json=google_test_keys.jwks))


class TestLoginRedirect:
    """AC1: /auth/login starts the OIDC authorization-code flow."""

    def test_login_redirects_to_google(self, auth_client: TestClient) -> None:
        """The redirect targets Google with the required OAuth parameters."""
        response = auth_client.get("/auth/login", follow_redirects=False)
        location = response.headers["location"]

        assert response.status_code == 302
        assert location.startswith(GOOGLE_AUTH_URL)
        params = {key: values[0] for key, values in parse_qs(urlparse(location).query).items()}
        assert params["client_id"] == CLIENT_ID
        assert params["response_type"] == "code"
        assert params["scope"] == "openid email"
        assert params["redirect_uri"].endswith("/auth/callback")
        assert params["state"]
        assert params["nonce"] == params["state"]

    def test_login_sets_state_cookie(self, auth_client: TestClient) -> None:
        """A short-lived HttpOnly state cookie binds the flow to the browser."""
        response = auth_client.get("/auth/login", follow_redirects=False)
        location = response.headers["location"]
        state_param = parse_qs(urlparse(location).query)["state"][0]

        assert response.cookies["cw_oidc_state"] == state_param

    def test_login_uses_explicit_redirect_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AUTH_REDIRECT_URI overrides the request-derived redirect URI."""
        from coach_web.app import create_app
        from coach_web.config import get_settings

        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", CLIENT_ID)
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("AUTH_SESSION_SECRET", SESSION_SECRET)
        monkeypatch.setenv("AUTH_REDIRECT_URI", "https://coach.example.ch/auth/callback")
        get_settings.cache_clear()

        with TestClient(create_app(get_settings()), base_url="https://testserver") as client:
            response = client.get("/auth/login", follow_redirects=False)

        assert "redirect_uri=https%3A%2F%2Fcoach.example.ch%2Fauth%2Fcallback" in (
            response.headers["location"]
        )


class TestCallback:
    """AC1/AC2: /auth/callback exchanges, verifies, whitelists and sessions."""

    @respx.mock
    async def test_successful_login_sets_session(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A whitelisted Google identity receives a stateless session cookie."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(_id_token_claims(nonce=state))
        _mock_google_token_exchange(google_test_keys, id_token)

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 302
        assert response.headers["location"] == "/"
        session = response.cookies["cw_session"]
        claims = verify_session_token(session, SESSION_SECRET)
        assert claims["email"] == OWNER_EMAIL
        assert _state_cookie_cleared(response)

    @respx.mock
    async def test_non_whitelisted_email_rejected_with_403(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """AC2: a valid Google identity outside the whitelist gets 403, no session."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(
            _id_token_claims(nonce=state, email="attacker@example.com")
        )
        _mock_google_token_exchange(google_test_keys, id_token)

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 403
        assert "cw_session" not in response.cookies
        assert _state_cookie_cleared(response)

    async def test_state_mismatch_rejected_with_400(self, auth_client: TestClient) -> None:
        """A state param not matching the state cookie is rejected (CSRF)."""
        _login_state(auth_client)

        response = auth_client.get(
            "/auth/callback?code=good-code&state=forged-state", follow_redirects=False
        )

        assert response.status_code == 400
        assert _state_cookie_cleared(response)

    async def test_missing_state_cookie_rejected_with_400(self, auth_client: TestClient) -> None:
        """A callback without a prior /auth/login (no state cookie) is rejected."""
        response = auth_client.get(
            "/auth/callback?code=good-code&state=any-state", follow_redirects=False
        )

        assert response.status_code == 400

    async def test_missing_code_rejected_with_400(self, auth_client: TestClient) -> None:
        """A callback without an authorization code is rejected."""
        state = _login_state(auth_client)

        response = auth_client.get(f"/auth/callback?state={state}", follow_redirects=False)

        assert response.status_code == 400

    @respx.mock
    async def test_invalid_id_token_rejected_with_401(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """An ID token failing verification (bad signature) returns 401."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(_id_token_claims(nonce=state)) + "tampered"
        _mock_google_token_exchange(google_test_keys, id_token)

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 401
        assert _state_cookie_cleared(response)

    @respx.mock
    async def test_nonce_mismatch_rejected_with_401(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """An ID token whose nonce does not match the flow state returns 401."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(_id_token_claims(nonce="replayed-nonce"))
        _mock_google_token_exchange(google_test_keys, id_token)

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 401

    @respx.mock
    async def test_token_exchange_failure_returns_502(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A failing Google token endpoint surfaces as 502."""
        state = _login_state(auth_client)
        _mock_google_token_exchange(google_test_keys, "unused", status_code=500)

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502

    @respx.mock
    async def test_token_exchange_without_id_token_returns_502(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A token response lacking id_token surfaces as 502."""
        state = _login_state(auth_client)
        respx.post(GOOGLE_TOKEN_URL).mock(return_value=Response(200, json={"x": "y"}))
        respx.get(GOOGLE_JWKS_URL).mock(return_value=Response(200, json=google_test_keys.jwks))

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502

    @respx.mock
    async def test_token_exchange_invalid_json_returns_502(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A non-JSON token response surfaces as 502."""
        state = _login_state(auth_client)
        respx.post(GOOGLE_TOKEN_URL).mock(
            return_value=Response(200, text="not-json", headers={"content-type": "text/plain"})
        )
        respx.get(GOOGLE_JWKS_URL).mock(return_value=Response(200, json=google_test_keys.jwks))

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502

    @respx.mock
    async def test_jwks_outage_returns_502(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A JWKS outage surfaces as 502 (upstream, not a bad credential)."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(_id_token_claims(nonce=state))
        respx.post(GOOGLE_TOKEN_URL).mock(return_value=Response(200, json={"id_token": id_token}))
        respx.get(GOOGLE_JWKS_URL).mock(return_value=Response(500))

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502

    @respx.mock
    async def test_token_endpoint_unreachable_returns_502(self, auth_client: TestClient) -> None:
        """A transport-level failure against Google surfaces as 502."""
        state = _login_state(auth_client)
        respx.post(GOOGLE_TOKEN_URL).mock(side_effect=httpx.ConnectError("unreachable"))

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502

    @respx.mock
    async def test_jwks_endpoint_unreachable_returns_502(
        self, auth_client: TestClient, google_test_keys: Any
    ) -> None:
        """A transport-level failure against the JWKS surfaces as 502."""
        state = _login_state(auth_client)
        id_token = google_test_keys.sign(_id_token_claims(nonce=state))
        respx.post(GOOGLE_TOKEN_URL).mock(return_value=Response(200, json={"id_token": id_token}))
        respx.get(GOOGLE_JWKS_URL).mock(side_effect=httpx.ConnectError("unreachable"))

        response = auth_client.get(
            f"/auth/callback?code=good-code&state={state}", follow_redirects=False
        )

        assert response.status_code == 502


class TestLogout:
    """Session termination stays stateless: the cookie is simply cleared.

    Logout is POST-only: a GET must return 405 so third-party pages cannot
    force-logout the owner cross-site (logout CSRF).
    """

    def test_logout_get_returns_405(self, auth_client: TestClient) -> None:
        """GET /auth/logout is rejected (no cross-site forced logout)."""
        response = auth_client.get("/auth/logout", follow_redirects=False)

        assert response.status_code == 405

    def test_logout_clears_cookie_and_redirects(self, auth_client: TestClient) -> None:
        """POST /auth/logout redirects home with a cleared session cookie."""
        response = auth_client.post("/auth/logout", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert 'cw_session=""' in response.headers["set-cookie"]

    def test_logout_invalidates_session(self, auth_client: TestClient) -> None:
        """After logout the browser no longer holds a usable session."""
        from coach_web.auth.tokens import create_session_token

        auth_client.cookies.set(
            "cw_session",
            create_session_token(OWNER_EMAIL, SESSION_SECRET, ttl_seconds=600),
        )
        assert auth_client.get("/api/nonexistent").status_code == 404

        response = auth_client.post("/auth/logout", follow_redirects=False)
        assert response.status_code == 302
        assert 'cw_session=""' in response.headers["set-cookie"]

        # The browser applies the clearing Set-Cookie: the jar drops the session.
        auth_client.cookies.clear()
        assert auth_client.get("/api/nonexistent").status_code == 401
