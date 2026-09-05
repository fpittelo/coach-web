"""Tests for coach_web.dashboard."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coach_web.dashboard import get_readiness_metrics
from coach_web.mcp_client import MCPConnectionError
from coach_web.models import ReadinessMetrics


class TestGetReadinessMetrics:
    """Data-fetching logic for the dashboard."""

    async def test_returns_readiness_metrics(self, settings: object) -> None:
        """get_readiness_metrics returns ReadinessMetrics on success."""
        expected = ReadinessMetrics(
            ftp=250,
            resting_hr=48,
            hrv_rmssd=65.5,
            sleep_hours=7.5,
            ctl=75.0,
            atl=60.0,
            tsb=15.0,
        )
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_readiness_dashboard = AsyncMock(return_value=expected)

        with patch("coach_web.dashboard.MCPClient", return_value=mock_client):
            metrics = await get_readiness_metrics()

        assert metrics == expected
        mock_client.get_readiness_dashboard.assert_awaited_once()

    async def test_raises_on_connection_failure(self, settings: object) -> None:
        """get_readiness_metrics propagates MCPConnectionError."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_readiness_dashboard = AsyncMock(side_effect=MCPConnectionError("refused"))

        with patch("coach_web.dashboard.MCPClient", return_value=mock_client):
            with pytest.raises(MCPConnectionError):
                await get_readiness_metrics()
