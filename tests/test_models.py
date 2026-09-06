"""Tests for coach_web.models."""

import pytest
from pydantic import ValidationError

from coach_web.models import (
    AthleteProfile,
    ChatMessage,
    FitnessTrend,
    FitnessTrendPoint,
    ReadinessMetrics,
    TrainingPlan,
)


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
            ChatMessage(role="system", content="Hello")  # type: ignore[arg-type]


class TestFitnessTrendPoint:
    """FitnessTrendPoint schema validation."""

    def test_valid_creation(self) -> None:
        """A valid FitnessTrendPoint instance can be created."""
        point = FitnessTrendPoint(
            date="2026-09-01",
            ctl=75.0,
            atl=60.0,
            tsb=15.0,
        )

        assert point.date == "2026-09-01"
        assert point.ctl == 75.0
        assert point.atl == 60.0
        assert point.tsb == 15.0

    def test_negative_ctl_raises(self) -> None:
        """Negative CTL values are rejected."""
        with pytest.raises(ValidationError):
            FitnessTrendPoint(
                date="2026-09-01",
                ctl=-1.0,
                atl=60.0,
                tsb=15.0,
            )


class TestFitnessTrend:
    """FitnessTrend schema validation."""

    def test_valid_creation_with_points(self) -> None:
        """A valid FitnessTrend instance can be created with points."""
        points = [
            FitnessTrendPoint(date="2026-09-01", ctl=75.0, atl=60.0, tsb=15.0),
            FitnessTrendPoint(date="2026-09-02", ctl=76.0, atl=62.0, tsb=14.0),
        ]
        trend = FitnessTrend(points=points)

        assert len(trend.points) == 2
        assert trend.points[0].date == "2026-09-01"
        assert trend.points[1].date == "2026-09-02"

    def test_empty_points_default(self) -> None:
        """FitnessTrend defaults to an empty points list."""
        trend = FitnessTrend()

        assert trend.points == []


class TestAthleteProfile:
    """AthleteProfile schema validation."""

    def test_valid_creation(self) -> None:
        """A valid AthleteProfile instance can be created."""
        profile = AthleteProfile(
            weight_kg=70.0,
            max_hr=185,
            resting_hr=48,
        )

        assert profile.weight_kg == 70.0
        assert profile.max_hr == 185
        assert profile.resting_hr == 48

    def test_negative_weight_raises(self) -> None:
        """Negative weight values are rejected."""
        with pytest.raises(ValidationError):
            AthleteProfile(
                weight_kg=-1.0,
                max_hr=185,
                resting_hr=48,
            )
