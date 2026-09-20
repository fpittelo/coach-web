"""Tests for coach_web.agent — OpenRouter client, agent loop and SSE pipeline.

Every test is fully hermetic: the OpenRouter HTTP layer is mocked with
``httpx.MockTransport`` and the MCP hub is replaced by an in-memory fake, so no
live API call is ever made.
"""

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coach_web.agent import (
    DEFAULT_OPENROUTER_MODEL,
    PROPOSE_PLAN_TOOL,
    AgentEvent,
    CoachAgent,
    OpenRouterClient,
    OpenRouterError,
    _accumulate_tool_call,
    _first_choice,
    _parse_arguments,
    create_agent,
)
from coach_web.app import create_app
from coach_web.config import Settings
from coach_web.mcp_hub import MCPClientHub, MCPHubError
from coach_web.models import ChatMessage, PlanProposal, WorkoutStep

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeStreamer:
    """In-memory stand-in for OpenRouterClient returning canned chunk streams."""

    model = "test/model"

    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.closed = False

    def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Record the request and replay the next canned chunk sequence."""
        self.requests.append({"messages": deepcopy(messages), "tools": deepcopy(tools)})
        chunks = self._responses.pop(0)

        async def _generate() -> AsyncIterator[dict[str, Any]]:
            for chunk in chunks:
                yield chunk

        return _generate()

    async def close(self) -> None:
        """Mark the streamer as closed."""
        self.closed = True


class RaisingStreamer(FakeStreamer):
    """Streamer that raises an OpenRouterError while streaming."""

    def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Return a generator that fails before yielding any chunk."""

        async def _generate() -> AsyncIterator[dict[str, Any]]:
            raise OpenRouterError("upstream exploded")
            yield {}  # pragma: no cover - makes this an async generator

        return _generate()


class FakeHub:
    """In-memory stand-in for MCPClientHub."""

    def __init__(
        self,
        tools: list[dict[str, Any]] | None = None,
        results: dict[str, str] | None = None,
    ) -> None:
        self._tools = tools or []
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.connected = False
        self.closed = False

    def list_all_tools(self) -> list[dict[str, Any]]:
        """Return the configured OpenAI tool schemas."""
        return self._tools

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Record the call and return the configured JSON payload."""
        self.calls.append((name, arguments))
        return self._results.get(name, "{}")

    async def connect(self) -> None:
        """Mark the hub as connected."""
        self.connected = True

    async def close(self) -> None:
        """Mark the hub as closed."""
        self.closed = True


def _chunk(
    *,
    content: str | None = None,
    reasoning: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    """Build an OpenAI-compatible streaming chunk."""
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


def _tool_call_delta(
    index: int,
    *,
    call_id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> dict[str, Any]:
    """Build a streamed tool-call delta fragment."""
    function: dict[str, Any] = {}
    if name is not None:
        function["name"] = name
    if arguments is not None:
        function["arguments"] = arguments
    delta: dict[str, Any] = {"index": index, "function": function}
    if call_id is not None:
        delta["id"] = call_id
    return delta


def _sse_bytes(chunks: list[dict[str, Any]]) -> bytes:
    """Serialise chunks as an SSE body terminated by ``[DONE]``."""
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


async def _collect(
    agent: CoachAgent,
    message: str,
    *,
    history: list[Any] | None = None,
) -> list[AgentEvent]:
    """Drain the agent event stream into a list."""
    return [event async for event in agent.run(message, history=history)]


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    """Build a valid plan proposal payload with optional overrides."""
    payload: dict[str, Any] = {
        "title": "Threshold Builder",
        "week_id": "2026-W38",
        "summary": "Three by twelve minutes at threshold.",
        "rationale": "Raise FTP ahead of the build block.",
        "steps": [
            {
                "label": "Warm-up",
                "duration_minutes": 15,
                "target_power_pct": 60,
                "target_power_watts": 150,
                "cadence_rpm": 90,
                "description": "Progressive spin-up",
            },
            {
                "label": "Threshold",
                "duration_minutes": 36,
                "target_power_pct": 98,
                "target_power_watts": 245,
                "cadence_rpm": 88,
            },
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# PlanProposal model
# ---------------------------------------------------------------------------


class TestPlanProposalModel:
    """Structured workout plan validation."""

    def test_accepts_valid_payload(self) -> None:
        """A well-formed plan proposal validates and exposes its steps."""
        plan = PlanProposal.model_validate(_plan_payload())

        assert plan.title == "Threshold Builder"
        assert plan.week_id == "2026-W38"
        assert len(plan.steps) == 2
        assert isinstance(plan.steps[0], WorkoutStep)

    def test_rejects_invalid_week_id(self) -> None:
        """week_id must follow the ISO YYYY-WNN pattern."""
        with pytest.raises(ValidationError):
            PlanProposal.model_validate(_plan_payload(week_id="week-38"))

    def test_rejects_non_positive_duration(self) -> None:
        """A workout step must have a strictly positive duration."""
        payload = _plan_payload(
            steps=[{"label": "Broken", "duration_minutes": 0}],
        )

        with pytest.raises(ValidationError):
            PlanProposal.model_validate(payload)

    def test_defaults_steps_to_empty_list(self) -> None:
        """Steps are optional and default to an empty list."""
        plan = PlanProposal.model_validate(
            {"title": "Rest", "week_id": "2026-W39", "summary": "Full recovery."}
        )

        assert plan.steps == []
        assert plan.rationale is None


# ---------------------------------------------------------------------------
# AgentEvent
# ---------------------------------------------------------------------------


class TestAgentEvent:
    """SSE event packet serialisation."""

    def test_serialises_to_single_line_json(self) -> None:
        """Events serialise to compact single-line JSON."""
        event = AgentEvent(type="token", data={"text": "hello"})

        payload = event.model_dump_json()

        assert payload == '{"type":"token","data":{"text":"hello"}}'
        assert "\n" not in payload

    def test_data_defaults_to_empty_dict(self) -> None:
        """Events may be emitted without a payload."""
        event = AgentEvent(type="done")

        assert event.data == {}


# ---------------------------------------------------------------------------
# OpenRouterClient
# ---------------------------------------------------------------------------


class TestOpenRouterClient:
    """Async OpenRouter chat-completions client."""

    async def test_streams_parsed_chunks_from_openrouter(self) -> None:
        """Chunks are fetched from the OpenRouter v1 endpoint and parsed."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                content=_sse_bytes([_chunk(content="hello")]),
                headers={"content-type": "text/event-stream"},
            )

        client = OpenRouterClient("sk-test", transport=httpx.MockTransport(handler))

        chunks = [
            chunk
            async for chunk in client.stream_chat_completion([{"role": "user", "content": "hi"}])
        ]

        assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert captured["headers"]["authorization"] == "Bearer sk-test"
        assert captured["body"]["stream"] is True
        assert captured["body"]["model"] == DEFAULT_OPENROUTER_MODEL
        assert chunks[0]["choices"][0]["delta"]["content"] == "hello"
        await client.close()

    async def test_forwards_tools_and_custom_model(self) -> None:
        """A custom model and the tool schemas are forwarded verbatim."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=_sse_bytes([]))

        client = OpenRouterClient(
            "sk-test",
            model="anthropic/claude-sonnet-4.5",
            transport=httpx.MockTransport(handler),
        )
        tools = [{"type": "function", "function": {"name": "noop"}}]

        _ = [chunk async for chunk in client.stream_chat_completion([], tools=tools)]

        assert captured["body"]["model"] == "anthropic/claude-sonnet-4.5"
        assert captured["body"]["tools"] == tools
        await client.close()

    async def test_custom_base_url_and_attribution_headers(self) -> None:
        """Base URL, referer and app title are configurable."""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, content=_sse_bytes([]))

        client = OpenRouterClient(
            "sk-test",
            base_url="https://proxy.local/api/v1",
            referer="https://example.test",
            app_title="Coach Test",
            transport=httpx.MockTransport(handler),
        )

        _ = [chunk async for chunk in client.stream_chat_completion([])]

        assert captured["url"] == "https://proxy.local/api/v1/chat/completions"
        assert captured["headers"]["http-referer"] == "https://example.test"
        assert captured["headers"]["x-title"] == "Coach Test"
        await client.close()

    async def test_http_error_raises_openrouter_error(self) -> None:
        """A non-2xx response is surfaced as a typed OpenRouterError."""
        handler = lambda request: httpx.Response(  # noqa: E731
            401, json={"error": {"message": "invalid api key"}}
        )
        client = OpenRouterClient("sk-bad", transport=httpx.MockTransport(handler))

        with pytest.raises(OpenRouterError, match="401"):
            _ = [chunk async for chunk in client.stream_chat_completion([])]

        await client.close()

    async def test_transport_error_raises_openrouter_error(self) -> None:
        """A transport failure is wrapped in a typed OpenRouterError."""

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = OpenRouterClient("sk-test", transport=httpx.MockTransport(handler))

        with pytest.raises(OpenRouterError, match="connection refused"):
            _ = [chunk async for chunk in client.stream_chat_completion([])]

        await client.close()

    async def test_ignores_done_marker_and_malformed_lines(self) -> None:
        """The [DONE] sentinel and non-JSON lines are skipped."""
        body = b"data: not-json\n\ndata: [DONE]\n\n"
        handler = lambda request: httpx.Response(  # noqa: E731
            200, content=body, headers={"content-type": "text/event-stream"}
        )
        client = OpenRouterClient("sk-test", transport=httpx.MockTransport(handler))

        chunks = [chunk async for chunk in client.stream_chat_completion([])]

        assert chunks == []
        await client.close()

    async def test_async_context_manager_closes_client(self) -> None:
        """The client can be used as an async context manager."""
        handler = lambda request: httpx.Response(200, content=_sse_bytes([]))  # noqa: E731

        async with OpenRouterClient("sk-test", transport=httpx.MockTransport(handler)) as client:
            assert isinstance(client, OpenRouterClient)
            assert client.model == DEFAULT_OPENROUTER_MODEL

        assert client._client.is_closed is True


# ---------------------------------------------------------------------------
# CoachAgent loop
# ---------------------------------------------------------------------------


class TestCoachAgentLoop:
    """Autonomous multi-turn tool-calling loop."""

    async def test_single_turn_streams_tokens_and_done(self) -> None:
        """A tool-free answer streams tokens then a terminal done event."""
        streamer = FakeStreamer(
            [[_chunk(content="Hello "), _chunk(content="athlete"), _chunk(finish_reason="stop")]]
        )
        hub = FakeHub()
        agent = CoachAgent(streamer, hub)

        events = await _collect(agent, "How am I?")

        types = [event.type for event in events]
        assert types[0] == "status"
        assert types[-1] == "done"
        assert [e.data["text"] for e in events if e.type == "token"] == ["Hello ", "athlete"]
        assert events[-1].data["message"] == "Hello athlete"
        assert hub.calls == []

    async def test_tool_call_is_executed_and_fed_back(self) -> None:
        """A requested MCP tool runs through the hub and its result is fed back."""
        tool_name = "coach-mcp__intervals_get_readiness_dashboard"
        streamer = FakeStreamer(
            [
                [
                    _chunk(
                        tool_calls=[
                            _tool_call_delta(
                                0,
                                call_id="call_1",
                                name=tool_name,
                                arguments='{"athlete_id"',
                            )
                        ]
                    ),
                    _chunk(
                        tool_calls=[_tool_call_delta(0, arguments=': "0"}')],
                        finish_reason="tool_calls",
                    ),
                ],
                [_chunk(content="Your form is good."), _chunk(finish_reason="stop")],
            ]
        )
        hub = FakeHub(
            tools=[{"type": "function", "function": {"name": tool_name}}],
            results={tool_name: '{"tsb": 5}'},
        )
        agent = CoachAgent(streamer, hub)

        events = await _collect(agent, "Readiness please")

        assert hub.calls == [(tool_name, {"athlete_id": "0"})]
        call_event = next(e for e in events if e.type == "tool_call")
        assert call_event.data["name"] == tool_name
        assert call_event.data["arguments"] == {"athlete_id": "0"}
        result_event = next(e for e in events if e.type == "tool_result")
        assert result_event.data["result"] == '{"tsb": 5}'
        assert any(e.type == "tool_start" for e in events)
        assert events[-1].data["message"] == "Your form is good."

        follow_up = streamer.requests[1]["messages"]
        assert follow_up[-2]["role"] == "assistant"
        assert follow_up[-2]["tool_calls"][0]["id"] == "call_1"
        assert follow_up[-1]["role"] == "tool"
        assert follow_up[-1]["content"] == '{"tsb": 5}'

    async def test_parallel_tool_calls_are_all_executed(self) -> None:
        """Multiple tool calls in a single turn are all dispatched."""
        streamer = FakeStreamer(
            [
                [
                    _chunk(
                        tool_calls=[
                            _tool_call_delta(0, call_id="a", name="t1", arguments="{}"),
                            _tool_call_delta(1, call_id="b", name="t2", arguments="{}"),
                        ],
                        finish_reason="tool_calls",
                    )
                ],
                [_chunk(content="done"), _chunk(finish_reason="stop")],
            ]
        )
        hub = FakeHub(results={"t1": "1", "t2": "2"})
        agent = CoachAgent(streamer, hub)

        await _collect(agent, "go")

        assert hub.calls == [("t1", {}), ("t2", {})]

    async def test_reasoning_deltas_become_thought_events(self) -> None:
        """Reasoning content is streamed as thought events."""
        streamer = FakeStreamer(
            [
                [
                    _chunk(reasoning="Let me think"),
                    _chunk(content="Answer"),
                    _chunk(finish_reason="stop"),
                ]
            ]
        )
        agent = CoachAgent(streamer, FakeHub())

        events = await _collect(agent, "hi")

        thoughts = [e.data["text"] for e in events if e.type == "thought"]
        assert thoughts == ["Let me think"]

    async def test_history_is_replayed_before_the_new_message(self) -> None:
        """Prior conversation turns are prepended to the request messages."""
        streamer = FakeStreamer([[_chunk(content="ok"), _chunk(finish_reason="stop")]])
        agent = CoachAgent(streamer, FakeHub())

        await _collect(agent, "and now?", history=[ChatMessage(role="user", content="before")])

        messages = streamer.requests[0]["messages"]
        assert messages[1] == {"role": "user", "content": "before"}
        assert messages[2] == {"role": "user", "content": "and now?"}

    async def test_max_tool_iterations_emits_error_then_done(self) -> None:
        """An unbounded tool loop is stopped with an error and a done event."""
        looping = [
            [
                _chunk(
                    tool_calls=[_tool_call_delta(0, call_id="x", name="t", arguments="{}")],
                    finish_reason="tool_calls",
                )
            ]
        ]
        streamer = FakeStreamer([deepcopy(looping[0]) for _ in range(3)])
        hub = FakeHub()
        agent = CoachAgent(streamer, hub, max_tool_iterations=2)

        events = await _collect(agent, "loop")

        error = next(e for e in events if e.type == "error")
        assert "maximum" in error.data["message"].lower()
        assert events[-1].type == "done"
        assert len(hub.calls) == 2

    async def test_openrouter_error_is_surfaced_as_event(self) -> None:
        """A streaming failure becomes an error event rather than an exception."""
        agent = CoachAgent(RaisingStreamer([]), FakeHub())

        events = await _collect(agent, "boom")

        assert [e.type for e in events] == ["status", "error", "done"]
        assert "upstream exploded" in events[1].data["message"]

    async def test_context_manager_connects_and_closes(self) -> None:
        """The agent connects the hub on enter and tears everything down on exit."""
        streamer = FakeStreamer([[_chunk(content="ok"), _chunk(finish_reason="stop")]])
        hub = FakeHub()

        async with CoachAgent(streamer, hub) as agent:
            assert hub.connected is True
            await _collect(agent, "hi")

        assert hub.closed is True
        assert streamer.closed is True


class TestCoachAgentPlanProposal:
    """Structured plan proposal emission."""

    async def test_propose_plan_emits_validated_plan_events(self) -> None:
        """A propose_plan tool call emits plan and plan_proposal events."""
        plan = _plan_payload()
        streamer = FakeStreamer(
            [
                [
                    _chunk(
                        tool_calls=[
                            _tool_call_delta(
                                0,
                                call_id="plan_1",
                                name=PROPOSE_PLAN_TOOL,
                                arguments=json.dumps(plan),
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                ],
                [_chunk(content="Here is your plan."), _chunk(finish_reason="stop")],
            ]
        )
        agent = CoachAgent(streamer, FakeHub())

        events = await _collect(agent, "Plan my week")

        plan_event = next(e for e in events if e.type == "plan")
        proposal_event = next(e for e in events if e.type == "plan_proposal")
        assert plan_event.data["plan"]["title"] == "Threshold Builder"
        assert proposal_event.data["plan"]["week_id"] == "2026-W38"
        result = next(e for e in events if e.type == "tool_result")
        assert "plan_proposal_emitted" in result.data["result"]

    async def test_invalid_plan_is_reported_without_plan_event(self) -> None:
        """An invalid proposal is rejected and reported back to the model."""
        streamer = FakeStreamer(
            [
                [
                    _chunk(
                        tool_calls=[
                            _tool_call_delta(
                                0,
                                call_id="plan_bad",
                                name=PROPOSE_PLAN_TOOL,
                                arguments=json.dumps(_plan_payload(week_id="nope")),
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                ],
                [_chunk(content="Let me fix that."), _chunk(finish_reason="stop")],
            ]
        )
        agent = CoachAgent(streamer, FakeHub())

        events = await _collect(agent, "Plan my week")

        assert all(e.type != "plan" for e in events)
        assert any(e.type == "error" for e in events)
        result = next(e for e in events if e.type == "tool_result")
        assert "Invalid plan proposal" in result.data["result"]

    async def test_propose_plan_schema_is_advertised_to_the_model(self) -> None:
        """The propose_plan tool schema is merged into the advertised tools."""
        streamer = FakeStreamer([[_chunk(content="ok"), _chunk(finish_reason="stop")]])
        hub = FakeHub(tools=[{"type": "function", "function": {"name": "mcp_tool"}}])
        agent = CoachAgent(streamer, hub)

        await _collect(agent, "hi")

        names = [tool["function"]["name"] for tool in streamer.requests[0]["tools"]]
        assert names == ["mcp_tool", PROPOSE_PLAN_TOOL]


# ---------------------------------------------------------------------------
# Defensive parsing branches
# ---------------------------------------------------------------------------


class TestAgentDefensiveBranches:
    """Malformed upstream payloads degrade gracefully instead of crashing."""

    def test_first_choice_returns_empty_mapping_without_choices(self) -> None:
        """Chunks without a usable first choice yield an empty mapping."""
        assert _first_choice({}) == {}
        assert _first_choice({"choices": []}) == {}
        assert _first_choice({"choices": ["not-a-dict"]}) == {}

    def test_accumulate_tool_call_defaults_a_missing_index(self) -> None:
        """A tool-call fragment without an index is accumulated at position zero."""
        accumulator: dict[int, dict[str, str]] = {}

        _accumulate_tool_call(accumulator, {"function": {"name": "t", "arguments": "{}"}})

        assert accumulator[0]["name"] == "t"
        assert accumulator[0]["arguments"] == "{}"

    def test_parse_arguments_tolerates_empty_and_malformed_json(self) -> None:
        """Empty, invalid and non-object arguments all collapse to an empty mapping."""
        assert _parse_arguments("") == {}
        assert _parse_arguments("{not json") == {}
        assert _parse_arguments("[1, 2]") == {}

    def test_parse_sse_line_ignores_non_object_payloads(self) -> None:
        """Comments, non-JSON scalars and the [DONE] sentinel are skipped."""
        assert OpenRouterClient._parse_sse_line(": keep-alive") is None
        assert OpenRouterClient._parse_sse_line("data: 42") is None
        assert OpenRouterClient._parse_sse_line("data: [DONE]") is None

    async def test_non_dict_delta_is_skipped(self) -> None:
        """A chunk whose delta is not an object is ignored without aborting."""
        streamer = FakeStreamer(
            [
                [
                    {"choices": [{"delta": None}]},
                    _chunk(content="ok"),
                    _chunk(finish_reason="stop"),
                ]
            ]
        )
        agent = CoachAgent(streamer, FakeHub())

        events = await _collect(agent, "hi")

        assert events[-1].data["message"] == "ok"

    async def test_malformed_tool_arguments_execute_with_empty_mapping(self) -> None:
        """Unparseable tool arguments are forwarded as an empty mapping."""
        streamer = FakeStreamer(
            [
                [
                    _chunk(
                        tool_calls=[_tool_call_delta(0, call_id="c1", name="t", arguments="{bad")],
                        finish_reason="tool_calls",
                    )
                ],
                [_chunk(content="ok"), _chunk(finish_reason="stop")],
            ]
        )
        hub = FakeHub()
        agent = CoachAgent(streamer, hub)

        await _collect(agent, "go")

        assert hub.calls == [("t", {})]

    async def test_hub_connection_failure_closes_resources(self) -> None:
        """A failed hub connection tears down both the hub and the streamer."""

        class FailingHub(FakeHub):
            """Hub whose connection always fails."""

            async def connect(self) -> None:
                """Raise a hub error to exercise the teardown path."""
                raise MCPHubError("no MCP server reachable")

        streamer = FakeStreamer([])
        hub = FailingHub()
        agent = CoachAgent(streamer, hub)

        with pytest.raises(MCPHubError):
            async with agent:
                pass

        assert hub.closed is True
        assert streamer.closed is True


# ---------------------------------------------------------------------------
# Agent settings & factory
# ---------------------------------------------------------------------------


class TestAgentSettings:
    """OpenRouter configuration defaults."""

    def test_defaults_are_safe_for_local_dev(self) -> None:
        """An empty API key and the public OpenRouter base URL are the defaults."""
        settings = Settings()

        assert settings.OPENROUTER_API_KEY == ""
        assert settings.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
        # Pinned literal: the default must be a CURRENT OpenRouter model.
        # anthropic/claude-3.5-sonnet was retired upstream and made live chat
        # fail with HTTP 404 "No endpoints found".
        assert settings.OPENROUTER_MODEL == "anthropic/claude-sonnet-4.5"
        assert settings.OPENROUTER_MODEL == DEFAULT_OPENROUTER_MODEL
        assert settings.AGENT_MAX_TOOL_ITERATIONS >= 1


class TestCreateAgent:
    """Application agent factory."""

    def test_builds_agent_from_settings(self, settings: Settings) -> None:
        """create_agent wires the OpenRouter client and the MCP hub."""
        agent = create_agent(settings)

        assert isinstance(agent, CoachAgent)
        assert agent.client.model == settings.OPENROUTER_MODEL
        assert isinstance(agent.hub, MCPClientHub)
        assert agent.hub.coach_url == settings.COACH_MCP_URL

    def test_app_exposes_agent_factory(self, settings: Settings) -> None:
        """The FastAPI application exposes the default agent factory on state."""
        app = create_app(settings)

        assert app.state.agent_factory is create_agent


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


class FakeAgent:
    """In-memory agent emitting a canned event sequence."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.messages: list[str] = []
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> "FakeAgent":
        """Mark the agent as entered."""
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Mark the agent as exited."""
        self.exited = True

    async def run(
        self,
        message: str,
        *,
        history: list[Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Replay the canned events for the supplied message."""
        self.messages.append(message)
        for event in self._events:
            yield event


class ExplodingAgent(FakeAgent):
    """Agent whose hub connection fails."""

    async def __aenter__(self) -> "ExplodingAgent":
        """Raise a hub connection error on enter."""
        raise MCPHubError("no MCP server reachable")


class TestAgentStreamEndpoint:
    """GET /api/agent/stream SSE contract."""

    def _client_with(self, settings: Settings, agent: Any) -> TestClient:
        """Build a TestClient whose agent factory returns the supplied agent."""
        app = create_app(settings)
        app.state.agent_factory = MagicMock(return_value=agent)
        return TestClient(app)

    def test_streams_typed_sse_events(self, settings: Settings) -> None:
        """The endpoint streams typed SSE packets and closes on done."""
        agent = FakeAgent(
            [
                AgentEvent(type="status", data={"phase": "thinking"}),
                AgentEvent(type="token", data={"text": "hello"}),
                AgentEvent(type="done", data={"message": "hello", "iterations": 1}),
            ]
        )
        client = self._client_with(settings, agent)

        with client.stream("GET", "/api/agent/stream", params={"message": "hi"}) as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        assert "event: status" in body
        assert "event: token" in body
        assert "event: done" in body
        assert '"text":"hello"' in body
        assert agent.messages == ["hi"]
        assert agent.entered is True
        assert agent.exited is True

    def test_chat_stream_alias_is_registered(self, settings: Settings) -> None:
        """The /api/chat/stream alias exposes the same stream."""
        agent = FakeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(settings, agent)

        with client.stream("GET", "/api/chat/stream", params={"message": "hi"}) as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "event: done" in body

    def test_missing_message_is_rejected(self, settings: Settings) -> None:
        """The message query parameter is mandatory."""
        client = self._client_with(settings, FakeAgent([]))

        response = client.get("/api/agent/stream")

        assert response.status_code == 422

    def test_connection_failure_emits_error_event(self, settings: Settings) -> None:
        """A hub failure mid-request degrades into an error SSE event."""
        client = self._client_with(settings, ExplodingAgent([]))

        with client.stream("GET", "/api/agent/stream", params={"message": "hi"}) as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "event: error" in body
        assert "no MCP server reachable" in body
