"""Tests for coach_web.models."""

import pytest
from pydantic import ValidationError

from coach_web.models import ChatMessage, ReadinessMetrics, TrainingPlan


class TestReadinessMetrics:
    """ReadinessMetrics schema validation."""

    def test_valid_creation(self) -> None:
        """A valid ReadinessMetrics instance can be created."""
        metrics = ReadinessMetrics(
            ftp=250,
            resting_hr=48,
            hrv_rmssd=65.5,
            sleep_hours=7.5,
            ctl=75.0,
            atl=60.0,
            tsb=15.0,
        )

        assert metrics.ftp == 250
        assert metrics.resting_hr == 48
        assert metrics.hrv_rmssd == 65.5
        assert metrics.sleep_hours == 7.5
        assert metrics.ctl == 75.0
        assert metrics.atl == 60.0
        assert metrics.tsb == 15.0

    def test_negative_values_raise(self) -> None:
        """Negative numeric values are rejected."""
        with pytest.raises(ValidationError):
            ReadinessMetrics(
                ftp=-1,
                resting_hr=48,
                hrv_rmssd=65.5,
                sleep_hours=7.5,
                ctl=75.0,
                atl=60.0,
                tsb=15.0,
            )

    def test_wrong_type_raises(self) -> None:
        """Values with incompatible types are rejected."""
        with pytest.raises(ValidationError):
            ReadinessMetrics(
                ftp="not-an-int",  # type: ignore[arg-type]
                resting_hr=48,
                hrv_rmssd=65.5,
                sleep_hours=7.5,
                ctl=75.0,
                atl=60.0,
                tsb=15.0,
            )

    def test_field_descriptions(self) -> None:
        """Each field has a description."""
        fields = ReadinessMetrics.model_fields
        for name in fields:
            assert fields[name].description, f"Field {name} lacks a description"


class TestTrainingPlan:
    """TrainingPlan schema validation."""

    def test_valid_creation(self) -> None:
        """A valid TrainingPlan instance can be created."""
        plan = TrainingPlan(
            number=1,
            title="Training Plan: 2026-W37",
            body="Build phase.",
            created_at="2026-09-01T10:00:00Z",
            week_id="2026-W37",
        )

        assert plan.number == 1
        assert plan.title == "Training Plan: 2026-W37"
        assert plan.body == "Build phase."
        assert plan.created_at == "2026-09-01T10:00:00Z"
        assert plan.week_id == "2026-W37"

    def test_week_id_validation(self) -> None:
        """week_id must match the ISO week pattern extracted from the title."""
        with pytest.raises(ValidationError):
            TrainingPlan(
                number=1,
                title="Training Plan: 2026-W37",
                body="Build phase.",
                created_at="2026-09-01T10:00:00Z",
                week_id="not-a-week",
            )

    def test_field_descriptions(self) -> None:
        """Each field has a description."""
        fields = TrainingPlan.model_fields
        for name in fields:
            assert fields[name].description, f"Field {name} lacks a description"


class TestChatMessage:
    """ChatMessage schema validation."""

    def test_valid_roles(self) -> None:
        """Both accepted roles can be used."""
        user_msg = ChatMessage(role="user", content="Hello")
        assistant_msg = ChatMessage(role="assistant", content="Hi")

        assert user_msg.role == "user"
        assert assistant_msg.role == "assistant"

    def test_invalid_role_raises(self) -> None:
        """Roles other than user/assistant are rejected."""
        with pytest.raises(ValidationError):
            ChatMessage(role="system", content="Hello")
