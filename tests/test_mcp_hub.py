"""Tests for coach_web.mcp_hub."""

import asyncio
import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, ConfigDict, create_model

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


def _permissive_tool(name: str, description: str = "A tool") -> Tool:
    """Build a Tool whose flat schema explicitly permits extra properties."""
    return Tool(
        name=name,
        description=description,
        input_schema={"type": "object"},
    )


class _MutationParams(BaseModel):
    """FastMCP-equivalent event write input model (no ``response_format``)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    name: str = "Workout"
    description: str | None = None
    workout_doc: str | None = None
    athlete_id: str = "0"


class _ReadParams(BaseModel):
    """FastMCP-equivalent read input model (declares ``response_format``)."""

    model_config = ConfigDict(extra="forbid")

    athlete_id: str = "0"
    response_format: str = "markdown"


def _wrapped_tool(name: str, params_model: type[BaseModel]) -> Tool:
    """Build a real mcp 2.x-style tool schema wrapping *params_model*.

    The sidecar advertises every tool as a single ``params`` Pydantic-model
    argument (``{"properties": {"params": {"$ref": "#/$defs/..."}}, "$defs":
    {...}}``), so this mirrors the real FastMCP/Pydantic v2 output rather
    than hand-rolling an already-inlined schema.
    """
    arguments_model = create_model(f"{name}Arguments", params=(params_model, ...))
    return Tool(
        name=name,
        description="Wrapped tool",
        input_schema=arguments_model.model_json_schema(),
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
        coach = _mock_client([_permissive_tool("intervals_get_readiness_dashboard")])
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


class TestParamsWrapping:
    """Sidecar ``params``-model argument normalization (#248 B2b).

    The coach-mcp sidecar (mcp 2.x ``MCPServer``) exposes every tool as a
    single ``params`` Pydantic-model argument, so its advertised schema is
    ``required: ["params"]``. Server-side call sites speak the flat legacy
    shape; the model speaks the wrapped shape it reads from the schema.
    The hub normalizes at this single transport boundary so both shapes work.
    """

    def _params_tool(self, name: str, description: str = "A tool") -> Tool:
        """Build a Tool whose schema requires a single ``params`` object."""
        return Tool(
            name=name,
            description=description,
            input_schema={
                "type": "object",
                "properties": {"params": {"type": "object", "additionalProperties": True}},
                "required": ["params"],
            },
        )

    async def test_flat_arguments_are_wrapped_into_params(self) -> None:
        """A flat legacy payload is wrapped as ``{"params": args}``."""
        coach = _mock_client([self._params_tool("icu_batch_create_events")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool(
            f"{COACH_SERVER}__icu_batch_create_events",
            {"events": [{"external_id": "e1"}], "response_format": "json"},
        )

        coach.call_tool.assert_awaited_once_with(
            "icu_batch_create_events",
            {"params": {"events": [{"external_id": "e1"}], "response_format": "json"}},
        )

    async def test_params_wrapped_arguments_receive_defaults_inside_params(self) -> None:
        """The model's wrapped shape is never double-wrapped.

        The injected default lands in the DECLARED container (``params``),
        never as a stray top-level key the sidecar's ``extra="forbid"`` model
        would reject (C5).
        """
        coach = _mock_client([self._params_tool("icu_batch_create_events")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool(
            f"{COACH_SERVER}__icu_batch_create_events",
            {"params": {"events": [{"external_id": "e1"}]}},
        )

        coach.call_tool.assert_awaited_once_with(
            "icu_batch_create_events",
            {"params": {"events": [{"external_id": "e1"}], "response_format": "json"}},
        )

    async def test_injected_response_format_lands_inside_params(self) -> None:
        """The coach default injection lands inside the wrapped params object."""
        coach = _mock_client([self._params_tool("icu_get_workout_compliance")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool(
            f"{COACH_SERVER}__icu_get_workout_compliance",
            {"start_date": "2026-10-05", "end_date": "2026-10-11"},
        )

        coach.call_tool.assert_awaited_once_with(
            "icu_get_workout_compliance",
            {
                "params": {
                    "start_date": "2026-10-05",
                    "end_date": "2026-10-11",
                    "response_format": "json",
                }
            },
        )

    async def test_tools_without_a_params_property_are_untouched(self) -> None:
        """A permissive flat schema keeps flat (unwrapped) args plus defaults."""
        coach = _mock_client([_permissive_tool("flat_tool")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool(f"{COACH_SERVER}__flat_tool", {"x": "1"})

        coach.call_tool.assert_awaited_once_with("flat_tool", {"x": "1", "response_format": "json"})

    async def test_wrapping_applies_to_unique_raw_name_resolution(self) -> None:
        """A unique un-namespaced raw name resolves and wraps identically."""
        coach = _mock_client([self._params_tool("icu_batch_create_events")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool("icu_batch_create_events", {"events": [{"external_id": "e1"}]})

        coach.call_tool.assert_awaited_once_with(
            "icu_batch_create_events",
            {"params": {"events": [{"external_id": "e1"}], "response_format": "json"}},
        )

    async def test_empty_arguments_are_wrapped_for_params_requiring_tools(self) -> None:
        """A no-argument call wraps to ``{"params": {}}`` instead of failing."""
        coach = _mock_client([self._params_tool("intervals_get_readiness_dashboard")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result("{}"))

        await hub.execute_tool(f"{COACH_SERVER}__intervals_get_readiness_dashboard")

        coach.call_tool.assert_awaited_once_with(
            "intervals_get_readiness_dashboard",
            {"params": {"response_format": "json"}},
        )


class TestResultSerialization:
    """Tool-result serialisation across mcp 1.x/2.x shapes (#248 B2b)."""

    async def test_mcp2_string_result_envelope_is_unwrapped(self) -> None:
        """mcp 2.x wraps a string tool return as ``{"result": <text>}``.

        The text content IS the tool's payload; serialising the envelope
        double-encodes it and every JSON parser downstream (confirm-token
        extraction, mirror parsers) sees a foreign ``result`` key instead.
        """
        coach = _mock_client([_tool("icu_batch_create_events")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        payload = json.dumps({"phase": "preview", "confirm_token": "tok"})
        coach.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text=payload)],
                structured_content={"result": payload},
            )
        )

        result = await hub.execute_tool(f"{COACH_SERVER}__icu_batch_create_events")

        assert json.loads(result) == {"phase": "preview", "confirm_token": "tok"}

    async def test_genuine_structured_payload_is_preserved(self) -> None:
        """A real structured payload (no matching text) stays the JSON body."""
        coach = _mock_client([_tool("t")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[],
                structured_content={"ftp": 260},
            )
        )

        result = await hub.execute_tool(f"{COACH_SERVER}__t")

        assert json.loads(result) == {"ftp": 260}

    async def test_structured_result_key_collision_is_preserved(self) -> None:
        """A genuine ``{"result": ...}`` payload is not mistaken for the envelope.

        The envelope unwrap requires the structured value to EQUAL the text
        content; a tool whose actual payload is ``{"result": <x>}`` serialises
        its text as the full JSON body, which never equals the bare value.
        """
        coach = _mock_client([_tool("t")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        structured = {"result": "different"}
        coach.call_tool = AsyncMock(
            return_value=CallToolResult(
                content=[TextContent(type="text", text=json.dumps(structured))],
                structured_content=structured,
            )
        )

        result = await hub.execute_tool(f"{COACH_SERVER}__t")

        assert json.loads(result) == {"result": "different"}

    async def test_plain_text_result_without_structured_content(self) -> None:
        """A text-only result (mcp 1.x / github-mcp shape) passes through."""
        coach = _mock_client([_tool("t")])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()
        coach.call_tool = AsyncMock(return_value=_text_result('{"ok": true}'))

        result = await hub.execute_tool(f"{COACH_SERVER}__t")

        assert json.loads(result) == {"ok": True}


class TestLocalSchemaGate:
    """Bounded local-only ``$defs``/``$ref`` gate before default injection (C5).

    The real coach-mcp sidecar advertises FastMCP/Pydantic v2 schemas where
    the arguments live under ``params`` behind a local ``#/$defs/<Model>``
    reference. Event-write models forbid extra keys and do not declare
    ``response_format``; injecting a default there makes the sidecar reject
    the call. Reads do declare ``response_format`` and must keep receiving the
    JSON default inside ``params``.
    """

    async def test_wrapped_mutation_schema_gets_no_response_format(self) -> None:
        """A real wrapped event-write schema is sent without response_format."""
        tool = _wrapped_tool("intervals_create_event", _MutationParams)
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(
            f"{COACH_SERVER}__intervals_create_event",
            {"event_id": "e1", "name": "Threshold", "description": "Prose"},
        )

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"event_id": "e1", "name": "Threshold", "description": "Prose"}}
        assert "response_format" not in json.dumps(sent)

    async def test_wrapped_mutation_payload_already_in_params_is_untouched(self) -> None:
        """The model's wrapped payload is not mutated with a forbidden default."""
        tool = _wrapped_tool("intervals_update_event", _MutationParams)
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(
            f"{COACH_SERVER}__intervals_update_event",
            {"params": {"event_id": "e1", "workout_doc": "- 10m 200w"}},
        )

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"event_id": "e1", "workout_doc": "- 10m 200w"}}
        assert "response_format" not in json.dumps(sent)

    async def test_wrapped_read_schema_receives_json_inside_params(self) -> None:
        """A read model declaring response_format keeps the JSON default inside params."""
        tool = _wrapped_tool("intervals_get_event", _ReadParams)
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(f"{COACH_SERVER}__intervals_get_event", {"athlete_id": "0"})

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"athlete_id": "0", "response_format": "json"}}

    async def test_root_ref_is_resolved_locally(self) -> None:
        """A root-level local ``$ref`` is dereferenced before the params lookup."""
        tool = Tool(
            name="root_ref_read",
            description="Root $ref",
            input_schema={
                "type": "object",
                "$ref": "#/$defs/ReadArguments",
                "$defs": {
                    "ReadArguments": {
                        "type": "object",
                        "properties": {"params": {"$ref": "#/$defs/ReadParams"}},
                        "required": ["params"],
                    },
                    "ReadParams": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"response_format": {"type": "string"}},
                    },
                },
            },
        )
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(f"{COACH_SERVER}__root_ref_read", {"params": {}})

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"response_format": "json"}}

    async def test_external_ref_is_never_followed_and_blocks_injection(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An ``http(s)`` ``$ref`` fails closed: no fetch, no default, tool-only log."""
        tool = Tool(
            name="external_ref_write",
            description="External $ref",
            input_schema={
                "type": "object",
                "properties": {"params": {"$ref": "https://evil.example/x.json"}},
                "required": ["params"],
            },
        )
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        with caplog.at_level(logging.WARNING, logger="coach_web.mcp_hub"):
            await hub.execute_tool(
                f"{COACH_SERVER}__external_ref_write",
                {"params": {"event_id": "e1"}},
            )

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"event_id": "e1"}}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("external_ref_write" in r.getMessage() for r in warnings)
        assert all("evil.example" not in r.getMessage() for r in warnings)
        assert len(warnings) == 1

    async def test_cyclic_ref_terminates_and_blocks_injection(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A self-referential ``$ref`` is bounded, non-permissive and logged once."""
        tool = Tool(
            name="cyclic_ref_write",
            description="Cyclic $ref",
            input_schema={
                "type": "object",
                "properties": {"params": {"$ref": "#/$defs/A"}},
                "$defs": {"A": {"$ref": "#/$defs/A"}},
            },
        )
        coach = _mock_client([tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        with caplog.at_level(logging.WARNING, logger="coach_web.mcp_hub"):
            await hub.execute_tool(
                f"{COACH_SERVER}__cyclic_ref_write",
                {"params": {"event_id": "e1"}},
            )

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"event_id": "e1"}}
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("cyclic_ref_write" in r.getMessage() for r in warnings)
        assert len(warnings) == 1

    @staticmethod
    def _chain_tool(name: str, ref_hops: int) -> Tool:
        """Build a tool whose ``params`` ref chain is *ref_hops* long."""
        defs: dict[str, Any] = {f"K{i}": {"$ref": f"#/$defs/K{i + 1}"} for i in range(ref_hops - 1)}
        defs[f"K{ref_hops - 1}"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"response_format": {"type": "string"}},
        }
        return Tool(
            name=name,
            description="Ref chain",
            input_schema={
                "type": "object",
                "properties": {"params": {"$ref": "#/$defs/K0"}},
                "$defs": defs,
            },
        )

    async def test_eight_ref_hops_still_resolve_and_inject(self) -> None:
        """The documented hop cap allows a chain of exactly eight refs."""
        coach = _mock_client([self._chain_tool("deep_ok_read", 8)])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(f"{COACH_SERVER}__deep_ok_read", {"params": {}})

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {"response_format": "json"}}

    async def test_more_than_eight_ref_hops_fail_closed(self) -> None:
        """A nine-ref chain is unresolvable: no injection, even though it declares it."""
        coach = _mock_client([self._chain_tool("deep_blocked_read", 9)])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(f"{COACH_SERVER}__deep_blocked_read", {"params": {}})

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"params": {}}

    async def test_non_permissive_flat_schema_gets_no_injection(self) -> None:
        """A flat schema with ``additionalProperties: false`` never gains a key."""
        coach = _mock_client(
            [
                Tool(
                    name="flat_forbid_write",
                    description="Flat forbid",
                    input_schema={
                        "type": "object",
                        "properties": {"event_id": {"type": "string"}},
                        "additionalProperties": False,
                    },
                )
            ]
        )
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        await hub.execute_tool(f"{COACH_SERVER}__flat_forbid_write", {"event_id": "e1"})

        sent = coach.call_tool.await_args.args[1]
        assert sent == {"event_id": "e1"}

    async def test_caller_arguments_and_nested_dicts_are_not_mutated(self) -> None:
        """C5d: deepcopy protects every caller-owned dict from in-place mutation."""
        read_tool = _wrapped_tool("intervals_get_event", _ReadParams)
        coach = _mock_client([read_tool])
        hub = _build_hub(coach, _mock_client())
        await hub.connect()

        wrapped: dict[str, Any] = {"params": {"athlete_id": "0"}}
        await hub.execute_tool(f"{COACH_SERVER}__intervals_get_event", wrapped)
        assert wrapped == {"params": {"athlete_id": "0"}}

        flat: dict[str, Any] = {"athlete_id": "0"}
        await hub.execute_tool(f"{COACH_SERVER}__intervals_get_event", flat)
        assert flat == {"athlete_id": "0"}
