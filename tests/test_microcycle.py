"""Tests for coach_web.microcycle — weekly draft, state machine, approval (#168).

The weekly microcycle (epic #161 story 2.1) is proposed by the agent as a
validated ``WeeklyPlanDraft``, rendered as a weekly plan card (#165 contract)
and written to the Intervals.icu calendar day by day on approval (#163: the
write is NOT atomic — per-day ``DayWriteResult`` outcomes are reported and a
partial failure stays retryable). Persistence follows MADR-008: drafts live in
the ``plan_drafts`` table (C3 — plaintext) with the draft state machine
(draft → submitted → approved/rejected; superseded on replacement).

Every test is hermetic: the MCP hub is replaced by an in-memory fake and the
persistence fixtures run against a per-test SQLite file.
"""

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from coach_web.microcycle import (
    ALLOWED_TRANSITIONS,
    DaySlot,
    PlanDraftRow,
    WeekApprovalRequest,
    WeekApprovalResponse,
    WeeklyPlanDraft,
    build_day_event_arguments,
    build_week_markdown,
    create_draft,
    list_drafts_by_week,
    transition_draft_status,
    upsert_week_draft,
    week_bounds,
    write_week_days,
)
from coach_web.plan_approval import COACH_CREATE_EVENT_TOOL, EVENT_START_TIME

WEEK_START = "2026-10-05"
"""Monday of ISO week 2026-W41 (pinned literal — the tests' calendar anchor)."""

WEEK_END = "2026-10-11"
"""Sunday of ISO week 2026-W41."""


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _raw_day(index: int, **overrides: Any) -> dict[str, Any]:
    """Build a raw day-slot payload for the i-th day of 2026-W41."""
    day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    hard = index in (1, 3)
    rest = index == 6
    payload: dict[str, Any] = {
        "day_of_week": day_names[index],
        "date": date.fromisocalendar(2026, 41, index + 1).isoformat(),
        "session_title": "Rest day" if rest else ("VO2 intervals" if hard else "Easy spin"),
        "focus": None if rest else ("Threshold zones" if hard else "Endurance zones"),
        "zones": [] if rest else (["Z4", "Z5"] if hard else ["Z1", "Z2"]),
        "planned_tss": 0.0 if rest else (85.0 if hard else 45.0),
        "duration_minutes": 0.0 if rest else (75.0 if hard else 60.0),
        "rest_day": rest,
    }
    payload.update(overrides)
    return payload


def _raw_week(**overrides: Any) -> dict[str, Any]:
    """Build a raw weekly-plan payload: 2 hard / 4 easy / 1 rest, 350 TSS."""
    payload: dict[str, Any] = {
        "week_id": "2026-W41",
        "title": "Base week 41",
        "summary": "Two hard days after the recovery block.",
        "rationale": "Volume capped at the availability budget.",
        "days": [_raw_day(i) for i in range(7)],
        "total_tss": 350.0,
        "hard_days": 2,
        "easy_days": 4,
    }
    payload.update(overrides)
    return payload


def _week(**overrides: Any) -> WeeklyPlanDraft:
    """Build a validated WeeklyPlanDraft with optional overrides."""
    return WeeklyPlanDraft.model_validate(_raw_week(**overrides))


class _RecordingHub:
    """In-memory stand-in for the MCP hub recording every tool call."""

    def __init__(self, results: list[str] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Record the call and replay the next configured payload."""
        self.calls.append((name, arguments))
        if self._results:
            return self._results.pop(0)
        return json.dumps({"id": "evt", "status": "created"})


# ---------------------------------------------------------------------------
# DaySlot model
# ---------------------------------------------------------------------------


class TestDaySlot:
    """DaySlot validation (#165 sketch)."""

    def test_accepts_a_valid_day(self) -> None:
        """A well-formed day slot validates with its defaults."""
        day = DaySlot.model_validate(_raw_day(0))

        assert day.day_of_week == "mon"
        assert day.date == WEEK_START
        assert day.rest_day is False
        assert day.zones == ["Z1", "Z2"]

    def test_rejects_a_malformed_date(self) -> None:
        """The date must be a real ISO calendar date."""
        with pytest.raises(ValidationError):
            DaySlot.model_validate(_raw_day(0, date="05-10-2026"))

    def test_rejects_more_than_five_zones(self) -> None:
        """The zones list is capped at 5 entries (#165 sketch)."""
        with pytest.raises(ValidationError):
            DaySlot.model_validate(_raw_day(0, zones=["Z1", "Z2", "Z3", "Z4", "Z5", "Z6"]))

    def test_rejects_an_unknown_day_name(self) -> None:
        """day_of_week is restricted to the seven ISO day literals."""
        with pytest.raises(ValidationError):
            DaySlot.model_validate(_raw_day(0, day_of_week="funday"))

    def test_rejects_negative_tss(self) -> None:
        """planned_tss is non-negative."""
        with pytest.raises(ValidationError):
            DaySlot.model_validate(_raw_day(0, planned_tss=-1))

    def test_rejects_blank_zones(self) -> None:
        """Zone labels must not be blank."""
        with pytest.raises(ValidationError, match="blank"):
            DaySlot.model_validate(_raw_day(0, zones=["Z1", "  "]))

    def test_rejects_oversized_zone_labels(self) -> None:
        """A single zone label is capped at 20 characters."""
        with pytest.raises(ValidationError, match="capped"):
            DaySlot.model_validate(_raw_day(0, zones=["Z" * 21]))


# ---------------------------------------------------------------------------
# WeeklyPlanDraft model — server-side verification posture (#165)
# ---------------------------------------------------------------------------


class TestWeeklyPlanDraft:
    """WeeklyPlanDraft validation: never trust agent-computed totals."""

    def test_accepts_a_valid_week(self) -> None:
        """A consistent 7-day draft validates."""
        draft = _week()

        assert draft.week_id == "2026-W41"
        assert len(draft.days) == 7
        assert draft.total_tss == 350.0
        assert draft.hard_days == 2
        assert draft.easy_days == 4

    def test_rejects_more_than_seven_days(self) -> None:
        """The draft is capped at 7 day slots (#165 sketch)."""
        days = [_raw_day(i % 7) for i in range(8)]
        with pytest.raises(ValidationError):
            WeeklyPlanDraft.model_validate(_raw_week(days=days))

    def test_rejects_a_total_tss_mismatch(self) -> None:
        """total_tss must equal the sum of the day planned_tss values."""
        with pytest.raises(ValidationError, match="total_tss"):
            WeeklyPlanDraft.model_validate(_raw_week(total_tss=999.0))

    def test_tolerates_float_rounding_within_tolerance(self) -> None:
        """A sub-cent drift between the declared and computed total passes."""
        payload = _raw_week()
        payload["total_tss"] = 350.03  # Σ = 350.0 — float noise, not a lie
        assert WeeklyPlanDraft.model_validate(payload).total_tss == 350.03

    def test_rejects_a_partition_violation(self) -> None:
        """hard_days + easy_days + rest days must partition the day count."""
        with pytest.raises(ValidationError, match="partition"):
            WeeklyPlanDraft.model_validate(_raw_week(hard_days=3))

    def test_rejects_a_day_outside_the_week(self) -> None:
        """Every day date must fall inside week_id's ISO week."""
        days = [_raw_day(i) for i in range(7)]
        days[0]["date"] = "2026-11-02"  # a Monday, but of another week
        with pytest.raises(ValidationError, match="must fall within week_id's ISO week"):
            WeeklyPlanDraft.model_validate(_raw_week(days=days))

    def test_rejects_a_day_name_not_matching_its_date(self) -> None:
        """day_of_week must agree with the date's weekday."""
        days = [_raw_day(i) for i in range(7)]
        days[0]["day_of_week"] = "fri"
        with pytest.raises(ValidationError, match="day_of_week"):
            WeeklyPlanDraft.model_validate(_raw_week(days=days))

    def test_rejects_an_invalid_week_id(self) -> None:
        """week_id must be a valid ISO week identifier."""
        with pytest.raises(ValidationError):
            WeeklyPlanDraft.model_validate(_raw_week(week_id="2026-W99"))


# ---------------------------------------------------------------------------
# Draft state machine (MADR-008 status enum)
# ---------------------------------------------------------------------------


class TestDraftStateMachine:
    """draft → submitted → approved/rejected; superseded on replacement."""

    def test_transition_table_matches_the_madr008_machine(self) -> None:
        """The allowed-transition table encodes the documented lifecycle."""
        assert ALLOWED_TRANSITIONS == {
            "draft": {"submitted", "superseded"},
            "submitted": {"approved", "rejected", "superseded"},
            "approved": {"superseded"},
            "rejected": {"superseded"},
            "superseded": set(),
        }

    async def test_legal_transition_persists(self, db_session_factory: Any) -> None:
        """A legal transition updates the status and commits."""
        async with db_session_factory() as session:
            row = await create_draft(session, _week())
            assert row.status == "draft"

            await transition_draft_status(session, row, "submitted")
            assert row.status == "submitted"

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "submitted"

    async def test_illegal_transition_is_rejected(self, db_session_factory: Any) -> None:
        """An out-of-machine transition raises and leaves the row untouched."""
        async with db_session_factory() as session:
            row = await create_draft(session, _week())

            with pytest.raises(ValueError, match="draft -> approved"):
                await transition_draft_status(session, row, "approved")

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "draft"

    async def test_approval_stamps_approved_at(self, db_session_factory: Any) -> None:
        """Transitioning to approved records the approval timestamp."""
        async with db_session_factory() as session:
            row = await create_draft(session, _week())
            await transition_draft_status(session, row, "submitted")
            await transition_draft_status(session, row, "approved")

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "approved"
        assert stored[0].approved_at is not None


# ---------------------------------------------------------------------------
# Repository (MADR-008 plan_drafts)
# ---------------------------------------------------------------------------


class TestPlanDraftRepository:
    """Create / list / transition semantics over plan_drafts."""

    async def test_create_draft_persists_week_bounds_and_content(
        self, db_session_factory: Any
    ) -> None:
        """A created draft carries the ISO week bounds and markdown content."""
        async with db_session_factory() as session:
            row = await create_draft(session, _week())

            stored = await list_drafts_by_week(session, WEEK_START)
        assert row.status == "draft"
        assert row.week_start_date == WEEK_START
        assert row.week_end_date == WEEK_END
        assert stored[0].id == row.id
        assert "# Base week 41" in stored[0].content
        assert "| VO2 intervals |" in stored[0].content

    async def test_create_draft_supersedes_previous_drafts_of_the_week(
        self, db_session_factory: Any
    ) -> None:
        """A new draft for the same week supersedes the previous one (replacement)."""
        async with db_session_factory() as session:
            first = await create_draft(session, _week())
            second = await create_draft(session, _week(title="Revised week 41"))

            stored = await list_drafts_by_week(session, WEEK_START)
        statuses = {row.id: row.status for row in stored}
        assert statuses[first.id] == "superseded"
        assert statuses[second.id] == "draft"

    async def test_list_drafts_by_week_is_scoped_to_the_week(self, db_session_factory: Any) -> None:
        """Drafts of other weeks never leak into the week listing."""
        other_days = [
            _raw_day(i, date=date.fromisocalendar(2026, 42, i + 1).isoformat()) for i in range(7)
        ]
        other_week = _week(week_id="2026-W42", days=other_days)
        async with db_session_factory() as session:
            await create_draft(session, _week())
            await create_draft(session, other_week)

            stored = await list_drafts_by_week(session, WEEK_START)
        assert len(stored) == 1
        assert stored[0].week_start_date == WEEK_START

    async def test_upsert_reuses_the_active_row_without_duplicating(
        self, db_session_factory: Any
    ) -> None:
        """Re-approving the same week reuses its active row (AC4: no duplicate)."""
        async with db_session_factory() as session:
            first = await upsert_week_draft(session, _week())
            second = await upsert_week_draft(session, _week())

            stored = await list_drafts_by_week(session, WEEK_START)
        assert first.id == second.id
        assert len(stored) == 1
        assert stored[0].status == "submitted"

    async def test_upsert_moves_a_fresh_draft_to_submitted(self, db_session_factory: Any) -> None:
        """A brand-new draft lands directly in submitted (approval in flight)."""
        async with db_session_factory() as session:
            row = await upsert_week_draft(session, _week())
        assert row.status == "submitted"

    async def test_upsert_replaces_a_superseded_history(self, db_session_factory: Any) -> None:
        """A new proposal after an approved week supersedes the old draft."""
        async with db_session_factory() as session:
            approved = await upsert_week_draft(session, _week())
            await transition_draft_status(session, approved, "approved")

            replacement = await upsert_week_draft(session, _week(title="Revised"))

            stored = await list_drafts_by_week(session, WEEK_START)
        statuses = {row.id: row.status for row in stored}
        assert statuses[approved.id] == "superseded"
        assert statuses[replacement.id] == "submitted"


# ---------------------------------------------------------------------------
# Calendar event arguments
# ---------------------------------------------------------------------------


class TestBuildDayEventArguments:
    """Per-day ``intervals_create_event`` payload construction."""

    def test_training_day_maps_to_a_workout_event(self) -> None:
        """A training day carries the WORKOUT category, load and duration."""
        args = build_day_event_arguments(DaySlot.model_validate(_raw_day(1)))
        tuesday = date.fromisocalendar(2026, 41, 2).isoformat()

        assert args["name"] == "VO2 intervals"
        assert args["type"] == "Ride"
        assert args["category"] == "WORKOUT"
        assert args["start_date_local"] == f"{tuesday}T{EVENT_START_TIME}"
        assert args["moving_time_seconds"] == 75 * 60
        assert args["icu_training_load"] == 85.0
        assert "Threshold zones" in args["description"]
        assert "Z4" in args["description"]

    def test_rest_day_maps_to_a_note_event_without_load(self) -> None:
        """A rest day is a calendar NOTE without duration or load."""
        args = build_day_event_arguments(DaySlot.model_validate(_raw_day(6)))

        assert args["category"] == "NOTE"
        assert "moving_time_seconds" not in args
        assert "icu_training_load" not in args
        assert args["start_date_local"].startswith(WEEK_END)


# ---------------------------------------------------------------------------
# Markdown rendition (plan_drafts.content)
# ---------------------------------------------------------------------------


class TestBuildWeekMarkdown:
    """The persisted draft content is a readable Markdown document."""

    def test_renders_header_summary_and_day_table(self) -> None:
        """The document carries the week metadata and one row per day."""
        markdown = build_week_markdown(_week())

        assert markdown.startswith("# Base week 41")
        assert "**Week:** 2026-W41" in markdown
        assert "**Total TSS:** 350" in markdown
        assert "Two hard days after the recovery block." in markdown
        assert "| tue |" in markdown
        assert "| sun |" in markdown
        assert "| Rest day" in markdown

    def test_omits_rationale_when_absent(self) -> None:
        """A draft without rationale omits the rationale section."""
        markdown = build_week_markdown(_week(rationale=None))

        assert "## Rationale" not in markdown


# ---------------------------------------------------------------------------
# write_week_days — sequential per-day calendar writes (#163)
# ---------------------------------------------------------------------------


class TestWriteWeekDays:
    """Sequential per-day ``intervals_create_event`` orchestration."""

    async def test_writes_every_day_in_order(self) -> None:
        """All 7 days are written sequentially, in draft order."""
        hub = _RecordingHub()

        results = await write_week_days(hub, _week())

        assert [result.date for result in results] == [day.date for day in _week().days]
        assert all(result.success for result in results)
        assert len(hub.calls) == 7
        assert all(name == COACH_CREATE_EVENT_TOOL for name, _ in hub.calls)

    async def test_writes_only_the_requested_subset(self) -> None:
        """A dates subset (retry-failed-days) writes only those days."""
        hub = _RecordingHub()
        failed = [date.fromisocalendar(2026, 41, 3).isoformat()]

        results = await write_week_days(hub, _week(), dates=failed)

        assert [result.date for result in results] == failed
        assert len(hub.calls) == 1

    async def test_a_failing_day_does_not_stop_the_week(self) -> None:
        """One rejected day is isolated; the remaining days still write."""
        hub = _RecordingHub(
            results=[json.dumps({"id": "ok"})] * 3 + [json.dumps({"error": "rejected"})] * 4
        )

        results = await write_week_days(hub, _week())

        assert sum(1 for result in results if result.success) == 3
        assert results[3].success is False
        assert results[3].error == "rejected"
        assert len(hub.calls) == 7

    async def test_malformed_tool_response_is_a_failure(self) -> None:
        """A non-JSON tool response is a per-day failure, never a crash."""
        hub = _RecordingHub(results=["not-json"] + [json.dumps({"id": "ok"})] * 6)

        results = await write_week_days(hub, _week())

        assert results[0].success is False
        assert results[0].error == "Malformed tool response"
        assert results[1].success is True

    async def test_non_object_tool_response_is_a_failure(self) -> None:
        """A JSON array tool response is a per-day failure, never a crash."""
        hub = _RecordingHub(results=["[1, 2]"] + [json.dumps({"id": "ok"})] * 6)

        results = await write_week_days(hub, _week())

        assert results[0].success is False
        assert results[0].error == "Malformed tool response"
        assert results[1].success is True

    async def test_a_raising_tool_call_is_a_failure(self) -> None:
        """A hub exception degrades to a per-day failure result."""

        class ExplodingHub(_RecordingHub):
            async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
                self.calls.append((name, arguments))
                raise RuntimeError("transport died")

        hub = ExplodingHub()

        results = await write_week_days(hub, _week())

        assert len(results) == 7
        assert all(result.success is False for result in results)
        assert all(result.error == "Calendar write failed" for result in results)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class TestWeekApprovalModels:
    """WeekApprovalRequest / WeekApprovalResponse schema validation."""

    def test_request_accepts_a_week_without_dates(self) -> None:
        """The plain approval request carries the whole week."""
        request = WeekApprovalRequest.model_validate({"week": _raw_week()})

        assert request.dates is None
        assert request.week.week_id == "2026-W41"

    def test_request_accepts_an_explicit_null_dates_field(self) -> None:
        """An explicit ``dates: null`` validates as a full approval."""
        request = WeekApprovalRequest.model_validate({"week": _raw_week(), "dates": None})

        assert request.dates is None

    def test_request_accepts_a_retry_subset(self) -> None:
        """A dates subset targets only the failed days."""
        failed = date.fromisocalendar(2026, 41, 3).isoformat()
        request = WeekApprovalRequest.model_validate({"week": _raw_week(), "dates": [failed]})

        assert request.dates == [failed]

    def test_request_rejects_dates_outside_the_week(self) -> None:
        """A date that is not a day of the week is rejected."""
        with pytest.raises(ValidationError):
            WeekApprovalRequest.model_validate({"week": _raw_week(), "dates": ["2027-01-01"]})

    def test_request_rejects_duplicate_dates(self) -> None:
        """Duplicate dates would double-write an event — rejected."""
        failed = date.fromisocalendar(2026, 41, 3).isoformat()
        with pytest.raises(ValidationError):
            WeekApprovalRequest.model_validate({"week": _raw_week(), "dates": [failed, failed]})

    def test_request_rejects_an_empty_subset(self) -> None:
        """An empty dates list would produce no results — rejected."""
        with pytest.raises(ValidationError):
            WeekApprovalRequest.model_validate({"week": _raw_week(), "dates": []})

    def test_response_requires_at_least_one_result(self) -> None:
        """The response carries 1–7 per-day outcomes."""
        with pytest.raises(ValidationError):
            WeekApprovalResponse.model_validate({"results": []})

    def test_orm_row_maps_the_madr008_table(self) -> None:
        """The ORM model targets the plan_drafts table with its CHECKs."""
        assert PlanDraftRow.__tablename__ == "plan_drafts"
        constraints = " ".join(
            str(constraint.sqltext)
            for constraint in PlanDraftRow.__table_args__
            if hasattr(constraint, "sqltext")
        )
        assert "week_start_date <= week_end_date" in constraints
        assert "superseded" in constraints


# ---------------------------------------------------------------------------
# week_bounds
# ---------------------------------------------------------------------------


class TestWeekBounds:
    """ISO week → (Monday, Sunday) date pair."""

    def test_resolves_the_week_bounds(self) -> None:
        """2026-W41 spans 2026-10-05 .. 2026-10-11."""
        assert week_bounds("2026-W41") == (WEEK_START, WEEK_END)
