"""Tests for coach_web.dashboard."""

from unittest.mock import AsyncMock, MagicMock, patch

import plotly.graph_objects as go
import pytest

from coach_web.dashboard import (
    _render_trend_charts,
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


class TestRenderTrendCharts:
    """Plotly chart rendering logic for fitness trends."""

    @patch("coach_web.dashboard.st")
    def test_renders_three_charts(self, mock_st: MagicMock) -> None:
        """_render_trend_charts creates 3 Plotly charts (CTL, ATL, TSB)."""
        points: list[FitnessTrendPoint] = [
            FitnessTrendPoint(date=f"2026-09-{day:02d}", ctl=70.0 + day, atl=50.0 + day, tsb=20.0)
            for day in range(1, 11)
        ]
        trend = FitnessTrend(points=points)

        _render_trend_charts(trend)

        assert mock_st.plotly_chart.call_count == 3

    @patch("coach_web.dashboard.st")
    def test_ctl_chart_uses_all_dates(self, mock_st: MagicMock) -> None:
        """CTL chart uses all trend points."""
        points: list[FitnessTrendPoint] = [
            FitnessTrendPoint(date=f"2026-09-{day:02d}", ctl=70.0 + day, atl=50.0 + day, tsb=20.0)
            for day in range(1, 11)
        ]
        trend = FitnessTrend(points=points)

        _render_trend_charts(trend)

        ctl_fig = mock_st.plotly_chart.call_args_list[0].args[0]
        assert isinstance(ctl_fig, go.Figure)
        scatter = ctl_fig.data[0]
        assert isinstance(scatter, go.Scatter)
        assert len(scatter.x) == len(points)
        assert list(scatter.x) == [p.date for p in points]

    @patch("coach_web.dashboard.st")
    def test_atl_chart_uses_last_7_dates(self, mock_st: MagicMock) -> None:
        """ATL chart uses only the last 7 trend points."""
        points: list[FitnessTrendPoint] = [
            FitnessTrendPoint(date=f"2026-09-{day:02d}", ctl=70.0 + day, atl=50.0 + day, tsb=20.0)
            for day in range(1, 11)
        ]
        trend = FitnessTrend(points=points)

        _render_trend_charts(trend)

        atl_fig = mock_st.plotly_chart.call_args_list[1].args[0]
        assert isinstance(atl_fig, go.Figure)
        scatter = atl_fig.data[0]
        assert isinstance(scatter, go.Scatter)
        assert len(scatter.x) == 7
        assert list(scatter.x) == [p.date for p in points[-7:]]

    @patch("coach_web.dashboard.st")
    def test_chart_titles_and_templates(self, mock_st: MagicMock) -> None:
        """Each chart has the correct title, template, and line color."""
        points: list[FitnessTrendPoint] = [
            FitnessTrendPoint(date=f"2026-09-{day:02d}", ctl=70.0 + day, atl=50.0 + day, tsb=20.0)
            for day in range(1, 11)
        ]
        trend = FitnessTrend(points=points)

        _render_trend_charts(trend)

        ctl_fig = mock_st.plotly_chart.call_args_list[0].args[0]
        assert isinstance(ctl_fig, go.Figure)
        assert ctl_fig.layout.title.text == "Fitness (CTL) — 42 Day Trend"
        assert ctl_fig.layout.template is not None
        assert ctl_fig.layout.template.layout.paper_bgcolor == "#FFFFFF"
        assert ctl_fig.data[0].line.color == "#FF0000"

        atl_fig = mock_st.plotly_chart.call_args_list[1].args[0]
        assert isinstance(atl_fig, go.Figure)
        assert atl_fig.layout.title.text == "Fatigue (ATL) — 7 Day Trend"
        assert atl_fig.layout.template.layout.paper_bgcolor == "#FFFFFF"
        assert atl_fig.data[0].line.color == "#707070"

        tsb_fig = mock_st.plotly_chart.call_args_list[2].args[0]
        assert isinstance(tsb_fig, go.Figure)
        assert tsb_fig.layout.title.text == "Form (TSB) — Training Stress Balance"
        assert tsb_fig.layout.template.layout.paper_bgcolor == "#FFFFFF"
        assert tsb_fig.data[0].line.color == "#B51F1F"


class TestRenderDashboardWithTrend:
    """Dashboard rendering with fitness trend data."""

    @patch("coach_web.dashboard.st")
    @patch("coach_web.dashboard.get_fitness_trend", new_callable=MagicMock)
    @patch("coach_web.dashboard.get_readiness_metrics", new_callable=MagicMock)
    @patch("coach_web.dashboard.asyncio.run")
    def test_renders_trend_charts_when_data_available(
        self,
        mock_run: MagicMock,
        mock_get_readiness_metrics: MagicMock,
        mock_get_fitness_trend: MagicMock,
        mock_st: MagicMock,
        sample_readiness_metrics: ReadinessMetrics,
    ) -> None:
        """render_dashboard renders trend charts when trend data is available."""
        mock_run.side_effect = lambda coro: coro
        mock_get_readiness_metrics.return_value = sample_readiness_metrics
        mock_get_fitness_trend.return_value = FitnessTrend(
            points=[
                FitnessTrendPoint(date="2026-09-01", ctl=75.0, atl=60.0, tsb=15.0),
            ],
        )
        mock_st.columns.return_value = [MagicMock() for _ in range(4)]

        render_dashboard()

        assert mock_st.plotly_chart.call_count == 3

    @patch("coach_web.dashboard.st")
    @patch("coach_web.dashboard.get_fitness_trend", new_callable=MagicMock)
    @patch("coach_web.dashboard.get_readiness_metrics", new_callable=MagicMock)
    @patch("coach_web.dashboard.asyncio.run")
    def test_shows_info_when_no_trend_data(
        self,
        mock_run: MagicMock,
        mock_get_readiness_metrics: MagicMock,
        mock_get_fitness_trend: MagicMock,
        mock_st: MagicMock,
        sample_readiness_metrics: ReadinessMetrics,
    ) -> None:
        """render_dashboard shows info message when trend has no points."""
        mock_run.side_effect = lambda coro: coro
        mock_get_readiness_metrics.return_value = sample_readiness_metrics
        mock_get_fitness_trend.return_value = FitnessTrend(points=[])
        mock_st.columns.return_value = [MagicMock() for _ in range(4)]

        render_dashboard()

        mock_st.info.assert_called_once()
        assert mock_st.plotly_chart.call_count == 0

    @patch("coach_web.dashboard.st")
    @patch("coach_web.dashboard.get_fitness_trend", new_callable=MagicMock)
    @patch("coach_web.dashboard.get_readiness_metrics", new_callable=MagicMock)
    @patch("coach_web.dashboard.asyncio.run")
    def test_handles_trend_fetch_exception(
        self,
        mock_run: MagicMock,
        mock_get_readiness_metrics: MagicMock,
        mock_get_fitness_trend: MagicMock,
        mock_st: MagicMock,
        sample_readiness_metrics: ReadinessMetrics,
    ) -> None:
        """render_dashboard handles exception in get_fitness_trend gracefully."""
        mock_run.side_effect = lambda coro: coro
        mock_get_readiness_metrics.return_value = sample_readiness_metrics
        mock_get_fitness_trend.side_effect = Exception("trend fetch failed")
        mock_st.columns.return_value = [MagicMock() for _ in range(4)]

        render_dashboard()

        mock_st.info.assert_called_once()
