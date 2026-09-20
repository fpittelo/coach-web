"""Stateless signed session tokens & Google ID-token verification (issue #65).

Sessions are short-lived HS256 JWTs signed with ``AUTH_SESSION_SECRET`` —
self-contained and stateless, with no server-side session store (AC5,
scale-to-zero). Google ID tokens are verified as RS256 against the Google
JWKS with pinned issuer, audience, nonce binding and a mandatory verified
email claim (the whitelist identity).
"""

import json
import time
import uuid
from typing import Any

import httpx
import jwt

GOOGLE_JWKS_URI = "https://www.googleapis.com/oauth2/v3/certs"
SESSION_ISSUER = "coach-web"
SESSION_AUDIENCE = "coach-web"
_SESSION_ALGORITHM = "HS256"
_ID_TOKEN_ALGORITHM = "RS256"  # noqa: S105 - algorithm name, not a secret
# Google documents the ID-token iss claim in two forms; some flows still
# emit the bare host. Accepting both prevents a fail-closed login outage.
_GOOGLE_ISSUER_FORMS = frozenset({"https://accounts.google.com", "accounts.google.com"})


class TokenError(Exception):
    """A token failed signature, expiry, issuer, audience or claim validation."""


def create_session_token(email: str, secret: str, ttl_seconds: int) -> str:
    """Create a signed, short-lived, stateless session token for *email*."""
    now = int(time.time())
    payload = {
        "iss": SESSION_ISSUER,
        "aud": SESSION_AUDIENCE,
        "sub": email,
        "email": email,
        "iat": now,
        "exp": now + ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return str(jwt.encode(payload, secret, algorithm=_SESSION_ALGORITHM))


def verify_session_token(token: str, secret: str) -> dict[str, Any]:
    """Verify a session token and return its claims, or raise TokenError."""
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            secret,
            algorithms=[_SESSION_ALGORITHM],
            issuer=SESSION_ISSUER,
            audience=SESSION_AUDIENCE,
            options={"require": ["exp", "iat", "sub", "aud", "email"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"Invalid session token: {exc}") from exc
    return claims


async def fetch_google_jwks(client: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch the Google JSON Web Key Set used to sign ID tokens."""
    response = await client.get(GOOGLE_JWKS_URI)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def verify_google_id_token(
    id_token: str,
    jwks: dict[str, Any],
    client_id: str,
    issuer: str,
    nonce: str | None = None,
) -> dict[str, Any]:
    """Verify a Google ID token (RS256 via JWKS) and return its claims.

    Enforces: RS256 algorithm pinning (alg-confusion safe), signature against
    the JWKS key matching ``kid``, ``iss``, ``aud``, ``exp``, a non-empty
    ``email`` claim, ``email_verified`` and the single-use ``nonce`` binding.
    """
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise TokenError(f"Malformed ID token header: {exc}") from exc
    if header.get("alg") != _ID_TOKEN_ALGORITHM:
        raise TokenError("Unsupported ID token algorithm")
    jwk = _find_jwk(jwks, header.get("kid"))
    if jwk is None:
        raise TokenError("Unknown signing key (kid)")
    key: Any = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
    try:
        claims: dict[str, Any] = jwt.decode(
            id_token,
            key,
            algorithms=[_ID_TOKEN_ALGORITHM],
            audience=client_id,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"Invalid ID token: {exc}") from exc
    if claims.get("iss") not in _accepted_issuers(issuer):
        raise TokenError("Invalid ID token issuer")
    if nonce is not None and claims.get("nonce") != nonce:
        raise TokenError("Nonce mismatch")
    if not claims.get("email"):
        raise TokenError("Missing email claim")
    if claims.get("email_verified") is not True:
        raise TokenError("Email not verified")
    return claims


def _accepted_issuers(configured: str) -> frozenset[str] | set[str]:
    """Return the ID-token issuer values accepted for *configured*.

    Google emits ``iss`` in two documented forms (with and without the
    https scheme); when either Google form is configured, both are accepted
    so a token variant cannot fail the login closed. Any other configured
    issuer must match exactly.
    """
    if configured in _GOOGLE_ISSUER_FORMS:
        return _GOOGLE_ISSUER_FORMS
    return {configured}


def _find_jwk(jwks: dict[str, Any], kid: Any) -> dict[str, Any] | None:
    """Return the JWKS entry matching *kid*, or None."""
    for entry in jwks.get("keys", []):
        jwk: dict[str, Any] = entry
        if jwk.get("kid") == kid:
            return jwk
    return None
