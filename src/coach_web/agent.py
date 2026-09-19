"""OpenRouter-powered agent loop with tool calling for Coach Web.

The agent streams chat completions from OpenRouter, autonomously invokes the
MCP tools exposed by :class:`~coach_web.mcp_hub.MCPClientHub`, feeds the results
back to the model, and finally emits a structured plan proposal for approval.

Every step is surfaced to the browser as a typed :class:`AgentEvent` which the
SSE endpoint serialises verbatim.
"""

import json
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from coach_web.config import Settings
from coach_web.mcp_hub import MCPClientHub
from coach_web.models import ChatMessage, PlanProposal

DEFAULT_OPENROUTER_MODEL = "anthropic/claude-3.5-sonnet"
"""Model routed through OpenRouter when no override is configured."""

OPENROUTER_CHAT_COMPLETIONS_PATH = "/chat/completions"
"""Relative path of the OpenRouter chat-completions endpoint."""

PROPOSE_PLAN_TOOL = "propose_plan"
"""Name of the synthetic tool the model calls to submit a structured plan."""

AgentEventType = Literal[
    "status",
    "thought",
    "token",
    "tool_call",
    "tool_start",
    "tool_result",
    "plan",
    "plan_proposal",
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
    "can review and approve the structured plan, then summarise it in your answer."
)


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
        tools = [*self.hub.list_all_tools(), plan_tool_schema()]

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
                else:
                    result = await self.hub.execute_tool(call["name"], arguments)
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

        Invalid proposals never reach the UI: the validation error is returned
        to the model so it can correct itself on the next iteration.
        """
        try:
            plan = PlanProposal.model_validate(arguments)
        except ValidationError as exc:
            message = f"Invalid plan proposal: {exc.errors()}"
            return json.dumps({"error": message}), [
                AgentEvent(type="error", data={"message": message})
            ]

        payload = plan.model_dump()
        events = [
            AgentEvent(type="plan", data={"plan": payload}),
            AgentEvent(type="plan_proposal", data={"plan": payload}),
        ]
        result = json.dumps({"status": "plan_proposal_emitted", "title": plan.title})
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


def plan_tool_schema() -> dict[str, Any]:
    """Return the synthetic ``propose_plan`` tool schema for the model."""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_PLAN_TOOL,
            "description": (
                "Propose a structured workout plan for the athlete to approve "
                "before it is scheduled."
            ),
            "parameters": PlanProposal.model_json_schema(),
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
