"""Cache-Control posture of the session-variant ``/`` route (issue #412).

Forensics (comment 7075893715 on #406): after a successful sign-in the browser
rendered the *unauthenticated* landing page instead of the console. ``GET /``
answered with ``ETag`` + ``Last-Modified`` and **no** ``Cache-Control``, so
RFC 9111 §4.2 heuristic freshness let the browser reuse the landing shell for
the later console request.

``/`` is identity-sensitive: its body branches on the session cookie (landing
vs. console), so no cache may store it — both render paths answer
``Cache-Control: no-store`` (AC1). Static assets (``/static/*``) are
fingerprint-free but cache-safe and deliberately keep their current behavior
(AC3 — no cache-busting, no header change).
"""

import pytest
from fastapi.testclient import TestClient

from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import create_session_token

OWNER_EMAIL = "frederic.pitteloud@gmail.com"
SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
NO_STORE = "no-store"


def _session_cookie(ttl_seconds: int = 600) -> str:
    """Craft a session token as if issued by a successful login."""
    return create_session_token(OWNER_EMAIL, SESSION_SECRET, ttl_seconds=ttl_seconds)


class TestSessionVariantRoutesAreNotStored:
    """``/`` never reaches a cache, on any of its session-variant render paths."""

    def test_unauthenticated_landing_is_not_stored(self, auth_client: TestClient) -> None:
        """AC1: the public landing render answers ``Cache-Control: no-store``."""
        response = auth_client.get("/")

        assert response.status_code == 200
        assert 'href="/auth/login"' in response.text  # the landing variant
        assert response.headers.get("cache-control") == NO_STORE

    def test_authenticated_console_is_not_stored(self, auth_client: TestClient) -> None:
        """AC1: the authenticated console render answers ``Cache-Control: no-store``."""
        auth_client.cookies.set(SESSION_COOKIE, _session_cookie())

        response = auth_client.get("/")

        assert response.status_code == 200
        assert 'class="identity-bar"' in response.text  # the console variant
        assert response.headers.get("cache-control") == NO_STORE

    def test_invalid_session_fails_safe_and_is_not_stored(self, auth_client: TestClient) -> None:
        """AC2: the fail-safe landing branch (bad cookie) is not cacheable either."""
        auth_client.cookies.set(SESSION_COOKIE, "not-a-jwt")

        response = auth_client.get("/")

        assert response.status_code == 200
        assert 'href="/auth/login"' in response.text
        assert response.headers.get("cache-control") == NO_STORE


class TestStaticAssetsKeepTheirCacheBehavior:
    """AC3: ``/static/*`` is untouched — no no-store, no cache-busting (#412)."""

    @pytest.mark.parametrize("path", ["/static/landing.css", "/static/index.html"])
    def test_static_asset_is_not_marked_no_store(self, auth_client: TestClient, path: str) -> None:
        """Static files keep their current (fingerprint-free, cache-safe) headers."""
        response = auth_client.get(path)

        assert response.status_code == 200
        assert response.headers.get("cache-control") != NO_STORE


class TestPublicLegalPagesAreCacheable:
    """#457 AC4: the value-free legal pages opt into short public caching."""

    @pytest.mark.parametrize("path", ["/disclaimer", "/privacy"])
    def test_legal_page_is_public_and_cacheable(self, auth_client: TestClient, path: str) -> None:
        """The static legal pages answer the pinned public cache header."""
        response = auth_client.get(path)

        assert response.status_code == 200
        assert response.headers.get("cache-control") == "public, max-age=300"
