"""Shared pytest fixtures for coach-web tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from coach_web.config import Settings, get_settings
from coach_web.models import ReadinessMetrics, TrainingPlan


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Return settings configured for the test environment."""
    monkeypatch.setenv("COACH_MCP_URL", "http://test-mcp.local/mcp")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "fpittelo/coach")
    monkeypatch.setenv("STREAMLIT_SERVER_PORT", "8501")
    monkeypatch.setenv("STREAMLIT_SERVER_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "30")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
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
