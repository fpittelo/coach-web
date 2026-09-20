"""Tests for the application-level whitelist middleware & route protection (#65).

Default-deny: every path is protected except the explicit public set (index
page, static assets, healthchecks, auth routes). ``/api/*`` therefore returns
401 without a valid session (AC3) and 403 for non-whitelisted identities (AC2).
"""

import pytest
from fastapi.testclient import TestClient

from coach_web.app import create_app
from coach_web.auth.middleware import AuthConfigError, is_public_path
from coach_web.auth.tokens import create_session_token
from coach_web.config import get_settings

OWNER_EMAIL = "frederic.pitteloud@gmail.com"
SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105


def _session_cookie(email: str = OWNER_EMAIL, ttl_seconds: int = 600) -> str:
    """Craft a session token as if issued by a successful login."""
    return create_session_token(email, SESSION_SECRET, ttl_seconds=ttl_seconds)


class TestRouteProtection:
    """AC3: /api/* requires a valid, whitelisted session."""

    def test_api_stream_requires_session(self, auth_client: TestClient) -> None:
        """GET /api/agent/stream without a session returns 401."""
        response = auth_client.get("/api/agent/stream", params={"message": "hi"})

        assert response.status_code == 401

    def test_api_approve_requires_session(self, auth_client: TestClient) -> None:
        """POST /api/plan/approve without a session returns 401."""
        response = auth_client.post("/api/plan/approve", json={})

        assert response.status_code == 401

    def test_unknown_api_path_requires_session(self, auth_client: TestClient) -> None:
        """Any /api/* path is protected even when no route matches."""
        response = auth_client.get("/api/nonexistent")

        assert response.status_code == 401

    def test_valid_session_reaches_router(self, auth_client: TestClient) -> None:
        """A whitelisted session passes the middleware through to the router."""
        auth_client.cookies.set("cw_session", _session_cookie())

        response = auth_client.get("/api/nonexistent")

        assert response.status_code == 404

    def test_valid_session_reaches_request_validation(self, auth_client: TestClient) -> None:
        """A whitelisted session reaches FastAPI validation (422, not 401)."""
        auth_client.cookies.set("cw_session", _session_cookie())

        response = auth_client.post("/api/plan/approve", json={})

        assert response.status_code == 422

    def test_non_whitelisted_email_is_forbidden(self, auth_client: TestClient) -> None:
        """AC2: a valid session for a non-whitelisted email returns 403."""
        auth_client.cookies.set("cw_session", _session_cookie(email="attacker@example.com"))

        response = auth_client.get("/api/nonexistent")

        assert response.status_code == 403
        assert response.json()["detail"] == "Email not whitelisted"

    def test_expired_session_is_unauthorized(self, auth_client: TestClient) -> None:
        """An expired session token returns 401."""
        auth_client.cookies.set("cw_session", _session_cookie(ttl_seconds=-10))

        response = auth_client.get("/api/nonexistent")

        assert response.status_code == 401

    def test_tampered_session_is_unauthorized(self, auth_client: TestClient) -> None:
        """A mutated session token returns 401."""
        auth_client.cookies.set("cw_session", _session_cookie() + "x")

        response = auth_client.get("/api/nonexistent")

        assert response.status_code == 401


class TestPublicPaths:
    """AC4: static assets, login page and healthchecks stay public."""

    def test_index_is_public(self, auth_client: TestClient) -> None:
        """GET / serves the SPA without a session."""
        assert auth_client.get("/").status_code == 200

    def test_static_assets_are_public(self, auth_client: TestClient) -> None:
        """GET /static/styles.css is served without a session."""
        response = auth_client.get("/static/styles.css")

        assert response.status_code == 200

    def test_health_is_public(self, auth_client: TestClient) -> None:
        """GET /health stays public for Cloud Run liveness probes."""
        assert auth_client.get("/health").status_code == 200

    def test_healthz_is_public(self, auth_client: TestClient) -> None:
        """GET /healthz stays public for Cloud Run startup probes."""
        assert auth_client.get("/healthz").status_code == 200

    def test_login_route_is_public(self, auth_client: TestClient) -> None:
        """GET /auth/login is reachable without a session (redirects to Google)."""
        response = auth_client.get("/auth/login", follow_redirects=False)

        assert response.status_code == 302

    def test_docs_are_protected_by_default_deny(self, auth_client: TestClient) -> None:
        """Non-public, non-API paths (e.g. /docs) are protected too."""
        response = auth_client.get("/docs")

        assert response.status_code == 401


class TestPathNormalization:
    """Defense-in-depth: the public-path check normalizes the raw path first."""

    def test_traversal_from_static_to_api_is_not_public(self) -> None:
        """Dot-segments cannot reclassify a protected path as public."""
        assert is_public_path("/static/../api/agent/stream") is False

    def test_traversal_from_auth_to_api_is_not_public(self) -> None:
        """Traversal via the auth prefix cannot reach protected paths."""
        assert is_public_path("/auth/../api/plan/approve") is False

    def test_trailing_slash_is_tolerated(self) -> None:
        """Trailing slashes do not change the public classification."""
        assert is_public_path("/health/") is True
        assert is_public_path("/static/styles.css/") is True

    def test_redundant_separators_are_collapsed(self) -> None:
        """Double slashes do not change the public classification."""
        assert is_public_path("/static//styles.css") is True

    def test_protocol_relative_root_fails_closed(self) -> None:
        """'//' is preserved by POSIX normalization and fails closed."""
        assert is_public_path("//health") is False

    def test_root_relative_traversal_fails_closed(self) -> None:
        """Root-relative dot-segments resolve to protected paths."""
        assert is_public_path("/../api/x") is False

    def test_relative_path_with_dotdot_fails_closed(self) -> None:
        """Paths that survive normalization with '..' segments fail closed."""
        assert is_public_path("../../../etc/passwd") is False


class TestAuthDisabled:
    """Local-dev ergonomics: auth is opt-in and default-off."""

    def test_api_open_when_disabled(self) -> None:
        """Without AUTH_ENABLED, requests are not intercepted (404, not 401)."""
        with TestClient(create_app()) as client:
            response = client.get("/api/nonexistent")

        assert response.status_code == 404

    def test_middleware_not_registered_when_disabled(self) -> None:
        """The middleware is absent from the stack when auth is disabled."""
        app = create_app()

        registered_names = [
            getattr(middleware.cls, "__name__", "") for middleware in app.user_middleware
        ]
        assert "AuthMiddleware" not in registered_names


class TestFailClosedConfiguration:
    """AUTH_ENABLED=true with missing credentials must refuse to boot."""

    def _enabled_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Enable auth with all credentials present (tests unset one)."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("AUTH_SESSION_SECRET", SESSION_SECRET)
        get_settings.cache_clear()

    def test_missing_client_id_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GOOGLE_OIDC_CLIENT_ID raises AuthConfigError."""
        self._enabled_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "")
        get_settings.cache_clear()

        with pytest.raises(AuthConfigError, match="GOOGLE_OIDC_CLIENT_ID"):
            create_app()

    def test_missing_client_secret_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing GOOGLE_OIDC_CLIENT_SECRET raises AuthConfigError."""
        self._enabled_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "")
        get_settings.cache_clear()

        with pytest.raises(AuthConfigError, match="GOOGLE_OIDC_CLIENT_SECRET"):
            create_app()

    def test_missing_session_secret_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing AUTH_SESSION_SECRET raises AuthConfigError."""
        self._enabled_env(monkeypatch)
        monkeypatch.setenv("AUTH_SESSION_SECRET", "")
        get_settings.cache_clear()

        with pytest.raises(AuthConfigError, match="AUTH_SESSION_SECRET"):
            create_app()

    def test_disabled_app_never_validates(self) -> None:
        """Auth disabled: missing credentials are irrelevant (local dev)."""
        app = create_app()

        assert app.title == "Coach Web"
