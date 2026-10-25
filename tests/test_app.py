"""Tests for the Coach Web FastAPI application factory, healthcheck and static serving."""

import re
import sqlite3
from collections.abc import AsyncIterator, Sequence
from datetime import date
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from coach_web.agent import (
    DEFAULT_SYSTEM_PROMPT,
    REASSERT_CLAUSE,
    AgentEvent,
    apply_temporal_anchor,
)
from coach_web.agent_settings import save_agent_settings
from coach_web.app import CSP_POLICY, STATIC_DIR, create_app, resolve_version
from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import create_session_token
from coach_web.config import Settings
from coach_web.debrief import DEFAULT_ATHLETE_ID
from coach_web.objectives import ObjectiveProfile, save_objective_profile

# The exact minimal policy decided in the STRIDE #87 sign-off (conditions
# C2/C5) and shipped as middleware in #84, with the single #319 img-src
# exception for the Google avatar CDN. Hardcoded here — independent of
# the application constant — so an accidental policy change fails this test.
EXPECTED_CSP_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self'; "
    "img-src 'self' data: https://*.googleusercontent.com; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'"
)


class _CspProbeAgent:
    """Minimal agent yielding one token event, for response-header pins (#148)."""

    async def __aenter__(self) -> "_CspProbeAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self,
        message: str,
        *,
        history: list[Any] | None = None,
        pre_emitted_outcomes: Sequence[str] = (),
    ) -> AsyncIterator[AgentEvent]:
        """Yield a single token event for the supplied message."""
        yield AgentEvent(type="token", data={"text": "hello"})


class TestCreateApp:
    """Application factory behaviour."""

    def test_returns_fastapi_instance(self) -> None:
        """create_app() returns a configured FastAPI application."""
        app = create_app()

        assert isinstance(app, FastAPI)

    def test_sets_application_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The application exposes the Coach Web title and the resolved version (#198)."""
        monkeypatch.setenv("COACH_WEB_VERSION", "1.2.3-test")
        app = create_app()

        assert app.title == "Coach Web"
        assert app.version == "1.2.3-test"

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


class TestResolveVersion:
    """Version resolution contract (#198): env override > metadata > fallback.

    The reported version must never be a stale hardcoded value: release
    images get the git tag via the build-time ``COACH_WEB_VERSION`` override,
    local installs report the installed distribution metadata, and bare
    source checkouts degrade to the honest ``0.0.0`` sentinel.
    """

    def test_env_override_wins_over_installed_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC1 (#198): a build-time override beats installed metadata."""
        monkeypatch.setenv("COACH_WEB_VERSION", "0.9.4")

        assert resolve_version() == "0.9.4"

    def test_env_override_reaches_the_health_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The override flows through create_app into /healthz (the bug surface)."""
        monkeypatch.setenv("COACH_WEB_VERSION", "0.9.4")

        with TestClient(create_app()) as client:
            payload = client.get("/healthz").json()

        assert payload["version"] == "0.9.4"

    def test_empty_override_falls_through_to_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty override (image built without the build-arg) is ignored."""
        monkeypatch.setenv("COACH_WEB_VERSION", "")

        assert resolve_version() == version("coach-web")

    def test_unset_override_reports_installed_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC3 (#198): with no override, the reported version IS the metadata."""
        monkeypatch.delenv("COACH_WEB_VERSION", raising=False)

        assert resolve_version() == version("coach-web")

    def test_bare_checkout_falls_back_to_the_honest_sentinel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PackageNotFoundError (bare checkout) resolves to 0.0.0 — never 0.1.0."""
        monkeypatch.delenv("COACH_WEB_VERSION", raising=False)

        with patch("coach_web.app.version", side_effect=PackageNotFoundError):
            assert resolve_version() == "0.0.0"


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
        """The affordance renders inside the actions group, last visible (#340)."""
        html = auth_client.get("/").text

        actions_at = html.index('<div class="identity-actions">')
        link_at = html.index(self.INJECTED_SIGNIN_LINK)
        actions_close = html.index("</div>", actions_at)
        assert actions_at < link_at < actions_close

        # #340 AC1: the link is the LAST visible element of the actions group.
        # Trailing HTML comments (the untouched #319 avatar-slot marker) are
        # not visible elements, and the group's own closing tag is excluded.
        tail = html[link_at + len(self.INJECTED_SIGNIN_LINK) : actions_close]
        assert re.findall(r"<(?!!--)[a-zA-Z/]", tail) == []

    def test_authenticated_root_renders_the_fallback_avatar(self, auth_client: TestClient) -> None:
        """#319 supersedes byte-for-byte: a signed-in session renders the avatar.

        The pre-#319 contract served the static file byte-for-byte to
        authenticated sessions; #319 AC2 replaces that with the profile
        avatar injection. A session issued before #319 (no name/picture
        claims) degrades to the initial-letter fallback with the email as
        the native tooltip — no broken image, no template error.
        """
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie())

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK not in response.text
        assert self.SIGNIN_SLOT_PREFIX in response.text
        assert '<span class="avatar avatar--fallback"' in response.text
        assert 'title="frederic.pitteloud@gmail.com"' in response.text
        assert ">F</span>" in response.text

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


class TestProfileAvatarContract:
    """Google profile avatar in the identity bar (#319).

    A signed-in session renders the owner's Google profile picture
    rightmost in ``identity-actions`` with the profile name as the native
    ``title`` tooltip (AC2). Degradation is deterministic (AC3): no
    ``picture`` claim → initial-letter avatar from the email local-part;
    no ``name`` claim → the email address as tooltip; a pre-#319 session
    token (neither claim) exercises both fallbacks. Unauthenticated and
    auth-disabled lanes render no avatar (unchanged rendering). Interpolated
    claims are HTML-escaped — no user data is ever rendered as raw HTML.
    """

    OWNER_EMAIL = "frederic.pitteloud@gmail.com"
    SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
    PROFILE_NAME = "Frederic Pitteloud"
    PICTURE_URL = "https://lh3.googleusercontent.com/a/photo-id"
    INJECTED_SIGNIN_LINK = '<a class="signin-link" href="/auth/login">Sign in</a>'

    def _session_cookie(
        self, name: str | None = None, picture: str | None = None, ttl_seconds: int = 600
    ) -> str:
        """Craft a session token as if issued by a successful login."""
        return create_session_token(
            self.OWNER_EMAIL,
            self.SESSION_SECRET,
            ttl_seconds=ttl_seconds,
            name=name,
            picture=picture,
        )

    def _identity_actions(self, html: str) -> str:
        """Return the identity-actions block of the rendered page."""
        actions_at = html.index('<div class="identity-actions">')
        return html[actions_at : html.index("</div>", actions_at)]

    def test_authenticated_root_renders_the_profile_avatar(self, auth_client: TestClient) -> None:
        """AC2: a signed-in session renders the Google picture as the avatar."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        avatar = '<img class="avatar"'
        assert avatar in response.text
        assert f'src="{self.PICTURE_URL}"' in response.text
        assert 'referrerpolicy="no-referrer"' in response.text
        assert f'alt="{self.PROFILE_NAME}"' in response.text
        assert f'title="{self.PROFILE_NAME}"' in response.text

    def test_avatar_sits_rightmost_in_identity_actions(self, auth_client: TestClient) -> None:
        """AC2: the avatar follows the New chat button inside identity-actions."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )
        html = auth_client.get("/").text

        block = self._identity_actions(html)
        assert '<button\n              class="button button--ghost new-chat"' in block
        assert '<img class="avatar"' in block
        assert block.index("New chat") < block.index('<img class="avatar"')

    def test_avatar_falls_back_to_the_initial_without_picture(
        self, auth_client: TestClient
    ) -> None:
        """AC3: no picture claim → initial-letter avatar from the email local-part."""
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME))

        response = auth_client.get("/")

        assert response.status_code == 200
        assert '<img class="avatar"' not in response.text
        assert '<span class="avatar avatar--fallback" title="Frederic Pitteloud">F</span>' in (
            response.text
        )

    def test_avatar_tooltip_falls_back_to_the_email_without_name(
        self, auth_client: TestClient
    ) -> None:
        """AC3: no name claim → the email address is the native tooltip."""
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie(picture=self.PICTURE_URL))

        response = auth_client.get("/")

        assert response.status_code == 200
        assert '<img class="avatar"' in response.text
        assert f'src="{self.PICTURE_URL}"' in response.text
        assert f'alt="{self.OWNER_EMAIL}"' in response.text
        assert f'title="{self.OWNER_EMAIL}"' in response.text

    def test_avatar_claims_are_html_escaped(self, auth_client: TestClient) -> None:
        """nLPD: interpolated claims never render as raw HTML (no injection)."""
        hostile_name = 'Eve"><script>alert(1)</script>'
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=hostile_name, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        assert "<script>" not in response.text
        assert 'title="Eve&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;"' in response.text

    def test_unauthenticated_root_renders_no_avatar(self, auth_client: TestClient) -> None:
        """AC3: the unauthenticated variant is unchanged — no avatar markup."""
        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.INJECTED_SIGNIN_LINK in response.text
        assert 'class="avatar' not in response.text


class TestAvatarSignOutMenu:
    """Sign-out dropdown on the profile avatar (#323).

    The #319 avatar becomes the toggle of a minimal Alpine dropdown whose
    single item submits ``POST /auth/logout`` through a native form —
    POST-only is binding (logout-CSRF, ``auth/router.py``): never a GET
    link, never a fetch bypass. The dropdown shell is part of the
    authenticated avatar-slot substitution; the unauthenticated and
    auth-disabled lanes stay avatar-free (byte-identical — the raw static
    file keeps the inert #319 marker). The #319 avatar contract (tooltip,
    no-referrer, fallback) is preserved verbatim inside the toggle.
    """

    OWNER_EMAIL = "frederic.pitteloud@gmail.com"
    SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
    PROFILE_NAME = "Frederic Pitteloud"
    PICTURE_URL = "https://lh3.googleusercontent.com/a/photo-id"
    # The exact server-injected toggle markup (multiline, house attribute
    # style) — pinned through aria-haspopup so the ARIA state is bound to
    # the avatar toggle, not to any element on the page.
    TOGGLE_OPEN = (
        "<button\n"
        '                class="avatar-menu__toggle"\n'
        '                type="button"\n'
        '                aria-haspopup="menu"'
    )
    LOGOUT_FORM = '<form method="post" action="/auth/logout">'

    def _session_cookie(
        self, name: str | None = None, picture: str | None = None, ttl_seconds: int = 600
    ) -> str:
        """Craft a session token as if issued by a successful login."""
        return create_session_token(
            self.OWNER_EMAIL,
            self.SESSION_SECRET,
            ttl_seconds=ttl_seconds,
            name=name,
            picture=picture,
        )

    def _identity_actions(self, html: str) -> str:
        """Return the identity-actions block of the rendered page."""
        actions_at = html.index('<div class="identity-actions">')
        return html[actions_at : html.index("</div>", actions_at)]

    def _toggle_bounds(self, html: str) -> tuple[int, int]:
        """Return the (open, close) character offsets of the menu toggle."""
        open_at = html.index(self.TOGGLE_OPEN)
        return open_at, html.index("</button>", open_at)

    def test_avatar_renders_as_a_menu_toggle(self, auth_client: TestClient) -> None:
        """AC1: the signed-in avatar is the toggle of a menu (ARIA wired)."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.TOGGLE_OPEN in response.text
        assert ":aria-expanded=\"open ? 'true' : 'false'\"" in response.text
        open_at, close_at = self._toggle_bounds(response.text)
        img_at = response.text.index('<img class="avatar"')
        assert open_at < img_at < close_at

    def test_sign_out_item_submits_the_logout_form(self, auth_client: TestClient) -> None:
        """AC2: the menu's single item POSTs /auth/logout via a native form."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.LOGOUT_FORM in response.text
        assert ">Sign out</button>" in response.text
        # POST-only (logout-CSRF): no GET affordance may exist for logout.
        assert 'href="/auth/logout"' not in response.text

    def test_sign_out_form_sits_in_identity_actions(self, auth_client: TestClient) -> None:
        """The menu lives in the identity bar's actions group, after New chat."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )
        html = auth_client.get("/").text

        block = self._identity_actions(html)
        assert self.LOGOUT_FORM in block
        assert block.index("New chat") < block.index(self.TOGGLE_OPEN)

    def test_menu_is_hidden_until_opened(self, auth_client: TestClient) -> None:
        """AC1: the panel is closed by default — x-show + x-cloak (no flash)."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        assert 'x-data="{ open: false }"' in response.text
        panel_at = response.text.index('class="avatar-menu__panel"')
        panel = response.text[panel_at : response.text.index(">", panel_at)]
        assert 'x-show="open"' in panel
        assert "x-cloak" in panel
        assert 'role="menu"' in panel

    def test_click_outside_and_escape_dismiss_the_menu(self, auth_client: TestClient) -> None:
        """AC1: click-outside and Escape both close the menu."""
        auth_client.cookies.set(
            SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME, picture=self.PICTURE_URL)
        )

        response = auth_client.get("/")

        assert response.status_code == 200
        assert '@click.outside="open = false"' in response.text
        assert '@keydown.escape="open = false"' in response.text

    def test_fallback_avatar_renders_inside_the_toggle(self, auth_client: TestClient) -> None:
        """AC3: the initial-letter fallback keeps the #319 contract in the toggle."""
        auth_client.cookies.set(SESSION_COOKIE, self._session_cookie(name=self.PROFILE_NAME))

        response = auth_client.get("/")

        assert response.status_code == 200
        assert self.TOGGLE_OPEN in response.text
        open_at, close_at = self._toggle_bounds(response.text)
        span_at = response.text.index('<span class="avatar avatar--fallback"')
        assert open_at < span_at < close_at
        assert 'title="Frederic Pitteloud">F</span>' in response.text

    def test_unauthenticated_root_has_no_sign_out_menu(self, auth_client: TestClient) -> None:
        """AC3: the unauthenticated variant stays avatar-free and menu-free."""
        response = auth_client.get("/")

        assert response.status_code == 200
        assert "avatar-menu" not in response.text
        assert "/auth/logout" not in response.text
        assert 'class="avatar' not in response.text

    def test_static_file_keeps_the_inert_avatar_slot(self) -> None:
        """AC3: the raw static file is untouched — the menu is server-injected.

        /static/index.html and the auth-disabled lanes serve the file
        byte-for-byte; the dropdown exists only in the authenticated
        substitution, so those lanes render exactly today's page.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "<!-- #319 avatar-slot:" in html
        assert "avatar-menu" not in html
        assert "/auth/logout" not in html


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


# ---------------------------------------------------------------------------
# Agent-settings prompt assembly at the stream endpoint (MADR-0026, #361)
# ---------------------------------------------------------------------------


class _PromptProbeAgent:
    """Minimal agent carrying the real default prompt, for assembly pins."""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    async def __aenter__(self) -> "_PromptProbeAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self,
        message: str,
        *,
        history: list[Any] | None = None,
        pre_emitted_outcomes: Sequence[str] = (),
    ) -> AsyncIterator[AgentEvent]:
        """Yield a single done event for the supplied message."""
        yield AgentEvent(type="done", data={"message": "ok"})


class TestAgentSettingsPromptAssembly:
    """POST /api/agent/stream routes through the assembly seam (#361).

    Failure posture (MADR-0026, cyber C6): a settings read failure joins the
    existing digest-degrade boundary — the prompt falls back to the anchored
    immutable core and the turn never blocks on the settings store.
    """

    def _client_with(self, settings: Settings, agent: Any, session_factory: Any) -> TestClient:
        """Build a TestClient with a stub agent and a real session factory."""
        app = create_app(settings)
        app.state.agent_factory = MagicMock(return_value=agent)
        app.state.db_session_factory = session_factory
        return TestClient(app)

    async def _stream_once(self, client: TestClient) -> None:
        """Drive one stream request to completion (prompt side effects only)."""
        with client.stream(
            "POST", "/api/agent/stream", json={"message": "hi", "history": []}
        ) as response:
            response.read()

    async def test_no_settings_row_is_byte_equivalent_to_the_legacy_prompt(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, db_session_factory: Any
    ) -> None:
        """AC4 regression: no settings row → today's prompt, byte-for-byte."""

        class _FrozenDate(date):
            @classmethod
            def today(cls) -> "_FrozenDate":
                return _FrozenDate(2026, 10, 25)  # Sunday of 2026-W43

        monkeypatch.setattr("coach_web.app.date", _FrozenDate)
        agent = _PromptProbeAgent()
        client = self._client_with(settings, agent, db_session_factory)

        await self._stream_once(client)

        expected = apply_temporal_anchor(DEFAULT_SYSTEM_PROMPT, _FrozenDate(2026, 10, 25))
        assert agent.system_prompt == expected
        assert REASSERT_CLAUSE not in agent.system_prompt

    async def test_settings_row_reaches_the_prompt_in_precedence_order(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, db_session_factory: Any
    ) -> None:
        """A persisted row injects the fenced block below the anchor, above digests."""

        class _FrozenDate(date):
            @classmethod
            def today(cls) -> "_FrozenDate":
                return _FrozenDate(2026, 10, 25)

        monkeypatch.setattr("coach_web.app.date", _FrozenDate)
        async with db_session_factory() as session:
            await save_objective_profile(
                session,
                ObjectiveProfile.model_validate(
                    {
                        "primary_goal": {
                            "objective_type": "outcome",
                            "title": "Podium at the national crit",
                            "target_date": "2027-07-10",
                        }
                    }
                ),
            )
            await save_agent_settings(
                session,
                athlete_id=DEFAULT_ATHLETE_ID,
                orientation="Be pragmatic; prioritise recovery on tired weeks.",
                verbosity="concise",
                tone="supportive",
            )
        agent = _PromptProbeAgent()
        client = self._client_with(settings, agent, db_session_factory)

        await self._stream_once(client)

        prompt = agent.system_prompt
        assert "Today is 2026-10-25 (Sunday)." in prompt
        assert "non-authoritative" in prompt.lower()
        assert "| Be pragmatic; prioritise recovery on tired weeks." in prompt
        assert "Verbosity: concise" in prompt
        assert "Tone: supportive" in prompt
        # Precedence: anchor (core) → orientation block → prefs → digest → re-assert.
        assert prompt.index("Today is ") < prompt.index("OWNER ORIENTATION")
        assert prompt.index("(END)") < prompt.index("Primary outcome goal")
        assert prompt.index("Primary outcome goal") < prompt.index(REASSERT_CLAUSE)
        assert prompt.endswith(REASSERT_CLAUSE)

    async def test_settings_read_failure_fails_open_to_the_anchored_core(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch, db_session_factory: Any
    ) -> None:
        """A settings-store outage degrades like any unreadable store (cyber C6)."""

        class _FrozenDate(date):
            @classmethod
            def today(cls) -> "_FrozenDate":
                return _FrozenDate(2026, 10, 25)

        monkeypatch.setattr("coach_web.app.date", _FrozenDate)

        async def _failing_read(*args: Any, **kwargs: Any) -> None:
            raise SQLAlchemyError("settings store unavailable")

        monkeypatch.setattr("coach_web.app.get_agent_settings", _failing_read)
        agent = _PromptProbeAgent()
        client = self._client_with(settings, agent, db_session_factory)

        await self._stream_once(client)

        expected = apply_temporal_anchor(DEFAULT_SYSTEM_PROMPT, _FrozenDate(2026, 10, 25))
        assert agent.system_prompt == expected
        assert "OWNER ORIENTATION" not in agent.system_prompt
        assert REASSERT_CLAUSE not in agent.system_prompt

    async def test_corrupted_settings_row_fails_open_to_the_anchored_core(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        migrated_db: Any,
        db_session_factory: Any,
    ) -> None:
        """A raw-SQL-corrupted enum row degrades to the anchored core."""

        class _FrozenDate(date):
            @classmethod
            def today(cls) -> "_FrozenDate":
                return _FrozenDate(2026, 10, 25)

        monkeypatch.setattr("coach_web.app.date", _FrozenDate)
        conn = sqlite3.connect(migrated_db)
        try:
            # CHECK-defeating schema tampering (the CHECKs block an honest
            # raw-SQL enum violation) — the degraded read path must hold.
            conn.execute("DROP TABLE agent_settings")
            conn.execute(
                "CREATE TABLE agent_settings ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "athlete_id INTEGER NOT NULL, "
                "orientation TEXT, "
                "verbosity TEXT, "
                "tone TEXT, "
                "updated_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO agent_settings (athlete_id, verbosity, updated_at) "
                "VALUES (1, 'shouty', 'u')"
            )
            conn.commit()
        finally:
            conn.close()
        agent = _PromptProbeAgent()
        client = self._client_with(settings, agent, db_session_factory)

        await self._stream_once(client)

        expected = apply_temporal_anchor(DEFAULT_SYSTEM_PROMPT, _FrozenDate(2026, 10, 25))
        assert agent.system_prompt == expected
        assert "OWNER ORIENTATION" not in agent.system_prompt
