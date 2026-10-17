"""Planned-vs-executed analysis — ICU compliance mirrors, GET budget, summary card.

Story 3.2 (epic #161) closes the coaching lifecycle loop: when the athlete
asks "how did my week go?", the coach pulls the ICU compliance data through
the coach-mcp tools and answers with a grounded analysis plus a debrief
summary card. This module is the analysis half of the story:

- **Contract mirrors** — strict Pydantic mirrors of the upstream coach-mcp
  whitelist shapes (:class:`ComplianceReportMirror` for
  ``icu_get_workout_compliance``, :class:`CompletedSessionsPageMirror` for
  ``icu_get_completed_sessions``). The hub hands tool results over as JSON
  strings (``response_format: "json"`` is injected by the hub); every parse
  failure — error payload, malformed JSON, missing field — yields ``None``
  so a failed read can never produce a card (AC1: no fabricated numbers).
- **GET budget** — PO decision 5a (#190): at most FOUR ICU GETs per user
  action. The per-tool costs are the upstream contracts (compliance = 2
  parallel GETs, completed sessions / wellness / trends = 1 each); the
  documented analysis flow (compliance + completed sessions = 3) fits, and
  :class:`IcuReadBudget` refuses — before execution — any call that would
  exceed the cap.
- **Summary card** — the #165 debrief-summary contract adapted to the
  weekly flow: one row per planned session with its adherence
  classification, ICU's own compliance score, the structure verdict and the
  executed load joined from the sessions page by date. The planned-load
  side stays unclaimed (the whitelisted upstream contract exposes no
  planned load) — the card renders it as absent rather than fabricating a
  number. Severity is assigned server-side and deterministically; the
  client only renders the tokenized chip (#165: never color alone).

nLPD posture: everything here operates on the whitelisted tool output only —
raw ICU payloads never reach this layer, and the card carries summary
fields exclusively.

Weekly adherence metric (#234, epic #213 S4): the pure half of the weekly
plan-adherence metric lives here too — :class:`WeekAdherenceMetric` (the
strict counts/percentage schema), :func:`iso_week_key` (the ISO week
identity) and :func:`compute_week_adherence` (planned vs completed session
keys → metric, ``None`` for zero-planned weeks). The persistence half (the
C3 ``week_adherence`` row and the debrief-save upsert) lives in
:mod:`coach_web.debrief` — the debrief-save path is the metric's
event-driven write trigger.
"""

import json
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

ICU_GET_BUDGET = 4
"""PO decision 5a (#190): at most four ICU GETs per user action."""

ICU_TOOL_GET_COSTS: dict[str, int] = {
    "icu_get_workout_compliance": 2,
    "icu_get_completed_sessions": 1,
    "icu_get_wellness": 1,
    "icu_get_trends": 1,
}
"""Raw tool name → ICU GETs issued per call (the upstream #82 contracts).

``icu_get_workout_compliance`` issues exactly two GETs (planned events +
executed activities, in parallel); the single-read tools issue one each.
``icu_add_session_comment`` is a write and costs no GET."""

NAMESPACE_SEPARATOR = "__"
"""Separator the MCP hub uses to namespace tool names as ``<server><sep><tool>``."""

BUDGET_EXHAUSTED_MESSAGE = (
    "ICU read budget exhausted for this action: {used} of {budget} GETs already "
    "used and {tool} would need {cost} more. Summarise from the data already "
    "fetched instead of calling further ICU read tools."
)
"""Model-facing budget-refusal text (counts only — no athlete data, nLPD)."""


class ComplianceEventMirror(BaseModel):
    """Mirror of the upstream ``WorkoutComplianceEvent`` whitelist shape."""

    event_id: str = Field(..., description="Planned calendar event ID.")
    date: str = Field(..., description="Planned event date (YYYY-MM-DD).")
    name: str = Field(..., description="Planned workout title.")
    status: Literal["completed", "missed", "upcoming"]
    compliance: float | None = Field(
        default=None, description="ICU's own planned-vs-executed score for the pair."
    )
    structure_diff: Literal["match", "mismatch", "unavailable"]


class ComplianceReportMirror(BaseModel):
    """Mirror of the upstream ``WorkoutComplianceReport`` whitelist shape."""

    oldest: str = Field(..., description="Reported range start (YYYY-MM-DD).")
    newest: str = Field(..., description="Reported range end (YYYY-MM-DD).")
    total_planned: int = Field(..., ge=0)
    completed: int = Field(..., ge=0)
    missed: int = Field(..., ge=0)
    upcoming: int = Field(..., ge=0)
    adherence_rate: float | None = Field(default=None)
    events: list[ComplianceEventMirror] = Field(default_factory=list)


class CompletedSessionMirror(BaseModel):
    """Mirror of the upstream ``CompletedSessionSummary`` whitelist shape."""

    id: str = Field(..., description="Intervals.icu activity ID.")
    date: str = Field(..., description="Session date (YYYY-MM-DD).")
    type: str = Field(..., description="Activity type (e.g. 'Ride', 'VirtualRide').")
    title: str = Field(..., description="Session title.")
    duration_seconds: float | None = Field(default=None)
    load: float | None = Field(default=None, description="ICU-provided training load (TSS).")


class CompletedSessionsPageMirror(BaseModel):
    """Mirror of the upstream ``CompletedSessionsPage`` whitelist shape."""

    oldest: str = Field(..., description="Reported range start (YYYY-MM-DD).")
    newest: str = Field(..., description="Reported range end (YYYY-MM-DD).")
    activity_type: str | None = Field(default=None)
    fetched: int = Field(..., ge=0)
    returned: int = Field(..., ge=0)
    sessions: list[CompletedSessionMirror] = Field(default_factory=list)


def _parse_mirror[T: BaseModel](model: type[T], result: str) -> T | None:
    """Parse one hub tool result into *model*, degrading to ``None`` on failure.

    The hub hands coach-tool results over as JSON strings (and as
    ``{"error": ...}`` JSON on any failure). Every failure mode — malformed
    JSON, non-object payload, schema drift — degrades to ``None`` so the
    caller can never mistake a failed read for fetched data.
    """
    try:
        parsed = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return model.model_validate(parsed)
    except ValueError:
        return None


def parse_compliance_report(result: str) -> ComplianceReportMirror | None:
    """Parse an ``icu_get_workout_compliance`` result into the report mirror."""
    return _parse_mirror(ComplianceReportMirror, result)


def parse_completed_sessions(result: str) -> CompletedSessionsPageMirror | None:
    """Parse an ``icu_get_completed_sessions`` result into the page mirror."""
    return _parse_mirror(CompletedSessionsPageMirror, result)


def icu_tool_get_cost(tool_name: str) -> int | None:
    """Return the ICU GET cost of *tool_name*, or ``None`` for non-ICU tools.

    The hub presents tools namespaced as ``<server>__<tool>``; the lookup
    strips the namespace so both the namespaced and raw forms resolve.
    """
    raw_name = tool_name.rsplit(NAMESPACE_SEPARATOR, 1)[-1]
    return ICU_TOOL_GET_COSTS.get(raw_name)


class IcuReadBudget:
    """Per-user-action ICU GET budget (PO decision 5a, #190 AC2).

    One instance lives for one agent run (one user action). Refusal is a
    decision, not a charge: a call refused by :meth:`would_exceed` is never
    executed and never consumes budget.
    """

    def __init__(self, budget: int = ICU_GET_BUDGET) -> None:
        """Start an empty budget with the documented cap."""
        self.budget = budget
        self.used = 0

    def would_exceed(self, cost: int) -> bool:
        """Return True when spending *cost* would push the total past the cap."""
        return self.used + cost > self.budget

    def consume(self, cost: int) -> None:
        """Charge *cost* GETs to this action's budget."""
        self.used += cost


class SessionDebriefRow(BaseModel):
    """One planned session's planned-vs-executed outcome row (#165 contract).

    The planned-vs-executed read per session is ICU's own: the adherence
    classification (completed / missed / upcoming), the native compliance
    score of the paired activity, and the structure verdict. ``executed_tss``
    joins from the completed-sessions page by date when that page was
    fetched; ``planned_tss``/``load_delta_pct`` stay None in this flow — the
    whitelisted upstream contract exposes no planned load, and the card
    never fabricates one.
    """

    session_date: str = Field(..., description="Planned session date (YYYY-MM-DD).")
    session_title: str = Field(..., description="Planned workout title.")
    status: Literal["completed", "missed", "upcoming"]
    compliance_pct: float | None = Field(
        default=None, description="ICU's own compliance score (completed sessions)."
    )
    structure_diff: Literal["match", "mismatch", "unavailable"]
    planned_tss: float | None = Field(
        default=None,
        description=(
            "Planned load — unclaimed in this flow (no whitelisted upstream "
            "source); rendered as absent, never fabricated."
        ),
    )
    executed_tss: float | None = Field(
        default=None, description="Executed load joined from the completed-sessions page."
    )
    load_delta_pct: float | None = Field(
        default=None,
        description=(
            "Load delta — unclaimed in this flow (needs both load sides); "
            "rendered as absent, never fabricated."
        ),
    )
    severity: Literal["ok", "warn", "critical"] = Field(
        ..., description="Server-assigned severity chip (word + tokenized color)."
    )


class DebriefSummaryCard(BaseModel):
    """The debrief summary card payload (#165 contract, weekly flow).

    Emitted as the SSE ``debrief_summary`` event data under the ``summary``
    key. Purely informational client-side — no actions, no editing.
    """

    oldest: str = Field(..., description="Analyzed range start (YYYY-MM-DD).")
    newest: str = Field(..., description="Analyzed range end (YYYY-MM-DD).")
    total_planned: int = Field(..., ge=0)
    completed: int = Field(..., ge=0)
    missed: int = Field(..., ge=0)
    upcoming: int = Field(..., ge=0)
    adherence_rate: float | None = Field(default=None)
    sessions: list[SessionDebriefRow] = Field(
        default_factory=list,
        description="Per-session planned-vs-executed rows, chronological.",
    )
    headline: str = Field(
        ..., description="Fixed-format plain-language read derived from the report numbers."
    )


def _row_severity(status: str, structure_diff: str) -> Literal["ok", "warn", "critical"]:
    """Assign the row severity deterministically (server-side, #165).

    A missed session is the critical outcome (the planned work did not
    happen); a completed session whose executed structure diverged from the
    plan is a warning; everything else is ok. The client never
    re-classifies — it renders the chip it is given.
    """
    if status == "missed":
        return "critical"
    if status == "completed" and structure_diff == "mismatch":
        return "warn"
    return "ok"


def _session_headline(report: ComplianceReportMirror) -> str:
    """Build the fixed-format plain-language headline from the report numbers."""
    if report.adherence_rate is not None:
        return (
            f"You planned {report.total_planned} workout(s) and completed "
            f"{report.completed} — {report.adherence_rate:.1f}% adherence "
            f"({report.missed} missed)."
        )
    return f"{report.upcoming} planned workout(s) upcoming — nothing rateable yet."


def build_debrief_summary_card(
    report: ComplianceReportMirror,
    sessions: CompletedSessionsPageMirror | None,
) -> DebriefSummaryCard:
    """Build the summary card from the fetched ICU data only (AC1).

    Every row is traceable to a fetched compliance event; the executed load
    joins from the fetched sessions page by date when that page exists. No
    number on the card is computed from anything the agent claimed.
    """
    executed_by_date: Mapping[str, float] = (
        {session.date: session.load for session in sessions.sessions if session.load is not None}
        if sessions is not None
        else {}
    )
    rows = [
        SessionDebriefRow(
            session_date=event.date,
            session_title=event.name,
            status=event.status,
            compliance_pct=event.compliance,
            structure_diff=event.structure_diff,
            executed_tss=executed_by_date.get(event.date),
            severity=_row_severity(event.status, event.structure_diff),
        )
        for event in sorted(report.events, key=lambda event: event.date)
    ]
    return DebriefSummaryCard(
        oldest=report.oldest,
        newest=report.newest,
        total_planned=report.total_planned,
        completed=report.completed,
        missed=report.missed,
        upcoming=report.upcoming,
        adherence_rate=report.adherence_rate,
        sessions=rows,
        headline=_session_headline(report),
    )


# ---------------------------------------------------------------------------
# Weekly adherence metric (#234 — epic #213 S4)
# ---------------------------------------------------------------------------

WEEK_KEY_PATTERN = r"^\d{4}-W\d{2}$"
"""ISO week key shape (``YYYY-Www``) — the metric row's week identity."""


class WeekAdherenceMetric(BaseModel):
    """One ISO week's plan-adherence metric (#234 AC1).

    COUNTS AND PERCENTAGES ONLY (MADR-008 C3): the metric is derived from
    existing DB compliance data — planned sessions from approved plan
    drafts' day rows, completed sessions from the athlete's debrief rows —
    and never carries health content (no RPE, no notes, no wellness values).
    """

    week_key: str = Field(..., pattern=WEEK_KEY_PATTERN, description="ISO week key (YYYY-Www).")
    planned_count: int = Field(
        ..., ge=0, strict=True, description="Planned sessions in the week (approved drafts)."
    )
    completed_count: int = Field(
        ..., ge=0, strict=True, description="Debriefed planned sessions (this athlete)."
    )
    adherence_pct: float = Field(
        ..., ge=0, le=100, description="Completed / planned sessions, in percent."
    )


def iso_week_key(day: date) -> str:
    """Return the ISO week key (``YYYY-Www``) of *day* (#234).

    Monday..Sunday of one ISO week share one key; ISO-year boundaries
    (e.g. 2026-12-28 → ``2026-W53``) follow :meth:`date.isocalendar`.
    """
    iso_year, iso_week, _ = day.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def compute_week_adherence(
    week_key: str,
    planned_keys: Iterable[str],
    completed_keys: Iterable[str],
) -> WeekAdherenceMetric | None:
    """Compute one ISO week's adherence from session keys (#234 AC1).

    ``planned_keys`` are the week's planned session keys (approved draft day
    rows); ``completed_keys`` are the athlete's debriefed session keys. The
    completed count is the INTERSECTION with the planned keys — a debrief
    for a session the plan no longer contains never inflates the metric, so
    adherence can never exceed 100%. A week with zero planned sessions
    yields ``None`` — no metric row (0/0 noise avoided).
    """
    planned = set(planned_keys)
    if not planned:
        return None
    completed = len(planned.intersection(completed_keys))
    return WeekAdherenceMetric(
        week_key=week_key,
        planned_count=len(planned),
        completed_count=completed,
        adherence_pct=round(completed / len(planned) * 100, 1),
    )
