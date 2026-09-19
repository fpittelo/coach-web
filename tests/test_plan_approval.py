"""Tests for coach_web.plan_approval — approval orchestration and HTTP endpoint.

The approval flow performs two side effects: it schedules an Intervals.icu event
through ``coach-mcp`` and commits a Markdown plan file to ``fpittelo/coach``
through ``github-mcp``. Every test is hermetic: the MCP hub is replaced by an
in-memory fake, so no network call is ever made.
"""

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coach_web.app import create_app
from coach_web.config import Settings
from coach_web.mcp_hub import MCPHubError
from coach_web.models import PlanApprovalRequest, PlanProposal
from coach_web.plan_approval import (
    COACH_CREATE_EVENT_TOOL,
    GITHUB_COMMIT_FILE_TOOL,
    approve_plan,
    build_event_arguments,
    build_plan_markdown,
    build_workout_doc,
    resolve_plan_date,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHub:
    """In-memory stand-in for MCPClientHub recording every tool call."""

    def __init__(self, results: dict[str, str] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.entered = False
        self.exited = False

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Record the call and return the configured JSON payload."""
        self.calls.append((name, arguments))
        return self._results.get(name, "{}")

    async def __aenter__(self) -> "FakeHub":
        """Mark the hub as entered."""
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Mark the hub as exited."""
        self.exited = True


class FailingHub(FakeHub):
    """Hub whose connection always fails."""

    async def __aenter__(self) -> "FailingHub":
        """Raise a hub error to exercise the endpoint failure path."""
        raise MCPHubError("no MCP server reachable")


def _plan(**overrides: Any) -> PlanProposal:
    """Build a valid plan proposal with optional overrides."""
    return PlanProposal.model_validate(_raw_plan(**overrides))


def _raw_plan(**overrides: Any) -> dict[str, Any]:
    """Build a raw plan payload, bypassing validation for negative tests."""
    payload: dict[str, Any] = {
        "title": "Threshold Builder",
        "week_id": "2026-W38",
        "summary": "Three by twelve minutes at threshold.",
        "rationale": "Raise FTP ahead of the build block.",
        "date": "2026-09-21",
        "steps": [
            {
                "label": "Warm-up",
                "duration_minutes": 15,
                "target_power_pct": 60,
                "target_power_watts": 150,
                "cadence_rpm": 90,
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
# Date resolution
# ---------------------------------------------------------------------------


class TestResolvePlanDate:
    """Workout date resolution from the proposal."""

    def test_explicit_date_wins(self) -> None:
        """An explicit date on the proposal is returned verbatim."""
        assert resolve_plan_date(_plan(date="2026-09-23")) == "2026-09-23"

    def test_derives_monday_from_iso_week(self) -> None:
        """Without an explicit date the Monday of the ISO week is used."""
        assert resolve_plan_date(_plan(date=None)) == "2026-09-14"


# ---------------------------------------------------------------------------
# Workout DSL
# ---------------------------------------------------------------------------


class TestBuildWorkoutDoc:
    """Intervals.icu workout DSL generation."""

    def test_renders_watts_cadence_and_duration(self) -> None:
        """Each step becomes a DSL line with duration, watts and cadence."""
        doc = build_workout_doc(_plan())

        assert doc.splitlines() == [
            "- Warm-up 15m 150w 90rpm",
            "- Threshold 36m 245w 88rpm",
        ]

    def test_falls_back_to_percentage_when_watts_absent(self) -> None:
        """A step without explicit watts uses its percentage target."""
        plan = _plan(steps=[{"label": "Endurance", "duration_minutes": 45, "target_power_pct": 70}])

        assert build_workout_doc(plan) == "- Endurance 45m 70%"

    def test_empty_steps_yield_empty_doc(self) -> None:
        """A plan without steps produces an empty DSL document."""
        assert build_workout_doc(_plan(steps=[])) == ""

    def test_step_without_target_omits_target_segment(self) -> None:
        """A step without any power target renders only its label and duration."""
        plan = _plan(steps=[{"label": "Free ride", "duration_minutes": 30}])

        assert build_workout_doc(plan) == "- Free ride 30m"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


class TestBuildPlanMarkdown:
    """Markdown plan document generation."""

    def test_contains_header_metadata_and_intervals(self) -> None:
        """The document carries the title, week, date, summary and interval table."""
        markdown = build_plan_markdown(_plan(), "2026-09-21")

        assert markdown.startswith("# Threshold Builder")
        assert "**Week:** 2026-W38" in markdown
        assert "**Date:** 2026-09-21" in markdown
        assert "**Duration:** 51m" in markdown
        assert "Three by twelve minutes at threshold." in markdown
        assert "Raise FTP ahead of the build block." in markdown
        assert "| Warm-up | 15m | 150 W (60%) | 90 rpm |" in markdown
        assert "| Threshold | 36m | 245 W (98%) | 88 rpm |" in markdown

    def test_omits_rationale_section_when_absent(self) -> None:
        """A proposal without rationale omits the rationale section."""
        markdown = build_plan_markdown(_plan(rationale=None), "2026-09-21")

        assert "## Rationale" not in markdown

    def test_omits_intervals_section_when_no_steps(self) -> None:
        """A proposal without steps omits the interval table."""
        markdown = build_plan_markdown(_plan(steps=[]), "2026-09-21")

        assert "## Intervals" not in markdown

    def test_renders_watts_without_percentage(self) -> None:
        """A step with only explicit watts renders the wattage alone."""
        plan = _plan(steps=[{"label": "Sprint", "duration_minutes": 1, "target_power_watts": 600}])

        markdown = build_plan_markdown(plan, "2026-09-21")

        assert "| Sprint | 1m | 600 W | — |" in markdown

    def test_renders_percentage_only_target(self) -> None:
        """A step with only a percentage target renders the percentage."""
        plan = _plan(steps=[{"label": "Endurance", "duration_minutes": 45, "target_power_pct": 70}])

        markdown = build_plan_markdown(plan, "2026-09-21")

        assert "| Endurance | 45m | 70% | — |" in markdown

    def test_renders_placeholder_without_target(self) -> None:
        """A step without any target renders an em dash placeholder."""
        plan = _plan(steps=[{"label": "Free ride", "duration_minutes": 30}])

        markdown = build_plan_markdown(plan, "2026-09-21")

        assert "| Free ride | 30m | — | — |" in markdown


# ---------------------------------------------------------------------------
# Event arguments
# ---------------------------------------------------------------------------


class TestBuildEventArguments:
    """Intervals.icu event payload construction."""

    def test_maps_plan_to_event_arguments(self) -> None:
        """The plan maps to a structured Intervals.icu event payload."""
        args = build_event_arguments(_plan(), "2026-09-21")

        assert args["name"] == "Threshold Builder"
        assert args["type"] == "Ride"
        assert args["category"] == "WORKOUT"
        assert args["start_date_local"] == "2026-09-21T08:00:00"
        assert args["moving_time_seconds"] == 51 * 60
        assert args["description"] == "Three by twelve minutes at threshold."
        assert args["workout_doc"] == "- Warm-up 15m 150w 90rpm\n- Threshold 36m 245w 88rpm"

    def test_omits_workout_doc_when_no_steps(self) -> None:
        """A stepless plan omits the workout_doc key."""
        args = build_event_arguments(_plan(steps=[]), "2026-09-21")

        assert "workout_doc" not in args


# ---------------------------------------------------------------------------
# approve_plan orchestration
# ---------------------------------------------------------------------------


class TestApprovePlan:
    """Two-step approval orchestration."""

    async def test_successful_approval_calls_both_tools(self) -> None:
        """A healthy approval schedules the event and commits the Markdown file."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: json.dumps({"id": "e1", "status": "created"}),
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"commit": {"sha": "abc"}}),
            }
        )

        response = await approve_plan(hub, _plan(), repo="fpittelo/coach")

        assert response.status == "approved"
        assert response.plan_title == "Threshold Builder"
        assert response.week_id == "2026-W38"
        assert response.date == "2026-09-21"
        assert [step.step for step in response.steps] == ["intervals_event", "github_commit"]
        assert all(step.success for step in response.steps)

        event_name, event_args = hub.calls[0]
        assert event_name == COACH_CREATE_EVENT_TOOL
        assert event_args is not None
        assert event_args["start_date_local"] == "2026-09-21T08:00:00"

        commit_name, commit_args = hub.calls[1]
        assert commit_name == GITHUB_COMMIT_FILE_TOOL
        assert commit_args is not None
        assert commit_args["owner"] == "fpittelo"
        assert commit_args["repo"] == "coach"
        assert commit_args["path"] == "plans/2026-W38.md"
        assert commit_args["branch"] == "main"
        assert "# Threshold Builder" in commit_args["content"]

    async def test_partial_failure_reports_partial_status(self) -> None:
        """A failing GitHub commit degrades the overall status to partial."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: json.dumps({"id": "e1"}),
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"error": "branch protected"}),
            }
        )

        response = await approve_plan(hub, _plan(), repo="fpittelo/coach")

        assert response.status == "partial"
        assert response.steps[0].success is True
        assert response.steps[1].success is False
        assert response.steps[1].error == "branch protected"

    async def test_total_failure_reports_failed_status(self) -> None:
        """When both side effects fail the status is failed."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: json.dumps({"error": "intervals down"}),
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"error": "github down"}),
            }
        )

        response = await approve_plan(hub, _plan(), repo="fpittelo/coach")

        assert response.status == "failed"
        assert all(not step.success for step in response.steps)

    async def test_malformed_tool_response_is_a_failure(self) -> None:
        """A non-JSON tool response is reported as a failure, never a crash."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: "not-json",
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"ok": True}),
            }
        )

        response = await approve_plan(hub, _plan(), repo="fpittelo/coach")

        assert response.status == "partial"
        assert response.steps[0].success is False
        assert response.steps[0].error is not None

    async def test_non_object_tool_response_is_a_failure(self) -> None:
        """A JSON array tool response is reported as a failure, never a crash."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: "[1, 2]",
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"ok": True}),
            }
        )

        response = await approve_plan(hub, _plan(), repo="fpittelo/coach")

        assert response.status == "partial"
        assert response.steps[0].success is False
        assert response.steps[0].error == "Malformed tool response"

    async def test_invalid_repository_fails_commit_step(self) -> None:
        """A malformed repository slug fails the commit step without calling GitHub."""
        hub = FakeHub({COACH_CREATE_EVENT_TOOL: json.dumps({"id": "e1"})})

        response = await approve_plan(hub, _plan(), repo="not-a-slug")

        assert response.status == "partial"
        assert response.steps[1].success is False
        assert "not-a-slug" in (response.steps[1].error or "")
        assert all(name != GITHUB_COMMIT_FILE_TOOL for name, _ in hub.calls)

    async def test_custom_branch_and_directory_are_forwarded(self) -> None:
        """Branch and directory overrides reach the GitHub commit payload."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: json.dumps({"id": "e1"}),
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"ok": True}),
            }
        )

        await approve_plan(
            hub,
            _plan(),
            repo="fpittelo/coach",
            branch="dev",
            directory="training-plans",
        )

        _, commit_args = hub.calls[1]
        assert commit_args is not None
        assert commit_args["branch"] == "dev"
        assert commit_args["path"] == "training-plans/2026-W38.md"


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


class TestPlanApproveEndpoint:
    """POST /api/plan/approve contract."""

    def _client_with(self, settings: Settings, hub: FakeHub) -> TestClient:
        """Build a TestClient whose hub factory returns the supplied hub."""
        app = create_app(settings)
        app.state.hub_factory = lambda _settings: hub
        return TestClient(app)

    def test_approve_returns_structured_response(self, settings: Settings) -> None:
        """A valid approval returns the aggregated two-step outcome."""
        hub = FakeHub(
            {
                COACH_CREATE_EVENT_TOOL: json.dumps({"id": "e1"}),
                GITHUB_COMMIT_FILE_TOOL: json.dumps({"ok": True}),
            }
        )
        client = self._client_with(settings, hub)

        response = client.post(
            "/api/plan/approve",
            json={"plan": _plan().model_dump()},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "approved"
        assert payload["week_id"] == "2026-W38"
        assert len(payload["steps"]) == 2
        assert hub.entered is True
        assert hub.exited is True

    def test_missing_plan_is_rejected(self, settings: Settings) -> None:
        """A payload without a plan is rejected with 422."""
        client = self._client_with(settings, FakeHub())

        response = client.post("/api/plan/approve", json={})

        assert response.status_code == 422

    def test_invalid_week_id_is_rejected(self, settings: Settings) -> None:
        """An invalid ISO week identifier is rejected with 422."""
        client = self._client_with(settings, FakeHub())

        response = client.post(
            "/api/plan/approve",
            json={"plan": _raw_plan(week_id="week-38")},
        )

        assert response.status_code == 422

    def test_invalid_date_is_rejected(self, settings: Settings) -> None:
        """A malformed explicit date is rejected with 422."""
        client = self._client_with(settings, FakeHub())

        response = client.post(
            "/api/plan/approve",
            json={"plan": _raw_plan(date="21-09-2026")},
        )

        assert response.status_code == 422

    def test_hub_connection_failure_returns_503(self, settings: Settings) -> None:
        """An unreachable MCP hub is surfaced as a 503."""
        client = self._client_with(settings, FailingHub())

        response = client.post(
            "/api/plan/approve",
            json={"plan": _plan().model_dump()},
        )

        assert response.status_code == 503
        assert "no MCP server reachable" in response.json()["detail"]

    def test_app_exposes_default_hub_factory(self, settings: Settings) -> None:
        """The application exposes the default hub factory on state."""
        app = create_app(settings)

        assert callable(app.state.hub_factory)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class TestPlanApprovalRequest:
    """PlanApprovalRequest schema validation."""

    def test_accepts_plan_payload(self) -> None:
        """A well-formed request wraps a validated plan proposal."""
        request = PlanApprovalRequest.model_validate({"plan": _plan().model_dump()})

        assert request.plan.title == "Threshold Builder"

    def test_rejects_missing_plan(self) -> None:
        """A request without a plan is rejected."""
        with pytest.raises(ValidationError):
            PlanApprovalRequest.model_validate({})
