"""Pydantic v2 data models for Coach Web."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_WEEK_ID_PATTERN: str = r"^\d{4}-W\d{2}$"


class ReadinessMetrics(BaseModel):
    """Athlete readiness metrics from Intervals.icu."""

    ftp: int = Field(..., description="Functional Threshold Power in watts", ge=0)
    resting_hr: int = Field(..., description="Resting heart rate in bpm", ge=0)
    hrv_rmssd: float = Field(..., description="HRV RMSSD in milliseconds", ge=0)
    sleep_hours: float = Field(..., description="Hours of sleep", ge=0)
    ctl: float = Field(..., description="Chronic Training Load", ge=0)
    atl: float = Field(..., description="Acute Training Load", ge=0)
    tsb: float = Field(..., description="Training Stress Balance")


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
        if not re.match(_WEEK_ID_PATTERN, value):
            raise ValueError(f"week_id must match ISO week pattern {_WEEK_ID_PATTERN!r}")
        return value


class ChatMessage(BaseModel):
    """A single chat message exchanged with the coach."""

    role: Literal["user", "assistant"] = Field(..., description="Message role: user or assistant")
    content: str = Field(..., description="Message content")
