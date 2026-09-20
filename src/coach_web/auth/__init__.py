"""Google OIDC authentication & whitelist enforcement (issue #65).

The email whitelist (``AUTH_WHITELIST_EMAILS``) IS the access-control
boundary: only whitelisted Google accounts may obtain a session, and every
authenticated request is re-checked against the whitelist (Swiss nLPD).
Sessions are stateless signed tokens — no server-side session store.
"""

from coach_web.auth.middleware import AuthConfigError, AuthMiddleware, validate_auth_config
from coach_web.auth.tokens import TokenError
from coach_web.auth.whitelist import is_whitelisted

__all__ = [
    "AuthConfigError",
    "AuthMiddleware",
    "TokenError",
    "is_whitelisted",
    "validate_auth_config",
]
