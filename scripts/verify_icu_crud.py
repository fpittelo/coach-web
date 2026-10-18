"""Intervals.icu CRUD release gate — full event lifecycle via the coach-mcp sidecar (#311).

Proves the sidecar's event write path against the real Intervals.icu account by
driving the sidecar's MCP tools (never direct ICU API calls) through the
lifecycle CREATE → READ → UPDATE → READ → DELETE → ABSENT, printing one
evidence line per step and exiting 0 only when every step passes.

Safety (issue #311 AC2):

* the test event is unmistakably marked (title prefix
  ``COACH-WEB RELEASE GATE — ``) and future-dated (+7 days);
* cleanup is guaranteed on success AND failure — the delete is attempted in a
  ``finally`` block even when a mid-lifecycle step fails;
* after the delete, absence is verified by listing the event's date window;
  an orphan test event fails the gate loudly.

The script holds no secrets: the ``INTERVALS_API_KEY`` lives in the sidecar's
environment and is never logged or echoed here.

Usage (inside the lane's compose network — the sidecar has no host port):

    docker run --rm --network coach-web-dev_coach-net \
        -v "$PWD:/workspace:ro" --entrypoint python \
        ghcr.io/fpittelo/coach-web:dev \
        /workspace/scripts/verify_icu_crud.py --mcp-url http://coach-mcp:8000/sse
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Protocol

from coach_web.mcp_client import MCPClient, MCPConnectionError

DEFAULT_MCP_URL = "http://coach-mcp:8000/sse"
"""Compose-internal URL of the lane's coach-mcp sidecar (no host port)."""

GATE_TITLE_PREFIX = "COACH-WEB RELEASE GATE — "
"""Unmistakable title prefix marking the test event (issue #311 AC2)."""

GATE_UPDATE_SUFFIX = " [UPDATED]"
"""Suffix applied by the UPDATE step so the change is visible on read-back."""

GATE_DAYS_AHEAD = 7
"""The test event is future-dated this many days from today."""

GATE_START_TIME = "08:00:00"
"""Fixed local start time of the test event."""


class SidecarError(Exception):
    """Raised when a sidecar tool call fails or returns an unusable payload."""


class Sidecar(Protocol):
    """Sidecar surface the gate drives (seam for tests).

    Contract: every method returns the sidecar's tool text on success and
    raises :class:`SidecarError` on any tool failure — the same conversion the
    real MCP adapter performs.
    """

    async def create_event(self, params: dict[str, Any]) -> str: ...

    async def get_event(self, event_id: str) -> str: ...

    async def list_events(self, oldest: str, newest: str) -> str: ...

    async def update_event(self, event_id: str, params: dict[str, Any]) -> str: ...

    async def delete_event(self, event_id: str) -> str: ...


@dataclass(frozen=True)
class GatePlan:
    """Deterministic inputs for one gate run."""

    title: str
    updated_title: str
    event_date: str
    start_date_local: str
    list_oldest: str
    list_newest: str


@dataclass(frozen=True)
class GateResult:
    """Outcome of one gate run: overall verdict plus per-step evidence lines."""

    passed: bool
    lines: list[str]


def format_evidence(step: str, ok: bool, detail: str) -> str:
    """Render one evidence line: 'PASS <step>: <detail>' or 'FAIL <step>: <detail>'."""
    verdict = "PASS" if ok else "FAIL"
    return f"{verdict} {step}: {detail}"


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the JSON object embedded in a sidecar response."""
    brace = text.find("{")
    if brace == -1:
        raise SidecarError(f"no JSON payload in sidecar response: {text[:200]}")
    try:
        payload: Any = json.loads(text[brace:])
    except json.JSONDecodeError as exc:
        raise SidecarError(f"unparseable JSON in sidecar response: {text[:200]}") from exc
    if not isinstance(payload, dict):
        raise SidecarError(f"expected a JSON object in sidecar response: {text[:200]}")
    return payload


def parse_event_list(text: str) -> list[dict[str, Any]]:
    """Parse the list tool's JSON array response into row dicts."""
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SidecarError(f"unparseable JSON in sidecar response: {text[:200]}") from exc
    if not isinstance(payload, list):
        raise SidecarError(f"expected a JSON array in sidecar response: {text[:200]}")
    return [row for row in payload if isinstance(row, dict)]


def build_plan(today: date) -> GatePlan:
    """Derive the gate plan: marked title, +7 days future date, ±1 day list window."""
    event_day = today + timedelta(days=GATE_DAYS_AHEAD)
    title = f"{GATE_TITLE_PREFIX}{event_day.isoformat()}"
    return GatePlan(
        title=title,
        updated_title=f"{title}{GATE_UPDATE_SUFFIX}",
        event_date=event_day.isoformat(),
        start_date_local=f"{event_day.isoformat()}T{GATE_START_TIME}",
        list_oldest=(event_day - timedelta(days=1)).isoformat(),
        list_newest=(event_day + timedelta(days=1)).isoformat(),
    )


async def _create_test_event(sidecar: Sidecar, plan: GatePlan) -> str:
    """Create the marked future-dated test event; return its ICU event id."""
    text = await sidecar.create_event(
        {
            "start_date_local": plan.start_date_local,
            "name": plan.title,
            "type": "Ride",
            "category": "WORKOUT",
            "description": (
                "Release-gate test event created and deleted by "
                "coach-web scripts/verify_icu_crud.py (#311)."
            ),
        }
    )
    event = extract_json_object(text)
    raw_id = event.get("id")
    if raw_id is None or isinstance(raw_id, bool):
        raise SidecarError(f"no event id in create response: {text[:200]}")
    return str(raw_id)


async def _read_assert_fields(
    sidecar: Sidecar,
    event_id: str,
    expected_name: str,
    event_date: str,
) -> str:
    """Read the event back through the sidecar and assert the expected fields."""
    event = extract_json_object(await sidecar.get_event(event_id))
    name = event.get("name")
    start = str(event.get("start_date_local", ""))
    if name != expected_name or not start.startswith(event_date):
        raise SidecarError(f"fields mismatch: name={name!r} start_date_local={start!r}")
    return "fields match (name, date)"


async def _update_title(sidecar: Sidecar, event_id: str, new_title: str) -> str:
    """Update the event title through the sidecar."""
    await sidecar.update_event(event_id, {"name": new_title})
    return f"title set to {new_title!r}"


async def _delete_event(sidecar: Sidecar, event_id: str) -> str:
    """Delete the event through the sidecar."""
    await sidecar.delete_event(event_id)
    return f"event id={event_id}"


async def _assert_absent(sidecar: Sidecar, event_id: str, oldest: str, newest: str) -> str:
    """Verify the event is gone by listing its date window through the sidecar."""
    rows = parse_event_list(await sidecar.list_events(oldest, newest))
    if any(str(row.get("id")) == event_id for row in rows):
        raise SidecarError(f"orphan event id={event_id} still listed")
    return f"event id={event_id} not listed"


async def run_gate(sidecar: Sidecar, plan: GatePlan) -> GateResult:
    """Execute the full CRUD lifecycle with guaranteed cleanup and absence proof.

    The delete is attempted in a ``finally`` block even when a mid-lifecycle
    step fails (issue #311 AC2); the absence check runs afterwards so an
    orphan test event fails the gate loudly.
    """
    lines: list[str] = []
    event_id: str | None = None
    deleted = False
    failure: str | None = None

    async def attempt(
        name: str,
        action: Callable[[], Awaitable[str]],
        detail_of: Callable[[str], str] | None = None,
    ) -> str | None:
        """Run one lifecycle step and record its evidence line."""
        nonlocal failure
        if failure is not None:
            return None
        try:
            value = await action()
        except SidecarError as exc:
            failure = f"{name}: {exc}"
            lines.append(format_evidence(name, False, str(exc)))
            return None
        lines.append(format_evidence(name, True, detail_of(value) if detail_of else value))
        return value

    try:
        created_id = await attempt(
            "create",
            lambda: _create_test_event(sidecar, plan),
            detail_of=lambda created: f"event id={created}",
        )
        if created_id is not None:
            event_id = created_id
            await attempt(
                "read",
                lambda: _read_assert_fields(sidecar, created_id, plan.title, plan.event_date),
            )
            await attempt("update", lambda: _update_title(sidecar, created_id, plan.updated_title))
            await attempt(
                "read-after-update",
                lambda: _read_assert_fields(
                    sidecar, created_id, plan.updated_title, plan.event_date
                ),
            )
            deleted = (
                await attempt("delete", lambda: _delete_event(sidecar, created_id)) is not None
            )
    finally:
        if event_id is not None and not deleted:
            try:
                await sidecar.delete_event(event_id)
                deleted = True
                lines.append(format_evidence("cleanup-delete", True, f"event id={event_id}"))
            except SidecarError as exc:
                lines.append(format_evidence("cleanup-delete", False, str(exc)))

    if event_id is not None:
        # The absence check is the orphan detector (AC2): it runs even when a
        # mid-lifecycle step already failed, and its failure fails the gate.
        verified_id = event_id
        try:
            detail = await _assert_absent(sidecar, verified_id, plan.list_oldest, plan.list_newest)
        except SidecarError as exc:
            failure = f"absent: {exc}"
            lines.append(format_evidence("absent", False, str(exc)))
        else:
            lines.append(format_evidence("absent", True, detail))

    return GateResult(passed=failure is None, lines=lines)


def _extract_text(result: Any) -> str:
    """Extract the first text content block from an MCP tool result."""
    content = getattr(result, "content", None) or []
    for item in content:
        if getattr(item, "type", None) == "text":
            return str(item.text)
    raise SidecarError("unexpected MCP tool result: no text content")


class McpSidecar:
    """Real sidecar adapter driving the coach-mcp event tools over MCP.

    The sidecar (mcp 2.x ``MCPServer``) exposes every tool as a single
    ``params`` Pydantic-model argument, so arguments are wrapped as
    ``{"params": ...}`` at this transport boundary (coach-web #248 B2b).
    """

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def connect(self) -> None:
        """Establish the MCP connection to the sidecar."""
        await self._client.connect()

    async def close(self) -> None:
        """Close the MCP connection."""
        await self._client.close()

    async def _call(self, tool: str, args: dict[str, Any]) -> str:
        """Call one sidecar tool; convert every failure into SidecarError."""
        try:
            result = await self._client.call_tool(tool, {"params": args})
        except MCPConnectionError as exc:
            raise SidecarError(f"{tool} transport failure: {exc}") from exc
        text = _extract_text(result)
        if getattr(result, "is_error", False):
            raise SidecarError(f"{tool} MCP error: {text}")
        if text.startswith("Error"):
            raise SidecarError(text)
        return text

    async def create_event(self, params: dict[str, Any]) -> str:
        """Schedule the test event via ``intervals_create_event``."""
        return await self._call("intervals_create_event", params)

    async def get_event(self, event_id: str) -> str:
        """Read one event back via ``intervals_get_event`` (JSON format)."""
        return await self._call(
            "intervals_get_event",
            {"event_id": event_id, "response_format": "json"},
        )

    async def list_events(self, oldest: str, newest: str) -> str:
        """List events in a window via ``intervals_list_events`` (JSON format)."""
        return await self._call(
            "intervals_list_events",
            {"oldest": oldest, "newest": newest, "response_format": "json"},
        )

    async def update_event(self, event_id: str, params: dict[str, Any]) -> str:
        """Update the event via ``intervals_update_event``."""
        return await self._call("intervals_update_event", {"event_id": event_id, **params})

    async def delete_event(self, event_id: str) -> str:
        """Delete the event via ``intervals_delete_event``."""
        return await self._call("intervals_delete_event", {"event_id": event_id})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Intervals.icu CRUD release gate via the coach-mcp sidecar (#311)."
    )
    parser.add_argument(
        "--mcp-url",
        default=DEFAULT_MCP_URL,
        help="coach-mcp sidecar MCP URL (default: the compose-internal lane URL).",
    )
    return parser.parse_args(argv)


async def _execute(url: str, plan: GatePlan) -> GateResult:
    """Connect to the sidecar, run the gate, and always disconnect."""
    sidecar = McpSidecar(MCPClient(url))
    try:
        await sidecar.connect()
    except MCPConnectionError as exc:
        return GateResult(passed=False, lines=[format_evidence("connect", False, str(exc))])
    try:
        return await run_gate(sidecar, plan)
    finally:
        await sidecar.close()


def main(argv: list[str] | None = None) -> int:
    """Run the gate; exit 0 only when every lifecycle step passes."""
    args = parse_args(argv)
    result = asyncio.run(_execute(args.mcp_url, build_plan(date.today())))
    for line in result.lines:
        print(line)
    if result.passed:
        print("GATE PASS")
        return 0
    print("GATE FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
