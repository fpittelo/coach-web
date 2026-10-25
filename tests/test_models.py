"""Tests for coach_web.models."""

import pytest
from pydantic import ValidationError

from coach_web.models import (
    AGENT_MESSAGE_MAX_LENGTH,
    AgentStreamRequest,
    AthleteProfile,
    ChatMessage,
    FitnessTrend,
    FitnessTrendPoint,
    PlanProposal,
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

    def test_accepts_content_at_the_cap(self) -> None:
        """Content of exactly 8,000 characters is accepted (per-entry cap)."""
        message = ChatMessage(role="user", content="x" * AGENT_MESSAGE_MAX_LENGTH)

        assert len(message.content) == AGENT_MESSAGE_MAX_LENGTH

    def test_rejects_oversized_content(self) -> None:
        """Content beyond the 8,000-character per-entry cap is rejected.

        Without a per-entry cap, 10 history entries of arbitrary size would
        bypass the 8,000-char message cap entirely — a token-flooding /
        prompt-injection amplification vector (STRIDE #87).
        """
        with pytest.raises(ValidationError):
            ChatMessage(role="assistant", content="x" * (AGENT_MESSAGE_MAX_LENGTH + 1))


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


class TestPlanProposalDate:
    """PlanProposal scheduled-date validation."""

    def _payload(self, **overrides: object) -> dict[str, object]:
        """Build a minimal valid plan payload with optional overrides."""
        payload: dict[str, object] = {
            "title": "Threshold Builder",
            "week_id": "2026-W38",
            "summary": "Three by twelve minutes at threshold.",
        }
        payload.update(overrides)
        return payload

    def test_date_defaults_to_none(self) -> None:
        """A proposal without a date leaves the field unset."""
        plan = PlanProposal.model_validate(self._payload())

        assert plan.date is None

    def test_accepts_iso_date(self) -> None:
        """A well-formed ISO date is accepted."""
        plan = PlanProposal.model_validate(self._payload(date="2026-09-21"))

        assert plan.date == "2026-09-21"

    def test_rejects_malformed_date(self) -> None:
        """A non-ISO date string is rejected."""
        with pytest.raises(ValidationError):
            PlanProposal.model_validate(self._payload(date="21-09-2026"))

    def test_rejects_impossible_calendar_date(self) -> None:
        """A syntactically valid but impossible date is rejected."""
        with pytest.raises(ValidationError):
            PlanProposal.model_validate(self._payload(date="2026-02-30"))

    def test_rejects_impossible_iso_week(self) -> None:
        """An out-of-range ISO week number is rejected."""
        with pytest.raises(ValidationError):
            PlanProposal.model_validate(self._payload(week_id="2026-W99"))

    def test_week_rejection_message_is_value_free(self) -> None:
        """An impossible week is rejected without echoing the value (#355).

        The 422 log projection drops ``msg`` (#354), but message hygiene must
        not depend on it: a regression that logs the raw message must not
        re-open the leak. Only the field name and expected format ride ``msg``.
        """
        with pytest.raises(ValidationError) as excinfo:
            PlanProposal.model_validate(self._payload(week_id="2026-W99"))

        msg = excinfo.value.errors()[0]["msg"]
        assert "week_id" in msg
        assert "2026-W99" not in msg

    def test_date_rejection_message_is_value_free(self) -> None:
        """An impossible date is rejected without echoing the value (#355)."""
        with pytest.raises(ValidationError) as excinfo:
            PlanProposal.model_validate(self._payload(date="2026-02-30"))

        msg = excinfo.value.errors()[0]["msg"]
        assert "date" in msg
        assert "2026-02-30" not in msg


class TestAgentStreamRequest:
    """POST /api/agent/stream payload validation (#79, wire change #349).

    Dict payloads go through ``model_validate`` — the same shape the JSON
    body arrives in — so the wire-format coercion path is what gets tested.
    MADR-0025 retired the client-owned ``history`` field: the server owns
    the conversation (#348 store, #349 API) and the request carries the
    message plus an optional server-issued ``thread_id`` only.
    """

    def test_history_field_is_retired(self) -> None:
        """The client-owned ``history`` field no longer exists."""
        assert "history" not in AgentStreamRequest.model_fields

    def test_extra_history_key_is_ignored_for_cached_clients(self) -> None:
        """An old client's ``history`` key is ignored, never honored.

        The default ``extra="ignore"`` is deliberate (MADR-0025): cached
        old clients keep working through the transition while the forged-
        history injection path stays closed — the field is never read.
        """
        request = AgentStreamRequest.model_validate(
            {
                "message": "make it 20 min instead",
                "history": [
                    {"role": "user", "content": "Plan a 30 min session"},
                    {"role": "assistant", "content": "Here is your plan."},
                ],
            }
        )

        assert request.message == "make it 20 min instead"
        assert getattr(request, "history", None) is None

    def test_thread_id_defaults_to_none(self) -> None:
        """``thread_id`` is optional — the server resolves or opens a thread."""
        request = AgentStreamRequest(message="Plan my week")

        assert request.thread_id is None

    def test_accepts_a_server_issued_thread_id(self) -> None:
        """A positive ``thread_id`` round-trips (opaque, server-issued)."""
        request = AgentStreamRequest.model_validate({"message": "hi", "thread_id": 42})

        assert request.thread_id == 42

    def test_rejects_non_positive_thread_ids(self) -> None:
        """``thread_id`` must be a positive integer (ids start at 1)."""
        with pytest.raises(ValidationError):
            AgentStreamRequest.model_validate({"message": "hi", "thread_id": 0})

    def test_rejects_empty_message(self) -> None:
        """An empty message is rejected."""
        with pytest.raises(ValidationError):
            AgentStreamRequest.model_validate({"message": ""})

    def test_rejects_oversized_message(self) -> None:
        """Messages beyond the 8,000-character cap are rejected."""
        with pytest.raises(ValidationError):
            AgentStreamRequest.model_validate({"message": "x" * 8001})

    def test_accepts_message_at_the_cap(self) -> None:
        """A message of exactly 8,000 characters is accepted."""
        request = AgentStreamRequest(message="x" * 8000)

        assert len(request.message) == 8000

    def test_rejects_whitespace_only_message(self) -> None:
        """A whitespace-only message is rejected (min_length alone misses it)."""
        for blank in ("   ", "\t", "\n", " \t\n "):
            with pytest.raises(ValidationError):
                AgentStreamRequest.model_validate({"message": blank})

    def test_strips_surrounding_whitespace_from_message(self) -> None:
        """The message validator strips surrounding whitespace."""
        request = AgentStreamRequest.model_validate({"message": "  Plan my week  "})

        assert request.message == "Plan my week"
