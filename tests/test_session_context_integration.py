"""AC3–AC5 — multi-turn intra-session grounding through ``POST /api/agent/stream``.

Pins the runtime wiring of MADR-0025 **Amendment 0002** (Epic #432, #435):

- **AC3 (06:50→06:53 regression):** Turn 1 fetches the baseline domains and
  populates the thread ledger; the 3-minutes-later follow-up injects the
  fenced ``session_metrics_block`` and performs **zero** ``coach-mcp`` calls —
  both when the model answers in one pass and when a stubborn model emits a
  window-jittered duplicate baseline call.
- **AC4 (fresh-sync trigger):** an athlete-reported sync cue evicts the
  ledger BEFORE prompt assembly, omits the stale block, executes fresh
  ``coach-mcp`` reads and refreshes the session baseline.
- **AC5 (degrade safety):** when conversation persistence degrades
  (``turn_thread is None``) the turn runs stateless with no ledger access.

Every test is hermetic: the model backend is an in-memory chunk replay and the
MCP hub an in-memory fake (no network, no live Intervals.icu call).
"""

import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coach_web.agent import CoachAgent
from coach_web.app import create_app
from coach_web.config import Settings
from coach_web.debrief import DEFAULT_ATHLETE_ID
from coach_web.errors import EncryptionReason, EncryptionUnavailableError
from coach_web.session_context import (
    SESSION_METRICS_BLOCK_BEGIN,
    SESSION_METRICS_BLOCK_END,
)
from tests.test_agent import FakeHub, FakeStreamer, _chunk, _tool_call_delta

WELLNESS_TOOL = "coach-mcp__icu_get_wellness"
TRENDS_TOOL = "coach-mcp__icu_get_trends"
ACTIVITIES_TOOL = "coach-mcp__intervals_list_activities"

_WELLNESS_PAYLOAD = json.dumps({"resting_hr": 44, "hrv_rmssd": 68})
_TRENDS_PAYLOAD = json.dumps({"ctl": 75, "atl": 60})
_ACTIVITIES_PAYLOAD = json.dumps({"sessions": [{"date": "2026-11-22", "load": 90}]})

_TODAY = date.today().isoformat()


def _events(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into ``(event, AgentEvent.data)`` pairs."""
    parsed: list[tuple[str, dict[str, Any]]] = []
    event_type: str | None = None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_type = line[len("event: ") :]
        elif line.startswith("data: ") and event_type is not None:
            payload = json.loads(line[len("data: ") :])
            data = payload.get("data") if isinstance(payload, dict) else None
            parsed.append((event_type, data if isinstance(data, dict) else {}))
    return parsed


def _tool_call_chunk(*, call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Build a one-call tool-call chunk."""
    return _chunk(
        tool_calls=[
            _tool_call_delta(0, call_id=call_id, name=name, arguments=json.dumps(arguments))
        ],
        finish_reason="tool_calls",
    )


def _turn_one_responses() -> list[list[dict[str, Any]]]:
    """Turn 1: the three baseline reads of the 06:50 message, then the reply."""
    return [
        [
            _chunk(
                tool_calls=[
                    _tool_call_delta(0, call_id="w1", name=WELLNESS_TOOL, arguments="{}"),
                    _tool_call_delta(1, call_id="t1", name=TRENDS_TOOL, arguments="{}"),
                    _tool_call_delta(
                        2,
                        call_id="a1",
                        name=ACTIVITIES_TOOL,
                        arguments=json.dumps({"oldest": "2026-11-15", "newest": _TODAY}),
                    ),
                ],
                finish_reason="tool_calls",
            )
        ],
        [_chunk(content="Resting HR 44, HRV 68, CTL 75.", finish_reason="stop")],
    ]


def _single_pass_turn_two() -> list[list[dict[str, Any]]]:
    """Turn 2 shape A: the model answers directly from the injected block."""
    return [[_chunk(content="Yes — keep 90 minutes of endurance today.", finish_reason="stop")]]


def _stubborn_turn_two() -> list[list[dict[str, Any]]]:
    """Turn 2 shape B: a window-jittered duplicate baseline call, then the reply."""
    return [
        [
            _tool_call_chunk(
                call_id="w2",
                name=WELLNESS_TOOL,
                arguments={"oldest": "2026-10-11", "newest": _TODAY},
            )
        ],
        [_chunk(content="Same numbers — keep 90 minutes of endurance.", finish_reason="stop")],
    ]


class _Harness:
    """App + fake model/hub harness for one multi-turn conversation."""

    def __init__(
        self,
        settings: Settings,
        responses: list[list[dict[str, Any]]],
        results: dict[str, str],
    ) -> None:
        self.streamer = FakeStreamer(responses)
        self.hub = FakeHub(
            tools=[
                {"type": "function", "function": {"name": name}}
                for name in (WELLNESS_TOOL, TRENDS_TOOL, ACTIVITIES_TOOL)
            ],
            results=results,
        )
        self.agents: list[CoachAgent] = []
        self.turn_prompts: list[str] = []
        self.app = create_app(settings)
        self.app.state.agent_factory = MagicMock(side_effect=self._factory)
        self.client = TestClient(self.app)

    def _factory(self, _settings: Settings) -> CoachAgent:
        """Create one agent per request, capturing it for prompt assertions."""
        agent = CoachAgent(self.streamer, self.hub)
        self.agents.append(agent)
        return agent

    @property
    def ledger(self) -> Any:
        """The app's in-process session metrics ledger."""
        return self.app.state.session_metrics_ledger

    def thread_id(self) -> int:
        """Open (or read) the active conversation thread id."""
        return int(self.client.post("/api/conversation/reset").json()["thread_id"])

    def stream(self, message: str, thread_id: int) -> list[tuple[str, dict[str, Any]]]:
        """Drive one streamed turn, recording the prompt the model saw."""
        first_request = len(self.streamer.requests)
        with self.client.stream(
            "POST",
            "/api/agent/stream",
            json={"message": message, "thread_id": thread_id},
        ) as response:
            assert response.status_code == 200
            events = _events(response.read().decode("utf-8"))
        self.turn_prompts.append(
            str(self.streamer.requests[first_request]["messages"][0]["content"])
        )
        return events

    @property
    def first_prompt(self) -> str:
        """The system prompt the model saw on the first turn."""
        return self.turn_prompts[0]

    @property
    def last_turn_prompt(self) -> str:
        """The system prompt the model saw on the most recent turn."""
        return self.turn_prompts[-1]


@pytest.fixture(autouse=True)
def _migrated_store(migrated_db: Any) -> Any:
    """The stream route needs the migrated per-test SQLite store."""
    return migrated_db


@pytest.mark.parametrize("turn_two", ["single_pass", "stubborn_jitter"])
def test_follow_up_turn_reuses_the_session_baseline(settings: Settings, turn_two: str) -> None:
    """AC3: Turn 2 injects the block and performs zero coach-mcp calls."""
    responses = [
        *_turn_one_responses(),
        *(_single_pass_turn_two() if turn_two == "single_pass" else _stubborn_turn_two()),
    ]
    harness = _Harness(
        settings,
        responses,
        {
            WELLNESS_TOOL: _WELLNESS_PAYLOAD,
            TRENDS_TOOL: _TRENDS_PAYLOAD,
            ACTIVITIES_TOOL: _ACTIVITIES_PAYLOAD,
        },
    )
    thread_id = harness.thread_id()

    harness.stream("How am I doing this morning?", thread_id)
    baseline_calls = list(harness.hub.calls)
    assert {name for name, _arguments in baseline_calls} == {
        WELLNESS_TOOL,
        TRENDS_TOOL,
        ACTIVITIES_TOOL,
    }
    assert SESSION_METRICS_BLOCK_BEGIN not in harness.first_prompt
    snapshot = harness.ledger.snapshot(DEFAULT_ATHLETE_ID, thread_id)
    assert snapshot is not None
    assert set(snapshot) == {"readiness_wellness", "fitness_summary", "recent_activities"}

    second = harness.stream("Should we adjust today's session then?", thread_id)

    # (a) the fenced block reaches the follow-up prompt at slot 5.
    prompt = harness.last_turn_prompt
    assert SESSION_METRICS_BLOCK_BEGIN in prompt
    assert SESSION_METRICS_BLOCK_END in prompt
    assert prompt.index(SESSION_METRICS_BLOCK_BEGIN) < prompt.index("=== BINDING RULES REASSERTED")
    # (b) zero coach-mcp executions on Turn 2 — jittered duplicate included.
    assert harness.hub.calls == baseline_calls
    # (c) identical baseline numbers are preserved across turns.
    results = [
        json.loads(data["result"]) for event_type, data in second if event_type == "tool_result"
    ]
    if turn_two == "stubborn_jitter":
        assert results and results[0]["reused_session_context"] is True
        assert results[0]["resting_hr"] == 44
        assert results[0]["hrv_rmssd"] == 68
    assert any(event_type == "done" for event_type, _data in second)


def test_fresh_sync_signal_invalidates_and_refreshes_the_baseline(
    settings: Settings,
) -> None:
    """AC4: a sync cue evicts, omits the stale block, re-fetches and updates."""
    results = {
        WELLNESS_TOOL: _WELLNESS_PAYLOAD,
        TRENDS_TOOL: _TRENDS_PAYLOAD,
        ACTIVITIES_TOOL: _ACTIVITIES_PAYLOAD,
    }
    harness = _Harness(
        settings,
        [
            *_turn_one_responses(),
            *_single_pass_turn_two(),
            [_tool_call_chunk(call_id="w3", name=WELLNESS_TOOL, arguments={})],
            [_chunk(content="Your new HRV is 61.", finish_reason="stop")],
        ],
        results,
    )
    thread_id = harness.thread_id()

    harness.stream("How am I doing this morning?", thread_id)
    harness.stream("Should we adjust today's session then?", thread_id)
    calls_before_sync = list(harness.hub.calls)
    results[WELLNESS_TOOL] = json.dumps({"resting_hr": 41, "hrv_rmssd": 61})

    third = harness.stream("I just finished my ride and logged my morning HRV", thread_id)

    # The stale block is omitted from the post-invalidation prompt.
    prompt = harness.last_turn_prompt
    assert SESSION_METRICS_BLOCK_BEGIN not in prompt
    # Fresh coach-mcp reads executed despite the earlier baseline.
    assert len(harness.hub.calls) == len(calls_before_sync) + 1
    assert harness.hub.calls[-1][0] == WELLNESS_TOOL
    result = json.loads(
        [data["result"] for event_type, data in third if event_type == "tool_result"][0]
    )
    assert result["hrv_rmssd"] == 61
    # The session baseline was refreshed with the new metrics.
    snapshot = harness.ledger.snapshot(DEFAULT_ATHLETE_ID, thread_id)
    assert snapshot is not None
    assert set(snapshot) == {"readiness_wellness"}
    assert json.loads(snapshot["readiness_wellness"].payload)["hrv_rmssd"] == 61


def test_persistence_degrade_runs_statelessly_without_ledger_access(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5b: ``turn_thread is None`` degrades to the cold core prompt."""

    def _unavailable(_settings: Settings) -> Any:
        raise EncryptionUnavailableError(EncryptionReason.KEY_UNAVAILABLE, "missing key")

    monkeypatch.setattr("coach_web.app.build_debrief_cipher", _unavailable)
    harness = _Harness(
        settings,
        [[_chunk(content="Stateless reply.", finish_reason="stop")]],
        {},
    )

    events = harness.stream("How am I doing this morning?", 1)

    assert any(event_type == "warning" for event_type, _data in events)
    prompt = harness.last_turn_prompt
    assert SESSION_METRICS_BLOCK_BEGIN not in prompt
    assert "BINDING RULES REASSERTED" not in prompt
    assert harness.agents[0].session_metrics_context is None
    assert harness.ledger.snapshot(DEFAULT_ATHLETE_ID, 1) is None
