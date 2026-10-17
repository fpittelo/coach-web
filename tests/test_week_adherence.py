"""Weekly adherence metric — computation & debrief-save upsert (#234).

Epic #213 S4: the ≥85% weekly plan-adherence target becomes observable
instead of aspirational. The metric is computed from EXISTING DB compliance
data only — planned sessions from approved plan drafts' day rows, completed
sessions from the athlete's debrief rows — and persisted as COUNTS AND
PERCENTAGES ONLY (MADR-008 C3: no RPE, no notes, no wellness values ever
join the metric row). The contract pinned here:

- computation (``compute_week_adherence``): adherence % = completed planned
  sessions / planned sessions; a debrief for a session the plan no longer
  contains never inflates the metric (intersection semantics, so adherence
  can never exceed 100%); a week with zero planned sessions yields NO
  metric (0/0 noise avoided);
- the ISO week key derivation (``iso_week_key``) groups Monday..Sunday of
  one ISO week under one ``YYYY-Www`` key, ISO-year boundaries included;
- persistence (``upsert_week_adherence``): the week_adherence row carries
  exactly the metric columns and is idempotent per (athlete, week) — a
  re-save updates the row, never duplicates it; the metric is scoped to the
  debriefing athlete (per-athlete isolation, #189 AC4 posture).
"""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from coach_web.analysis import (
    WeekAdherenceMetric,
    compute_week_adherence,
    iso_week_key,
)
from coach_web.debrief import DebriefRow, upsert_week_adherence
from coach_web.microcycle import (
    DaySlot,
    PlanDraftRow,
    WeeklyPlanDraft,
    build_week_markdown,
    week_bounds,
)

METRIC_ATHLETE_ID = 1
OTHER_ATHLETE_ID = 42

TEST_ENCRYPTION_KEY = Fernet.generate_key()
"""Per-module Fernet key — generated, never a committed literal (gitleaks)."""

TEST_CIPHER = Fernet(TEST_ENCRYPTION_KEY)


def _day(**overrides: object) -> DaySlot:
    """Return a valid day slot with optional overrides."""
    payload: dict[str, object] = {
        "day_of_week": "mon",
        "date": "2026-10-05",
        "session_title": "Easy spin",
        "focus": None,
        "zones": [],
        "planned_tss": 45,
        "duration_minutes": 60,
        "rest_day": False,
    }
    payload.update(overrides)
    return DaySlot.model_validate(payload)


def _week_draft(**overrides: object) -> WeeklyPlanDraft:
    """Return a valid weekly draft whose Monday is 2026-10-05 (ISO 2026-W41)."""
    payload: dict[str, object] = {
        "week_id": "2026-W41",
        "title": "Base week",
        "summary": "Endurance-focused base week.",
        "rationale": None,
        "days": [
            _day(day_of_week="mon", date="2026-10-05", session_title="Easy spin"),
            _day(
                day_of_week="tue",
                date="2026-10-06",
                session_title="Threshold 3x12",
                planned_tss=95,
                duration_minutes=80,
            ),
        ],
        "total_tss": 140,
        "hard_days": 1,
        "easy_days": 1,
    }
    payload.update(overrides)
    return WeeklyPlanDraft.model_validate(payload)


async def _seed_approved_draft(session: Any, draft: WeeklyPlanDraft) -> None:
    """Persist one approved plan_drafts row through the shared session."""
    now = datetime.now(UTC).isoformat()
    week_start, week_end = week_bounds(draft.week_id)
    session.add(
        PlanDraftRow(
            week_start_date=week_start,
            week_end_date=week_end,
            content=build_week_markdown(draft),
            status="approved",
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()


async def _seed_debrief(session: Any, *, athlete_id: int, plan_session_key: str) -> None:
    """Persist one debrief row directly (the upsert reads keys only).

    #243: the RPE is seeded the way the save path stores it — the Fernet
    token (the metric itself never reads the column; C3 counts only).
    """
    now = datetime.now(UTC).isoformat()
    session.add(
        DebriefRow(
            athlete_id=athlete_id,
            session_external_id=None,
            plan_session_key=plan_session_key,
            rpe=TEST_CIPHER.encrypt(b"5").decode("ascii"),
            notes_encrypted=None,
            created_at=now,
            updated_at=now,
        )
    )
    await session.commit()


def _metric_rows(db_path: Path) -> list[tuple[int, str, int, int, float]]:
    """Return (athlete, week, planned, completed, pct) for every metric row."""
    with sqlite3.connect(db_path) as conn:
        return [
            (int(row[0]), str(row[1]), int(row[2]), int(row[3]), float(row[4]))
            for row in conn.execute(
                "SELECT athlete_id, week_key, planned_count, completed_count, "
                "adherence_pct FROM week_adherence ORDER BY id"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# Pure computation (#234 AC1)
# ---------------------------------------------------------------------------


class TestWeekAdherenceComputation:
    """compute_week_adherence — planned vs completed session keys → metric."""

    def test_adherence_is_completed_over_planned(self) -> None:
        """Three of four planned sessions debriefed → 75.0% with exact counts."""
        metric = compute_week_adherence(
            "2026-W41",
            planned_keys={"2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"},
            completed_keys={"2026-10-05", "2026-10-06", "2026-10-07"},
        )

        assert metric == WeekAdherenceMetric(
            week_key="2026-W41", planned_count=4, completed_count=3, adherence_pct=75.0
        )

    def test_full_week_is_100_percent(self) -> None:
        """Every planned session debriefed → 100.0%."""
        metric = compute_week_adherence(
            "2026-W41",
            planned_keys={"2026-10-05", "2026-10-06"},
            completed_keys={"2026-10-05", "2026-10-06"},
        )

        assert metric is not None
        assert metric.adherence_pct == 100.0
        assert metric.planned_count == 2
        assert metric.completed_count == 2

    def test_zero_planned_week_yields_no_metric(self) -> None:
        """A week without planned sessions yields None — no 0/0 metric row."""
        assert (
            compute_week_adherence("2026-W41", planned_keys=set(), completed_keys={"2026-10-05"})
            is None
        )

    def test_debriefs_outside_the_plan_never_inflate_adherence(self) -> None:
        """A debrief for an unplanned session is not a completed planned session."""
        metric = compute_week_adherence(
            "2026-W41",
            planned_keys={"2026-10-05", "2026-10-06"},
            completed_keys={"2026-10-05", "2026-10-06", "2099-01-01"},
        )

        assert metric is not None
        assert metric.completed_count == 2
        assert metric.adherence_pct == 100.0

    def test_metric_model_rejects_out_of_range_percentage(self) -> None:
        """The percentage is CHECK-bounded to 0..100 (schema strictness)."""
        with pytest.raises(ValidationError):
            WeekAdherenceMetric(
                week_key="2026-W41", planned_count=1, completed_count=1, adherence_pct=150.0
            )

    def test_metric_model_rejects_non_integer_counts(self) -> None:
        """The counts are strict integers (Pydantic v2 strict schema)."""
        with pytest.raises(ValidationError):
            WeekAdherenceMetric.model_validate(
                {
                    "week_key": "2026-W41",
                    "planned_count": "2",
                    "completed_count": 1,
                    "adherence_pct": 100.0,
                }
            )


class TestIsoWeekKey:
    """iso_week_key — the metric row's week identity."""

    def test_monday_and_sunday_share_the_week_key(self) -> None:
        """Monday..Sunday of one ISO week map to one YYYY-Www key."""
        assert iso_week_key(date(2026, 10, 5)) == "2026-W41"
        assert iso_week_key(date(2026, 10, 11)) == "2026-W41"

    def test_iso_year_boundary_stays_in_its_own_iso_year(self) -> None:
        """2026-12-28 belongs to ISO week 53 of 2026 — not 2027."""
        assert iso_week_key(date(2026, 12, 28)) == "2026-W53"


# ---------------------------------------------------------------------------
# Persistence roundtrip on the debrief-save path (#234 AC2)
# ---------------------------------------------------------------------------


class TestWeekAdherenceUpsert:
    """upsert_week_adherence — recompute + idempotent upsert per (athlete, week)."""

    async def test_upsert_persists_counts_and_percentage(
        self, db_session_factory, db_path: Path
    ) -> None:
        """One planned + one debriefed session → a 50.0% metric row (C3 counts)."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await _seed_debrief(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

            metric = await upsert_week_adherence(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

        assert metric == WeekAdherenceMetric(
            week_key="2026-W41", planned_count=2, completed_count=1, adherence_pct=50.0
        )
        assert _metric_rows(db_path) == [(METRIC_ATHLETE_ID, "2026-W41", 2, 1, 50.0)]

    async def test_resave_upserts_a_single_row(self, db_session_factory, db_path: Path) -> None:
        """A re-save recomputes and updates — never a duplicate row."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await _seed_debrief(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )
            await upsert_week_adherence(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )
            await upsert_week_adherence(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

        assert _metric_rows(db_path) == [(METRIC_ATHLETE_ID, "2026-W41", 2, 1, 50.0)]

    async def test_zero_planned_week_writes_no_row(self, db_session_factory, db_path: Path) -> None:
        """No approved draft for the week → no metric, no row (0/0 noise avoided)."""
        async with db_session_factory() as session:
            await _seed_debrief(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

            metric = await upsert_week_adherence(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

        assert metric is None
        assert _metric_rows(db_path) == []

    async def test_metric_is_scoped_to_the_athlete(self, db_session_factory, db_path: Path) -> None:
        """Athlete B's metric never counts athlete A's debriefs (#189 AC4 posture)."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await _seed_debrief(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

            metric = await upsert_week_adherence(
                session, athlete_id=OTHER_ATHLETE_ID, plan_session_key="2026-10-05"
            )

        assert metric is not None
        assert metric.completed_count == 0
        assert metric.adherence_pct == 0.0
        assert _metric_rows(db_path) == [(OTHER_ATHLETE_ID, "2026-W41", 2, 0, 0.0)]

    async def test_metric_row_carries_no_health_content(
        self, db_session_factory, db_path: Path
    ) -> None:
        """C3 pin: the persisted row exposes exactly the metric columns."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await _seed_debrief(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )
            await upsert_week_adherence(
                session, athlete_id=METRIC_ATHLETE_ID, plan_session_key="2026-10-05"
            )

        with sqlite3.connect(db_path) as conn:
            row = conn.execute("SELECT * FROM week_adherence").fetchone()
            columns = [info[1] for info in conn.execute("PRAGMA table_info(week_adherence)")]

        assert tuple(columns) == (
            "id",
            "athlete_id",
            "week_key",
            "planned_count",
            "completed_count",
            "adherence_pct",
            "computed_at",
        )
        assert row is not None
        assert len(row) == 7
        # Counts and a percentage — never an RPE, never notes, never wellness.
        assert row[3] == 2 and row[4] == 1 and row[5] == 50.0
