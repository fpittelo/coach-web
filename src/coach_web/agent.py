"""OpenRouter-powered agent loop with tool calling for Coach Web.

The agent streams chat completions from OpenRouter, autonomously invokes the
MCP tools exposed by :class:`~coach_web.mcp_hub.MCPClientHub`, feeds the results
back to the model, and finally emits a structured plan proposal for approval.

Every step is surfaced to the browser as a typed :class:`AgentEvent` which the
SSE endpoint serialises verbatim.
"""

import json
import logging
from collections.abc import AsyncIterator
from datetime import date
from types import TracebackType
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from coach_web.analysis import (
    BUDGET_EXHAUSTED_MESSAGE,
    NAMESPACE_SEPARATOR,
    CompletedSessionsPageMirror,
    ComplianceReportMirror,
    IcuReadBudget,
    build_debrief_summary_card,
    icu_tool_get_cost,
    parse_completed_sessions,
    parse_compliance_report,
)
from coach_web.config import Settings
from coach_web.debrief import RpeRequest
from coach_web.mcp_hub import MCPClientHub
from coach_web.microcycle import WeeklyPlanDraft
from coach_web.models import ChatMessage, PlanProposal
from coach_web.periodization import PeriodizationProposal

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "anthropic/claude-sonnet-4.5"
"""Model routed through OpenRouter when no override is configured."""

OPENROUTER_CHAT_COMPLETIONS_PATH = "/chat/completions"
"""Relative path of the OpenRouter chat-completions endpoint."""

PROPOSE_PLAN_TOOL = "propose_plan"
"""Name of the synthetic tool the model calls to submit a structured plan."""

PROPOSE_WEEK_PLAN_TOOL = "propose_week_plan"
"""Name of the synthetic tool the model calls to submit a weekly microcycle (#168)."""

PROPOSE_PERIODIZATION_TOOL = "propose_periodization"
"""Name of the synthetic tool the model calls to submit a season phase plan (#182)."""

REQUEST_RPE_TOOL = "request_rpe"
"""Name of the synthetic tool the model calls to open a session check-in (#189)."""

ICU_COMPLIANCE_TOOL = "icu_get_workout_compliance"
"""Raw coach-mcp tool whose result drives the debrief summary card (#190)."""

ICU_COMPLETED_SESSIONS_TOOL = "icu_get_completed_sessions"
"""Raw coach-mcp tool whose result joins executed load into the card (#190)."""

PLAN_VALIDATION_UI_MESSAGE = (
    "Invalid plan proposal — the coach will retry with corrected arguments."
)
"""Generic UI-facing validation failure message (nLPD: no plan data echoed)."""

WEEK_PLAN_VALIDATION_UI_MESSAGE = (
    "Invalid weekly plan — the coach will retry with corrected arguments."
)
"""Generic UI-facing weekly-plan validation failure message (nLPD posture)."""

PERIODIZATION_VALIDATION_UI_MESSAGE = (
    "Invalid periodization plan — the coach will retry with corrected arguments."
)
"""Generic UI-facing periodization validation failure message (nLPD posture)."""

RPE_REQUEST_VALIDATION_UI_MESSAGE = (
    "Invalid check-in request — the coach will retry with corrected arguments."
)
"""Generic UI-facing RPE-request validation failure message (nLPD posture, #189)."""

AgentEventType = Literal[
    "status",
    "thought",
    "token",
    "tool_call",
    "tool_start",
    "tool_result",
    "plan",
    "plan_proposal",
    "week_plan",
    "periodization_plan",
    "rpe_request",
    "debrief_summary",
    "done",
    "error",
]
"""Typed SSE event categories emitted by the agent loop."""

DEFAULT_SYSTEM_PROMPT = (
    "You are Coach Web, an expert endurance cycling coach embedded in Frederic's "
    "personal training cockpit. Always ground your advice in the athlete's real data "
    "by calling the available tools (Intervals.icu readiness, fitness and calendar; "
    "GitHub training-plan issues) before making recommendations. Explain your "
    "reasoning concisely and use explicit wattage and durations. When you are ready "
    f"to prescribe a workout block, call the `{PROPOSE_PLAN_TOOL}` tool so the athlete "
    "can review and approve the structured plan, then summarise it in your answer. "
    "When the athlete asks for a full training week — or a weekly microcycle is the "
    f"right deliverable — call the `{PROPOSE_WEEK_PLAN_TOOL}` tool with the complete "
    "7-day plan grounded in the athlete's objectives, current phase and weekly "
    "availability, then summarise it in your answer. "
    "When the athlete asks for a mid-week adjustment of the current week — or their "
    f"week falls apart — call the `{PROPOSE_WEEK_PLAN_TOOL}` tool again for the "
    "current week: keep the already-approved past days exactly as they are, propose "
    "replacements only for future days, and state the adjustment rationale. "
    "When the athlete asks you to structure their season — or a macrocycle phase "
    f"plan is the right deliverable — call the `{PROPOSE_PERIODIZATION_TOOL}` tool "
    "with the complete phase sequence grounded in the objective's target date and "
    "weekly availability, then summarise it in your answer. "
    "When the athlete has completed a planned session — or the pending-debrief "
    f"context lists one — open the debrief by calling the `{REQUEST_RPE_TOOL}` tool "
    "with the session's ISO date and title, asking how the session went; the "
    "athlete answers through the check-in card, then you respond to their RPE. "
    "When the athlete asks how their training went — or a week review is the "
    f"right deliverable — call the `{ICU_COMPLIANCE_TOOL}` tool first (adding "
    f"`{ICU_COMPLETED_SESSIONS_TOOL}` when the executed load per session helps) "
    "and ground every claim in the fetched sessions; never invent numbers. ICU "
    "reads are budgeted to four GETs per message, so fetch once and analyse — "
    "the debrief summary card is rendered automatically from the fetched "
    "compliance report."
)

TEMPORAL_ANCHOR_FORMAT = "Today is {date} ({weekday})."
"""The stable, parseable anchor line: ISO date + weekday name (#220)."""


def apply_temporal_anchor(system_prompt: str, today: date) -> str:
    """Append the server-side temporal anchor line to a base system prompt (#220).

    The model is never told today's date by any digest — it would otherwise
    infer the date from tool payloads and anchor on the wrong time of year.
    The caller (the request scope in :mod:`coach_web.app`) owns the clock
    boundary (N3, PR #173): ``today`` is injected, never re-read here, so the
    anchor shares the digests' single per-request resolution.
    """
    anchor = TEMPORAL_ANCHOR_FORMAT.format(date=today.isoformat(), weekday=today.strftime("%A"))
    return f"{system_prompt}\n\n{anchor}"


class OpenRouterError(Exception):
    """Raised when the OpenRouter API cannot be reached or returns an error."""


class AgentEvent(BaseModel):
    """A single typed packet streamed to the browser over SSE."""

    type: AgentEventType = Field(..., description="Event category")
    data: dict[str, Any] = Field(default_factory=dict, description="Event payload")


class ChatCompletionStreamer(Protocol):
    """Structural contract for a streaming chat-completion backend."""

    model: str

    def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed streaming chunks for the supplied conversation."""
        ...

    async def close(self) -> None:
        """Release the underlying HTTP resources."""
        ...


class ToolExecutor(Protocol):
    """Structural contract for the MCP tool hub used by the agent."""

    def list_all_tools(self) -> list[dict[str, Any]]:
        """Return every tool as an OpenAI/OpenRouter function schema."""
        ...

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Execute a tool and return its JSON string payload."""
        ...

    async def connect(self) -> None:
        """Open every upstream MCP session."""
        ...

    async def close(self) -> None:
        """Close every upstream MCP session."""
        ...


class OpenRouterClient:
    """Async OpenRouter client streaming chat completions with tool calling."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        model: str = DEFAULT_OPENROUTER_MODEL,
        timeout: float = 60.0,
        referer: str = "https://github.com/fpittelo/coach-web",
        app_title: str = "Coach Web",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client around a dedicated ``httpx.AsyncClient``."""
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.referer = referer
        self.app_title = app_title
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
        )

    async def stream_chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream a chat completion, yielding parsed OpenRouter chunks.

        Raises:
            OpenRouterError: if the request fails or OpenRouter returns non-2xx.
        """
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        try:
            async with self._client.stream(
                "POST",
                OPENROUTER_CHAT_COMPLETIONS_PATH,
                json=payload,
                headers=self._headers(),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise OpenRouterError(
                        f"OpenRouter returned HTTP {response.status_code}: {body}"
                    )
                async for line in response.aiter_lines():
                    chunk = self._parse_sse_line(line)
                    if chunk is not None:
                        yield chunk
        except httpx.HTTPError as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc}") from exc

    def _headers(self) -> dict[str, str]:
        """Build the attribution headers sent to OpenRouter."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": self.referer,
            "X-Title": self.app_title,
        }

    @staticmethod
    def _parse_sse_line(line: str) -> dict[str, Any] | None:
        """Parse a single SSE ``data:`` line into a JSON object when possible."""
        stripped = line.strip()
        if not stripped.startswith("data:"):
            return None
        payload = stripped[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return None
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return parsed
        return None

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        """Return the client for async context-manager usage."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close the HTTP client on context exit."""
        await self.close()


class _WeekAnalysisCollector:
    """Per-run collector turning ICU read results into the summary card (#190).

    One instance lives for one agent run (one user action). The compliance
    report and the completed-sessions page are held as they arrive; every
    arrival that leaves a parsed report in hand re-emits the card — the #165
    replacement rule makes the re-emission safe (the newest card wins). A
    failed or malformed read parses to ``None`` and can never produce a
    card, so no fabricated data ever reaches the UI (AC1).
    """

    def __init__(self) -> None:
        """Start an empty collection for one run."""
        self.report: ComplianceReportMirror | None = None
        self.page: CompletedSessionsPageMirror | None = None

    def card_events(self, tool_name: str, result: str) -> list[AgentEvent]:
        """Update the held state from *result* and return any card events.

        Only the two analysis tools are observed; every other tool result
        leaves the collector untouched. The card is built SERVER-SIDE from
        the parsed upstream whitelist payload — never from agent-claimed
        numbers.
        """
        raw_name = tool_name.rsplit(NAMESPACE_SEPARATOR, 1)[-1]
        if raw_name == ICU_COMPLIANCE_TOOL:
            self.report = parse_compliance_report(result)
        elif raw_name == ICU_COMPLETED_SESSIONS_TOOL:
            self.page = parse_completed_sessions(result)
        else:
            return []
        if self.report is None:
            return []
        card = build_debrief_summary_card(self.report, self.page)
        return [AgentEvent(type="debrief_summary", data={"summary": card.model_dump()})]


class CoachAgent:
    """Autonomous agent loop bridging OpenRouter and the dual MCP hub."""

    def __init__(
        self,
        client: ChatCompletionStreamer,
        hub: ToolExecutor,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tool_iterations: int = 8,
    ) -> None:
        """Store the streaming backend, the tool hub and the loop limits."""
        self.client = client
        self.hub = hub
        self.system_prompt = system_prompt
        self.max_tool_iterations = max_tool_iterations

    async def run(
        self,
        message: str,
        *,
        history: list[ChatMessage] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the agent loop for one user turn, yielding typed events.

        The loop streams the model output token by token, executes every
        requested tool through the MCP hub, feeds the results back, and stops on
        the first assistant message without tool calls or when the iteration cap
        is reached.
        """
        messages = self._build_messages(message, history)
        tools = [
            *self.hub.list_all_tools(),
            plan_tool_schema(),
            week_plan_tool_schema(),
            periodization_tool_schema(),
            rpe_request_tool_schema(),
        ]
        # Per-user-action ICU GET budget (PO decision 5a, #190 AC2) and the
        # analysis collector that turns the fetched compliance data into the
        # debrief summary card — both live and die with this single run.
        budget = IcuReadBudget()
        collector = _WeekAnalysisCollector()

        for iteration in range(1, self.max_tool_iterations + 1):
            yield AgentEvent(type="status", data={"phase": "thinking", "iteration": iteration})

            content_parts: list[str] = []
            tool_calls: dict[int, dict[str, str]] = {}
            try:
                async for chunk in self.client.stream_chat_completion(messages, tools=tools):
                    choice = _first_choice(chunk)
                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        continue

                    reasoning = delta.get("reasoning")
                    if isinstance(reasoning, str) and reasoning:
                        yield AgentEvent(type="thought", data={"text": reasoning})

                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        content_parts.append(content)
                        yield AgentEvent(type="token", data={"text": content})

                    for delta_call in delta.get("tool_calls") or []:
                        if isinstance(delta_call, dict):
                            _accumulate_tool_call(tool_calls, delta_call)
            except OpenRouterError as exc:
                yield AgentEvent(type="error", data={"message": str(exc)})
                yield AgentEvent(
                    type="done",
                    data={"message": "".join(content_parts), "iterations": iteration},
                )
                return

            assembled = [tool_calls[index] for index in sorted(tool_calls)]
            if not assembled:
                yield AgentEvent(
                    type="done",
                    data={"message": "".join(content_parts), "iterations": iteration},
                )
                return

            messages.append(_assistant_tool_message("".join(content_parts), assembled))
            for call in assembled:
                arguments = _parse_arguments(call["arguments"])
                yield AgentEvent(
                    type="tool_call",
                    data={"id": call["id"], "name": call["name"], "arguments": arguments},
                )
                yield AgentEvent(type="tool_start", data={"id": call["id"], "name": call["name"]})
                if call["name"] == PROPOSE_PLAN_TOOL:
                    result, plan_events = self._handle_plan_proposal(arguments)
                    for event in plan_events:
                        yield event
                elif call["name"] == PROPOSE_WEEK_PLAN_TOOL:
                    result, week_events = self._handle_week_plan_proposal(arguments)
                    for event in week_events:
                        yield event
                elif call["name"] == PROPOSE_PERIODIZATION_TOOL:
                    result, periodization_events = self._handle_periodization_proposal(arguments)
                    for event in periodization_events:
                        yield event
                elif call["name"] == REQUEST_RPE_TOOL:
                    result, rpe_events = self._handle_rpe_request(arguments)
                    for event in rpe_events:
                        yield event
                else:
                    cost = icu_tool_get_cost(call["name"])
                    if cost is not None and budget.would_exceed(cost):
                        # Budget refusal (PO decision 5a): the call is NOT
                        # executed — the model is told to summarise from the
                        # data already fetched. The message carries counts
                        # and the tool name only (nLPD).
                        result = json.dumps(
                            {
                                "error": BUDGET_EXHAUSTED_MESSAGE.format(
                                    used=budget.used,
                                    budget=budget.budget,
                                    tool=call["name"],
                                    cost=cost,
                                )
                            }
                        )
                    else:
                        if cost is not None:
                            budget.consume(cost)
                        result = await self.hub.execute_tool(call["name"], arguments)
                        for event in collector.card_events(call["name"], result):
                            yield event
                yield AgentEvent(
                    type="tool_result",
                    data={"id": call["id"], "name": call["name"], "result": result},
                )
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})

        yield AgentEvent(
            type="error",
            data={"message": f"Exceeded maximum of {self.max_tool_iterations} tool iterations"},
        )
        yield AgentEvent(
            type="done",
            data={"message": "", "iterations": self.max_tool_iterations},
        )

    def _build_messages(
        self,
        message: str,
        history: list[ChatMessage] | None,
    ) -> list[dict[str, Any]]:
        """Assemble the system, historical and current user messages."""
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        for item in history or []:
            messages.append({"role": item.role, "content": item.content})
        messages.append({"role": "user", "content": message})
        return messages

    @staticmethod
    def _handle_plan_proposal(arguments: dict[str, Any]) -> tuple[str, list[AgentEvent]]:
        """Validate a plan proposal and build its approval events.

        A payload nested under a single ``params`` key (a shape some models
        emit) is unwrapped before validation. On failure the model receives
        field-level detail without the rejected values, the UI only receives a
        generic error message, and the full validation error is logged
        server-side (nLPD: plan data never reaches browser-facing surfaces).
        """
        candidate = arguments
        if set(arguments) == {"params"} and isinstance(arguments["params"], dict):
            candidate = arguments["params"]
        try:
            plan = PlanProposal.model_validate(candidate)
        except ValidationError as exc:
            logger.warning("Rejected plan proposal arguments: %s", exc.errors())
            # nLPD: project the model-facing detail down to loc/type only.
            # ``include_input=False`` strips ``input`` but NOT ``msg``/``ctx``,
            # and custom validators embed the rejected value in their message
            # on the semantic branch. Enforcing value-freeness centrally at the
            # emission point protects against ALL current and future validators;
            # the model still learns WHICH field is wrong (``loc``) and can
            # self-correct (formats are documented in the tool schema).
            detail = [
                {"loc": err.get("loc"), "type": err.get("type")}
                for err in exc.errors(include_url=False, include_input=False)
            ]
            return json.dumps({"error": f"Invalid plan proposal: {detail}"}), [
                AgentEvent(
                    type="error",
                    data={"message": PLAN_VALIDATION_UI_MESSAGE},
                )
            ]

        payload = plan.model_dump()
        events = [
            AgentEvent(type="plan", data={"plan": payload}),
            AgentEvent(type="plan_proposal", data={"plan": payload}),
        ]
        result = json.dumps({"status": "plan_proposal_emitted", "title": plan.title})
        return result, events

    @staticmethod
    def _handle_week_plan_proposal(arguments: dict[str, Any]) -> tuple[str, list[AgentEvent]]:
        """Validate a weekly plan draft and build its ``week_plan`` event.

        Mirrors the plan-proposal posture: a ``params``-nested payload is
        unwrapped, the draft is validated server-side (summary counts are
        re-verified — never trust agent-computed totals, #165), the model
        receives field-level detail without the rejected values, and the UI
        only receives a generic error message (nLPD).
        """
        candidate = arguments
        if set(arguments) == {"params"} and isinstance(arguments["params"], dict):
            candidate = arguments["params"]
        try:
            draft = WeeklyPlanDraft.model_validate(candidate)
        except ValidationError as exc:
            logger.warning("Rejected weekly plan arguments: %s", exc.errors())
            detail = [
                _value_free_detail(err)
                for err in exc.errors(include_url=False, include_input=False)
            ]
            return json.dumps({"error": f"Invalid weekly plan: {detail}"}), [
                AgentEvent(
                    type="error",
                    data={"message": WEEK_PLAN_VALIDATION_UI_MESSAGE},
                ),
            ]

        payload = draft.model_dump()
        events = [AgentEvent(type="week_plan", data={"draft": payload})]
        result = json.dumps({"status": "week_plan_emitted", "week_id": draft.week_id})
        return result, events

    @staticmethod
    def _handle_periodization_proposal(arguments: dict[str, Any]) -> tuple[str, list[AgentEvent]]:
        """Validate a periodization proposal and build its ``periodization_plan`` event.

        Mirrors the week-plan posture (#182): a ``params``-nested payload is
        unwrapped, the proposal is validated server-side through the #167
        domain models (per-phase date order, plan-level monotonic
        non-overlap — never trust agent numbers), the model receives
        field-level detail without the rejected values, and the UI only
        receives a generic error message (nLPD). Coverage of the objective's
        target date is cross-checked at the approval endpoint, where the
        persisted objective row is authoritative.
        """
        candidate = arguments
        if set(arguments) == {"params"} and isinstance(arguments["params"], dict):
            candidate = arguments["params"]
        try:
            proposal = PeriodizationProposal.model_validate(candidate)
        except ValidationError as exc:
            logger.warning("Rejected periodization proposal arguments: %s", exc.errors())
            detail = [
                _value_free_detail(err)
                for err in exc.errors(include_url=False, include_input=False)
            ]
            return json.dumps({"error": f"Invalid periodization proposal: {detail}"}), [
                AgentEvent(
                    type="error",
                    data={"message": PERIODIZATION_VALIDATION_UI_MESSAGE},
                ),
            ]

        events = [
            AgentEvent(
                type="periodization_plan",
                data={
                    "plan": {"phases": [phase.model_dump() for phase in proposal.phases]},
                    "rationale": proposal.rationale,
                },
            )
        ]
        result = json.dumps(
            {
                "status": "periodization_plan_emitted",
                "phases": len(proposal.phases),
            }
        )
        return result, events

    @staticmethod
    def _handle_rpe_request(arguments: dict[str, Any]) -> tuple[str, list[AgentEvent]]:
        """Validate an RPE request and build its ``rpe_request`` event (#189).

        Mirrors the periodization posture: a ``params``-nested payload is
        unwrapped, the request is validated server-side through the #189
        domain model (the session key must be a real ISO date — never trust
        agent numbers), the model receives field-level detail without the
        rejected values, and the UI only receives a generic error message
        (nLPD). The payload carries session METADATA only (key + title) —
        debrief notes and RPE values never travel through the agent loop.
        """
        candidate = arguments
        if set(arguments) == {"params"} and isinstance(arguments["params"], dict):
            candidate = arguments["params"]
        try:
            request = RpeRequest.model_validate(candidate)
        except ValidationError as exc:
            logger.warning("Rejected RPE request arguments: %s", exc.errors())
            detail = [
                _value_free_detail(err)
                for err in exc.errors(include_url=False, include_input=False)
            ]
            return json.dumps({"error": f"Invalid RPE request: {detail}"}), [
                AgentEvent(
                    type="error",
                    data={"message": RPE_REQUEST_VALIDATION_UI_MESSAGE},
                ),
            ]

        events = [
            AgentEvent(type="rpe_request", data={"checkin": request.model_dump()}),
        ]
        result = json.dumps(
            {
                "status": "rpe_request_emitted",
                "plan_session_key": request.plan_session_key,
            }
        )
        return result, events

    async def aclose(self) -> None:
        """Close the tool hub and the streaming backend."""
        await self.hub.close()
        await self.client.close()

    async def __aenter__(self) -> "CoachAgent":
        """Connect the tool hub, tearing down on failure."""
        try:
            await self.hub.connect()
        except Exception:
            await self.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Close every resource on context exit."""
        await self.aclose()


def _value_free_detail(err: Any) -> dict[str, Any]:
    """Project one validation error down to a value-free model-facing detail.

    Field-level errors keep the ``loc``/``type`` projection (their messages
    may embed rejected values — nLPD). Model-level invariant errors (empty
    ``loc``, e.g. the summary-count or monotonicity checks) carry a static,
    value-free rule description in ``msg`` — included so the model can
    self-correct.
    """
    entry: dict[str, Any] = {"loc": err.get("loc"), "type": err.get("type")}
    if not err.get("loc"):
        entry["msg"] = err.get("msg")
    return entry


def plan_tool_schema() -> dict[str, Any]:
    """Return the synthetic ``propose_plan`` tool schema for the model."""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_PLAN_TOOL,
            "description": (
                "Propose a structured workout plan for the athlete to approve "
                "before it is scheduled. Pass every plan field (title, week_id, "
                "summary, steps, ...) as top-level tool arguments; never nest "
                "them under a params key."
            ),
            "parameters": PlanProposal.model_json_schema(),
        },
    }


def week_plan_tool_schema() -> dict[str, Any]:
    """Return the synthetic ``propose_week_plan`` tool schema for the model (#168)."""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_WEEK_PLAN_TOOL,
            "description": (
                "Propose a complete 7-day weekly microcycle for the athlete to "
                "review and approve as one weekly card. Pass every field (week_id, "
                "title, summary, days, total_tss, hard_days, easy_days, ...) as "
                "top-level tool arguments; never nest them under a params key. "
                "Each day carries an ISO date inside week_id's ISO week and a "
                "matching day_of_week; total_tss must equal the sum of the days' "
                "planned_tss and hard_days + easy_days + rest days must equal the "
                "day count."
            ),
            "parameters": WeeklyPlanDraft.model_json_schema(),
        },
    }


def periodization_tool_schema() -> dict[str, Any]:
    """Return the synthetic ``propose_periodization`` tool schema for the model (#182)."""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_PERIODIZATION_TOOL,
            "description": (
                "Propose a season phase plan (macrocycle periodization) for the "
                "athlete to review and approve as one card. Pass every field "
                "(phases, rationale) as top-level tool arguments; never nest "
                "them under a params key. Each phase carries a phase_type "
                "(base|build|peak|taper|recovery|competition), a name and ISO "
                "start/end dates (start_date must not be after end_date); the "
                "phases must be monotonic and non-overlapping — a phase starts "
                "strictly after the previous one ends (contiguous encoding: "
                "next.start = prev.end + 1, shared days are rejected) — and the "
                "sequence must cover the window to the objective's target date."
            ),
            "parameters": PeriodizationProposal.model_json_schema(),
        },
    }


def rpe_request_tool_schema() -> dict[str, Any]:
    """Return the synthetic ``request_rpe`` tool schema for the model (#189)."""
    return {
        "type": "function",
        "function": {
            "name": REQUEST_RPE_TOOL,
            "description": (
                "Open a post-session check-in (debrief) for one completed planned "
                "session: the athlete rates the session's perceived exertion "
                "through the check-in card. Pass every field (plan_session_key, "
                "session_title) as top-level tool arguments; never nest them "
                "under a params key. plan_session_key is the session's ISO date "
                "(YYYY-MM-DD) from the pending-debrief context or the "
                "conversation; session_title is the planned session's title."
            ),
            "parameters": RpeRequest.model_json_schema(),
        },
    }


def create_agent(settings: Settings) -> CoachAgent:
    """Build a fully wired agent from application settings."""
    hub = MCPClientHub.from_settings(settings)
    client = OpenRouterClient(
        settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
        model=settings.OPENROUTER_MODEL,
        timeout=settings.OPENROUTER_TIMEOUT_SECONDS,
        referer=settings.OPENROUTER_REFERER,
        app_title=settings.OPENROUTER_APP_TITLE,
    )
    return CoachAgent(client, hub, max_tool_iterations=settings.AGENT_MAX_TOOL_ITERATIONS)


def _first_choice(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return the first choice of a streaming chunk, or an empty mapping."""
    choices = chunk.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            return first
    return {}


def _accumulate_tool_call(
    accumulator: dict[int, dict[str, str]],
    delta: dict[str, Any],
) -> None:
    """Merge a streamed tool-call fragment into the accumulator."""
    index = delta.get("index")
    if not isinstance(index, int):
        index = 0
    entry = accumulator.setdefault(index, {"id": "", "name": "", "arguments": ""})

    call_id = delta.get("id")
    if isinstance(call_id, str) and call_id:
        entry["id"] = call_id

    function = delta.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name:
            entry["name"] += name
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            entry["arguments"] += arguments


def _parse_arguments(raw: str) -> dict[str, Any]:
    """Parse streamed tool-call arguments into a mapping, tolerating junk."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _assistant_tool_message(
    content: str,
    assembled: list[dict[str, str]],
) -> dict[str, Any]:
    """Build the assistant message that carries the requested tool calls."""
    return {
        "role": "assistant",
        "content": content or None,
        "tool_calls": [
            {
                "id": call["id"],
                "type": "function",
                "function": {"name": call["name"], "arguments": call["arguments"]},
            }
            for call in assembled
        ],
    }
