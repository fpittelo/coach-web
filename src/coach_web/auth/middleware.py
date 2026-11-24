"""Application-level whitelist middleware (issue #65).

Default-deny: every path is protected except the explicit public set (index
page, static assets, healthchecks, auth routes). ``/api/*`` routes therefore
return 401 without a valid session (AC3), and any authenticated request whose
email is not whitelisted returns 403 (AC2) — the whitelist IS the access-control
boundary and is re-checked on every request.

Implemented as a pure-ASGI middleware (not BaseHTTPMiddleware) so the SSE
agent stream passes through untouched.
"""

import posixpath
from typing import Any
from urllib.parse import SplitResult, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from coach_web.auth.tokens import TokenError, verify_session_token
from coach_web.auth.whitelist import is_whitelisted
from coach_web.config import Settings

SESSION_COOKIE = "cw_session"

PUBLIC_PATHS = frozenset({"/", "/health", "/healthz", "/disclaimer", "/privacy"})
PUBLIC_PREFIXES = ("/static/", "/auth/")

PROBE_EXEMPT_PATHS = frozenset({"/health", "/healthz"})
"""Exact paths exempt from host validation and canonical-host redirect (#316).

The Cloud Run startup probe sends a platform-internal Host; those paths must
answer regardless of the Host header.
"""

_DEFAULT_PORTS = {"http": 80, "https": 443}


class AuthConfigError(RuntimeError):
    """AUTH_ENABLED=true but required auth settings are missing (fail closed)."""


def is_public_path(path: str) -> bool:
    """Return True for paths reachable without authentication (AC4).

    The raw ASGI path is normalized first (dot-segment traversal, redundant
    separators, trailing slashes) so e.g. ``/static/../api/...`` cannot be
    classified public; any residual ``..`` segment fails closed.
    """
    normalized = posixpath.normpath(path)
    if ".." in normalized.split("/"):
        return False
    return normalized in PUBLIC_PATHS or normalized.startswith(PUBLIC_PREFIXES)


def validate_auth_config(settings: Settings) -> None:
    """Fail closed when auth is enabled without its required credentials."""
    required = (
        ("GOOGLE_OIDC_CLIENT_ID", settings.GOOGLE_OIDC_CLIENT_ID),
        ("GOOGLE_OIDC_CLIENT_SECRET", settings.GOOGLE_OIDC_CLIENT_SECRET),
        ("AUTH_SESSION_SECRET", settings.AUTH_SESSION_SECRET),
    )
    missing = [name for name, value in required if not value]
    if missing:
        raise AuthConfigError("AUTH_ENABLED=true requires non-empty " + ", ".join(missing))
    if len(settings.AUTH_SESSION_SECRET.encode("utf-8")) < 32:
        raise AuthConfigError("AUTH_SESSION_SECRET must be at least 32 bytes for HS256 (RFC 7518)")


class AuthMiddleware:
    """Default-deny authentication & whitelist enforcement middleware."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce the whitelist boundary or pass the request through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = self._settings(scope)
        if not settings.AUTH_ENABLED or is_public_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        claims = self._session_claims(scope, settings)
        if claims is None:
            await self._reject(
                scope, receive, send, status_code=401, detail="Authentication required"
            )
            return
        if not is_whitelisted(claims.get("email"), settings.AUTH_WHITELIST_EMAILS):
            await self._reject(
                scope, receive, send, status_code=403, detail="Email not whitelisted"
            )
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _settings(scope: Scope) -> Settings:
        """Resolve the application settings from the ASGI scope."""
        request = Request(scope)
        settings: Settings = request.app.state.settings
        return settings

    @staticmethod
    def _session_claims(scope: Scope, settings: Settings) -> dict[str, Any] | None:
        """Return verified session claims, or None when unauthenticated."""
        request = Request(scope)
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        try:
            return verify_session_token(token, settings.AUTH_SESSION_SECRET)
        except TokenError:
            return None

    @staticmethod
    async def _reject(
        scope: Scope, receive: Receive, send: Send, status_code: int, detail: str
    ) -> None:
        """Emit a JSON error response without touching the downstream app."""
        response: Response = JSONResponse(status_code=status_code, content={"detail": detail})
        await response(scope, receive, send)


def _canonical_origin(settings: Settings) -> SplitResult | None:
    """Return the canonical origin parsed from ``AUTH_REDIRECT_URI``.

    Fail-safe by design (#406): auth disabled, an empty setting or a
    non-absolute URI derives no canonical origin, so no redirect is ever
    emitted (no redirect loop possible).
    """
    if not settings.AUTH_ENABLED:
        return None
    raw = settings.AUTH_REDIRECT_URI.strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        _ = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return parts


def _host_matches(scope: Scope, canonical: SplitResult) -> bool:
    """Return True when the request Host denotes the canonical origin."""
    headers = dict(scope["headers"])
    raw_host = headers.get(b"host", b"").decode("latin-1")
    try:
        request_host, request_port = _split_host(raw_host)
    except ValueError:
        return False
    if not request_host or request_host != (canonical.hostname or "").lower():
        return False
    if request_port is None:
        # A port-less Host is the scheme default at the edge (Cloud Run
        # terminates TLS and forwards the public hostname verbatim).
        return True
    return request_port == (canonical.port or _DEFAULT_PORTS[canonical.scheme])


def _split_host(value: str) -> tuple[str, int | None]:
    """Split a Host header value into (lowercased hostname, port or None)."""
    parts = urlsplit(f"//{value}")
    return (parts.hostname or "").lower(), parts.port


def _raw_path(scope: Scope) -> str:
    """Return the request path as sent (percent-encoded), best effort."""
    raw = scope.get("raw_path")
    if isinstance(raw, bytes):
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError:
            pass
    return str(scope["path"])


def canonical_redirect_url(scope: Scope, settings: Settings) -> str | None:
    """Return the canonical URL to redirect to, or None to pass through."""
    canonical = _canonical_origin(settings)
    if canonical is None:
        return None
    if scope.get("path") in PROBE_EXEMPT_PATHS:
        return None
    if _host_matches(scope, canonical):
        return None
    target = f"{canonical.scheme}://{canonical.netloc}{_raw_path(scope)}"
    query = scope.get("query_string", b"").decode("ascii", "ignore")
    if query:
        target = f"{target}?{query}"
    return target


class CanonicalHostMiddleware:
    """302 non-canonical hosts to the ``AUTH_REDIRECT_URI`` origin (#406).

    Cloud Run serves the app under two URL families while the OIDC ``state``
    cookie is host-scoped (#339): a login started on the non-canonical host
    fails at the callback on the canonical host. Normalizing every entry to
    the canonical origin before auth guarantees a login can only start on the
    host that receives the callback. Pure ASGI (no BaseHTTPMiddleware) so the
    SSE stream is untouched, and fail-safe: no canonical origin derivable
    (unset/invalid ``AUTH_REDIRECT_URI``, ``AUTH_ENABLED=false``) => pass
    through, and the probe paths stay exempt (#316).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Redirect non-canonical hosts; pass everything else through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings: Settings = Request(scope).app.state.settings
        target = canonical_redirect_url(scope, settings)
        if target is None:
            await self.app(scope, receive, send)
            return
        response: Response = RedirectResponse(target, status_code=302)
        await response(scope, receive, send)
