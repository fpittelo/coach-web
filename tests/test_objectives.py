"""ObjectiveProfile domain: strict validation, persistence mapping, digest (#166).

The profile is the coach's strategic anchor (epic #161 story 1.1): primary
goal + secondary goals + weekly availability + priority disciplines, validated
by Pydantic v2 at the API boundary and persisted per MADR-008 as a single
active ``athlete_objectives`` row. The profile-level fields (weekly
availability hours, priority disciplines, secondary goals) travel in a JSON
envelope inside the ``availability_notes`` TEXT column — JSON-in-TEXT is the
MADR-008 convention ("JSON payloads use the built-in JSON1 type affinity
(TEXT)") — while the primary goal maps onto the dedicated columns, keeping a
1:1 profile↔row mapping without inventing columns outside the ADR schema.
"""

from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from coach_web.objectives import (
    OBJECTIVES_DIGEST_MAX_CHARS,
    ObjectiveGoal,
    ObjectiveProfile,
    apply_objectives_digest,
    get_objective_profile,
    objectives_digest,
    save_objective_profile,
)


def _goal_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective goal payload with optional overrides."""
    payload: dict[str, Any] = {
        "objective_type": "outcome",
        "title": "Race the Alpenbrevet 2027",
        "description": "Finish the middle route under nine hours.",
        "target_metric": "event_time",
        "target_value": 9.0,
        "target_date": "2027-09-11",
    }
    payload.update(overrides)
    return payload


def _profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective profile payload with optional overrides."""
    payload: dict[str, Any] = {
        "primary_goal": _goal_payload(),
        "secondary_goals": [
            _goal_payload(
                objective_type="process",
                title="Three threshold sessions weekly",
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
        ],
        "weekly_availability_hours": 8.5,
        "priority_disciplines": ["road", "crit"],
    }
    payload.update(overrides)
    return payload


class TestObjectiveGoalValidation:
    """Strict Pydantic validation of a single objective goal (AC4)."""

    def test_accepts_a_valid_goal(self) -> None:
        """A fully populated goal validates."""
        goal = ObjectiveGoal.model_validate(_goal_payload())

        assert goal.objective_type == "outcome"
        assert goal.title == "Race the Alpenbrevet 2027"
        assert goal.target_value == 9.0

    def test_rejects_objective_type_outside_the_enum(self) -> None:
        """objective_type is locked to the MADR-008 CHECK enum."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(objective_type="fantasy"))

    def test_rejects_blank_title(self) -> None:
        """An empty title is rejected."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(title=""))

    def test_rejects_oversized_title(self) -> None:
        """Titles beyond 200 characters are rejected."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(title="x" * 201))

    def test_rejects_malformed_target_date(self) -> None:
        """target_date must match the ISO date pattern."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(target_date="09/2027"))

    def test_rejects_impossible_calendar_date(self) -> None:
        """target_date must be a real calendar date."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(target_date="2027-02-30"))

    def test_rejects_negative_target_value(self) -> None:
        """target_value is bounded at zero from below (mission validation)."""
        with pytest.raises(ValidationError):
            ObjectiveGoal.model_validate(_goal_payload(target_value=-1.0))

    def test_accepts_optionals_as_none(self) -> None:
        """A goal with only type and title validates."""
        goal = ObjectiveGoal.model_validate(
            _goal_payload(
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
        )

        assert goal.target_metric is None
        assert goal.target_date is None


class TestObjectiveProfileValidation:
    """Strict Pydantic validation of the aggregate profile (AC4)."""

    def test_accepts_a_valid_profile(self) -> None:
        """A fully populated profile validates."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        assert profile.primary_goal.title == "Race the Alpenbrevet 2027"
        assert profile.weekly_availability_hours == 8.5
        assert profile.priority_disciplines == ["road", "crit"]

    def test_rejects_availability_hours_below_zero(self) -> None:
        """Weekly availability is bounded at zero from below."""
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(weekly_availability_hours=-0.5))

    def test_rejects_availability_hours_above_168(self) -> None:
        """Weekly availability is capped at the 168 hours of a week."""
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(weekly_availability_hours=168.5))

    def test_accepts_availability_hours_bounds(self) -> None:
        """Both bounds 0 and 168 are accepted."""
        for hours in (0, 168):
            profile = ObjectiveProfile.model_validate(
                _profile_payload(weekly_availability_hours=hours)
            )
            assert profile.weekly_availability_hours == hours

    def test_rejects_more_than_five_secondary_goals(self) -> None:
        """The secondary goals list is capped at five entries."""
        secondaries = [
            _goal_payload(
                title=f"Secondary {index}",
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
            for index in range(6)
        ]
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(secondary_goals=secondaries))

    def test_accepts_five_secondary_goals(self) -> None:
        """Five secondary goals are accepted."""
        secondaries = [
            _goal_payload(
                title=f"Secondary {index}",
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
            for index in range(5)
        ]
        profile = ObjectiveProfile.model_validate(_profile_payload(secondary_goals=secondaries))

        assert len(profile.secondary_goals) == 5

    def test_rejects_blank_priority_discipline(self) -> None:
        """Whitespace-only discipline entries are rejected."""
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(priority_disciplines=["road", "   "]))

    def test_rejects_oversized_priority_discipline(self) -> None:
        """A single discipline label beyond 50 characters is rejected."""
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(priority_disciplines=["x" * 51]))

    def test_strips_discipline_whitespace(self) -> None:
        """Discipline entries are whitespace-stripped on validation."""
        profile = ObjectiveProfile.model_validate(
            _profile_payload(priority_disciplines=[" road ", "crit"])
        )

        assert profile.priority_disciplines == ["road", "crit"]

    def test_rejects_more_than_eight_disciplines(self) -> None:
        """The priority disciplines list is capped at eight entries."""
        disciplines = [f"discipline-{index}" for index in range(9)]
        with pytest.raises(ValidationError):
            ObjectiveProfile.model_validate(_profile_payload(priority_disciplines=disciplines))


class TestObjectiveRepository:
    """Persistence round-trip over the migrated SQLite database (AC2)."""

    async def test_get_returns_none_on_empty_database(self, db_session_factory: Any) -> None:
        """No persisted profile reads back as None (graceful empty state)."""
        async with db_session_factory() as session:
            assert await get_objective_profile(session) is None

    async def test_save_then_get_round_trips_every_field(self, db_session_factory: Any) -> None:
        """A saved profile reads back with every field intact."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        async with db_session_factory() as session:
            await save_objective_profile(session, profile)
            loaded = await get_objective_profile(session)

        assert loaded == profile

    async def test_save_upserts_the_single_active_row(self, db_session_factory: Any) -> None:
        """Saving twice updates the same row instead of stacking profiles."""
        first = ObjectiveProfile.model_validate(_profile_payload())
        second = ObjectiveProfile.model_validate(
            _profile_payload(
                primary_goal=_goal_payload(title="Updated goal"),
                weekly_availability_hours=10.0,
            )
        )

        async with db_session_factory() as session:
            await save_objective_profile(session, first)
            await save_objective_profile(session, second)
            loaded = await get_objective_profile(session)
            rows = (await session.execute(text("SELECT COUNT(*) FROM athlete_objectives"))).scalar()

        assert loaded is not None
        assert loaded.primary_goal.title == "Updated goal"
        assert loaded.weekly_availability_hours == 10.0
        assert rows == 1

    async def test_saved_row_is_active_with_timestamps(self, db_session_factory: Any) -> None:
        """The persisted row is status=active with ordered UTC timestamps."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        async with db_session_factory() as session:
            await save_objective_profile(session, profile)
            row = (
                await session.execute(
                    text("SELECT status, created_at, updated_at FROM athlete_objectives")
                )
            ).fetchone()

        assert row is not None
        assert row[0] == "active"
        assert row[1] <= row[2]

    async def test_corrupt_availability_envelope_degrades_to_defaults(
        self, db_session_factory: Any
    ) -> None:
        """A tampered availability_notes envelope reads back as an empty profile."""
        profile = ObjectiveProfile.model_validate(_profile_payload())
        async with db_session_factory() as session:
            await save_objective_profile(session, profile)
            await session.execute(
                text("UPDATE athlete_objectives SET availability_notes = 'not-json'")
            )
            loaded = await get_objective_profile(session)

        assert loaded is not None
        assert loaded.primary_goal.title == "Race the Alpenbrevet 2027"
        assert loaded.weekly_availability_hours == 0.0
        assert loaded.secondary_goals == []
        assert loaded.priority_disciplines == []

    async def test_non_object_envelope_degrades_to_defaults(self, db_session_factory: Any) -> None:
        """A JSON envelope that is not an object reads back as an empty profile."""
        profile = ObjectiveProfile.model_validate(_profile_payload())
        async with db_session_factory() as session:
            await save_objective_profile(session, profile)
            await session.execute(
                text("UPDATE athlete_objectives SET availability_notes = '[1, 2]'")
            )
            loaded = await get_objective_profile(session)

        assert loaded is not None
        assert loaded.weekly_availability_hours == 0.0
        assert loaded.secondary_goals == []

    async def test_null_availability_envelope_degrades_to_defaults(
        self, db_session_factory: Any
    ) -> None:
        """A NULL availability_notes column reads back as an empty profile."""
        profile = ObjectiveProfile.model_validate(_profile_payload())
        async with db_session_factory() as session:
            await save_objective_profile(session, profile)
            await session.execute(text("UPDATE athlete_objectives SET availability_notes = NULL"))
            loaded = await get_objective_profile(session)

        assert loaded is not None
        assert loaded.weekly_availability_hours == 0.0
        assert loaded.secondary_goals == []


class TestObjectivesDigest:
    """Token-budgeted objectives digest for the coach system prompt (AC3)."""

    def test_digest_contains_the_primary_goal_facts(self) -> None:
        """Type, title, target metric/value and date appear in the digest."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        digest = objectives_digest(profile)

        assert "Primary outcome goal: Race the Alpenbrevet 2027" in digest
        assert "event_time 9" in digest
        assert "by 2027-09-11" in digest

    def test_digest_contains_availability_and_disciplines(self) -> None:
        """Weekly availability hours and priority disciplines appear."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        digest = objectives_digest(profile)

        assert "Weekly availability: 8.5 h" in digest
        assert "Priority disciplines: road, crit" in digest

    def test_digest_contains_secondary_goals(self) -> None:
        """Secondary goals appear after the primary goal."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        digest = objectives_digest(profile)

        assert "Secondary process goal: Three threshold sessions weekly" in digest

    def test_digest_omits_absent_target_bits(self) -> None:
        """A primary goal without targets renders no target clause."""
        profile = ObjectiveProfile.model_validate(
            _profile_payload(
                primary_goal=_goal_payload(target_metric=None, target_value=None, target_date=None)
            )
        )

        digest = objectives_digest(profile)

        assert "by " not in digest

    def test_digest_respects_the_token_budget(self) -> None:
        """A maximal profile never exceeds the digest character budget."""
        huge_secondaries = [
            _goal_payload(
                title="S" * 200,
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
            for _ in range(5)
        ]
        huge_disciplines = [f"d{i}" * 16 for i in range(8)]
        profile = ObjectiveProfile.model_validate(
            _profile_payload(
                primary_goal=_goal_payload(target_metric="m" * 100, target_value=123456.789),
                secondary_goals=huge_secondaries,
                priority_disciplines=huge_disciplines,
            )
        )

        digest = objectives_digest(profile)

        assert len(digest) <= OBJECTIVES_DIGEST_MAX_CHARS
        assert "Race the Alpenbrevet 2027" in digest
        assert "S" * 200 not in digest

    def test_apply_appends_the_digest_to_a_base_prompt(self) -> None:
        """apply_objectives_digest composes base prompt + digest."""
        profile = ObjectiveProfile.model_validate(_profile_payload())

        composed = apply_objectives_digest("BASE PROMPT", profile)

        assert composed.startswith("BASE PROMPT")
        assert "Primary outcome goal" in composed
