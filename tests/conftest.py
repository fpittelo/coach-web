"""Shared pytest fixtures for coach-web tests."""

from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict

import coach_web
from coach_web.app import create_app
from coach_web.config import Settings, get_settings
from coach_web.db import build_db_engine, build_session_factory
from coach_web.models import ReadinessMetrics, TrainingPlan

TEST_DATA_ENCRYPTION_KEY = Fernet.generate_key()
"""Generated Fernet key backing the default test environment (never committed)."""


# ---------------------------------------------------------------------------
# aiosqlite worker teardown tolerance (#293)
#
# aiosqlite runs every connection's SQL on a private worker thread and
# delivers each result through ``future.get_loop().call_soon_threadsafe()``.
# When a connection's event loop closes before the worker delivers, that
# call raises ``RuntimeError("Event loop is closed")``; aiosqlite's
# ``except BaseException`` handler then calls ``call_soon_threadsafe`` a
# SECOND time (to deliver the exception), which raises again and kills the
# worker thread. pytest's threadexception plugin surfaces the death as
# ``PytestUnhandledThreadExceptionWarning`` — fatal under the zero-warning
# gate (``-W error``). Fired once in CI (#292 run #332, diagnosed on #293):
# the unwritable-database degrade tests leave aiosqlite's failed-connect
# stop sentinel queued with a future bound to the TestClient portal loop,
# which closes at the ``with`` exit; under CI scheduling the worker processes
# the sentinel only after the loop closed.
#
# This suite creates and tears down many short-lived event loops (TestClient
# portals, pytest-asyncio runners), so the race window is real here. The
# remedy makes the worker's teardown DETERMINISTIC: a delivery to a closed
# loop is dropped — the future is orphaned (nobody can await it), so dropping
# is safe, while killing the worker is not. The worker always drains its
# queue and exits cleanly, so the warning can no longer occur at all.
# Test-process only: production aiosqlite behavior is untouched, and the
# ``-W error`` gate itself is never weakened.
# ---------------------------------------------------------------------------


def _deliver_to_future(setter: Any, future: Any, value: Any) -> None:
    """Deliver *value* through *setter* to *future*, tolerating a closed loop.

    A future whose loop closed before delivery can never be awaited by
    anyone, so the delivery is dropped instead of killing the worker thread
    (the #293 race). Live-loop deliveries are untouched.
    """
    try:
        future.get_loop().call_soon_threadsafe(setter, future, value)
    except RuntimeError:
        # RuntimeError("Event loop is closed") — the only failure mode of
        # call_soon_threadsafe on a loop that died mid-flight.
        pass


def _tolerant_connection_worker_thread(tx: Any) -> None:
    """aiosqlite's ``_connection_worker_thread`` with closed-loop tolerance.

    Mirrors aiosqlite 0.22.x ``core._connection_worker_thread`` exactly, with
    the two ``call_soon_threadsafe`` delivery sites routed through
    :func:`_deliver_to_future`.
    """
    while True:
        # Continues running until all queue items are processed, even after
        # connection is closed (so we can finalize all futures).
        future, function = tx.get()
        try:
            result = function()
            if future:
                _deliver_to_future(aiosqlite.core.set_result, future, result)
            if result is aiosqlite.core._STOP_RUNNING_SENTINEL:
                break
        except BaseException as e:  # noqa: BLE001 - mirrors aiosqlite's worker contract
            if future:
                _deliver_to_future(aiosqlite.core.set_exception, future, e)


def pytest_configure(config: pytest.Config) -> None:
    """Install the tolerant aiosqlite worker before any connection exists.

    ``Connection.__init__`` resolves the worker target from the module
    global at thread creation, so patching at configure time (before any
    test opens a connection) covers every connection in the process.
    """
    aiosqlite.core._connection_worker_thread = _tolerant_connection_worker_thread


@pytest.fixture(autouse=True)
def _isolate_settings_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the ``get_settings`` singleton and disable .env loading during tests."""
    monkeypatch.setattr(Settings, "model_config", SettingsConfigDict(extra="ignore"))
    # The app's Host allowlist defaults to loopback names only (#112); TestClient
    # uses the synthetic "testserver" host, so allow it for the test environment.
    monkeypatch.setenv("TRUSTED_HOSTS", '["localhost", "127.0.0.1", "testserver"]')
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _default_encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test a valid ``DATA_ENCRYPTION_KEY`` unless it overrides one.

    Migration 0006 (#243) encrypts the debrief RPE at rest and FAILS CLOSED
    when the key is missing (MADR-008: a failed migration aborts startup), so
    every migration-running test needs a key in the environment. Tests pinning
    the missing/invalid-key behavior delenv/setenv explicitly.
    """
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", TEST_DATA_ENCRYPTION_KEY.decode("ascii"))


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


# ---------------------------------------------------------------------------
# Persistence fixtures (issue #166 — MADR-008 SQLite/WAL + Alembic)
# ---------------------------------------------------------------------------


def alembic_config() -> Config:
    """Build the packaged Alembic config (script_location resolves via %(here)s)."""
    ini_path = Path(coach_web.__file__).parent / "alembic.ini"
    return Config(str(ini_path))


@pytest.fixture(autouse=True)
def _isolate_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point COACH_DB_PATH at a per-test SQLite file (never the container default).

    The container default (``/data/coach.db``) is unwritable outside the lane
    volume; every test gets its own database file so persistence tests are
    hermetic and the pre-existing suites never touch the filesystem default.
    """
    monkeypatch.setenv("COACH_DB_PATH", str(tmp_path / "coach.db"))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """The per-test SQLite database file path (the file _isolate_database sets)."""
    return tmp_path / "coach.db"


@pytest.fixture
def migrated_db(db_path: Path) -> Path:
    """Apply the Alembic migrations to the per-test database file."""
    command.upgrade(alembic_config(), "head")
    return db_path


@pytest.fixture
async def db_session_factory(migrated_db: Path) -> AsyncIterator[Any]:
    """A session factory bound to a fresh engine over the migrated database."""
    engine = build_db_engine(str(migrated_db))
    factory = build_session_factory(engine)
    yield factory
    await engine.dispose()
