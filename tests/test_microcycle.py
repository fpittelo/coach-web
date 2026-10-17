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
import re
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from coach_web.microcycle import (
    ALLOWED_TRANSITIONS,
    APPROVED_WEEK_DIGEST_MAX_CHARS,
    CALENDAR_WRITE_FAILED,
    COACH_BATCH_CREATE_EVENTS_TOOL,
    DaySlot,
    PlanDraftRow,
    WeekApprovalRequest,
    WeekApprovalResponse,
    WeeklyPlanDraft,
    adjusted_future_days,
    apply_approved_week_digest,
    approved_week_digest,
    build_batch_event_draft,
    build_week_markdown,
    list_drafts_by_week,
    moved_session_dates,
    moved_session_origins,
    past_days_render_identically,
    transition_draft_status,
    upsert_week_draft,
    week_bounds,
    write_week_days,
)
from coach_web.periodization import PeriodizationPhase

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
        """The allowed-transition table encodes the documented lifecycle.

        Superseded for PR #174 (F3): a REJECTED draft returns to
        ``submitted`` on re-approval — the athlete changed their mind.
        """
        assert ALLOWED_TRANSITIONS == {
            "draft": {"submitted", "superseded"},
            "submitted": {"approved", "rejected", "superseded"},
            "approved": {"superseded"},
            "rejected": {"submitted", "superseded"},
            "superseded": set(),
        }

    async def test_legal_transition_persists(self, db_session_factory: Any) -> None:
        """A legal transition updates the status and commits."""
        async with db_session_factory() as session:
            row = await upsert_week_draft(session, _week())
            assert row.status == "submitted"

            await transition_draft_status(session, row, "approved")
            assert row.status == "approved"

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "approved"

    async def test_illegal_transition_is_rejected(self, db_session_factory: Any) -> None:
        """An out-of-machine transition raises and leaves the row untouched."""
        async with db_session_factory() as session:
            row = await upsert_week_draft(session, _week())

            with pytest.raises(ValueError, match="submitted -> draft"):
                await transition_draft_status(session, row, "draft")

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "submitted"

    async def test_approval_stamps_approved_at(self, db_session_factory: Any) -> None:
        """Transitioning to approved records the approval timestamp."""
        async with db_session_factory() as session:
            row = await upsert_week_draft(session, _week())
            await transition_draft_status(session, row, "approved")

            stored = await list_drafts_by_week(session, WEEK_START)
        assert stored[0].status == "approved"
        assert stored[0].approved_at is not None


# ---------------------------------------------------------------------------
# Repository (MADR-008 plan_drafts)
# ---------------------------------------------------------------------------


class TestPlanDraftRepository:
    """Upsert / list / transition semantics over plan_drafts."""

    async def test_upsert_persists_week_bounds_and_content(self, db_session_factory: Any) -> None:
        """A created draft carries the ISO week bounds and markdown content."""
        async with db_session_factory() as session:
            row = await upsert_week_draft(session, _week())

            stored = await list_drafts_by_week(session, WEEK_START)
        assert row.status == "submitted"
        assert row.week_start_date == WEEK_START
        assert row.week_end_date == WEEK_END
        assert stored[0].id == row.id
        assert "# Base week 41" in stored[0].content
        assert "| VO2 intervals |" in stored[0].content

    async def test_upsert_supersedes_previous_drafts_of_the_week(
        self, db_session_factory: Any
    ) -> None:
        """A changed proposal for the same week supersedes the previous one."""
        async with db_session_factory() as session:
            first = await upsert_week_draft(session, _week())
            second = await upsert_week_draft(session, _week(title="Revised week 41"))

            stored = await list_drafts_by_week(session, WEEK_START)
        statuses = {row.id: row.status for row in stored}
        assert statuses[first.id] == "superseded"
        assert statuses[second.id] == "submitted"

    async def test_list_drafts_by_week_is_scoped_to_the_week(self, db_session_factory: Any) -> None:
        """Drafts of other weeks never leak into the week listing."""
        other_days = [
            _raw_day(i, date=date.fromisocalendar(2026, 42, i + 1).isoformat()) for i in range(7)
        ]
        other_week = _week(week_id="2026-W42", days=other_days)
        async with db_session_factory() as session:
            await upsert_week_draft(session, _week())
            await upsert_week_draft(session, other_week)

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

    async def test_upsert_promotes_a_rejected_draft_to_submitted(
        self, db_session_factory: Any
    ) -> None:
        """Re-approval of a rejected week promotes it back to submitted (F3)."""
        async with db_session_factory() as session:
            rejected = await upsert_week_draft(session, _week())
            await transition_draft_status(session, rejected, "rejected")

            again = await upsert_week_draft(session, _week())

            stored = await list_drafts_by_week(session, WEEK_START)
        assert again.id == rejected.id
        assert again.status == "submitted"
        assert len(stored) == 1


# ---------------------------------------------------------------------------
# Batch event drafts (D1/C6, #225)
# ---------------------------------------------------------------------------


class TestBuildBatchEventDraft:
    """Per-day ``icu_batch_create_events`` draft construction."""

    def test_training_day_maps_to_a_batch_workout_draft(self) -> None:
        """A training day carries the WORKOUT category, load and duration."""
        draft = build_batch_event_draft(DaySlot.model_validate(_raw_day(1)), "2026-W41")
        tuesday = date.fromisocalendar(2026, 41, 2).isoformat()

        assert draft["external_id"] == "coach-web-2026-W41-tue"
        assert draft["date"] == tuesday
        assert draft["name"] == "VO2 intervals"
        assert draft["type"] == "Ride"
        assert draft["category"] == "WORKOUT"
        assert draft["moving_time_seconds"] == 75 * 60
        assert draft["icu_training_load"] == 85.0
        assert "Threshold zones" in draft["description"]

    def test_rest_day_maps_to_a_note_draft_without_load(self) -> None:
        """A rest day is a calendar NOTE without duration or load."""
        draft = build_batch_event_draft(DaySlot.model_validate(_raw_day(6)), "2026-W41")

        assert draft["external_id"] == "coach-web-2026-W41-sun"
        assert draft["category"] == "NOTE"
        assert "moving_time_seconds" not in draft
        assert "icu_training_load" not in draft

    def test_external_ids_fit_the_coach_mcp_contract(self) -> None:
        """Keys match the coach-mcp character pattern and its 128-char cap."""
        pattern = re.compile(r"^[A-Za-z0-9_.-]+$")
        for index in range(7):
            draft = build_batch_event_draft(DaySlot.model_validate(_raw_day(index)), "2026-W41")
            assert pattern.fullmatch(draft["external_id"])
            assert len(draft["external_id"]) <= 128


# ---------------------------------------------------------------------------
# Two-phase batch week writes (D1, #225)
# ---------------------------------------------------------------------------


STUB_CONFIRM_TOKEN = "stub-confirm-token"  # noqa: S105
"""Stub preview token of the two-phase batch hub (a payload fingerprint)."""


class _BatchHub:
    """Stub hub implementing the two-phase ``icu_batch_create_events`` contract.

    Preview calls (without ``confirm_token``) return a stub token; execute
    calls return the configured report (or a per-event ``created`` report).
    """

    def __init__(
        self,
        report: dict[str, Any] | None = None,
        preview_response: str | None = None,
        report_response: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._report = report
        self._preview_response = preview_response
        self._report_response = report_response

    async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Record the call and answer per the two-phase contract."""
        self.calls.append((name, arguments or {}))
        payload = arguments or {}
        if payload.get("confirm_token") is None:
            if self._preview_response is not None:
                return self._preview_response
            events = payload.get("events") or []
            return json.dumps(
                {
                    "phase": "preview",
                    "item_count": len(events),
                    "confirm_token": STUB_CONFIRM_TOKEN,
                    "events": events,
                    "note": "Re-invoke with confirm_token to place these events.",
                }
            )
        if self._report_response is not None:
            return self._report_response
        if self._report is not None:
            return json.dumps(self._report)
        events = payload.get("events") or []
        return json.dumps(
            {
                "created": len(events),
                "updated": 0,
                "failed": 0,
                "outcomes": [
                    {
                        "external_id": event["external_id"],
                        "outcome": "created",
                        "event_id": f"evt-{event['date']}",
                        "reason": None,
                    }
                    for event in events
                ],
            }
        )


class TestBatchWeekWrites:
    """Two-phase ``icu_batch_create_events`` orchestration (D1, #225)."""

    async def test_writes_the_week_as_one_two_phase_batch(self) -> None:
        """Preview first, then execute with the token over the identical batch."""
        hub = _BatchHub()

        results = await write_week_days(hub, _week())

        assert len(hub.calls) == 2
        preview_tool, preview_args = hub.calls[0]
        execute_tool, execute_args = hub.calls[1]
        assert preview_tool == COACH_BATCH_CREATE_EVENTS_TOOL
        assert execute_tool == COACH_BATCH_CREATE_EVENTS_TOOL
        assert "confirm_token" not in preview_args
        assert preview_args["response_format"] == "json"
        assert execute_args["confirm_token"] == STUB_CONFIRM_TOKEN
        assert execute_args["events"] == preview_args["events"]
        assert [result.date for result in results] == [day.date for day in _week().days]
        assert all(result.success for result in results)

    async def test_outcomes_map_back_to_week_order(self) -> None:
        """Report outcomes map onto the draft's week order by external_id."""
        report = {
            "created": 1,
            "updated": 1,
            "failed": 1,
            "outcomes": [
                {
                    "external_id": "coach-web-2026-W41-wed",
                    "outcome": "failed",
                    "event_id": None,
                    "reason": "rejected upstream",
                },
                {
                    "external_id": "coach-web-2026-W41-mon",
                    "outcome": "created",
                    "event_id": "1",
                    "reason": None,
                },
                {
                    "external_id": "coach-web-2026-W41-tue",
                    "outcome": "updated",
                    "event_id": "2",
                    "reason": None,
                },
            ],
        }
        hub = _BatchHub(report=report)
        subset = [date.fromisocalendar(2026, 41, i + 1).isoformat() for i in range(3)]

        results = await write_week_days(hub, _week(), dates=subset)

        assert [result.date for result in results] == subset
        assert [result.success for result in results] == [True, True, False]
        # C3: a static error only — the upstream reason never surfaces.
        assert results[2].error == CALENDAR_WRITE_FAILED

    async def test_updated_outcomes_succeed_so_reapproval_never_duplicates(self) -> None:
        """``updated`` outcomes are successes — re-approval upserts, not duplicates (AC4)."""
        report = {
            "created": 0,
            "updated": 7,
            "failed": 0,
            "outcomes": [
                {
                    "external_id": f"coach-web-2026-W41-{day}",
                    "outcome": "updated",
                    "event_id": str(index),
                    "reason": None,
                }
                for index, day in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))
            ],
        }
        hub = _BatchHub(report=report)

        results = await write_week_days(hub, _week())

        assert len(results) == 7
        assert all(result.success for result in results)

    async def test_non_json_preview_fails_the_whole_batch_without_executing(self) -> None:
        """A non-JSON preview fails every day; no execute call is made (C3)."""
        hub = _BatchHub(preview_response="Error: upstream hiccup")

        results = await write_week_days(hub, _week())

        assert len(hub.calls) == 1
        assert len(results) == 7
        assert all(not result.success for result in results)
        assert all(result.error == CALENDAR_WRITE_FAILED for result in results)

    async def test_non_json_report_fails_the_whole_batch(self) -> None:
        """A non-JSON execute response fails every day of the batch (C3)."""
        hub = _BatchHub(report_response="<html>500</html>")

        results = await write_week_days(hub, _week())

        assert len(hub.calls) == 2
        failed = all(not result.success for result in results)
        assert failed and all(result.error == CALENDAR_WRITE_FAILED for result in results)

    async def test_preview_without_a_confirm_token_fails_the_whole_batch(self) -> None:
        """A preview without a confirm token cannot be executed (C4)."""
        hub = _BatchHub(
            preview_response=json.dumps({"phase": "preview", "item_count": 7, "events": []})
        )

        results = await write_week_days(hub, _week())

        assert len(hub.calls) == 1
        assert all(not result.success for result in results)

    async def test_report_without_outcomes_fails_the_whole_batch(self) -> None:
        """A report without an outcome list is a structural failure (C3)."""
        hub = _BatchHub(report_response=json.dumps({"created": 7}))

        results = await write_week_days(hub, _week())

        assert len(hub.calls) == 2
        assert all(not result.success for result in results)

    async def test_a_raising_tool_call_fails_the_whole_batch(self) -> None:
        """A hub exception degrades to per-day failures, never a crash."""

        class ExplodingHub(_BatchHub):
            async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
                self.calls.append((name, arguments or {}))
                raise RuntimeError("transport died")

        results = await write_week_days(ExplodingHub(), _week())

        assert len(results) == 7
        assert all(result.error == CALENDAR_WRITE_FAILED for result in results)

    async def test_subset_writes_only_the_requested_days(self) -> None:
        """A dates subset (retry-failed-days) batches only those days."""
        hub = _BatchHub()
        failed = [date.fromisocalendar(2026, 41, 3).isoformat()]

        results = await write_week_days(hub, _week(), dates=failed)

        execute_events = hub.calls[1][1]["events"]
        assert [draft["external_id"] for draft in execute_events] == ["coach-web-2026-W41-wed"]
        assert [result.date for result in results] == failed

    async def test_an_empty_selection_writes_nothing(self) -> None:
        """Dates matching no day produce no batch call and no results."""
        hub = _BatchHub()

        results = await write_week_days(hub, _week(), dates=["2027-01-01"])

        assert results == []
        assert hub.calls == []

    async def test_the_confirm_token_never_leaks_into_logs_or_results(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """C4: the confirm token stays in the endpoint's local scope."""
        hub = _BatchHub()

        with caplog.at_level("DEBUG"):
            results = await write_week_days(hub, _week())

        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert STUB_CONFIRM_TOKEN not in blob
        assert all(STUB_CONFIRM_TOKEN not in result.model_dump_json() for result in results)


# ---------------------------------------------------------------------------
# Past-day immutability (AC3, D3.3 — #225)
# ---------------------------------------------------------------------------


class TestPastDayImmutability:
    """Server-side past-day lock for mid-week adjustments."""

    TODAY = date(2026, 10, 8)
    """Thursday of 2026-W41 — four locked days (mon–thu), three adjustable."""

    @staticmethod
    def _stored_content() -> str:
        """The pristine stored rendition both sides are compared against."""
        return build_week_markdown(_week())

    def _renders_identically(self, draft: WeeklyPlanDraft) -> bool:
        """Compare the adjusted draft against the pristine stored content."""
        return past_days_render_identically(draft, self._stored_content(), self.TODAY)

    def test_an_unchanged_draft_matches_its_stored_content(self) -> None:
        """Identical content trivially renders past days identically."""
        assert self._renders_identically(_week()) is True

    def test_an_altered_past_day_is_rejected(self) -> None:
        """Changing a past day's row is an immutability violation."""
        days = [_raw_day(i) for i in range(7)]
        days[0] = _raw_day(0, session_title="Rewritten Monday")

        assert self._renders_identically(_week(days=days)) is False

    def test_a_future_day_may_change(self) -> None:
        """Changing a strictly-after-today day keeps past days immutable."""
        days = [_raw_day(i) for i in range(7)]
        days[5] = _raw_day(5, session_title="Recovery roll")

        assert self._renders_identically(_week(days=days)) is True

    def test_the_today_boundary_day_is_locked(self) -> None:
        """The rule is date <= today: the current day itself is locked."""
        days = [_raw_day(i) for i in range(7)]
        days[2] = _raw_day(2, session_title="Today tweaked")

        assert self._renders_identically(_week(days=days)) is False

    def test_a_day_after_today_is_mutable(self) -> None:
        """The immediate day before ``today`` is mutable; ``today`` is not."""
        days = [_raw_day(i) for i in range(7)]
        days[3] = _raw_day(3, session_title="Rescheduled Thursday")
        draft = _week(days=days)
        stored = self._stored_content()

        # Thursday 10-08 is still adjustable when ``today`` is the Wednesday...
        assert past_days_render_identically(draft, stored, date(2026, 10, 7)) is True
        # ...and locked once ``today`` reaches the day itself (date <= today).
        assert past_days_render_identically(draft, stored, date(2026, 10, 8)) is False

    def test_a_past_day_missing_from_stored_content_is_rejected(self) -> None:
        """Fail closed when the stored content lacks a past day's row."""
        stored = "\n".join(
            line for line in self._stored_content().splitlines() if "| 2026-10-05 |" not in line
        )

        assert past_days_render_identically(_week(), stored, self.TODAY) is False


# ---------------------------------------------------------------------------
# Adjustment guardrail comparisons (#233)
# ---------------------------------------------------------------------------


class TestAdjustedSessionDetection:
    """Draft-vs-stored comparison powering the #233 guardrails."""

    TODAY = date(2026, 10, 8)
    """Thursday of 2026-W41 — mon–thu locked, fri/sat/sun adjustable."""

    @staticmethod
    def _distinct_days() -> list[dict[str, Any]]:
        """Seven day slots with a distinct session title per day."""
        titles = (
            "Monday endurance",
            "Tuesday intervals",
            "Wednesday endurance",
            "Thursday intervals",
            "Friday endurance",
            "Saturday endurance",
        )
        days = [_raw_day(index, session_title=titles[index]) for index in range(6)]
        days.append(
            _raw_day(
                6,
                session_title="Sunday spin",
                focus="Endurance zones",
                zones=["Z1", "Z2"],
                planned_tss=30.0,
                duration_minutes=45.0,
                rest_day=False,
            )
        )
        return days

    def _distinct_week(self, **overrides: Any) -> WeeklyPlanDraft:
        """A validated draft over the distinct-title days (380 TSS)."""
        payload = _raw_week(total_tss=380.0, easy_days=5)
        payload["days"] = self._distinct_days()
        payload.update(overrides)
        return WeeklyPlanDraft.model_validate(payload)

    def _stored_content(self, **overrides: Any) -> str:
        """The pristine stored rendition of the distinct-title week."""
        return build_week_markdown(self._distinct_week(**overrides))

    def test_an_unchanged_draft_reports_no_adjusted_days(self) -> None:
        """Identical content yields no adjusted days."""
        assert adjusted_future_days(_week(), build_week_markdown(_week()), self.TODAY) == []

    def test_a_changed_future_day_is_reported(self) -> None:
        """A future day whose row differs is reported by date."""
        days = [_raw_day(i) for i in range(7)]
        days[5] = _raw_day(5, session_title="Recovery roll")

        adjusted = adjusted_future_days(_week(days=days), build_week_markdown(_week()), self.TODAY)

        assert [day.date for day in adjusted] == ["2026-10-10"]

    def test_past_and_current_days_are_never_reported(self) -> None:
        """Past and current days belong to the #225 immutability gate, not here."""
        days = [_raw_day(i) for i in range(7)]
        days[0] = _raw_day(0, session_title="Rewritten Monday")
        days[3] = _raw_day(3, session_title="Tweaked Thursday")

        adjusted = adjusted_future_days(_week(days=days), build_week_markdown(_week()), self.TODAY)

        assert adjusted == []

    def test_a_moved_session_title_is_reported_on_its_new_date(self) -> None:
        """A stored title re-appearing on another date is a move target."""
        days = self._distinct_days()
        days[4] = _raw_day(
            4,
            session_title="Rest day",
            focus=None,
            zones=[],
            planned_tss=0.0,
            duration_minutes=0.0,
            rest_day=True,
        )
        days[6] = _raw_day(
            6,
            session_title="Friday endurance",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=45.0,
            duration_minutes=60.0,
            rest_day=False,
        )
        draft = self._distinct_week(days=days, total_tss=350.0, easy_days=4)

        moved = moved_session_dates(draft, self._stored_content(), self.TODAY)

        assert moved == ["2026-10-11"]

    def test_a_moved_session_maps_its_new_date_to_its_original(self) -> None:
        """The origins view keys each moved session's new date to its stored date.

        The same title-identity detection as :func:`moved_session_dates`,
        exposing the original date the gate compares phases against (#233
        AC1 ruling): the Friday endurance session moved onto Sunday maps
        the new date 2026-10-11 to its original 2026-10-09.
        """
        days = self._distinct_days()
        days[4] = _raw_day(
            4,
            session_title="Rest day",
            focus=None,
            zones=[],
            planned_tss=0.0,
            duration_minutes=0.0,
            rest_day=True,
        )
        days[6] = _raw_day(
            6,
            session_title="Friday endurance",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=45.0,
            duration_minutes=60.0,
            rest_day=False,
        )
        draft = self._distinct_week(days=days, total_tss=350.0, easy_days=4)

        origins = moved_session_origins(draft, self._stored_content(), self.TODAY)

        assert origins == {"2026-10-11": "2026-10-09"}

    def test_a_same_date_edit_is_not_a_move(self) -> None:
        """An intensity edit keeping the title on its date is not a move."""
        days = self._distinct_days()
        days[6] = _raw_day(
            6,
            session_title="Sunday spin",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=20.0,
            duration_minutes=45.0,
            rest_day=False,
        )
        draft = self._distinct_week(days=days, total_tss=370.0)

        moved = moved_session_dates(draft, self._stored_content(), self.TODAY)

        assert moved == []

    def test_a_brand_new_title_is_not_a_move(self) -> None:
        """A same-date session replacement (new title) is not a move."""
        days = self._distinct_days()
        days[5] = _raw_day(5, session_title="Brand new session")

        draft = self._distinct_week(days=days)

        moved = moved_session_dates(draft, self._stored_content(), self.TODAY)

        assert moved == []


# ---------------------------------------------------------------------------
# Approved-week digest (D4/C5, #225)
# ---------------------------------------------------------------------------


class TestApprovedWeekDigest:
    """Approved-week digest — metadata only, single-line, token-bounded."""

    def _approved_row(self, **overrides: Any) -> PlanDraftRow:
        """A persisted-looking approved row carrying the rendered content."""
        content = build_week_markdown(_week(**overrides))
        return PlanDraftRow(
            week_start_date=WEEK_START,
            week_end_date=WEEK_END,
            content=content,
            status="approved",
            created_at="t0",
            updated_at="t0",
        )

    def test_digest_carries_week_title_and_day_metadata_only(self) -> None:
        """The digest exposes week_id, title and per-day metadata — nothing else."""
        digest = approved_week_digest(self._approved_row())

        assert "2026-W41" in digest
        assert "Base week 41" in digest
        assert "2026-10-05: Easy spin (TSS 45)" in digest
        assert "2026-10-11: Rest day (rest day)" in digest
        assert "Threshold zones" not in digest  # focus/zones never digested
        assert "Endurance zones" not in digest
        assert "75m" not in digest  # durations never digested

    def test_titles_collapse_to_a_single_line(self) -> None:
        """Titles are stripped of control characters and stay single-line (C5)."""
        digest = approved_week_digest(self._approved_row(title="Safe\x07\x1b title"))

        assert "Safe title" in digest
        assert "\r" not in digest
        assert "\x1b" not in digest

    def test_a_line_broken_title_never_leaks_its_fragments(self) -> None:
        """A title carrying \\r\\n degrades to its parseable segment — no leak."""
        digest = approved_week_digest(self._approved_row(title="Long\r\nInjected\x1b"))

        assert "\r" not in digest
        assert "\x1b" not in digest
        assert "Injected" not in digest

    def test_session_titles_collapse_to_a_single_line(self) -> None:
        """A session title with control characters collapses to one safe line."""
        days = [_raw_day(i, session_title="VO2\trepeats\x07") for i in range(7)]
        digest = approved_week_digest(self._approved_row(days=days))

        assert "VO2 repeats" in digest
        assert "\x07" not in digest

    def test_the_digest_is_token_bounded(self) -> None:
        """A maximal week never exceeds the digest character budget (C5)."""
        days = [_raw_day(i, session_title="S" * 190) for i in range(7)]
        digest = approved_week_digest(self._approved_row(days=days))

        assert len(digest) <= APPROVED_WEEK_DIGEST_MAX_CHARS

    @staticmethod
    def _build_phase() -> PeriodizationPhase:
        """The build phase of the guardrail season (#233 scenarios)."""
        return PeriodizationPhase.model_validate(
            {
                "phase_type": "build",
                "name": "Build",
                "start_date": "2026-09-28",
                "end_date": "2026-10-10",
            }
        )

    def test_the_digest_carries_the_phase_window_and_peak_date(self) -> None:
        """AC4 (#233): the adjustment context names the phase window and peak date."""
        digest = approved_week_digest(
            self._approved_row(), phase=self._build_phase(), target_date="2026-10-14"
        )

        assert "Current phase: Build (build), 2026-09-28 to 2026-10-10" in digest
        assert "Peak/target date: 2026-10-14" in digest
        assert len(digest) <= APPROVED_WEEK_DIGEST_MAX_CHARS

    def test_the_digest_facts_are_metadata_only(self) -> None:
        """The guardrail facts carry no health data — no RPE, no wellness."""
        digest = approved_week_digest(
            self._approved_row(), phase=self._build_phase(), target_date="2026-10-14"
        )

        assert "RPE" not in digest
        assert "rpe" not in digest
        assert "wellness" not in digest

    def test_without_facts_the_digest_keeps_the_225_output(self) -> None:
        """No phase/target facts → the digest is byte-identical to #225's."""
        digest = approved_week_digest(self._approved_row())

        assert "Current phase:" not in digest
        assert "Peak/target date:" not in digest

    def test_a_line_broken_phase_name_collapses_to_one_safe_line(self) -> None:
        """A phase name carrying control characters collapses to one safe line (C5).

        Unlike stored titles (split at the markdown-line level), the phase
        name arrives from the persisted plan, so the digest's single-line
        hygiene collapses it in place — no control characters survive.
        """
        phase = PeriodizationPhase.model_validate(
            {
                "phase_type": "build",
                "name": "Bad\r\nInjected\x1b",
                "start_date": "2026-09-28",
                "end_date": "2026-10-10",
            }
        )
        digest = approved_week_digest(self._approved_row(), phase=phase)

        assert "\r" not in digest
        assert "\x1b" not in digest
        assert "Current phase: Bad Injected (build), 2026-09-28 to 2026-10-10" in digest

    def test_apply_appends_the_digest_to_the_prompt(self) -> None:
        """apply_approved_week_digest composes the base prompt with the digest."""
        prompt = apply_approved_week_digest("BASE PROMPT", self._approved_row())

        assert prompt.startswith("BASE PROMPT\n\n")
        assert "2026-W41" in prompt

    def test_apply_forwards_the_guardrail_facts(self) -> None:
        """apply_approved_week_digest passes the phase/peak facts through (#233)."""
        prompt = apply_approved_week_digest(
            "BASE PROMPT", self._approved_row(), phase=self._build_phase(), target_date="2026-10-14"
        )

        assert "Current phase: Build (build), 2026-09-28 to 2026-10-10" in prompt
        assert "Peak/target date: 2026-10-14" in prompt


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
