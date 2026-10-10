"""Periodization phasing — macrocycle phases, calendar resolution (#167).

The macrocycle phase plan (epic #161 story 1.2) hangs off the active
objective: an ordered sequence of non-overlapping date ranges (base → build
→ peak → taper → recovery → competition) covering the window to the
objective's target date. The current phase is computed from the calendar —
``f(today, phases)``, no stored state, no scheduler (KIS) — and feeds the
coach digest with the current phase, the target countdown and the phase's
load-scaling hint, so weekly load recommendations scale with the phase.

Persistence follows MADR-008: the phases live in the ``periodization_phases``
table (C1 — plaintext, tactical coaching state), FK-cascaded onto their
parent ``athlete_objectives`` row (a phase is meaningless without its
objective). The plan is replaced wholesale per objective (PUT semantics).
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import CheckConstraint, Float, ForeignKey, Text, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.db import Base
from coach_web.models import validate_iso_date

PHASE_TYPES = ("base", "build", "peak", "taper", "recovery", "competition")
"""MADR-008 ``phase_type`` CHECK enum."""

PhaseType = Literal["base", "build", "peak", "taper", "recovery", "competition"]

MAX_PHASES = 24
"""Cap on the phases per objective (KIS — a macrocycle is a handful of phases)."""

MAX_PHASE_NAME_LENGTH = 200
"""Cap on a single phase name."""

MAX_PHASE_FOCUS_LENGTH = 200
"""Cap on the optional focus line."""

MAX_PHASE_NOTES_LENGTH = 2000
"""Cap on the optional free-form notes."""

PHASE_LOAD_HINTS: dict[PhaseType, str] = {
    "base": "aerobic base, high volume, low intensity",
    "build": "threshold development, moderate volume, rising intensity",
    "peak": "race-specific work, reduced volume, high intensity",
    "taper": "taper, rapidly reducing volume, sharpening intensity",
    "recovery": "recovery, minimal volume and intensity",
    "competition": "competition, race execution, hold sharpness",
}
"""Phase-type → intensity/volume modifier hints for the coach prompt (AC2).

The plan-generation prompt scales weekly load with the CURRENT phase through
these hints; the map is contract-tested against the MADR-008 enum so a new
phase type cannot silently ship without a scaling hint.
"""


class PeriodizationPhase(BaseModel):
    """One macrocycle phase of the periodization plan (MADR-008 C1)."""

    phase_type: PhaseType = Field(
        ...,
        description="Phase type per the MADR-008 CHECK enum",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PHASE_NAME_LENGTH,
        description="Human-readable phase name",
    )
    start_date: str = Field(
        ...,
        description="Phase start date (ISO YYYY-MM-DD, inclusive)",
    )
    end_date: str = Field(
        ...,
        description="Phase end date (ISO YYYY-MM-DD, inclusive)",
    )
    focus: str | None = Field(
        default=None,
        max_length=MAX_PHASE_FOCUS_LENGTH,
        description="Optional training focus, e.g. aerobic base, long rides",
    )
    weekly_hours_target: float | None = Field(
        default=None,
        ge=0,
        le=168,
        description="Optional weekly hours target (0–168)",
    )
    notes: str | None = Field(
        default=None,
        max_length=MAX_PHASE_NOTES_LENGTH,
        description="Optional free-form notes",
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def _validate_dates(cls, value: str | None) -> str | None:
        """Validate the phase dates as real ISO calendar dates (shared rule).

        The annotation mirrors the shared validator's Optional contract; the
        fields themselves are required ``str``, so ``None`` never arrives.
        """
        return validate_iso_date(value)

    @model_validator(mode="after")
    def _validate_date_order(self) -> "PeriodizationPhase":
        """Reject an inverted range (mirrors the DB CHECK constraint)."""
        if date.fromisoformat(self.start_date) > date.fromisoformat(self.end_date):
            raise ValueError("start_date must not be after end_date")
        return self


class PeriodizationPlan(BaseModel):
    """The objective's periodization plan — its ordered macrocycle phases.

    Cross-phase invariant (AC3): the phases are monotonic and
    non-overlapping — each phase must start strictly after its predecessor
    ends (inclusive date ranges may never share a day). The coverage
    cross-check against the parent objective's target date needs external
    context and lives in :func:`validate_plan_coverage`.
    """

    phases: list[PeriodizationPhase] = Field(
        default_factory=list,
        max_length=MAX_PHASES,
        description="Ordered macrocycle phases (earliest first)",
    )

    @model_validator(mode="after")
    def _validate_monotonic_non_overlapping(self) -> "PeriodizationPlan":
        """Reject overlapping or out-of-order phases.

        ``next.start > prev.end`` enforces both properties at once: it
        forbids shared days (non-overlapping) and implies strictly increasing
        starts (monotonic), so even disjoint out-of-order ranges are caught.
        """
        for previous, following in zip(self.phases, self.phases[1:], strict=False):
            if date.fromisoformat(following.start_date) <= date.fromisoformat(previous.end_date):
                raise ValueError("phases must be monotonic and non-overlapping")
        return self


class PeriodizationPlanResponse(BaseModel):
    """Response payload for the periodization endpoints."""

    objective_id: int | None = Field(
        default=None,
        description="Parent objective id, null when no objective is active",
    )
    phases: list[PeriodizationPhase] = Field(
        default_factory=list,
        description="Ordered macrocycle phases of the active objective",
    )


class PhaseStatus(BaseModel):
    """Calendar-computed periodization state for the coach digest (#167).

    Purely computed — no stored state, no scheduler (KIS): ``phase`` is the
    phase containing ``today`` (None when today falls outside every phase),
    ``days_to_target`` the signed calendar-day countdown to the parent
    objective's target date (None when the objective has no target date).
    """

    phase: PeriodizationPhase | None = Field(
        default=None,
        description="The phase containing today, None when today is outside the plan",
    )
    days_to_target: int | None = Field(
        default=None,
        description="Signed days from today to the objective target date",
    )


def validate_plan_coverage(plan: PeriodizationPlan, target_date: str | None) -> None:
    """Raise ValueError unless the plan spans the objective's target date.

    The phases must cover the window to the objective's target date: the
    target must fall within ``[first start, last end]``. An empty plan (no
    periodization) or an objective without a target date skips the check —
    clearing the phases is always legitimate.
    """
    if target_date is None or not plan.phases:
        return
    target = date.fromisoformat(target_date)
    first_start = date.fromisoformat(
        min(plan.phases, key=lambda phase: phase.start_date).start_date
    )
    last_end = date.fromisoformat(max(plan.phases, key=lambda phase: phase.end_date).end_date)
    if first_start > target or last_end < target:
        raise ValueError("phases must cover the window to the objective target date")


def resolve_phase_status(
    phases: Sequence[PeriodizationPhase],
    target_date: str | None,
    today: date,
) -> PhaseStatus:
    """Compute the current phase and target countdown from the calendar.

    ``f(today, phases)`` — no stored state, no scheduler. ``today`` is
    injected so callers and frozen-clock tests control the clock. Phase
    boundaries are inclusive: a phase contains ``today`` when
    ``start_date <= today <= end_date``.
    """
    current: PeriodizationPhase | None = None
    for phase in phases:
        if date.fromisoformat(phase.start_date) <= today <= date.fromisoformat(phase.end_date):
            current = phase
            break
    days = None if target_date is None else (date.fromisoformat(target_date) - today).days
    return PhaseStatus(phase=current, days_to_target=days)


class PeriodizationPhaseRow(Base):
    """ORM model for the MADR-008 ``periodization_phases`` table (C1, plaintext)."""

    __tablename__ = "periodization_phases"
    __table_args__ = (
        CheckConstraint(
            "phase_type IN ('base', 'build', 'peak', 'taper', 'recovery', 'competition')",
            name="ck_periodization_phases_phase_type",
        ),
        CheckConstraint("start_date <= end_date", name="ck_periodization_phases_date_order"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    objective_id: Mapped[int] = mapped_column(
        ForeignKey("athlete_objectives.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    phase_type: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[str] = mapped_column(Text, nullable=False)
    end_date: Mapped[str] = mapped_column(Text, nullable=False)
    focus: Mapped[str | None] = mapped_column(Text, nullable=True)
    weekly_hours_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


def _phase_from_row(row: PeriodizationPhaseRow) -> PeriodizationPhase:
    """Rebuild a phase from a persisted row.

    ``model_validate`` re-runs the strict Pydantic validation over the stored
    values; the DB CHECK constraints guarantee the enum and date-order rules
    pass.
    """
    return PeriodizationPhase.model_validate(
        {
            "phase_type": row.phase_type,
            "name": row.name,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "focus": row.focus,
            "weekly_hours_target": row.weekly_hours_target,
            "notes": row.notes,
        }
    )


async def list_phases(session: AsyncSession, objective_id: int) -> list[PeriodizationPhase]:
    """Return the objective's phases ordered by start date (then id)."""
    result = await session.execute(
        select(PeriodizationPhaseRow)
        .where(PeriodizationPhaseRow.objective_id == objective_id)
        .order_by(PeriodizationPhaseRow.start_date, PeriodizationPhaseRow.id)
    )
    return [_phase_from_row(row) for row in result.scalars().all()]


async def replace_phases(
    session: AsyncSession, objective_id: int, plan: PeriodizationPlan
) -> list[PeriodizationPhase]:
    """Replace the objective's phase set wholesale (PUT semantics) and commit."""
    await session.execute(
        delete(PeriodizationPhaseRow).where(PeriodizationPhaseRow.objective_id == objective_id)
    )
    now = _utc_now_iso()
    for phase in plan.phases:
        session.add(
            PeriodizationPhaseRow(
                objective_id=objective_id,
                phase_type=phase.phase_type,
                name=phase.name,
                start_date=phase.start_date,
                end_date=phase.end_date,
                focus=phase.focus,
                weekly_hours_target=phase.weekly_hours_target,
                notes=phase.notes,
                created_at=now,
                updated_at=now,
            )
        )
    await session.commit()
    return list(plan.phases)


async def delete_phases(session: AsyncSession, objective_id: int) -> int:
    """Remove every phase of the objective; returns the number removed."""
    existing = await list_phases(session, objective_id)
    if not existing:
        return 0
    await session.execute(
        delete(PeriodizationPhaseRow).where(PeriodizationPhaseRow.objective_id == objective_id)
    )
    await session.commit()
    return len(existing)
