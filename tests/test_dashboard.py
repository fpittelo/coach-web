"""Tests for coach_web.dashboard."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coach_web.dashboard import (
    get_fitness_trend,
    get_readiness_metrics,
    render_dashboard,
)
from coach_web.mcp_client import MCPConnectionError
from coach_web.models import FitnessTrend, FitnessTrendPoint, ReadinessMetrics


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


class TestGetFitnessTrend:
    """Data-fetching logic for fitness trends."""

    async def test_returns_fitness_trend(self, settings: object) -> None:
        """get_fitness_trend returns FitnessTrend on success."""
        expected = FitnessTrend(
            points=[
                FitnessTrendPoint(date="2026-09-01", ctl=75.0, atl=60.0, tsb=15.0),
                FitnessTrendPoint(date="2026-09-02", ctl=76.0, atl=62.0, tsb=14.0),
            ],
        )
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_fitness_summary = AsyncMock(return_value=expected)

        with patch("coach_web.dashboard.MCPClient", return_value=mock_client):
            trend = await get_fitness_trend()

        assert trend == expected
        mock_client.get_fitness_summary.assert_awaited_once()

    async def test_raises_on_connection_failure(self, settings: object) -> None:
        """get_fitness_trend propagates MCPConnectionError."""
        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get_fitness_summary = AsyncMock(side_effect=MCPConnectionError("refused"))

        with patch("coach_web.dashboard.MCPClient", return_value=mock_client):
            with pytest.raises(MCPConnectionError):
                await get_fitness_trend()


class TestRenderDashboard:
    """Streamlit dashboard rendering."""

    @patch("coach_web.dashboard.st")
    @patch("coach_web.dashboard.get_fitness_trend", new_callable=MagicMock)
    @patch("coach_web.dashboard.get_readiness_metrics", new_callable=MagicMock)
    @patch("coach_web.dashboard.asyncio.run")
    def test_render_with_metrics(
        self,
        mock_run: MagicMock,
        mock_get_readiness_metrics: MagicMock,
        mock_get_fitness_trend: MagicMock,
        mock_st: MagicMock,
        sample_readiness_metrics: ReadinessMetrics,
    ) -> None:
        """render_dashboard displays all metric cards when data is available."""
        mock_run.side_effect = lambda coro: coro
        mock_get_readiness_metrics.return_value = sample_readiness_metrics
        mock_get_fitness_trend.return_value = FitnessTrend(points=[])
        mock_st.columns.return_value = [MagicMock() for _ in range(4)]

        render_dashboard()

        assert mock_st.metric.called
        assert mock_st.metric.call_count >= 7

    @patch("coach_web.dashboard.st")
    @patch("coach_web.dashboard.get_readiness_metrics", new_callable=MagicMock)
    def test_render_on_connection_error(
        self,
        mock_get_readiness_metrics: MagicMock,
        mock_st: MagicMock,
    ) -> None:
        """render_dashboard shows a warning when the MCP server is unreachable."""
        mock_get_readiness_metrics.side_effect = MCPConnectionError("refused")

        render_dashboard()

        mock_st.warning.assert_called_once()
