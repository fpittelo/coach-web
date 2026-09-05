"""Tests for coach_web.mcp_client."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coach_web.mcp_client import MCPClient, MCPConnectionError
from coach_web.models import AthleteProfile, FitnessTrend, ReadinessMetrics


def _mock_transport(mock_streams: tuple[MagicMock, MagicMock]) -> MagicMock:
    """Return a MagicMock that behaves as an async context manager."""
    transport = MagicMock()
    transport.__aenter__ = AsyncMock(return_value=mock_streams)
    transport.__aexit__ = AsyncMock(return_value=None)
    return transport


class TestMCPClient:
    """MCP client wrapper behaviour."""

    def test_init_stores_url(self) -> None:
        """The client stores the provided URL."""
        client = MCPClient("http://mcp.local/mcp")

        assert client.url == "http://mcp.local/mcp"

    async def test_connect_and_call_tool(self) -> None:
        """connect establishes a session and call_tool invokes it."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
        mock_streams = (MagicMock(), MagicMock())

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=_mock_transport(mock_streams),
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                client = MCPClient("http://mcp.local/mcp")
                await client.connect()
                result = await client.call_tool("test_tool", {"key": "value"})

        mock_session.initialize.assert_awaited_once()
        mock_session.call_tool.assert_awaited_once_with(
            "test_tool",
            {"key": "value"},
        )
        assert result is not None

    async def test_get_readiness_dashboard(self) -> None:
        """get_readiness_dashboard maps a tool result to ReadinessMetrics."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[
                    MagicMock(
                        type="text",
                        text='{"ftp": 250, "resting_hr": 48, "hrv_rmssd": 65.5, '
                        '"sleep_hours": 7.5, "ctl": 75.0, "atl": 60.0, "tsb": 15.0}',
                    ),
                ],
            ),
        )
        mock_streams = (MagicMock(), MagicMock())

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=_mock_transport(mock_streams),
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                client = MCPClient("http://mcp.local/mcp")
                await client.connect()
                metrics = await client.get_readiness_dashboard()

        assert isinstance(metrics, ReadinessMetrics)
        assert metrics.ftp == 250
        assert metrics.resting_hr == 48

    async def test_async_context_manager(self) -> None:
        """The client can be used as an async context manager."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_streams = (MagicMock(), MagicMock())
        mock_transport = _mock_transport(mock_streams)

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=mock_transport,
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                async with MCPClient("http://mcp.local/mcp") as client:
                    assert isinstance(client, MCPClient)

        mock_transport.__aexit__.assert_awaited_once()

    async def test_connection_error(self) -> None:
        """A connection failure raises MCPConnectionError."""
        with patch(
            "coach_web.mcp_client.streamable_http_client",
            side_effect=ConnectionError("refused"),
        ):
            client = MCPClient("http://mcp.local/mcp")

            with pytest.raises(MCPConnectionError):
                await client.connect()


class TestGetFitnessSummary:
    """MCP client get_fitness_summary behaviour."""

    async def test_get_fitness_summary_returns_trend(self) -> None:
        """get_fitness_summary maps a tool result to FitnessTrend."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[
                    MagicMock(
                        type="text",
                        text='[{"date": "2026-09-01", "ctl": 75.0, "atl": 60.0, "tsb": 15.0}, '
                        '{"date": "2026-09-02", "ctl": 76.0, "atl": 62.0, "tsb": 14.0}]',
                    ),
                ],
            ),
        )
        mock_streams = (MagicMock(), MagicMock())

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=_mock_transport(mock_streams),
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                client = MCPClient("http://mcp.local/mcp")
                await client.connect()
                trend = await client.get_fitness_summary()

        assert isinstance(trend, FitnessTrend)
        assert len(trend.points) == 2
        assert trend.points[0].date == "2026-09-01"
        assert trend.points[0].ctl == 75.0
        assert trend.points[1].date == "2026-09-02"
        assert trend.points[1].ctl == 76.0

    async def test_get_fitness_summary_empty(self) -> None:
        """get_fitness_summary handles an empty trend list."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[MagicMock(type="text", text="[]")],
            ),
        )
        mock_streams = (MagicMock(), MagicMock())

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=_mock_transport(mock_streams),
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                client = MCPClient("http://mcp.local/mcp")
                await client.connect()
                trend = await client.get_fitness_summary()

        assert isinstance(trend, FitnessTrend)
        assert trend.points == []


class TestGetAthleteProfile:
    """MCP client get_athlete_profile behaviour."""

    async def test_get_athlete_profile_returns_profile(self) -> None:
        """get_athlete_profile maps a tool result to AthleteProfile."""
        mock_session = MagicMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(
            return_value=MagicMock(
                content=[
                    MagicMock(
                        type="text",
                        text='{"weight_kg": 70.0, "max_hr": 185, "resting_hr": 48}',
                    ),
                ],
            ),
        )
        mock_streams = (MagicMock(), MagicMock())

        with patch(
            "coach_web.mcp_client.streamable_http_client",
            return_value=_mock_transport(mock_streams),
        ):
            with patch(
                "coach_web.mcp_client.ClientSession",
                return_value=mock_session,
            ):
                client = MCPClient("http://mcp.local/mcp")
                await client.connect()
                profile = await client.get_athlete_profile()

        assert isinstance(profile, AthleteProfile)
        assert profile.weight_kg == 70.0
        assert profile.max_hr == 185
        assert profile.resting_hr == 48
