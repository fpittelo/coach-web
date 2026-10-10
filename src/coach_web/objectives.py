"""Athlete objective profile — validation, persistence and coach digest (#166).

The profile is the coach's strategic anchor (epic #161 story 1.1): a primary
goal, optional secondary goals, the weekly availability budget and the
priority disciplines. Every generated training block must align with it, so
the agent orchestrator injects a compact, token-budgeted digest of the active
profile into the coach system prompt on every turn (absent objectives → no
digest).

Persistence follows MADR-008: the profile lives in the ``athlete_objectives``
table (C1 — plaintext, tactical coaching state) as a single active row. The
primary goal maps onto the dedicated columns; the profile-level fields
(weekly availability hours, priority disciplines, secondary goals) travel in
a JSON envelope inside the ``availability_notes`` TEXT column — JSON-in-TEXT
is the MADR-008 convention ("JSON payloads use the built-in JSON1 type
affinity (TEXT)"). This keeps a 1:1 profile↔row mapping without inventing
columns outside the ADR schema; the mapping is revisited when
``periodization_phases`` lands with its own story.
"""

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import CheckConstraint, Float, Text, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.db import Base
from coach_web.models import _validate_iso_date

OBJECTIVES_DIGEST_MAX_CHARS = 600
"""Hard character budget for the objectives digest (~150 tokens at ~4 chars/token)."""

OBJECTIVE_TYPES = ("outcome", "process", "milestone")
"""MADR-008 ``objective_type`` CHECK enum."""

OBJECTIVE_STATUSES = ("active", "achieved", "abandoned")
"""MADR-008 ``status`` CHECK enum."""

MAX_SECONDARY_GOALS = 5
"""Cap on the secondary goals list (KIS — the profile is a compact anchor)."""

MAX_PRIORITY_DISCIPLINES = 8
"""Cap on the priority disciplines list."""

MAX_DISCIPLINE_LENGTH = 50
"""Cap on a single priority discipline label."""

ObjectiveType = Literal["outcome", "process", "milestone"]
ObjectiveStatus = Literal["active", "achieved", "abandoned"]

_DIGEST_HEADER = "Athlete objectives (authoritative — align every recommendation with these goals):"


class ObjectiveGoal(BaseModel):
    """A single objective goal (primary or secondary)."""

    objective_type: ObjectiveType = Field(
        ...,
        description="Goal type per the MADR-008 CHECK enum",
    )
    title: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Human-readable goal title",
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional goal description",
    )
    target_metric: str | None = Field(
        default=None,
        max_length=100,
        description="Optional target metric, e.g. ftp, 20min_watts, event_time",
    )
    target_value: float | None = Field(
        default=None,
        ge=0,
        description="Optional target value (>= 0)",
    )
    target_date: str | None = Field(
        default=None,
        description="Optional target date (ISO YYYY-MM-DD)",
    )

    @field_validator("target_date")
    @classmethod
    def _validate_target_date(cls, value: str | None) -> str | None:
        """Validate the optional target date as a real ISO calendar date."""
        return _validate_iso_date(value)


class ObjectiveProfile(BaseModel):
    """The athlete's objective profile — the coach's strategic anchor."""

    primary_goal: ObjectiveGoal = Field(
        ...,
        description="The primary goal all training blocks align with",
    )
    secondary_goals: list[ObjectiveGoal] = Field(
        default_factory=list,
        max_length=MAX_SECONDARY_GOALS,
        description="Optional supporting goals",
    )
    weekly_availability_hours: float = Field(
        default=0.0,
        ge=0,
        le=168,
        description="Weekly training availability in hours (0–168)",
    )
    priority_disciplines: list[str] = Field(
        default_factory=list,
        max_length=MAX_PRIORITY_DISCIPLINES,
        description="Priority disciplines, e.g. road, crit, time trial",
    )

    @field_validator("priority_disciplines")
    @classmethod
    def _validate_disciplines(cls, value: list[str]) -> list[str]:
        """Strip discipline labels and reject blank or oversized entries."""
        stripped = [item.strip() for item in value]
        if any(not item for item in stripped):
            raise ValueError("priority disciplines must not be blank")
        if any(len(item) > MAX_DISCIPLINE_LENGTH for item in stripped):
            raise ValueError(
                f"priority disciplines are capped at {MAX_DISCIPLINE_LENGTH} characters"
            )
        return stripped


class ObjectiveProfileResponse(BaseModel):
    """Response payload for the objectives endpoints."""

    profile: ObjectiveProfile | None = Field(
        default=None,
        description="The persisted profile, or null when none exists yet",
    )


class AthleteObjective(Base):
    """ORM model for the MADR-008 ``athlete_objectives`` table (C1, plaintext)."""

    __tablename__ = "athlete_objectives"
    __table_args__ = (
        CheckConstraint(
            "objective_type IN ('outcome', 'process', 'milestone')",
            name="ck_athlete_objectives_objective_type",
        ),
        CheckConstraint(
            "status IN ('active', 'achieved', 'abandoned')",
            name="ck_athlete_objectives_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    objective_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_metric: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    availability_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


def _availability_payload(profile: ObjectiveProfile) -> str:
    """Serialize the profile-level fields into the availability_notes envelope."""
    return json.dumps(
        {
            "weekly_availability_hours": profile.weekly_availability_hours,
            "priority_disciplines": profile.priority_disciplines,
            "secondary_goals": [goal.model_dump() for goal in profile.secondary_goals],
        },
        ensure_ascii=False,
    )


def _parse_availability_payload(row: AthleteObjective) -> dict[str, Any]:
    """Parse the availability envelope, degrading to an empty mapping.

    The envelope is application-written; a corrupt or non-object payload
    (external tampering) degrades to defaults instead of failing the read.
    """
    if not row.availability_notes:
        return {}
    try:
        payload = json.loads(row.availability_notes)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _goal_from_row(row: AthleteObjective) -> ObjectiveGoal:
    """Rebuild the primary goal from the dedicated row columns.

    ``model_validate`` re-runs the strict Pydantic validation over the stored
    values; the DB CHECK constraints guarantee the enum fields pass.
    """
    return ObjectiveGoal.model_validate(
        {
            "objective_type": row.objective_type,
            "title": row.title,
            "description": row.description,
            "target_metric": row.target_metric,
            "target_value": row.target_value,
            "target_date": row.target_date,
        }
    )


def _profile_from_row(row: AthleteObjective) -> ObjectiveProfile:
    """Rebuild the aggregate profile from a persisted row."""
    payload = _parse_availability_payload(row)
    raw_secondaries = payload.get("secondary_goals")
    raw_disciplines = payload.get("priority_disciplines")
    return ObjectiveProfile.model_validate(
        {
            "primary_goal": _goal_from_row(row).model_dump(),
            "secondary_goals": raw_secondaries if isinstance(raw_secondaries, list) else [],
            "weekly_availability_hours": payload.get("weekly_availability_hours", 0.0),
            "priority_disciplines": raw_disciplines if isinstance(raw_disciplines, list) else [],
        }
    )


async def _active_row(session: AsyncSession) -> AthleteObjective | None:
    """Return the single active objective row (lowest id), or None."""
    result = await session.execute(
        select(AthleteObjective)
        .where(AthleteObjective.status == "active")
        .order_by(AthleteObjective.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_objective_profile(session: AsyncSession) -> ObjectiveProfile | None:
    """Return the active objective profile, or None when none is persisted."""
    row = await _active_row(session)
    if row is None:
        return None
    return _profile_from_row(row)


async def save_objective_profile(
    session: AsyncSession, profile: ObjectiveProfile
) -> ObjectiveProfile:
    """Upsert the single active objective profile row and commit.

    The profile replaces the active row's primary-goal columns and
    availability envelope wholesale (PUT semantics); ``status`` stays
    ``active`` — status transitions belong to later lifecycle stories.
    """
    row = await _active_row(session)
    now = _utc_now_iso()
    if row is None:
        row = AthleteObjective(created_at=now, status="active")
        session.add(row)
    row.objective_type = profile.primary_goal.objective_type
    row.title = profile.primary_goal.title
    row.description = profile.primary_goal.description
    row.target_metric = profile.primary_goal.target_metric
    row.target_value = profile.primary_goal.target_value
    row.target_date = profile.primary_goal.target_date
    row.availability_notes = _availability_payload(profile)
    row.updated_at = now
    await session.commit()
    return profile


def objectives_digest(profile: ObjectiveProfile) -> str:
    """Build the token-budgeted objectives digest for the coach system prompt.

    The primary-goal line is always included; every optional line (weekly
    availability, priority disciplines, each secondary goal) is appended only
    while the digest stays within ``OBJECTIVES_DIGEST_MAX_CHARS`` (~150 tokens
    at ~4 chars/token).
    """
    primary = profile.primary_goal
    head = f"Primary {primary.objective_type} goal: {primary.title}"
    target_bits: list[str] = []
    if primary.target_metric is not None and primary.target_value is not None:
        target_bits.append(f"{primary.target_metric} {primary.target_value:g}")
    if primary.target_date is not None:
        target_bits.append(f"by {primary.target_date}")
    if target_bits:
        head = f"{head} ({', '.join(target_bits)})"

    lines = [head]
    candidates = [f"Weekly availability: {profile.weekly_availability_hours:g} h"]
    if profile.priority_disciplines:
        candidates.append("Priority disciplines: " + ", ".join(profile.priority_disciplines))
    candidates.extend(
        f"Secondary {goal.objective_type} goal: {goal.title}" for goal in profile.secondary_goals
    )
    for candidate in candidates:
        if len("\n".join([*lines, candidate])) + len(_DIGEST_HEADER) > OBJECTIVES_DIGEST_MAX_CHARS:
            break
        lines.append(candidate)
    return "\n".join([_DIGEST_HEADER, *lines])


def apply_objectives_digest(system_prompt: str, profile: ObjectiveProfile) -> str:
    """Append the objectives digest to a base system prompt."""
    return f"{system_prompt}\n\n{objectives_digest(profile)}"
