"""Tests for the Intervals.icu CRUD release-gate script (#311).

``scripts/verify_icu_crud.py`` drives the coach-mcp sidecar's event tools
through the full CREATE → READ → UPDATE → READ → DELETE → ABSENT lifecycle.
The live sidecar call is an external system and is NOT unit-tested; the seam
is the :class:`Sidecar` protocol, which tests inject as an in-memory fake
honouring the same error contract as the real MCP adapter (raise
``SidecarError`` on any tool failure, return the tool text on success).
"""

import importlib.util
import json
from datetime import date, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from coach_web.mcp_client import MCPConnectionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "verify_icu_crud.py"


def _load_gate() -> ModuleType:
    """Load scripts/verify_icu_crud.py as a module."""
    assert SCRIPT_PATH.is_file(), "scripts/verify_icu_crud.py must exist"
    spec = importlib.util.spec_from_file_location("verify_icu_crud", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATE = _load_gate()
SidecarError = GATE.SidecarError


class FakeSidecar:
    """In-memory sidecar double honouring the Sidecar error contract."""

    def __init__(
        self,
        *,
        read_name_override: str | None = None,
        ignore_updates: bool = False,
        fail_create: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.events: dict[str, dict[str, Any]] = {}
        self.next_id = 1000
        self.delete_calls: list[str] = []
        self.read_name_override = read_name_override
        self.ignore_updates = ignore_updates
        self.fail_create = fail_create
        self.fail_delete = fail_delete

    async def create_event(self, params: dict[str, Any]) -> str:
        """Create the event in memory, or fail when configured to."""
        if self.fail_create:
            raise SidecarError("Error scheduling event: upstream 500")
        self.next_id += 1
        event_id = str(self.next_id)
        self.events[event_id] = {
            "name": params["name"],
            "start_date_local": params["start_date_local"],
        }
        payload = json.dumps({"id": int(event_id), "name": params["name"]})
        return f"Successfully scheduled event '{params['name']}': {payload}"

    async def get_event(self, event_id: str) -> str:
        """Return the stored event as JSON, or raise when absent."""
        event = self.events.get(event_id)
        if event is None:
            raise SidecarError(f"Error fetching event '{event_id}': not found")
        name = self.read_name_override or event["name"]
        return json.dumps(
            {
                "id": int(event_id),
                "name": name,
                "start_date_local": event["start_date_local"],
            }
        )

    async def list_events(self, oldest: str, newest: str) -> str:
        """Return every stored event as a JSON array."""
        rows = [
            {
                "id": int(event_id),
                "name": event["name"],
                "start_date_local": event["start_date_local"],
            }
            for event_id, event in self.events.items()
        ]
        return json.dumps(rows)

    async def update_event(self, event_id: str, params: dict[str, Any]) -> str:
        """Apply the update to the stored event, or raise when absent."""
        event = self.events.get(event_id)
        if event is None:
            raise SidecarError(f"Error updating event '{event_id}': not found")
        if not self.ignore_updates:
            event.update(params)
        return f"Successfully updated event '{event_id}': {json.dumps({'id': int(event_id)})}"

    async def delete_event(self, event_id: str) -> str:
        """Delete the stored event, or raise when configured to fail."""
        self.delete_calls.append(event_id)
        if self.fail_delete:
            raise SidecarError("Error deleting event: upstream 500")
        self.events.pop(event_id, None)
        return f"Successfully deleted event '{event_id}': {json.dumps({'id': int(event_id)})}"


def _plan() -> Any:
    """Build a deterministic gate plan from a fixed today."""
    return GATE.build_plan(date(2026, 10, 18))


class TestEvidenceAndParsing:
    """Evidence-line formatting and sidecar response parsing."""

    def test_format_evidence_pass_line(self) -> None:
        """A passing step renders as 'PASS <step>: <detail>'."""
        line = GATE.format_evidence("create", True, "event id=12345")

        assert line == "PASS create: event id=12345"

    def test_format_evidence_fail_line(self) -> None:
        """A failing step renders as 'FAIL <step>: <detail>'."""
        line = GATE.format_evidence("read", False, "fields mismatch")

        assert line == "FAIL read: fields mismatch"

    def test_extract_json_object_from_create_message(self) -> None:
        """The create tool's success message carries the event JSON payload."""
        text = "Successfully scheduled event 'Gate event': " '{"id": 12345, "name": "Gate event"}'

        payload = GATE.extract_json_object(text)

        assert payload["id"] == 12345

    def test_extract_json_object_raises_without_json(self) -> None:
        """A response without a JSON payload is a sidecar error."""
        with pytest.raises(GATE.SidecarError, match="no JSON payload"):
            GATE.extract_json_object("Successfully scheduled event 'X': (nothing)")

    def test_parse_event_list_returns_rows(self) -> None:
        """The list tool's JSON array parses into row dicts."""
        rows = GATE.parse_event_list('[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]')

        assert [row["id"] for row in rows] == [1, 2]

    def test_parse_event_list_rejects_non_list(self) -> None:
        """A non-array list response is a sidecar error."""
        with pytest.raises(GATE.SidecarError, match="expected a JSON array"):
            GATE.parse_event_list('{"error": "unexpected"}')


class TestBuildPlan:
    """The deterministic gate plan derived from today's date."""

    def test_title_carries_gate_prefix_and_future_date(self) -> None:
        """The title is unmistakably marked and future-dated (+7 days)."""
        plan = _plan()

        assert plan.title.startswith("COACH-WEB RELEASE GATE — ")
        assert plan.title.endswith("2026-10-25")
        assert plan.event_date == str(date(2026, 10, 18) + timedelta(days=7))

    def test_event_is_future_dated_at_default_start_time(self) -> None:
        """The event starts on the future date at a fixed local time."""
        plan = _plan()

        assert plan.start_date_local == "2026-10-25T08:00:00"

    def test_list_window_covers_the_event_date(self) -> None:
        """The absence-check window spans one day either side of the event."""
        plan = _plan()

        assert plan.list_oldest == "2026-10-24"
        assert plan.list_newest == "2026-10-26"

    def test_updated_title_keeps_the_gate_prefix(self) -> None:
        """The update step's title stays marked and differs from the original."""
        plan = _plan()

        assert plan.updated_title == f"{plan.title} [UPDATED]"
        assert plan.updated_title.startswith("COACH-WEB RELEASE GATE — ")


class TestRunGate:
    """The lifecycle state machine driven against a fake sidecar."""

    async def test_full_lifecycle_passes_with_six_evidence_lines(self) -> None:
        """All six steps pass, the event is gone, and delete ran exactly once."""
        sidecar = FakeSidecar()

        result = await GATE.run_gate(sidecar, _plan())

        assert result.passed is True
        assert len(result.lines) == 6
        assert all(line.startswith("PASS ") for line in result.lines)
        assert result.lines[0] == "PASS create: event id=1001"
        assert result.lines[1] == "PASS read: fields match (name, date)"
        assert result.lines[4] == "PASS delete: event id=1001"
        assert result.lines[5] == "PASS absent: event id=1001 not listed"
        assert sidecar.events == {}
        assert sidecar.delete_calls == ["1001"]

    async def test_read_mismatch_fails_and_still_cleans_up(self) -> None:
        """A mid-lifecycle failure still deletes the event (AC2 try/finally)."""
        sidecar = FakeSidecar(read_name_override="SOMETHING ELSE")

        result = await GATE.run_gate(sidecar, _plan())

        assert result.passed is False
        assert any(line.startswith("FAIL read:") for line in result.lines)
        assert any(line.startswith("PASS cleanup-delete:") for line in result.lines)
        assert any(line.startswith("PASS absent:") for line in result.lines)
        assert sidecar.events == {}
        assert len(sidecar.delete_calls) == 1

    async def test_failed_delete_fails_loudly_on_orphan(self) -> None:
        """When delete fails twice, the orphan is reported loudly (AC2)."""
        sidecar = FakeSidecar(fail_delete=True)

        result = await GATE.run_gate(sidecar, _plan())

        assert result.passed is False
        assert any(line.startswith("FAIL delete:") for line in result.lines)
        assert any(line.startswith("FAIL cleanup-delete:") for line in result.lines)
        assert any("orphan event id=" in line for line in result.lines)
        assert len(sidecar.events) == 1
        assert len(sidecar.delete_calls) == 2

    async def test_create_failure_skips_cleanup_and_absence(self) -> None:
        """No event was created, so no cleanup or absence check runs."""
        sidecar = FakeSidecar(fail_create=True)

        result = await GATE.run_gate(sidecar, _plan())

        assert result.passed is False
        assert result.lines == ["FAIL create: Error scheduling event: upstream 500"]
        assert sidecar.delete_calls == []

    async def test_stale_read_after_update_fails(self) -> None:
        """An update that never becomes visible fails the gate."""
        sidecar = FakeSidecar(ignore_updates=True)

        result = await GATE.run_gate(sidecar, _plan())

        assert result.passed is False
        assert any(line.startswith("FAIL read-after-update:") for line in result.lines)
        assert any(line.startswith("PASS cleanup-delete:") for line in result.lines)
        assert sidecar.events == {}


class TestMcpSidecarAdapter:
    """The real adapter's params wrapping and error conversion."""

    def _result(self, text: str, *, is_error: bool = False) -> Any:
        """Build a minimal MCP tool-result stand-in."""
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            is_error=is_error,
        )

    def _stub_client(self, result: Any) -> Any:
        """Build an MCPClient stand-in recording call_tool arguments."""
        calls: list[tuple[str, dict[str, Any]]] = []

        class StubClient:
            """Records call_tool invocations and replays one result."""

            async def call_tool(
                self,
                tool_name: str,
                arguments: dict[str, Any] | None = None,
            ) -> Any:
                calls.append((tool_name, arguments or {}))
                return result

        stub = StubClient()
        stub.calls = calls  # type: ignore[attr-defined]
        return stub

    async def test_call_wraps_arguments_in_params(self) -> None:
        """The sidecar schema requires a single params object (#248 B2b)."""
        stub = self._stub_client(self._result("ok"))
        sidecar = GATE.McpSidecar(stub)

        text = await sidecar.create_event({"name": "x"})

        assert text == "ok"
        assert stub.calls == [("intervals_create_event", {"params": {"name": "x"}})]

    async def test_call_raises_on_mcp_error_result(self) -> None:
        """An MCP-level error result raises SidecarError."""
        stub = self._stub_client(self._result("boom", is_error=True))
        sidecar = GATE.McpSidecar(stub)

        with pytest.raises(GATE.SidecarError, match="boom"):
            await sidecar.delete_event("7")

    async def test_call_raises_on_error_text(self) -> None:
        """The sidecar reports tool failures as 'Error ...' text."""
        stub = self._stub_client(self._result("Error fetching event '7': not found"))
        sidecar = GATE.McpSidecar(stub)

        with pytest.raises(GATE.SidecarError, match="Error fetching event"):
            await sidecar.get_event("7")

    async def test_call_raises_sidecar_error_on_transport_failure(self) -> None:
        """A transport failure surfaces as SidecarError (clean FAIL line)."""

        class FailingClient:
            """MCPClient stand-in whose call_tool always fails."""

            async def call_tool(
                self,
                tool_name: str,
                arguments: dict[str, Any] | None = None,
            ) -> Any:
                raise MCPConnectionError("connection closed")

        sidecar = GATE.McpSidecar(FailingClient())

        with pytest.raises(GATE.SidecarError, match="transport failure"):
            await sidecar.list_events("2026-10-24", "2026-10-26")

    async def test_get_event_requests_json_format(self) -> None:
        """Reads pin response_format=json for deterministic parsing."""
        stub = self._stub_client(self._result("{}"))
        sidecar = GATE.McpSidecar(stub)

        await sidecar.get_event("5")

        assert stub.calls[0][1] == {"params": {"event_id": "5", "response_format": "json"}}


class TestCli:
    """The command-line surface."""

    def test_default_mcp_url_is_the_lane_sidecar(self) -> None:
        """--mcp-url defaults to the compose-internal sidecar URL."""
        args = GATE.parse_args([])

        assert args.mcp_url == "http://coach-mcp:8000/sse"

    def test_mcp_url_override(self) -> None:
        """--mcp-url accepts an explicit sidecar URL."""
        args = GATE.parse_args(["--mcp-url", "http://elsewhere:8000/sse"])

        assert args.mcp_url == "http://elsewhere:8000/sse"
