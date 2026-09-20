"""Email whitelist — the application-level access-control boundary (issue #65).

Only whitelisted emails may obtain a session (login callback) and every
authenticated request is re-checked against the whitelist, so a session
issued before a whitelist tightening is rejected immediately. Comparison
is case-insensitive and whitespace-tolerant because Google may render the
email claim with different casing. An empty whitelist rejects everyone
(fail closed).
"""


def normalize_email(email: str) -> str:
    """Return the canonical comparison form of an email address."""
    return email.strip().lower()


def is_whitelisted(email: str | None, whitelist: list[str] | None) -> bool:
    """Return True only when *email* is a non-empty member of *whitelist*."""
    if not email or not whitelist:
        return False
    normalized = normalize_email(email)
    return any(normalize_email(entry) == normalized for entry in whitelist)
