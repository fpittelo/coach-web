"""Tests for the Coach Web FastAPI application factory, healthcheck and static serving."""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coach_web.agent import AgentEvent
from coach_web.app import CSP_POLICY, STATIC_DIR, create_app
from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import create_session_token
from coach_web.config import Settings

# The exact minimal policy decided in the STRIDE #87 sign-off (conditions
# C2/C5) and shipped as middleware in #84. Hardcoded here — independent of
# the application constant — so an accidental policy change fails this test.
EXPECTED_CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
)


class _CspProbeAgent:
    """Minimal agent yielding one token event, for response-header pins (#148)."""

    async def __aenter__(self) -> "_CspProbeAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self, message: str, *, history: list[Any] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Yield a single token event for the supplied message."""
        yield AgentEvent(type="token", data={"text": "hello"})


class TestCreateApp:
    """Application factory behaviour."""

    def test_returns_fastapi_instance(self) -> None:
        """create_app() returns a configured FastAPI application."""
        app = create_app()

        assert isinstance(app, FastAPI)

    def test_sets_application_metadata(self) -> None:
        """The application exposes the Coach Web title and a resolved version."""
        app = create_app()

        assert app.title == "Coach Web"
        assert app.version
        assert app.version != "0.0.0"

    def test_mounts_static_directory(self) -> None:
        """Static assets are mounted under /static."""
        app = create_app()

        mounted_paths = {getattr(route, "path", None) for route in app.routes}

        assert "/static" in mounted_paths

    def test_registers_health_routes(self) -> None:
        """Both /health and the /healthz alias are registered."""
        app = create_app()

        registered_paths = {getattr(route, "path", None) for route in app.routes}

        assert "/health" in registered_paths
        assert "/healthz" in registered_paths

    def test_index_html_is_present_in_static_dir(self) -> None:
        """The Swiss minimalist placeholder index.html ships with the package."""
        index = STATIC_DIR / "index.html"

        assert index.is_file()
        assert "Coach Web" in index.read_text(encoding="utf-8")

    def test_cors_middleware_allows_configured_origin(self) -> None:
        """CORS middleware echoes the configured origin."""
        with TestClient(create_app()) as client:
            response = client.get("/health", headers={"Origin": "http://localhost:8000"})

        assert response.headers.get("access-control-allow-origin") == "http://localhost:8000"


class TestHealthEndpoint:
    """Healthcheck contract."""

    def test_health_returns_healthy_payload(self) -> None:
        """GET /health returns the canonical status payload."""
        with TestClient(create_app()) as client:
            response = client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "healthy"
        assert payload["service"] == "coach-web"
        assert payload["version"]
        assert payload["uptime_seconds"] >= 0

    def test_healthz_alias_matches_health(self) -> None:
        """GET /healthz is an alias of GET /health."""
        with TestClient(create_app()) as client:
            health = client.get("/health")
            healthz = client.get("/healthz")

        assert healthz.status_code == 200
        health_payload = health.json()
        healthz_payload = healthz.json()
        assert healthz_payload["status"] == health_payload["status"]
        assert healthz_payload["service"] == health_payload["service"]
        assert healthz_payload["version"] == health_payload["version"]


class TestStaticServing:
    """Static asset serving."""

    def test_root_serves_index_html(self) -> None:
        """GET / serves the single-page interface."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Coach Web" in response.text

    def test_static_mount_serves_index_html(self) -> None:
        """GET /static/index.html serves the same placeholder."""
        with TestClient(create_app()) as client:
            response = client.get("/static/index.html")

        assert response.status_code == 200
        assert "Coach Web" in response.text


class TestSignInAffordance:
    """Conditional sign-in affordance on the public landing route (#180).

    ``/`` is public by design (AC4 #65), but an unauthenticated visitor in an
    auth-enabled lane previously hit a silent 401 dead-end: the shell offered
    no path to ``/auth/login``. The route now reads the session cookie with
    the existing HS256 verification and conditionally injects a "Sign in"
    link into the identity bar. Fail-safe by contract: any cookie/validation
    error resolves to the unauthenticated variant — the landing page can
    never 500.
    """

    OWNER_EMAIL = "frederic.pitteloud@gmail.com"
    SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105

    # The exact server-injected identity-bar link (no x-show — the banner's
    # Alpine-gated sign-in action in the static file carries one, so this
    # exact string is unambiguous between the two surfaces).
    INJECTED_SIGNIN_LINK = '<a class="signin-link" href="/auth/login">Sign in</a>'
    SIGNIN_SLOT_PREFIX = "<!-- #180 signin-slot:"

    def _session_cookie(self, ttl_seconds: int = 600) -> str:
        """Craft a session token as if issued by a successful login."""
        return create_session_token(self.OWNER_EMAIL, self.SESSION_SECRET, ttl_seconds=ttl_seconds)

    def test_unauthenticated_root_surfaces_signin_link(self, auth_client: TestClient) -> None:
        """AC1: an unauthenticated visitor gets a visible Sign in affordance."""
        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK in response.text
        assert self.SIGNIN_SLOT_PREFIX not in response.text

    def test_signin_link_sits_in_the_identity_bar(self, auth_client: TestClient) -> None:
        """The affordance renders inside the identity bar's actions group."""
        html = auth_client.get("/").text

        actions_at = html.index('<div class="identity-actions">')
        link_at = html.index(self.INJECTED_SIGNIN_LINK)
        actions_close = html.index("</div>", actions_at)
        assert actions_at < link_at < actions_close

    def test_authenticated_root_is_unchanged(self, auth_client: TestClient) -> None:
        """A valid session serves the static file byte-for-byte (identity as today)."""
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie())

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK not in response.text
        assert self.SIGNIN_SLOT_PREFIX in response.text
        assert response.text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_auth_disabled_root_is_unchanged(self) -> None:
        """AC3: AUTH_ENABLED=false lanes (dev/qa) render exactly as today."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK not in response.text
        assert self.SIGNIN_SLOT_PREFIX in response.text
        assert response.text == (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_invalid_session_cookie_fails_safe(self, auth_client: TestClient) -> None:
        """A malformed cookie renders the unauthenticated variant — never a 500."""
        auth_client.cookies.set(SESSION_COOKIE, "not-a-jwt")

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK in response.text

    def test_expired_session_cookie_fails_safe(self, auth_client: TestClient) -> None:
        """An expired session renders the unauthenticated variant."""
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie(ttl_seconds=-10))

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK in response.text

    def test_signin_variant_keeps_the_page_contract(self, auth_client: TestClient) -> None:
        """The injected variant is the same page: brand, identity bar, composer."""
        response = auth_client.get("/")
        html = response.text

        assert "Coach Web" in html
        assert 'class="identity-bar"' in html
        assert '<form class="composer"' in html
        # CSP untouched (#180 constraint): the ASGI wrapper stamps the exact
        # policy on the injected variant too.
        assert response.headers["content-security-policy"] == CSP_POLICY


class TestLifespan:
    """Lifespan startup/shutdown handling."""

    def test_lifespan_populates_application_state(self) -> None:
        """Startup exposes resolved settings and a monotonic start timestamp."""
        app = create_app()

        with TestClient(app):
            assert app.state.settings is not None
            assert app.state.settings.service_name == "coach-web"
            assert app.state.started_at > 0

    def test_lifespan_shutdown_is_clean(self) -> None:
        """The context manager exits without raising."""
        app = create_app()

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


class TestRun:
    """CLI entrypoint."""

    @patch("coach_web.app.uvicorn")
    def test_run_invokes_uvicorn_factory(self, mock_uvicorn: MagicMock) -> None:
        """run() boots uvicorn against the application factory."""
        from coach_web.app import run
        from coach_web.config import get_settings

        get_settings.cache_clear()
        run()

        mock_uvicorn.run.assert_called_once()
        args, kwargs = mock_uvicorn.run.call_args
        assert args[0] == "coach_web.app:create_app"
        assert kwargs["factory"] is True
        assert kwargs["host"] == "0.0.0.0"  # noqa: S104
        assert kwargs["port"] == 8000


class TestTrustedHostMiddleware:
    """AC6 (#112): the Host allowlist rejects DNS-rebinding attempts."""

    def test_default_allowed_hosts_are_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TRUSTED_HOSTS defaults to loopback names only."""
        monkeypatch.delenv("TRUSTED_HOSTS", raising=False)

        assert Settings().TRUSTED_HOSTS == ["localhost", "127.0.0.1"]

    def test_evil_host_is_rejected(self) -> None:
        """A request with a non-allowlisted Host is rejected with 400."""
        app = create_app(Settings(TRUSTED_HOSTS=["localhost", "127.0.0.1"]))

        with TestClient(app, base_url="http://evil.com") as client:
            response = client.get("/health")

        assert response.status_code == 400

    def test_localhost_host_is_allowed(self) -> None:
        """A request with Host: localhost passes the allowlist."""
        app = create_app(Settings(TRUSTED_HOSTS=["localhost", "127.0.0.1"]))

        with TestClient(app, base_url="http://localhost") as client:
            response = client.get("/health")

        assert response.status_code == 200

    def test_loopback_ip_host_is_allowed(self) -> None:
        """A request with Host: 127.0.0.1 passes the allowlist."""
        app = create_app(Settings(TRUSTED_HOSTS=["localhost", "127.0.0.1"]))

        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = client.get("/health")

        assert response.status_code == 200

    def test_allowed_hosts_are_configurable(self) -> None:
        """A custom TRUSTED_HOSTS entry is honored (testability)."""
        app = create_app(Settings(TRUSTED_HOSTS=["coach.example.ch"]))

        with TestClient(app, base_url="http://coach.example.ch") as client:
            response = client.get("/health")

        assert response.status_code == 200


class TestCspMiddleware:
    """AC (#84, STRIDE #87 conditions C2/C5): the minimal v0.7 CSP everywhere.

    The vendored Alpine standard build compiles ``x-`` expressions at runtime,
    so ``script-src`` must carry ``'unsafe-eval'``; it is contained because
    ``script-src`` has no ``'unsafe-inline'`` (injected inline scripts are
    blocked) and DOMPurify strips every Alpine directive (#81). The
    middleware is registered outermost, so even middleware-generated error
    responses (TrustedHost 400, auth 401/403) carry the header.
    """

    def test_shipped_policy_matches_the_87_signoff(self) -> None:
        """The shipped constant equals the policy decided in #87 verbatim."""
        assert CSP_POLICY == EXPECTED_CSP_POLICY

    def test_index_response_carries_the_exact_policy(self) -> None:
        """GET / is stamped with the exact CSP directive string."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY

    def test_health_response_carries_the_exact_policy(self) -> None:
        """GET /health is stamped with the exact CSP directive string."""
        with TestClient(create_app()) as client:
            response = client.get("/health")

        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY

    def test_static_assets_carry_the_exact_policy(self) -> None:
        """Static assets are stamped too (single-origin delivery)."""
        with TestClient(create_app()) as client:
            for path in ("/static/styles.css", "/static/app.js"):
                response = client.get(path)
                assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY, path

    def test_middleware_generated_error_responses_carry_the_policy(self) -> None:
        """Even the TrustedHost 400 rejection path is stamped (outermost)."""
        app = create_app(Settings(TRUSTED_HOSTS=["localhost", "127.0.0.1"]))

        with TestClient(app, base_url="http://evil.com") as client:
            response = client.get("/health")

        assert response.status_code == 400
        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY

    def test_unhandled_exception_500_carries_the_exact_policy(self) -> None:
        """Even the ServerErrorMiddleware-rendered 500 carries the header (#148 AC1).

        Starlette builds ``ServerErrorMiddleware`` as the true outermost layer
        of its middleware stack, so a middleware registered via
        ``add_middleware`` runs inside it and cannot stamp the 500 it renders
        for an unhandled exception (the #127 review advisory). The CSP stamp
        must wrap the whole ASGI app so EVERY response — including 500s —
        carries the exact #87 policy.
        """
        app = create_app()

        @app.get("/boom", include_in_schema=False)
        async def boom() -> None:
            raise RuntimeError("boom")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")

        assert response.status_code == 500
        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY

    def test_sse_stream_response_carries_the_exact_policy(self, settings: Settings) -> None:
        """AC2 (#148): the SSE streaming response carries the exact #87 policy.

        Regression pin for the #127 review advisory: the header must survive
        any middleware reordering on the streaming path (no buffering).
        """
        app = create_app(settings)
        app.state.agent_factory = MagicMock(return_value=_CspProbeAgent())

        with TestClient(app) as client:
            with client.stream(
                "POST", "/api/agent/stream", json={"message": "hi", "history": []}
            ) as response:
                response.read()

        assert "text/event-stream" in response.headers["content-type"]
        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY

    def test_auth_401_response_carries_the_exact_policy(self, auth_client: TestClient) -> None:
        """AC2 (#148): the auth 401 rejection carries the exact #87 policy.

        Regression pin for the #127 review advisory: the whitelist boundary's
        own JSON error response must stay stamped after any reordering.
        """
        response = auth_client.post("/api/agent/stream", json={"message": "hi"})

        assert response.status_code == 401
        assert response.headers["content-security-policy"] == EXPECTED_CSP_POLICY
