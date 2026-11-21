"""Tests for the canonical-host redirect middleware (issue #406, ruling part 2).

Cloud Run serves the service under two URL families. The OIDC ``state`` cookie
is host-scoped, so a login started on the non-canonical host fails at the
callback on the canonical host ("Invalid OAuth state", the #339 failure class).
The middleware normalizes every non-canonical entry to the canonical origin
(derived from ``AUTH_REDIRECT_URI``) before auth runs; canonical requests and
probe paths pass untouched, and the middleware is fail-safe (no canonical
origin derivable, or auth disabled => no redirect).
"""

from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient

from coach_web.app import create_app
from coach_web.config import Settings
from tests.test_auth_flow import (
    CLIENT_ID,
    SESSION_SECRET,
    _id_token_claims,
    _login_state,
    _mock_google_token_exchange,
)

CANONICAL_HOST = "coach-web-afff35tqxq-oa.a.run.app"
ALT_HOST = "coach-web-853194930683.europe-west6.run.app"
CANONICAL_ORIGIN = f"https://{CANONICAL_HOST}"
REDIRECT_URI = f"{CANONICAL_ORIGIN}/auth/callback"


def _app(**overrides: Any) -> Any:
    """Build an auth-enabled app with both Cloud Run host forms trusted."""
    values: dict[str, Any] = {
        "AUTH_ENABLED": True,
        "GOOGLE_OIDC_CLIENT_ID": CLIENT_ID,
        "GOOGLE_OIDC_CLIENT_SECRET": "test-client-secret",
        "AUTH_SESSION_SECRET": SESSION_SECRET,
        "AUTH_REDIRECT_URI": REDIRECT_URI,
        "TRUSTED_HOSTS": [CANONICAL_HOST, ALT_HOST],
    }
    values.update(overrides)
    return create_app(Settings(**values))


class TestRedirect:
    """Non-canonical hosts are normalized to the AUTH_REDIRECT_URI origin."""

    def test_non_canonical_host_redirects_to_canonical(self) -> None:
        """Host != canonical => 302 to the same path on the canonical origin."""
        with TestClient(_app(), base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/api/conversation", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == f"{CANONICAL_ORIGIN}/api/conversation"

    def test_redirect_preserves_query_string(self) -> None:
        """The query string survives the redirect."""
        with TestClient(_app(), base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/api/week?week=2026-W37&x=1", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == f"{CANONICAL_ORIGIN}/api/week?week=2026-W37&x=1"

    def test_redirect_runs_before_auth(self) -> None:
        """A protected path redirects (302) instead of answering 401 first."""
        with TestClient(_app(), base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/api/conversation", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"].startswith(CANONICAL_ORIGIN)


class TestPassThrough:
    """Canonical entries and fail-safe configurations never redirect."""

    def test_canonical_host_passes_through(self) -> None:
        """Host == canonical => untouched (reaches auth: 401, no redirect)."""
        with TestClient(_app(), base_url=CANONICAL_ORIGIN) as client:
            response = client.get("/api/nonexistent", follow_redirects=False)

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_unset_redirect_uri_is_fail_safe(self) -> None:
        """No AUTH_REDIRECT_URI => no canonical origin => no redirect."""
        with TestClient(_app(AUTH_REDIRECT_URI=""), base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/api/nonexistent", follow_redirects=False)

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_invalid_redirect_uri_is_fail_safe(self) -> None:
        """A relative/invalid AUTH_REDIRECT_URI derives no origin => no redirect."""
        with TestClient(
            _app(AUTH_REDIRECT_URI="/auth/callback"), base_url=f"https://{ALT_HOST}"
        ) as client:
            response = client.get("/api/nonexistent", follow_redirects=False)

        assert response.status_code == 401
        assert "location" not in response.headers

    def test_auth_disabled_never_redirects(self) -> None:
        """AUTH_ENABLED=false (dev/qa lanes) => no redirect even with a URI set."""
        app = create_app(
            Settings(
                AUTH_ENABLED=False,
                AUTH_REDIRECT_URI=REDIRECT_URI,
                TRUSTED_HOSTS=[CANONICAL_HOST, ALT_HOST],
            )
        )
        with TestClient(app, base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/api/nonexistent", follow_redirects=False)

        assert response.status_code == 404
        assert "location" not in response.headers


class TestProbeExemption:
    """The exact health-probe paths stay reachable regardless of Host (#316)."""

    @pytest.mark.parametrize("path", ["/health", "/healthz"])
    def test_probe_paths_exempt_on_non_canonical_host(self, path: str) -> None:
        """A trusted but non-canonical Host reaches /health{,z} without a redirect."""
        with TestClient(_app(), base_url=f"https://{ALT_HOST}") as client:
            response = client.get(path)

        assert response.status_code == 200

    def test_platform_probe_host_reaches_healthz(self) -> None:
        """The Cloud Run startup probe Host is neither host-checked nor redirected."""
        with TestClient(_app(), base_url="http://169.254.169.126") as client:
            response = client.get("/healthz")

        assert response.status_code == 200


class TestWiringOrder:
    """The host allowlist stays outermost; canonical redirect sits before auth."""

    def test_order_trusted_host_canonical_auth(self) -> None:
        """ProbeExemptTrustedHost -> CanonicalHost -> AuthMiddleware."""
        app = _app()
        names = [getattr(middleware.cls, "__name__", "") for middleware in app.user_middleware]

        assert names.index("ProbeExemptTrustedHostMiddleware") < names.index(
            "CanonicalHostMiddleware"
        )
        assert names.index("CanonicalHostMiddleware") < names.index("AuthMiddleware")

    def test_untrusted_host_is_still_rejected(self) -> None:
        """A Host outside TRUSTED_HOSTS gets 400, never a redirect (no open proxy)."""
        with TestClient(_app(), base_url="https://evil.example") as client:
            response = client.get("/", follow_redirects=False)

        assert response.status_code == 400
        assert response.text == "Invalid host header"


class TestCanonicalLoginFlow:
    """Cross-host entry normalizes before the host-scoped state cookie is set."""

    def test_cross_host_entry_redirects_without_setting_state(self) -> None:
        """Login started on the non-canonical host only redirects — no state cookie."""
        with TestClient(_app(), base_url=f"https://{ALT_HOST}") as client:
            response = client.get("/auth/login", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["location"] == f"{CANONICAL_ORIGIN}/auth/login"
        assert "cw_oidc_state" not in response.cookies
        assert not any(
            "cw_oidc_state" in header for header in response.headers.get_list("set-cookie")
        )

    @respx.mock
    def test_login_callback_round_trip_on_canonical_host(self, google_test_keys: Any) -> None:
        """Login -> callback with the state cookie succeeds on the canonical host."""
        with TestClient(_app(), base_url=CANONICAL_ORIGIN) as client:
            state = _login_state(client)
            id_token = google_test_keys.sign(_id_token_claims(nonce=state))
            _mock_google_token_exchange(google_test_keys, id_token)

            response = client.get(
                f"/auth/callback?code=good-code&state={state}", follow_redirects=False
            )

        assert response.status_code == 302
        assert response.headers["location"] == "/"
        assert "cw_session" in response.cookies
