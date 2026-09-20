"""Google OIDC authorization-code flow routes (issue #65).

``/auth/login`` redirects to Google with a single-use state value that
doubles as the ID-token nonce and is bound to a short-lived HttpOnly cookie
(stateless CSRF protection — no server-side store). ``/auth/callback``
exchanges the code, verifies the ID token against the Google JWKS, enforces
the whitelist and issues the stateless session cookie; the state cookie is
cleared on every callback exit. ``/auth/logout`` is POST-only (logout-CSRF
safe) and clears the session cookie.

Secrets (client secret, session secret) are read from settings only, are
never logged and never appear in responses (Swiss nLPD).
"""

import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.responses import Response

from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import (
    TokenError,
    create_session_token,
    fetch_google_jwks,
    verify_google_id_token,
)
from coach_web.auth.whitelist import is_whitelisted
from coach_web.config import Settings

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 - public URL
STATE_COOKIE = "cw_oidc_state"
STATE_COOKIE_TTL_SECONDS = 600
GOOGLE_TIMEOUT_SECONDS = 10.0
_OAUTH_SCOPE = "openid email"


def _resolve_redirect_uri(request: Request, settings: Settings) -> str:
    """Return the OAuth redirect URI (explicit setting or request-derived)."""
    if settings.AUTH_REDIRECT_URI:
        return settings.AUTH_REDIRECT_URI
    return str(request.base_url).rstrip("/") + "/auth/callback"


def _state_cleared(status_code: int, detail: str) -> Response:
    """Build a callback response that also clears the single-use state cookie.

    The state value doubles as the ID-token nonce, so it is deleted on every
    callback exit — success and failure — to keep the single-use guarantee.
    """
    response: Response = JSONResponse(status_code=status_code, content={"detail": detail})
    response.delete_cookie(STATE_COOKIE, path="/", secure=True, httponly=True)
    return response


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    """Start the OIDC authorization-code flow at Google."""
    settings: Settings = request.app.state.settings
    state = secrets.token_urlsafe(32)
    params = urlencode(
        {
            "client_id": settings.GOOGLE_OIDC_CLIENT_ID,
            "redirect_uri": _resolve_redirect_uri(request, settings),
            "response_type": "code",
            "scope": _OAUTH_SCOPE,
            "state": state,
            # The state value doubles as the ID-token nonce: both are
            # single-use, browser-bound random values (stateless by design).
            "nonce": state,
        }
    )
    response = RedirectResponse(f"{GOOGLE_AUTH_ENDPOINT}?{params}", status_code=302)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_COOKIE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
) -> Response:
    """Finish the OIDC flow: exchange, verify, whitelist and issue a session."""
    settings: Settings = request.app.state.settings
    cookie_state = request.cookies.get(STATE_COOKIE)
    if not code or not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return _state_cleared(400, "Invalid OAuth state")

    try:
        id_token = await _exchange_code(request, settings, code)
        jwks = await _fetch_jwks()
    except HTTPException as exc:
        return _state_cleared(exc.status_code, str(exc.detail))

    try:
        claims = verify_google_id_token(
            id_token,
            jwks,
            client_id=settings.GOOGLE_OIDC_CLIENT_ID,
            issuer=settings.GOOGLE_OIDC_ISSUER,
            nonce=state,
        )
    except TokenError:
        return _state_cleared(401, "Invalid Google ID token")

    email: str = claims["email"]
    if not is_whitelisted(email, settings.AUTH_WHITELIST_EMAILS):
        return _state_cleared(403, "Email not whitelisted")

    session = create_session_token(
        email,
        settings.AUTH_SESSION_SECRET,
        settings.AUTH_SESSION_TTL_SECONDS,
    )
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=settings.AUTH_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,
        path="/",
    )
    response.delete_cookie(STATE_COOKIE, path="/", secure=True, httponly=True)
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    """Clear the session cookie and return to the public landing page.

    POST-only: a state-changing action must not be reachable via GET, or any
    third-party page could force-logout the owner cross-site (logout CSRF).
    """
    response = RedirectResponse("/", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
    return response


async def _exchange_code(request: Request, settings: Settings, code: str) -> str:
    """Exchange the authorization code for a Google ID token."""
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_OIDC_CLIENT_ID,
                    "client_secret": settings.GOOGLE_OIDC_CLIENT_SECRET,
                    "redirect_uri": _resolve_redirect_uri(request, settings),
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Google token endpoint unreachable") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Google token exchange failed")
    try:
        payload: dict[str, Any] = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail="Google token exchange returned invalid JSON"
        ) from exc
    id_token = payload.get("id_token")
    if not id_token:
        raise HTTPException(status_code=502, detail="Google token exchange returned no ID token")
    return str(id_token)


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch the Google JWKS used to verify ID tokens."""
    try:
        async with httpx.AsyncClient(timeout=GOOGLE_TIMEOUT_SECONDS) as client:
            return await fetch_google_jwks(client)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Google JWKS unavailable") from exc
