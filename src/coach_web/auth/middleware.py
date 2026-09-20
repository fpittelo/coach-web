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

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from coach_web.auth.tokens import TokenError, verify_session_token
from coach_web.auth.whitelist import is_whitelisted
from coach_web.config import Settings

SESSION_COOKIE = "cw_session"

PUBLIC_PATHS = frozenset({"/", "/health", "/healthz"})
PUBLIC_PREFIXES = ("/static/", "/auth/")


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
