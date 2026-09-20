"""Tests for stateless session tokens & Google ID-token verification (issue #65).

Sessions are short-lived HS256 JWTs signed with AUTH_SESSION_SECRET — no
server-side session store (AC5). Google ID tokens are verified as RS256
against the Google JWKS with pinned issuer/audience and nonce binding.
"""

import time
from typing import Any

import httpx
import jwt
import pytest
import respx
from httpx import Response

from coach_web.auth.tokens import (
    GOOGLE_JWKS_URI,
    SESSION_AUDIENCE,
    SESSION_ISSUER,
    TokenError,
    create_session_token,
    fetch_google_jwks,
    verify_google_id_token,
    verify_session_token,
)

OWNER_EMAIL = "frederic.pitteloud@gmail.com"
SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
CLIENT_ID = "test-client-id"
ISSUER = "https://accounts.google.com"


class TestCreateSessionToken:
    """Stateless session token issuance (HS256)."""

    def test_roundtrip_returns_email_claims(self) -> None:
        """A created token verifies back to the same email identity."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)
        claims = verify_session_token(token, SECRET)

        assert claims["email"] == OWNER_EMAIL
        assert claims["sub"] == OWNER_EMAIL
        assert claims["iss"] == SESSION_ISSUER

    def test_token_is_a_three_segment_jwt(self) -> None:
        """The session is a self-contained signed JWT (no server-side store)."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)

        assert token.count(".") == 2

    def test_token_carries_audience_claim(self) -> None:
        """Session JWTs are audience-pinned (key reuse elsewhere cannot mint them)."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)
        claims = verify_session_token(token, SECRET)

        assert claims["aud"] == SESSION_AUDIENCE

    def test_expiry_is_iat_plus_ttl(self) -> None:
        """The token is short-lived: exp = iat + ttl."""
        before = int(time.time())
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)
        after = int(time.time())
        claims = verify_session_token(token, SECRET)

        assert before <= claims["iat"] <= after
        assert claims["exp"] - claims["iat"] == 600


class TestVerifySessionToken:
    """Session token validation rejects every forgery vector."""

    def test_expired_token_is_rejected(self) -> None:
        """An expired session token is invalid."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=-10)

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)

    def test_wrong_secret_is_rejected(self) -> None:
        """A token signed with a different key is invalid."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)

        with pytest.raises(TokenError):
            verify_session_token(token, "another-signing-key-0123456789abcdef")

    def test_tampered_signature_is_rejected(self) -> None:
        """A mutated token fails signature verification."""
        token = create_session_token(OWNER_EMAIL, SECRET, ttl_seconds=600)

        with pytest.raises(TokenError):
            verify_session_token(token + "x", SECRET)

    def test_garbage_is_rejected(self) -> None:
        """Non-JWT garbage is invalid."""
        with pytest.raises(TokenError):
            verify_session_token("garbage", SECRET)

    def test_missing_email_claim_is_rejected(self) -> None:
        """A session token without the email claim is invalid."""
        now = int(time.time())
        token = jwt.encode(
            {"iss": SESSION_ISSUER, "sub": OWNER_EMAIL, "iat": now, "exp": now + 600},
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)

    def test_wrong_issuer_is_rejected(self) -> None:
        """A session token issued by another issuer is invalid."""
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": "https://evil.example",
                "sub": OWNER_EMAIL,
                "email": OWNER_EMAIL,
                "iat": now,
                "exp": now + 600,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)

    def test_wrong_audience_is_rejected(self) -> None:
        """A session token minted for another service audience is invalid."""
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": SESSION_ISSUER,
                "aud": "other-service",
                "sub": OWNER_EMAIL,
                "email": OWNER_EMAIL,
                "iat": now,
                "exp": now + 600,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)

    def test_missing_audience_is_rejected(self) -> None:
        """A session token without the aud claim is invalid."""
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": SESSION_ISSUER,
                "sub": OWNER_EMAIL,
                "email": OWNER_EMAIL,
                "iat": now,
                "exp": now + 600,
            },
            SECRET,
            algorithm="HS256",
        )

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)

    def test_unexpected_algorithm_is_rejected(self) -> None:
        """Algorithm pinning: HS512-signed tokens are invalid."""
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": SESSION_ISSUER,
                "sub": OWNER_EMAIL,
                "email": OWNER_EMAIL,
                "iat": now,
                "exp": now + 600,
            },
            "x" * 64,
            algorithm="HS512",
        )

        with pytest.raises(TokenError):
            verify_session_token(token, SECRET)


def _id_token_claims(**overrides: Any) -> dict[str, Any]:
    """Return valid Google ID-token claims, overridable per test."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "google-user-123",
        "email": OWNER_EMAIL,
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
        "nonce": "nonce-123",
    }
    claims.update(overrides)
    return claims


class TestVerifyGoogleIdToken:
    """Google ID-token verification (RS256 via JWKS)."""

    def test_valid_token_returns_claims(self, google_test_keys: Any) -> None:
        """A correctly signed token verifies and yields the email claim."""
        token = google_test_keys.sign(_id_token_claims())

        claims = verify_google_id_token(
            token,
            google_test_keys.jwks,
            client_id=CLIENT_ID,
            issuer=ISSUER,
            nonce="nonce-123",
        )

        assert claims["email"] == OWNER_EMAIL

    def test_wrong_audience_is_rejected(self, google_test_keys: Any) -> None:
        """Tokens minted for another client are rejected."""
        token = google_test_keys.sign(_id_token_claims(aud="other-client"))

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_bare_issuer_variant_is_accepted(self, google_test_keys: Any) -> None:
        """Google's bare 'accounts.google.com' iss form is accepted."""
        token = google_test_keys.sign(_id_token_claims(iss="accounts.google.com"))

        claims = verify_google_id_token(
            token,
            google_test_keys.jwks,
            client_id=CLIENT_ID,
            issuer="https://accounts.google.com",
            nonce="nonce-123",
        )

        assert claims["email"] == OWNER_EMAIL

    def test_https_issuer_variant_is_accepted(self, google_test_keys: Any) -> None:
        """The https iss form is accepted when the bare form is configured."""
        token = google_test_keys.sign(_id_token_claims())

        claims = verify_google_id_token(
            token,
            google_test_keys.jwks,
            client_id=CLIENT_ID,
            issuer="accounts.google.com",
            nonce="nonce-123",
        )

        assert claims["email"] == OWNER_EMAIL

    def test_non_google_issuer_requires_exact_match(self, google_test_keys: Any) -> None:
        """A custom configured issuer accepts only its exact value."""
        token = google_test_keys.sign(_id_token_claims(iss="https://custom.example"))

        claims = verify_google_id_token(
            token,
            google_test_keys.jwks,
            client_id=CLIENT_ID,
            issuer="https://custom.example",
            nonce="nonce-123",
        )

        assert claims["email"] == OWNER_EMAIL

    def test_wrong_issuer_is_rejected(self, google_test_keys: Any) -> None:
        """Tokens from another issuer are rejected."""
        token = google_test_keys.sign(_id_token_claims(iss="https://evil.example"))

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_expired_token_is_rejected(self, google_test_keys: Any) -> None:
        """Expired ID tokens are rejected."""
        token = google_test_keys.sign(_id_token_claims(exp=int(time.time()) - 10))

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_unknown_kid_is_rejected(self, google_test_keys: Any) -> None:
        """Tokens signed by an unknown key (kid) are rejected."""
        token = google_test_keys.sign(_id_token_claims(), kid="rotated-away-key")

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_nonce_mismatch_is_rejected(self, google_test_keys: Any) -> None:
        """Nonce binding: replayed tokens with a foreign nonce are rejected."""
        token = google_test_keys.sign(_id_token_claims(nonce="evil-nonce"))

        with pytest.raises(TokenError):
            verify_google_id_token(
                token,
                google_test_keys.jwks,
                client_id=CLIENT_ID,
                issuer=ISSUER,
                nonce="nonce-123",
            )

    def test_missing_email_is_rejected(self, google_test_keys: Any) -> None:
        """ID tokens without an email claim are rejected."""
        token = google_test_keys.sign(_id_token_claims(email=None))

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_unverified_email_is_rejected(self, google_test_keys: Any) -> None:
        """ID tokens with an unverified email are rejected (identity boundary)."""
        token = google_test_keys.sign(_id_token_claims(email_verified=False))

        with pytest.raises(TokenError):
            verify_google_id_token(token, google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER)

    def test_algorithm_confusion_is_rejected(self, google_test_keys: Any) -> None:
        """HS256 alg-confusion tokens are rejected (RS256 pinned)."""
        token = jwt.encode(
            _id_token_claims(),
            "attacker-hmac-secret-0123456789abcdef",
            algorithm="HS256",  # noqa: S106
        )

        with pytest.raises(TokenError):
            verify_google_id_token(
                token,
                google_test_keys.jwks,
                client_id=CLIENT_ID,
                issuer=ISSUER,
                nonce="nonce-123",
            )

    def test_garbage_token_is_rejected(self, google_test_keys: Any) -> None:
        """Non-JWT garbage is rejected."""
        with pytest.raises(TokenError):
            verify_google_id_token(
                "garbage", google_test_keys.jwks, client_id=CLIENT_ID, issuer=ISSUER
            )


class TestFetchGoogleJwks:
    """JWKS retrieval over HTTP (mocked with respx)."""

    @respx.mock
    async def test_fetches_google_jwks(self) -> None:
        """The JWKS URI is fetched and the key set returned."""
        payload = {"keys": [{"kid": "abc123", "kty": "RSA"}]}
        route = respx.get(GOOGLE_JWKS_URI).mock(return_value=Response(200, json=payload))

        async with httpx.AsyncClient() as client:
            jwks = await fetch_google_jwks(client)

        assert route.called
        assert jwks == payload

    @respx.mock
    async def test_http_error_propagates(self) -> None:
        """Transport/HTTP failures surface as httpx errors (mapped to 502)."""
        respx.get(GOOGLE_JWKS_URI).mock(return_value=Response(500))

        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPError):
                await fetch_google_jwks(client)
