"""Pydantic v2 data models for Coach Web."""

import re
from datetime import date as date_type
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

_WEEK_ID_PATTERN: str = r"^\d{4}-W\d{2}$"
_DATE_PATTERN: str = r"^\d{4}-\d{2}-\d{2}$"


def _validate_iso_week(value: str) -> str:
    """Validate an ISO week identifier (``YYYY-WNN``) semantically."""
    if not re.match(_WEEK_ID_PATTERN, value):
        raise ValueError(f"week_id must match ISO week pattern {_WEEK_ID_PATTERN!r}")
    year, week = value.split("-W")
    try:
        date_type.fromisocalendar(int(year), int(week), 1)
    except ValueError as exc:
        raise ValueError(f"week_id {value!r} is not a valid ISO week") from exc
    return value


def _validate_iso_date(value: str | None) -> str | None:
    """Validate an optional ISO calendar date (``YYYY-MM-DD``)."""
    if value is None:
        return None
    if not re.match(_DATE_PATTERN, value):
        raise ValueError(f"date must match ISO date pattern {_DATE_PATTERN!r}")
    try:
        date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"date {value!r} is not a valid calendar date") from exc
    return value


class ReadinessMetrics(BaseModel):
    """Athlete readiness metrics from Intervals.icu."""

    ftp: int = Field(..., description="Functional Threshold Power in watts", ge=0)
    resting_hr: int = Field(..., description="Resting heart rate in bpm", ge=0)
    hrv_rmssd: float = Field(..., description="HRV RMSSD in milliseconds", ge=0)
    sleep_hours: float = Field(..., description="Hours of sleep", ge=0)
    ctl: float = Field(..., description="Chronic Training Load", ge=0)
    atl: float = Field(..., description="Acute Training Load", ge=0)
    tsb: float = Field(..., description="Training Stress Balance")


class FitnessTrendPoint(BaseModel):
    """A single point in a fitness trend history."""

    date: str = Field(..., description="ISO 8601 date (YYYY-MM-DD)")
    ctl: float = Field(..., description="Chronic Training Load on that date", ge=0)
    atl: float = Field(..., description="Acute Training Load on that date", ge=0)
    tsb: float = Field(..., description="Training Stress Balance on that date")


class FitnessTrend(BaseModel):
    """A series of fitness trend points."""

    points: list[FitnessTrendPoint] = Field(
        default_factory=list,
        description="Chronological fitness trend data points",
    )


class AthleteProfile(BaseModel):
    """Athlete profile data from Intervals.icu."""

    weight_kg: float = Field(..., description="Athlete weight in kg", ge=0)
    max_hr: int = Field(..., description="Maximum heart rate in bpm", ge=0)
    resting_hr: int = Field(..., description="Resting heart rate in bpm", ge=0)


class TrainingPlan(BaseModel):
    """A weekly training plan represented as a GitHub issue."""

    number: int = Field(..., description="GitHub issue number")
    title: str = Field(..., description="GitHub issue title")
    body: str = Field(..., description="GitHub issue body")
    created_at: str = Field(..., description="ISO 8601 creation timestamp")
    week_id: str = Field(..., description="ISO week identifier (YYYY-WNN)")

    @field_validator("week_id")
    @classmethod
    def _validate_week_id(cls, value: str) -> str:
        return _validate_iso_week(value)


class ChatMessage(BaseModel):
    """A single chat message exchanged with the coach."""

    role: Literal["user", "assistant"] = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")


class WorkoutStep(BaseModel):
    """A single structured workout step within a plan proposal."""

    label: str = Field(..., description="Human-readable step label (e.g. 'Threshold')")
    duration_minutes: float = Field(..., gt=0, description="Step duration in minutes")
    target_power_pct: float | None = Field(
        default=None,
        ge=0,
        le=250,
        description="Target power as a percentage of FTP",
    )
    target_power_watts: int | None = Field(
        default=None,
        ge=0,
        description="Explicit target power in watts",
    )
    cadence_rpm: int | None = Field(
        default=None,
        ge=0,
        le=200,
        description="Target cadence in revolutions per minute",
    )
    description: str | None = Field(default=None, description="Free-form step instructions")


class PlanProposal(BaseModel):
    """A structured workout plan awaiting athlete approval.

    Emitted by the agent as a JSON SSE event so the UI can render an approval
    card. Every proposal is validated before it leaves the agent loop.
    """

    title: str = Field(..., description="Plan title")
    week_id: str = Field(..., description="ISO week identifier (YYYY-WNN)")
    summary: str = Field(..., description="One-paragraph plan summary")
    rationale: str | None = Field(
        default=None,
        description="Why this plan is proposed, based on athlete data",
    )
    date: str | None = Field(
        default=None,
        description="Scheduled workout date (YYYY-MM-DD); defaults to the ISO week's Monday",
    )
    steps: list[WorkoutStep] = Field(
        default_factory=list,
        description="Ordered workout steps",
    )

    @field_validator("week_id")
    @classmethod
    def _validate_week_id(cls, value: str) -> str:
        return _validate_iso_week(value)

    @field_validator("date")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        return _validate_iso_date(value)


class PlanApprovalRequest(BaseModel):
    """Payload submitted by the UI when the athlete approves a plan proposal."""

    plan: PlanProposal = Field(..., description="The plan proposal to approve")


class ApprovalStepResult(BaseModel):
    """Outcome of a single side effect performed during plan approval."""

    step: Literal["intervals_event", "github_commit"] = Field(
        ...,
        description="Side effect identifier",
    )
    success: bool = Field(..., description="Whether the side effect succeeded")
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Parsed upstream tool response",
    )
    error: str | None = Field(
        default=None,
        description="Failure reason when the side effect did not succeed",
    )


class PlanApprovalResponse(BaseModel):
    """Aggregated outcome of a plan approval request."""

    status: Literal["approved", "partial", "failed"] = Field(
        ...,
        description="Overall approval status",
    )
    plan_title: str = Field(..., description="Approved plan title")
    week_id: str = Field(..., description="ISO week identifier (YYYY-WNN)")
    date: str = Field(..., description="Scheduled workout date (YYYY-MM-DD)")
    steps: list[ApprovalStepResult] = Field(
        default_factory=list,
        description="Per-step side-effect outcomes",
    )
