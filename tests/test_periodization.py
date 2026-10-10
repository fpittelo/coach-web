"""Periodization domain: phases, plan validation, resolution, digest (#167).

The macrocycle phase plan (epic #161 story 1.2) hangs off the active
objective: ordered, non-overlapping date ranges covering the window to the
objective's target date. The current phase is computed from the calendar —
``f(today, phases)``, no stored state, no scheduler (KIS) — and feeds the
coach digest with the current phase, the target countdown and the phase's
load-scaling hint. Persistence follows MADR-008: the phases live in the
``periodization_phases`` table (C1 — plaintext), FK-cascaded onto their
parent objective row.
"""

from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from coach_web.objectives import (
    OBJECTIVES_DIGEST_MAX_CHARS,
    ObjectiveProfile,
    apply_objectives_digest,
    objectives_digest,
    save_objective_profile,
)
from coach_web.periodization import (
    MAX_PHASES,
    PHASE_LOAD_HINTS,
    PHASE_TYPES,
    PeriodizationPhase,
    PeriodizationPlan,
    delete_phases,
    list_phases,
    replace_phases,
    resolve_phase_status,
    validate_plan_coverage,
)


def _phase_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid periodization phase payload with optional overrides."""
    payload: dict[str, Any] = {
        "phase_type": "base",
        "name": "Aerobic base",
        "start_date": "2026-10-01",
        "end_date": "2026-12-15",
        "focus": "Long rides, endurance",
        "weekly_hours_target": 8.0,
    }
    payload.update(overrides)
    return payload


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid two-phase plan payload with optional overrides."""
    payload: dict[str, Any] = {
        "phases": [
            _phase_payload(),
            _phase_payload(
                phase_type="build",
                name="Threshold build",
                start_date="2026-12-16",
                end_date="2027-02-01",
                focus=None,
                weekly_hours_target=None,
            ),
        ]
    }
    payload.update(overrides)
    return payload


def _goal_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective goal payload with optional overrides."""
    payload: dict[str, Any] = {
        "objective_type": "outcome",
        "title": "Race the Alpenbrevet 2027",
        "description": None,
        "target_metric": None,
        "target_value": None,
        "target_date": "2027-09-11",
    }
    payload.update(overrides)
    return payload


def _profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective profile payload with optional overrides."""
    payload: dict[str, Any] = {
        "primary_goal": _goal_payload(),
        "secondary_goals": [],
        "weekly_availability_hours": 8.5,
        "priority_disciplines": ["road"],
    }
    payload.update(overrides)
    return payload


class TestPeriodizationPhaseValidation:
    """Strict Pydantic validation of a single macrocycle phase (AC3)."""

    def test_accepts_a_valid_phase(self) -> None:
        """A fully populated phase validates."""
        phase = PeriodizationPhase.model_validate(_phase_payload())

        assert phase.phase_type == "base"
        assert phase.name == "Aerobic base"
        assert phase.weekly_hours_target == 8.0

    def test_rejects_phase_type_outside_the_enum(self) -> None:
        """phase_type is locked to the MADR-008 CHECK enum."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(phase_type="fantasy"))

    def test_rejects_blank_name(self) -> None:
        """An empty phase name is rejected."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(name=""))

    def test_rejects_oversized_name(self) -> None:
        """Names beyond 200 characters are rejected."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(name="x" * 201))

    def test_rejects_malformed_start_date(self) -> None:
        """start_date must match the ISO date pattern."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(start_date="10/2026"))

    def test_rejects_impossible_calendar_date(self) -> None:
        """Phase dates must be real calendar dates."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(end_date="2026-02-30"))

    def test_rejects_start_after_end(self) -> None:
        """An inverted date range is rejected (mirrors the DB CHECK)."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(
                _phase_payload(start_date="2026-12-16", end_date="2026-12-15")
            )

    def test_accepts_single_day_phase(self) -> None:
        """A single-day phase (start == end) is valid."""
        phase = PeriodizationPhase.model_validate(
            _phase_payload(start_date="2026-12-15", end_date="2026-12-15")
        )

        assert phase.start_date == phase.end_date

    def test_rejects_negative_weekly_hours_target(self) -> None:
        """The weekly hours target is bounded at zero from below."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(weekly_hours_target=-1.0))

    def test_rejects_weekly_hours_target_above_168(self) -> None:
        """The weekly hours target is capped at the 168 hours of a week."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(weekly_hours_target=168.5))

    def test_accepts_weekly_hours_target_bounds(self) -> None:
        """Both bounds 0 and 168 are accepted."""
        for hours in (0, 168):
            phase = PeriodizationPhase.model_validate(_phase_payload(weekly_hours_target=hours))
            assert phase.weekly_hours_target == hours

    def test_accepts_optionals_as_none(self) -> None:
        """A phase with only type, name and dates validates."""
        phase = PeriodizationPhase.model_validate(
            _phase_payload(focus=None, weekly_hours_target=None, notes=None)
        )

        assert phase.focus is None
        assert phase.weekly_hours_target is None
        assert phase.notes is None

    def test_rejects_oversized_focus(self) -> None:
        """Focus beyond 200 characters is rejected."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(focus="x" * 201))

    def test_rejects_oversized_notes(self) -> None:
        """Notes beyond 2000 characters are rejected."""
        with pytest.raises(ValidationError):
            PeriodizationPhase.model_validate(_phase_payload(notes="x" * 2001))


class TestPeriodizationPlanValidation:
    """Cross-phase plan validation: monotonic, non-overlapping (AC3)."""

    def test_accepts_a_valid_plan(self) -> None:
        """A two-phase plan with contiguous ranges validates."""
        plan = PeriodizationPlan.model_validate(_plan_payload())

        assert [phase.name for phase in plan.phases] == ["Aerobic base", "Threshold build"]

    def test_accepts_an_empty_plan(self) -> None:
        """An empty plan (no periodization) is valid — clearing is legitimate."""
        plan = PeriodizationPlan.model_validate({"phases": []})

        assert plan.phases == []

    def test_rejects_phases_sharing_a_boundary_day(self) -> None:
        """Inclusive date ranges may not share a day: next.start == prev.end fails."""
        overlapping = _plan_payload(
            phases=[
                _phase_payload(end_date="2026-12-15"),
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-15",
                    end_date="2027-02-01",
                ),
            ]
        )
        with pytest.raises(ValidationError):
            PeriodizationPlan.model_validate(overlapping)

    def test_rejects_directly_overlapping_phases(self) -> None:
        """A phase starting inside its predecessor is rejected."""
        overlapping = _plan_payload(
            phases=[
                _phase_payload(end_date="2026-12-15"),
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-01",
                    end_date="2027-02-01",
                ),
            ]
        )
        with pytest.raises(ValidationError):
            PeriodizationPlan.model_validate(overlapping)

    def test_accepts_contiguous_phases(self) -> None:
        """A phase starting the day after its predecessor ends is valid."""
        contiguous = _plan_payload(
            phases=[
                _phase_payload(end_date="2026-12-15"),
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-16",
                    end_date="2027-02-01",
                ),
            ]
        )
        plan = PeriodizationPlan.model_validate(contiguous)

        assert len(plan.phases) == 2

    def test_rejects_non_monotonic_disjoint_phases(self) -> None:
        """Out-of-order phases fail even when the ranges do not intersect."""
        unordered = _plan_payload(
            phases=[
                _phase_payload(start_date="2027-01-01", end_date="2027-02-01"),
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-01-01",
                    end_date="2026-02-01",
                ),
            ]
        )
        with pytest.raises(ValidationError):
            PeriodizationPlan.model_validate(unordered)

    def _sequential_phases(self, count: int) -> list[dict[str, Any]]:
        """Non-overlapping single-day phases, one per calendar day."""
        return [
            _phase_payload(
                name=f"Phase {index}",
                start_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
                end_date=(date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            )
            for index in range(count)
        ]

    def test_rejects_more_than_the_phase_cap(self) -> None:
        """The phase list is capped (MAX_PHASES entries)."""
        with pytest.raises(ValidationError):
            PeriodizationPlan.model_validate({"phases": self._sequential_phases(MAX_PHASES + 1)})

    def test_accepts_the_phase_cap(self) -> None:
        """Exactly MAX_PHASES entries are accepted."""
        plan = PeriodizationPlan.model_validate({"phases": self._sequential_phases(MAX_PHASES)})

        assert len(plan.phases) == MAX_PHASES


class TestPhasePlanCoverage:
    """The plan must span the parent objective's target date (AC3)."""

    def test_accepts_any_plan_without_a_target_date(self) -> None:
        """Without a target date there is nothing to cross-check."""
        plan = PeriodizationPlan.model_validate(_plan_payload())

        validate_plan_coverage(plan, None)

    def test_accepts_an_empty_plan_with_a_target_date(self) -> None:
        """Clearing the phases is legitimate regardless of the target date."""
        plan = PeriodizationPlan.model_validate({"phases": []})

        validate_plan_coverage(plan, "2027-09-11")

    def test_accepts_a_plan_covering_the_target_date(self) -> None:
        """A target date inside the plan span validates."""
        plan = PeriodizationPlan.model_validate(
            _plan_payload(
                phases=[
                    _phase_payload(end_date="2026-12-15"),
                    _phase_payload(
                        phase_type="build",
                        name="Threshold build",
                        start_date="2026-12-16",
                        end_date="2027-09-11",
                    ),
                ]
            )
        )

        validate_plan_coverage(plan, "2027-09-11")

    def test_rejects_a_plan_ending_before_the_target_date(self) -> None:
        """A plan that ends before the target leaves the window uncovered."""
        plan = PeriodizationPlan.model_validate(_plan_payload())

        with pytest.raises(ValueError, match="target"):
            validate_plan_coverage(plan, "2027-09-11")

    def test_rejects_a_plan_starting_after_the_target_date(self) -> None:
        """A plan that starts after the target date cannot cover it."""
        plan = PeriodizationPlan.model_validate(
            _plan_payload(
                phases=[
                    _phase_payload(start_date="2027-10-01", end_date="2027-12-15"),
                ]
            )
        )

        with pytest.raises(ValueError, match="target"):
            validate_plan_coverage(plan, "2027-09-11")


class TestPhaseResolution:
    """Calendar-computed current phase and countdown — frozen clock (AC1)."""

    FROZEN_TODAY = date(2026, 11, 15)

    def _phases(self) -> list[PeriodizationPhase]:
        """A three-phase plan around the frozen today."""
        return [
            PeriodizationPhase.model_validate(_phase_payload()),
            PeriodizationPhase.model_validate(
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-16",
                    end_date="2027-02-01",
                )
            ),
            PeriodizationPhase.model_validate(
                _phase_payload(
                    phase_type="taper",
                    name="Race taper",
                    start_date="2027-02-02",
                    end_date="2027-03-01",
                )
            ),
        ]

    def test_returns_the_phase_containing_today(self) -> None:
        """Today inside a phase resolves to that phase."""
        status = resolve_phase_status(self._phases(), "2027-09-11", self.FROZEN_TODAY)

        assert status.phase is not None
        assert status.phase.name == "Aerobic base"
        assert status.phase.phase_type == "base"

    def test_phase_boundaries_are_inclusive(self) -> None:
        """Both the start day and the end day belong to the phase."""
        for today in (date(2026, 10, 1), date(2026, 12, 15)):
            status = resolve_phase_status(self._phases(), None, today)

            assert status.phase is not None, today
            assert status.phase.name == "Aerobic base", today

    def test_returns_none_before_the_first_phase(self) -> None:
        """Today before every phase resolves to no current phase."""
        status = resolve_phase_status(self._phases(), None, date(2026, 9, 30))

        assert status.phase is None

    def test_returns_none_after_the_last_phase(self) -> None:
        """Today after every phase resolves to no current phase."""
        status = resolve_phase_status(self._phases(), None, date(2027, 3, 2))

        assert status.phase is None

    def test_returns_none_in_a_gap_between_phases(self) -> None:
        """Today in a gap between two phases resolves to no current phase."""
        gapped = [
            PeriodizationPhase.model_validate(_phase_payload(end_date="2026-11-01")),
            PeriodizationPhase.model_validate(
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-16",
                    end_date="2027-02-01",
                )
            ),
        ]

        status = resolve_phase_status(gapped, None, self.FROZEN_TODAY)

        assert status.phase is None

    def test_days_to_target_is_none_without_a_target_date(self) -> None:
        """No target date on the parent objective means no countdown."""
        status = resolve_phase_status(self._phases(), None, self.FROZEN_TODAY)

        assert status.days_to_target is None

    def test_days_to_target_counts_calendar_days(self) -> None:
        """The countdown is the signed calendar-day difference."""
        expected = (date(2027, 9, 11) - self.FROZEN_TODAY).days

        status = resolve_phase_status(self._phases(), "2027-09-11", self.FROZEN_TODAY)

        assert status.days_to_target == expected
        assert status.days_to_target > 0

    def test_days_to_target_is_negative_after_the_target(self) -> None:
        """A target date in the past yields a negative countdown."""
        status = resolve_phase_status(self._phases(), "2026-11-01", self.FROZEN_TODAY)

        assert status.days_to_target == (date(2026, 11, 1) - self.FROZEN_TODAY).days
        assert status.days_to_target < 0


class TestPhaseLoadHints:
    """Per-phase-type load-scaling hints exposed to the coach prompt (AC2)."""

    def test_every_phase_type_has_a_load_hint(self) -> None:
        """The hint map covers exactly the MADR-008 phase_type enum."""
        assert tuple(PHASE_LOAD_HINTS) == PHASE_TYPES

    def test_load_hints_are_non_empty(self) -> None:
        """Every hint is a non-empty, lowercase-keyed string."""
        for phase_type, hint in PHASE_LOAD_HINTS.items():
            assert hint, phase_type
            assert hint == hint.strip(), phase_type


class TestObjectivesDigestExtension:
    """The objectives digest gains the phase state when phases exist (AC2)."""

    FROZEN_TODAY = date(2026, 11, 15)

    def _profile(self, **overrides: Any) -> ObjectiveProfile:
        """A minimal objective profile with a target date."""
        return ObjectiveProfile.model_validate(_profile_payload(**overrides))

    def test_digest_without_phases_is_unchanged(self) -> None:
        """Regression: the default path is byte-identical to the #166 output."""
        profile = self._profile()

        assert objectives_digest(profile) == objectives_digest(profile, phases=[])

    def test_digest_contains_the_current_phase_line(self) -> None:
        """The current phase renders with name, type and date range."""
        phases = [
            PeriodizationPhase.model_validate(_phase_payload()),
            PeriodizationPhase.model_validate(
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-16",
                    end_date="2027-02-01",
                )
            ),
        ]

        digest = objectives_digest(self._profile(), phases=phases, today=self.FROZEN_TODAY)

        assert "Current phase: Aerobic base (base), 2026-10-01 to 2026-12-15" in digest

    def test_digest_contains_the_load_focus_hint(self) -> None:
        """The current phase's load-scaling hint is exposed to the prompt."""
        phases = [PeriodizationPhase.model_validate(_phase_payload())]

        digest = objectives_digest(self._profile(), phases=phases, today=self.FROZEN_TODAY)

        assert f"Phase load focus: {PHASE_LOAD_HINTS['base']}" in digest

    def test_digest_contains_the_days_to_target_countdown(self) -> None:
        """The countdown to the objective's target date is rendered."""
        phases = [PeriodizationPhase.model_validate(_phase_payload())]
        expected = (date(2027, 9, 11) - self.FROZEN_TODAY).days

        digest = objectives_digest(self._profile(), phases=phases, today=self.FROZEN_TODAY)

        assert f"Days to target: {expected}" in digest

    def test_digest_reports_no_current_phase_outside_the_plan(self) -> None:
        """Today outside every phase renders the plan span instead."""
        phases = [PeriodizationPhase.model_validate(_phase_payload())]

        digest = objectives_digest(self._profile(), phases=phases, today=date(2027, 1, 15))

        assert "Current phase: none (plan spans 2026-10-01 to 2026-12-15)" in digest
        assert "Phase load focus" not in digest

    def test_digest_omits_the_countdown_without_a_target_date(self) -> None:
        """No target date on the primary goal means no countdown line."""
        phases = [PeriodizationPhase.model_validate(_phase_payload())]
        profile = self._profile(primary_goal=_goal_payload(target_date=None))

        digest = objectives_digest(profile, phases=phases, today=self.FROZEN_TODAY)

        assert "Days to target" not in digest
        assert "Current phase: Aerobic base (base)" in digest

    def test_digest_with_phases_respects_the_token_budget(self) -> None:
        """A maximal profile plus a full plan never exceeds the budget."""
        huge_secondaries = [_goal_payload(title="S" * 200, target_date=None) for _ in range(5)]
        profile = self._profile(
            primary_goal=_goal_payload(target_metric="m" * 100, target_value=123456.789),
            secondary_goals=huge_secondaries,
            priority_disciplines=[f"d{i}" * 16 for i in range(8)],
        )
        phases = [
            PeriodizationPhase.model_validate(_phase_payload(name="P" * 200, focus="F" * 200))
            for _ in range(6)
        ]

        digest = objectives_digest(profile, phases=phases, today=self.FROZEN_TODAY)

        assert len(digest) <= OBJECTIVES_DIGEST_MAX_CHARS

    def test_apply_objectives_digest_passes_phases_through(self) -> None:
        """apply_objectives_digest composes the base prompt with the phase state."""
        phases = [PeriodizationPhase.model_validate(_phase_payload())]

        composed = apply_objectives_digest(
            "BASE PROMPT", self._profile(), phases=phases, today=self.FROZEN_TODAY
        )

        assert composed.startswith("BASE PROMPT")
        assert "Current phase: Aerobic base (base)" in composed


class TestPhaseTypeEnumContract:
    """The phase_type enum matches the MADR-008 CHECK enum exactly."""

    def test_phase_types_match_the_madr008_enum(self) -> None:
        """base/build/peak/taper/recovery/competition, in MADR-008 order."""
        assert PHASE_TYPES == ("base", "build", "peak", "taper", "recovery", "competition")

    def test_phase_type_literal_accepts_every_enum_value(self) -> None:
        """Every enum value validates as a phase_type."""
        for phase_type in PHASE_TYPES:
            phase = PeriodizationPhase.model_validate(_phase_payload(phase_type=phase_type))

            assert phase.phase_type == phase_type


class TestPeriodizationRepository:
    """Persistence round-trip over the migrated SQLite database (AC3)."""

    async def _seed_objective(self, session: Any) -> int:
        """Persist the active objective profile and return its row id."""
        profile = ObjectiveProfile.model_validate(_profile_payload())
        await save_objective_profile(session, profile)
        row = await session.execute(text("SELECT id FROM athlete_objectives"))
        return int(row.scalar_one())

    async def test_list_returns_empty_without_phases(self, db_session_factory: Any) -> None:
        """An objective without phases lists as empty (graceful empty state)."""
        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)

            assert await list_phases(session, objective_id) == []

    async def test_replace_then_list_round_trips_every_field(self, db_session_factory: Any) -> None:
        """A saved plan reads back with every field intact."""
        plan = PeriodizationPlan.model_validate(
            _plan_payload(phases=[_phase_payload(notes="Coach note")])
        )

        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)
            saved = await replace_phases(session, objective_id, plan)
            loaded = await list_phases(session, objective_id)

        assert saved == list(plan.phases)
        assert loaded == list(plan.phases)

    async def test_replace_upserts_wholesale(self, db_session_factory: Any) -> None:
        """A second replace swaps the phase set instead of stacking plans."""
        first = PeriodizationPlan.model_validate(_plan_payload())
        second = PeriodizationPlan.model_validate(
            _plan_payload(
                phases=[
                    _phase_payload(
                        phase_type="peak",
                        name="Race peak",
                        start_date="2027-08-01",
                        end_date="2027-09-10",
                    )
                ]
            )
        )

        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)
            await replace_phases(session, objective_id, first)
            await replace_phases(session, objective_id, second)
            loaded = await list_phases(session, objective_id)
            rows = (
                await session.execute(text("SELECT COUNT(*) FROM periodization_phases"))
            ).scalar()

        assert [phase.name for phase in loaded] == ["Race peak"]
        assert rows == 1

    async def test_list_orders_by_start_date(self, db_session_factory: Any) -> None:
        """Phases read back ordered by start date, whatever the insert order."""
        plan = PeriodizationPlan.model_validate(_plan_payload())

        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)
            await replace_phases(session, objective_id, plan)
            # Raw-SQL tampering bypasses the plan validator: the second row
            # now starts BEFORE the first, and the read must still sort it.
            await session.execute(
                text(
                    "UPDATE periodization_phases SET start_date = '2026-09-01', "
                    "end_date = '2026-09-30' WHERE name = 'Aerobic base'"
                )
            )
            loaded = await list_phases(session, objective_id)

        assert [phase.start_date for phase in loaded] == ["2026-09-01", "2026-12-16"]

    async def test_delete_phases_removes_and_counts(self, db_session_factory: Any) -> None:
        """delete_phases removes every phase and reports the count."""
        plan = PeriodizationPlan.model_validate(_plan_payload())

        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)
            await replace_phases(session, objective_id, plan)

            removed = await delete_phases(session, objective_id)
            remaining = await list_phases(session, objective_id)
            removed_again = await delete_phases(session, objective_id)

        assert removed == 2
        assert remaining == []
        assert removed_again == 0

    async def test_deleting_the_objective_cascades(self, db_session_factory: Any) -> None:
        """Removing the parent objective removes its phases (MADR-008 CASCADE)."""
        from coach_web.objectives import AthleteObjective

        plan = PeriodizationPlan.model_validate(_plan_payload())

        async with db_session_factory() as session:
            objective_id = await self._seed_objective(session)
            await replace_phases(session, objective_id, plan)
            objective = await session.get(AthleteObjective, objective_id)
            assert objective is not None
            await session.delete(objective)
            await session.commit()
            rows = (
                await session.execute(text("SELECT COUNT(*) FROM periodization_phases"))
            ).scalar()

        assert rows == 0
