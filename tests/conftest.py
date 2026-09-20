"""Shared pytest fixtures for coach-web tests."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict

from coach_web.app import create_app
from coach_web.config import Settings, get_settings
from coach_web.models import ReadinessMetrics, TrainingPlan


@pytest.fixture(autouse=True)
def _isolate_settings_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the ``get_settings`` singleton and disable .env loading during tests."""
    monkeypatch.setattr(Settings, "model_config", SettingsConfigDict(extra="ignore"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return settings configured for the test environment."""
    monkeypatch.setenv("COACH_MCP_URL", "http://test-mcp.local/mcp")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "fpittelo/coach")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "30")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "8080")
    monkeypatch.setenv("CORS_ORIGINS", '["http://test.local"]')
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def mock_mcp_client() -> MagicMock:
    """Return a mock MCPClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.call_tool = AsyncMock()
    client.get_readiness_dashboard = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def mock_github_client() -> MagicMock:
    """Return a mock GitHubClient."""
    client = MagicMock()
    client.close = AsyncMock()
    client.fetch_training_plans = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.fixture
def sample_readiness_metrics() -> ReadinessMetrics:
    """Return a sample ReadinessMetrics instance."""
    return ReadinessMetrics(
        ftp=250,
        resting_hr=48,
        hrv_rmssd=65.5,
        sleep_hours=7.5,
        ctl=75.0,
        atl=60.0,
        tsb=15.0,
    )


@pytest.fixture
def sample_training_plan() -> TrainingPlan:
    """Return a single sample TrainingPlan instance."""
    return TrainingPlan(
        number=1,
        title="Training Plan: 2026-W37",
        body="Build phase with threshold intervals.",
        created_at="2026-09-01T10:00:00Z",
        week_id="2026-W37",
    )


@pytest.fixture
def sample_training_plans() -> list[TrainingPlan]:
    """Return a list of six sample TrainingPlan instances."""
    return [
        TrainingPlan(
            number=i,
            title=f"Training Plan: 2026-W{37 - idx}",
            body=f"Week {37 - idx} training plan.",
            created_at=f"2026-09-0{idx + 1}T10:00:00Z",
            week_id=f"2026-W{37 - idx}",
        )
        for idx, i in enumerate(range(1, 7), start=0)
    ]


# ---------------------------------------------------------------------------
# Google OIDC authentication fixtures (issue #65)
# ---------------------------------------------------------------------------

AUTH_TEST_SESSION_SECRET = "unit-test-session-signing-key-0123456789abcdef"  # noqa: S105
"""HS256 signing key used by auth tests (mirrors AUTH_SESSION_SECRET env)."""

AUTH_TEST_OWNER_EMAIL = "frederic.pitteloud@gmail.com"
"""The whitelisted owner email (mirrors the AUTH_WHITELIST_EMAILS default)."""


@dataclass
class GoogleTestKeys:
    """RSA test keypair mimicking the Google JWKS for ID-token signing."""

    private_pem: str
    jwks: dict[str, Any]

    def sign(self, claims: dict[str, Any], kid: str = "test-google-key") -> str:
        """Sign claims as a Google-style RS256 ID token with the test key."""
        return str(jwt.encode(claims, self.private_pem, algorithm="RS256", headers={"kid": kid}))


@pytest.fixture(scope="session")
def google_test_keys() -> GoogleTestKeys:
    """Generate an RSA keypair once per session to mock Google ID-token signing."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    jwk["kid"] = "test-google-key"
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return GoogleTestKeys(private_pem=pem, jwks={"keys": [jwk]})


@pytest.fixture
def auth_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings with auth enabled and deterministic test credentials (#65)."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("AUTH_SESSION_SECRET", AUTH_TEST_SESSION_SECRET)
    monkeypatch.setenv("GOOGLE_OIDC_ISSUER", "https://accounts.google.com")
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def auth_client(auth_settings: Settings) -> Iterator[TestClient]:
    """Test client against an auth-enabled app (https base for Secure cookies)."""
    with TestClient(create_app(auth_settings), base_url="https://testserver") as client:
        yield client
