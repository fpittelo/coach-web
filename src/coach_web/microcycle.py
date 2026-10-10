"""Weekly microcycle — draft model, state machine, persistence, approval (#168).

The coach proposes a structured 7-day microcycle (epic #161 story 2.1) as a
validated :class:`WeeklyPlanDraft`, the UI renders it as a weekly plan card
(#165 contract), and approval writes one Intervals.icu event per day through
``coach-mcp`` (``intervals_create_event`` — no batch tool exists, #163). The
write is deliberately NOT atomic: every day yields its own
:class:`DayWriteResult`, a partial failure stays retryable (the draft stays
``submitted``), and only a fully successful week transitions to ``approved``.

Persistence follows MADR-008: drafts live in the ``plan_drafts`` table
(C3 — plaintext, operational workflow state) with the draft state machine
``draft → submitted → approved/rejected``, and ``superseded`` when a new
proposal replaces an existing draft for the same week. The summary counts are
NEVER trusted from the agent: the Pydantic model re-verifies ``total_tss``
against the day sum and the hard/easy/rest partition server-side (#165
model_validator posture).
"""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import CheckConstraint, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.db import Base
from coach_web.models import _validate_iso_week, validate_iso_date
from coach_web.plan_approval import (
    COACH_CREATE_EVENT_TOOL,
    EVENT_START_TIME,
    ToolExecutor,
)

logger = logging.getLogger(__name__)

DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
"""ISO weekday names, Monday-first (``isocalendar().weekday - 1`` indexes)."""

DayOfWeek = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

MAX_WEEK_DAYS = 7
"""A microcycle spans at most 7 day slots (#165 sketch)."""

MAX_ZONES_PER_DAY = 5
"""Cap on the zones list per day (#165 sketch)."""

MAX_SESSION_TITLE_LENGTH = 200
"""Cap on a single session title."""

MAX_FOCUS_LENGTH = 200
"""Cap on the optional focus line."""

MAX_ZONE_LENGTH = 20
"""Cap on a single zone label."""

MAX_TEXT_LENGTH = 2000
"""Cap on the summary/rationale free text."""

TOTAL_TSS_TOLERANCE = 0.05
"""Float-noise tolerance for the declared-vs-computed total TSS check."""

ACTIVE_STATUSES: tuple[str, ...] = ("draft", "submitted")
"""Statuses of a draft that can still be reused by an approval request."""

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "superseded"}),
    "submitted": frozenset({"approved", "rejected", "superseded"}),
    "approved": frozenset({"superseded"}),
    "rejected": frozenset({"submitted", "superseded"}),
    "superseded": frozenset(),
}
"""Draft state machine: draft → submitted → approved/rejected; superseded on
replacement; a REJECTED draft returns to ``submitted`` on re-approval (the
athlete changed their mind — the write is allowed again, PR #174 F3).
``superseded`` is terminal (retention pruning is operational, out of scope
here)."""


class DaySlot(BaseModel):
    """One day of a proposed weekly microcycle (#165 sketch)."""

    day_of_week: DayOfWeek = Field(..., description="ISO weekday name, Monday-first")
    date: str = Field(..., description="ISO calendar date (YYYY-MM-DD)")
    session_title: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SESSION_TITLE_LENGTH,
        description="Session title, e.g. 'VO2 intervals' or 'Rest day'",
    )
    focus: str | None = Field(
        default=None,
        max_length=MAX_FOCUS_LENGTH,
        description="Optional focus line, e.g. 'Threshold zones'",
    )
    zones: list[str] = Field(
        default_factory=list,
        max_length=MAX_ZONES_PER_DAY,
        description="Training zones, e.g. ['Z4', 'Z5']",
    )
    planned_tss: float = Field(default=0, ge=0, description="Planned training stress")
    duration_minutes: float = Field(default=0, ge=0, description="Planned duration")
    rest_day: bool = Field(default=False, description="True when the day is a rest day")

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str) -> str:
        """Validate the day date as a real ISO calendar date (shared rule)."""
        return validate_iso_date(value) or value

    @field_validator("zones")
    @classmethod
    def _validate_zones(cls, value: list[str]) -> list[str]:
        """Strip zone labels and reject blank or oversized entries."""
        stripped = [item.strip() for item in value]
        if any(not item for item in stripped):
            raise ValueError("zones must not be blank")
        if any(len(item) > MAX_ZONE_LENGTH for item in stripped):
            raise ValueError(f"zones are capped at {MAX_ZONE_LENGTH} characters")
        return stripped


class WeeklyPlanDraft(BaseModel):
    """A 7-day microcycle awaiting approval (#165 sketch).

    Emitted as the SSE ``week_plan`` event; approved as one POST — NOT atomic
    (#163). The summary counts are agent-computed but server-verified: the
    model validators re-derive ``total_tss`` from the day slots and enforce
    the hard/easy/rest partition, so a hallucinated summary can never render.
    """

    week_id: str = Field(..., description="ISO week identifier (YYYY-WNN)")
    title: str = Field(
        ..., min_length=1, max_length=MAX_SESSION_TITLE_LENGTH, description="Week title"
    )
    summary: str = Field(
        ..., min_length=1, max_length=MAX_TEXT_LENGTH, description="One-paragraph week summary"
    )
    rationale: str | None = Field(
        default=None,
        max_length=MAX_TEXT_LENGTH,
        description="Why this week is proposed, grounded in objectives/phase/availability",
    )
    days: list[DaySlot] = Field(
        ...,
        min_length=1,
        max_length=MAX_WEEK_DAYS,
        description="Day slots in weekly order",
    )
    total_tss: float = Field(..., ge=0, description="Declared week total TSS")
    hard_days: int = Field(..., ge=0, le=MAX_WEEK_DAYS, description="Hard day count")
    easy_days: int = Field(..., ge=0, le=MAX_WEEK_DAYS, description="Easy day count")

    @field_validator("week_id")
    @classmethod
    def _validate_week_id(cls, value: str) -> str:
        return _validate_iso_week(value)

    @model_validator(mode="after")
    def _check_summary_counts(self) -> "WeeklyPlanDraft":
        """Re-verify the agent-computed summary counts (#165 posture).

        ``total_tss`` must equal Σ ``planned_tss`` (within float tolerance)
        and hard + easy + rest days must partition ``len(days)`` — the rest
        count is derived from the day slots, the only ground truth.
        """
        computed = sum(day.planned_tss for day in self.days)
        if abs(computed - self.total_tss) > TOTAL_TSS_TOLERANCE:
            raise ValueError("total_tss must equal the sum of the day planned_tss values")
        rest_days = sum(1 for day in self.days if day.rest_day)
        if self.hard_days + self.easy_days + rest_days != len(self.days):
            raise ValueError("hard_days, easy_days and rest days must partition the week's days")
        return self

    @model_validator(mode="after")
    def _check_days_within_week(self) -> "WeeklyPlanDraft":
        """Every day date must fall inside week_id's ISO week, weekday-aligned.

        The calendar write uses each day's ``date`` verbatim, so a draft with
        a stray date (or a mismatched ``day_of_week`` label) would schedule
        events on the wrong days — the check keeps the rendered card and the
        written calendar honest. The message is deliberately VALUE-FREE: it
        is surfaced to the model for self-correction (nLPD posture).
        """
        year, week = self.week_id.split("-W")
        for day in self.days:
            iso = date.fromisoformat(day.date).isocalendar()
            if (iso.year, iso.week) != (int(year), int(week)):
                raise ValueError(
                    "every day date must fall within week_id's ISO week "
                    "and match its day_of_week"
                )
            if DAY_NAMES[iso.weekday - 1] != day.day_of_week:
                raise ValueError(
                    "every day date must fall within week_id's ISO week "
                    "and match its day_of_week"
                )
        return self


class DayWriteResult(BaseModel):
    """Per-day outcome of the week-approval POST (#163: batch is not atomic)."""

    date: str = Field(..., description="ISO calendar date of the day slot")
    success: bool = Field(..., description="Whether the calendar event was created")
    error: str | None = Field(
        default=None,
        description="Failure reason when the day write did not succeed",
    )


class WeekApprovalRequest(BaseModel):
    """Payload submitted by the UI when the athlete approves a weekly plan.

    ``dates`` is ``None`` for a full approval or the failed-days subset for a
    retry (#165 retry-failed-days affordance).
    """

    week: WeeklyPlanDraft = Field(..., description="The weekly plan draft to approve")
    dates: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_WEEK_DAYS,
        description="None = all days; a subset = retry the failed days only",
    )

    @field_validator("dates")
    @classmethod
    def _validate_dates(cls, value: list[str] | None) -> list[str] | None:
        """Validate the subset dates and reject duplicates (no double writes)."""
        if value is None:
            return None
        for item in value:
            validate_iso_date(item)
        if len(set(value)) != len(value):
            raise ValueError("dates must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _check_dates_within_week(self) -> "WeekApprovalRequest":
        """Every requested date must be a day of the proposed week."""
        if self.dates is not None:
            known = {day.date for day in self.week.days}
            unknown = [item for item in self.dates if item not in known]
            if unknown:
                raise ValueError("dates must be days of the proposed week")
        return self


class WeekApprovalResponse(BaseModel):
    """Aggregated per-day outcome of a weekly plan approval."""

    results: list[DayWriteResult] = Field(
        ...,
        min_length=1,
        max_length=MAX_WEEK_DAYS,
        description="Per-day write outcomes, in week order",
    )


class PlanDraftRow(Base):
    """ORM model for the MADR-008 ``plan_drafts`` table (C3, plaintext)."""

    __tablename__ = "plan_drafts"
    # The status CHECK enum below is the ORM mirror of migration 0003's
    # CHECK (both hardcoded deliberately — migrations stay self-contained,
    # never importing application symbols) and of the MADR-008 schema
    # sketch; keep the three in sync when the lifecycle changes.
    __table_args__ = (
        CheckConstraint(
            "week_start_date <= week_end_date",
            name="ck_plan_drafts_week_range",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'superseded')",
            name="ck_plan_drafts_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    week_start_date: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    week_end_date: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="draft", server_default="draft"
    )
    approved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_issue_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


def week_bounds(week_id: str) -> tuple[str, str]:
    """Return the (Monday, Sunday) ISO date pair of an ISO week identifier."""
    year, week = week_id.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    sunday = date.fromisocalendar(int(year), int(week), 7)
    return monday.isoformat(), sunday.isoformat()


def _format_duration(minutes: float) -> str:
    """Format a duration in minutes using the compact ``15m`` notation."""
    return f"{minutes:g}m"


def build_week_markdown(draft: WeeklyPlanDraft) -> str:
    """Render the weekly draft as the persisted ``plan_drafts.content`` document."""
    lines = [
        f"# {draft.title}",
        "",
        f"- **Week:** {draft.week_id}",
        f"- **Total TSS:** {draft.total_tss:g}",
        f"- **Distribution:** {draft.hard_days} hard / {draft.easy_days} easy",
        "",
        "## Summary",
        "",
        draft.summary,
    ]
    if draft.rationale:
        lines += ["", "## Rationale", "", draft.rationale]
    lines += [
        "",
        "## Days",
        "",
        "| Day | Date | Session | Focus/Zones | TSS | Duration |",
        "|:---|:---|:---|:---|:---|:---|",
    ]
    for day in draft.days:
        focus_bits = [bit for bit in (day.focus, "–".join(day.zones) if day.zones else "") if bit]
        focus = " ".join(focus_bits) or "—"
        load = "—" if day.rest_day else f"{day.planned_tss:g}"
        duration = "—" if day.rest_day else _format_duration(day.duration_minutes)
        lines.append(
            f"| {day.day_of_week} | {day.date} | {day.session_title} | {focus} "
            f"| {load} | {duration} |"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Repository (MADR-008 plan_drafts)
# ---------------------------------------------------------------------------


async def list_drafts_by_week(session: AsyncSession, week_start_date: str) -> list[PlanDraftRow]:
    """Return every draft row of the week, oldest first."""
    result = await session.execute(
        select(PlanDraftRow)
        .where(PlanDraftRow.week_start_date == week_start_date)
        .order_by(PlanDraftRow.id)
    )
    return list(result.scalars().all())


async def _supersede_rows(session: AsyncSession, rows: list[PlanDraftRow]) -> None:
    """Move the given rows to the terminal ``superseded`` status (replacement)."""
    now = _utc_now_iso()
    for row in rows:
        row.status = "superseded"
        row.updated_at = now


async def transition_draft_status(
    session: AsyncSession,
    row: PlanDraftRow,
    new_status: str,
    *,
    approved_at: str | None = None,
) -> PlanDraftRow:
    """Transition the draft row's status, enforcing the state machine.

    Raises:
        ValueError: on an out-of-machine transition (the row is untouched).
    """
    if new_status not in ALLOWED_TRANSITIONS[row.status]:
        raise ValueError(f"illegal draft transition: {row.status} -> {new_status}")
    row.status = new_status
    row.updated_at = _utc_now_iso()
    if new_status == "approved":
        row.approved_at = approved_at or _utc_now_iso()
    await session.commit()
    return row


async def upsert_week_draft(session: AsyncSession, draft: WeeklyPlanDraft) -> PlanDraftRow:
    """Create-or-reuse the draft row for the week and move it to ``submitted``.

    A non-superseded row whose content matches the incoming proposal is
    REUSED — an identical re-approval (page reload mid-approval, retry)
    never duplicates the draft (AC4). A CHANGED proposal is a replacement:
    a new row is created and every previous non-superseded draft of the week
    becomes ``superseded`` (#165 replacement rule). The row lands in
    ``submitted``: the approval POST is the human-in-the-loop gate (#87 C7)
    and the write is in flight. An already-APPROVED matching row is left
    untouched — the caller short-circuits before re-issuing calendar writes
    (PR #174 F3); a REJECTED matching row is promoted back to ``submitted``.
    """
    week_start, week_end = week_bounds(draft.week_id)
    content = build_week_markdown(draft)
    rows = await list_drafts_by_week(session, week_start)
    matching = [row for row in rows if row.status != "superseded" and row.content == content]
    now = _utc_now_iso()
    if matching:
        row = matching[0]
        # Defensive: at most one matching row should exist; any extra active
        # match is superseded so the invariant cannot silently break.
        await _supersede_rows(
            session, [extra for extra in matching[1:] if extra.status in ACTIVE_STATUSES]
        )
    else:
        await _supersede_rows(session, [row for row in rows if row.status != "superseded"])
        row = PlanDraftRow(
            week_start_date=week_start,
            week_end_date=week_end,
            content=content,
            status="draft",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    if row.status in ("draft", "rejected"):
        # A fresh draft lands in submitted (approval in flight); a REJECTED
        # draft is promoted back to submitted on re-approval — the athlete
        # changed their mind, so the write is allowed again (PR #174 F3).
        row.status = "submitted"
        row.updated_at = now
    await session.commit()
    return row


# ---------------------------------------------------------------------------
# Calendar write orchestration (#163: sequential, non-atomic)
# ---------------------------------------------------------------------------


def build_day_event_arguments(day: DaySlot) -> dict[str, Any]:
    """Build the ``intervals_create_event`` argument payload for one day.

    Training days map to a ``WORKOUT`` event carrying the planned load and
    duration; rest days map to a lightweight ``NOTE`` calendar entry so the
    week renders completely on the Intervals.icu calendar (AC2: all 7 days).
    """
    arguments: dict[str, Any] = {
        "name": day.session_title,
        "type": "Ride",
        "category": "NOTE" if day.rest_day else "WORKOUT",
        "start_date_local": f"{day.date}T{EVENT_START_TIME}",
    }
    focus_bits = [bit for bit in (day.focus, "–".join(day.zones) if day.zones else "") if bit]
    if focus_bits:
        arguments["description"] = " · ".join(focus_bits)
    if day.duration_minutes > 0:
        arguments["moving_time_seconds"] = int(round(day.duration_minutes * 60))
    if day.planned_tss > 0:
        arguments["icu_training_load"] = day.planned_tss
    return arguments


def _day_result(day_date: str, raw: str) -> DayWriteResult:
    """Convert a raw tool response into a structured per-day outcome."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return DayWriteResult(date=day_date, success=False, error="Malformed tool response")
    if not isinstance(parsed, dict):
        return DayWriteResult(date=day_date, success=False, error="Malformed tool response")
    error = parsed.get("error")
    if error:
        return DayWriteResult(date=day_date, success=False, error=str(error))
    return DayWriteResult(date=day_date, success=True)


async def write_week_days(
    hub: ToolExecutor,
    week: WeeklyPlanDraft,
    dates: list[str] | None = None,
) -> list[DayWriteResult]:
    """Write one Intervals.icu event per requested day, sequentially.

    ``dates`` selects the retry-failed-days subset (``None`` = all days). A
    failing day never stops the week: every day yields its own
    :class:`DayWriteResult`, so the response reports exactly what succeeded
    and what did not (#163 — no silent partial calendar).
    """
    selected = [day for day in week.days if dates is None or day.date in set(dates)]
    results: list[DayWriteResult] = []
    for day in selected:
        try:
            raw = await hub.execute_tool(COACH_CREATE_EVENT_TOOL, build_day_event_arguments(day))
        except Exception as exc:  # noqa: BLE001 - one day must never crash the week
            logger.warning("Calendar write failed for %s: %s", day.date, exc)
            results.append(
                DayWriteResult(date=day.date, success=False, error="Calendar write failed")
            )
            continue
        results.append(_day_result(day.date, raw))
    return results
