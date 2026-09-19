"""Tests for coach_web.mcp_hub."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from coach_web.config import Settings
from coach_web.mcp_client import MCPConnectionError
from coach_web.mcp_hub import (
    COACH_SERVER,
    GITHUB_SERVER,
    MCPClientHub,
    MCPHubError,
)


def _tool(name: str, description: str = "A tool") -> Tool:
    """Build a minimal MCP Tool definition for tests."""
    return Tool(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )


def _text_result(text: str, *, is_error: bool = False) -> CallToolResult:
    """Build a CallToolResult carrying a single text content block."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=is_error,
    )


def _mock_client(tools: list[Tool] | None = None) -> MagicMock:
    """Build a MagicMock standing in for an MCPClient."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.close = AsyncMock()
    client.list_tools = AsyncMock(return_value=tools or [])
    client.call_tool = AsyncMock(return_value=_text_result("{}"))
    return client


def _build_hub(coach: MagicMock, github: MagicMock, **kwargs: Any) -> MCPClientHub:
    """Construct a hub whose MCPClient instances are the supplied mocks."""
    with patch("coach_web.mcp_hub.MCPClient", side_effect=[coach, github]):
        return MCPClientHub("http://coach/mcp", "http://github/mcp", **kwargs)


class TestConstruction:
    """Hub construction and configuration."""

    def test_init_stores_urls(self) -> None:
        """The hub stores both server URLs and the call timeout."""
        hub = _build_hub(_mock_client(), _mock_client(), call_timeout=12.5)

        assert hub.coach_url == "http://coach/mcp"
        assert hub.github_url == "http://github/mcp"
        assert hub.call_timeout == 12.5

    def test_from_settings_uses_configured_urls(self, settings: Settings) -> None:
        """from_settings wires the configured coach and github MCP URLs."""
        with patch("coach_web.mcp_hub.MCPClient"):
            hub = MCPClientHub.from_settings(settings)

        assert hub.coach_url == settings.COACH_MCP_URL
        assert hub.github_url == settings.GITHUB_MCP_URL


class TestConnect:
    """Connection lifecycle behaviour."""

    async def test_connect_establishes_both_sessions(self) -> None:
        """connect initialises both coach-mcp and github-mcp sessions."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)

        await hub.connect()

        coach.connect.assert_awaited_once()
        github.connect.assert_awaited_once()
        assert hub.connected_servers == {COACH_SERVER, GITHUB_SERVER}
        assert hub.connection_errors == {}

    async def test_connect_partial_failure_is_resilient(self) -> None:
        """A single unreachable server does not abort the hub."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        github = _mock_client()
        github.connect = AsyncMock(side_effect=MCPConnectionError("refused"))
        hub = _build_hub(coach, github)

        await hub.connect()

        assert hub.connected_servers == {COACH_SERVER}
        assert GITHUB_SERVER in hub.connection_errors

    async def test_connect_total_failure_raises_hub_error(self) -> None:
        """When no server is reachable a typed MCPHubError is raised."""
        coach = _mock_client()
        github = _mock_client()
        coach.connect = AsyncMock(side_effect=MCPConnectionError("coach down"))
        github.connect = AsyncMock(side_effect=MCPConnectionError("github down"))
        hub = _build_hub(coach, github)

        with pytest.raises(MCPHubError):
            await hub.connect()

    async def test_connect_survives_list_tools_failure(self) -> None:
        """A failing tool listing degrades gracefully without dropping the session."""
        coach = _mock_client()
        coach.list_tools = AsyncMock(side_effect=RuntimeError("boom"))
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)

        await hub.connect()

        assert hub.connected_servers == {COACH_SERVER, GITHUB_SERVER}
        assert len(hub.list_all_tools()) == 1

    async def test_reconnect_reestablishes_sessions(self) -> None:
        """reconnect closes and re-opens both sessions."""
        coach = _mock_client()
        github = _mock_client()
        hub = _build_hub(coach, github)

        await hub.connect()
        await hub.reconnect()

        assert coach.connect.await_count == 2
        assert github.connect.await_count == 2
        assert coach.close.await_count == 1

    async def test_close_closes_all_clients(self) -> None:
        """close tears down every client and clears the registry."""
        coach = _mock_client()
        github = _mock_client()
        hub = _build_hub(coach, github)
        await hub.connect()

        await hub.close()

        coach.close.assert_awaited_once()
        github.close.assert_awaited_once()
        assert hub.connected_servers == set()
        assert hub.list_all_tools() == []

    async def test_close_survives_client_error(self) -> None:
        """A failing client close is swallowed so shutdown always completes."""
        coach = _mock_client()
        github = _mock_client()
        github.close = AsyncMock(side_effect=RuntimeError("already gone"))
        hub = _build_hub(coach, github)
        await hub.connect()

        await hub.close()

        assert hub.connected_servers == set()

    async def test_async_context_manager(self) -> None:
        """The hub can be used as an async context manager."""
        coach = _mock_client()
        github = _mock_client()

        with patch("coach_web.mcp_hub.MCPClient", side_effect=[coach, github]):
            async with MCPClientHub("http://coach/mcp", "http://github/mcp") as hub:
                assert isinstance(hub, MCPClientHub)

        coach.close.assert_awaited_once()
        github.close.assert_awaited_once()


class TestListAllTools:
    """Tool aggregation and OpenAI schema conversion."""

    def test_list_all_tools_empty_when_not_connected(self) -> None:
        """No tools are exposed before connect."""
        hub = _build_hub(_mock_client(), _mock_client())

        assert hub.list_all_tools() == []

    async def test_list_all_tools_aggregates_and_namespaces(self) -> None:
        """Tools from both servers are merged with namespaced names."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard", "Readiness")])
        github = _mock_client([_tool("list_issues", "List issues")])
        hub = _build_hub(coach, github)
        await hub.connect()

        schemas = hub.list_all_tools()

        names = {schema["function"]["name"] for schema in schemas}
        assert names == {
            f"{COACH_SERVER}__intervals_get_readiness_dashboard",
            f"{GITHUB_SERVER}__list_issues",
        }
        for schema in schemas:
            assert schema["type"] == "function"
            assert schema["function"]["parameters"]["type"] == "object"

    async def test_list_all_tools_preserves_description(self) -> None:
        """The tool description is carried into the OpenAI schema."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard", "Readiness")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        schema = hub.list_all_tools()[0]

        assert schema["function"]["description"] == "Readiness"


class TestExecuteTool:
    """Tool routing, argument injection and error handling."""

    async def test_execute_tool_routes_to_coach_and_injects_response_format(self) -> None:
        """coach-mcp tools receive response_format=json by default."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result('{"ftp": 250}'))

        result = await hub.execute_tool(
            f"{COACH_SERVER}__intervals_get_readiness_dashboard",
            {"athlete_id": "0"},
        )

        coach.call_tool.assert_awaited_once_with(
            "intervals_get_readiness_dashboard",
            {"athlete_id": "0", "response_format": "json"},
        )
        assert json.loads(result) == {"ftp": 250}

    async def test_execute_tool_preserves_explicit_response_format(self) -> None:
        """An explicit response_format is never overwritten."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(
            f"{COACH_SERVER}__intervals_get_readiness_dashboard",
            {"response_format": "markdown"},
        )

        coach.call_tool.assert_awaited_once_with(
            "intervals_get_readiness_dashboard",
            {"response_format": "markdown"},
        )

    async def test_execute_tool_does_not_inject_for_github(self) -> None:
        """github-mcp tools are called with their arguments untouched."""
        coach = _mock_client()
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)
        await hub.connect()

        await hub.execute_tool(f"{GITHUB_SERVER}__list_issues", {"state": "open"})

        github.call_tool.assert_awaited_once_with("list_issues", {"state": "open"})

    async def test_execute_tool_accepts_unique_raw_name(self) -> None:
        """A unique un-namespaced tool name still routes correctly."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)
        await hub.connect()

        await hub.execute_tool("intervals_get_readiness_dashboard")

        coach.call_tool.assert_awaited_once()

    async def test_execute_tool_ambiguous_raw_name_returns_error(self) -> None:
        """An ambiguous raw name is rejected instead of guessing a server."""
        coach = _mock_client([_tool("shared")])
        github = _mock_client([_tool("shared")])
        hub = _build_hub(coach, github)
        await hub.connect()

        result = await hub.execute_tool("shared")

        assert "Unknown tool" in json.loads(result)["error"]

    async def test_execute_tool_unknown_returns_error(self) -> None:
        """An unknown tool yields a graceful JSON error string."""
        hub = _build_hub(_mock_client(), _mock_client())
        await hub.connect()

        result = await hub.execute_tool("does_not_exist")

        assert "Unknown tool" in json.loads(result)["error"]

    async def test_execute_tool_timeout_returns_error(self) -> None:
        """A slow tool call is aborted and reported as a timeout."""
        coach = _mock_client([_tool("slow_tool")])
        hub = _build_hub(coach, _mock_client(), call_timeout=0.01)
        await hub.connect()

        async def _slow(*args: Any, **kwargs: Any) -> CallToolResult:
            await asyncio.sleep(1)
            return _text_result("{}")

        coach.call_tool = AsyncMock(side_effect=_slow)

        result = await hub.execute_tool(f"{COACH_SERVER}__slow_tool")

        assert "timed out" in json.loads(result)["error"]

    async def test_execute_tool_connection_error_returns_error(self) -> None:
        """A dropped connection yields a graceful JSON error string."""
        coach = _mock_client([_tool("boom")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(side_effect=MCPConnectionError("dropped"))

        result = await hub.execute_tool(f"{COACH_SERVER}__boom")

        assert "dropped" in json.loads(result)["error"]

    async def test_execute_tool_unexpected_error_returns_error(self) -> None:
        """Any unexpected failure is converted into a graceful error string."""
        coach = _mock_client([_tool("boom")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(side_effect=RuntimeError("kaboom"))

        result = await hub.execute_tool(f"{COACH_SERVER}__boom")

        assert "kaboom" in json.loads(result)["error"]

    async def test_execute_tool_disconnected_server_returns_error(self) -> None:
        """A registered tool whose server dropped is reported as disconnected."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        github = _mock_client([_tool("list_issues")])
        hub = _build_hub(coach, github)
        await hub.connect()
        hub._connected.discard(GITHUB_SERVER)

        result = await hub.execute_tool(f"{GITHUB_SERVER}__list_issues")

        assert "not connected" in json.loads(result)["error"]

    async def test_execute_tool_returns_structured_content(self) -> None:
        """Structured content is serialised as JSON when present."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[],
                structured_content={"ftp": 260},
            )
        )

        result = await hub.execute_tool(f"{COACH_SERVER}__intervals_get_readiness_dashboard")

        assert json.loads(result) == {"ftp": 260}

    async def test_execute_tool_error_result_returns_error(self) -> None:
        """An MCP error result is surfaced as a JSON error string."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("upstream failure", is_error=True))

        result = await hub.execute_tool(f"{COACH_SERVER}__intervals_get_readiness_dashboard")

        assert "upstream failure" in json.loads(result)["error"]

    async def test_execute_tool_empty_result_returns_empty_object(self) -> None:
        """A result without text or structured content serialises to an empty object."""
        coach = _mock_client([_tool("intervals_get_readiness_dashboard")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=CallToolResult(content=[]))

        result = await hub.execute_tool(f"{COACH_SERVER}__intervals_get_readiness_dashboard")

        assert json.loads(result) == {}
