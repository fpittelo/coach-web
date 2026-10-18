"""Alembic migration contract for the persistence layer (#166/#167, MADR-008).

Migration 0001 creates ONLY the ``athlete_objectives`` table (KIS scoping:
the remaining MADR-008 tables arrive with their own stories as incremental
migrations). Migration 0002 (#167) adds ``periodization_phases`` — the
macrocycle phase plan per objective, FK-cascaded onto its parent row. The
contract pins the exact column set, the CHECK constraints, the foreign key
and the downgrade path, so the schema the ORM models against can never drift
silently. Migrations are also exercised through the packaged ``alembic.ini``
(script_location resolved via ``%(here)s``) with the database URL derived
from ``COACH_DB_PATH`` — the same wiring the container entrypoint uses at
startup (MADR-008: a failed migration aborts startup).
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet

from tests.conftest import alembic_config

EXPECTED_COLUMNS = (
    "id",
    "objective_type",
    "title",
    "description",
    "target_metric",
    "target_value",
    "target_date",
    "availability_notes",
    "status",
    "created_at",
    "updated_at",
)

OBJECTIVE_TYPE_CHECK = "objective_type IN ('outcome', 'process', 'milestone')"
STATUS_CHECK = "status IN ('active', 'achieved', 'abandoned')"

EXPECTED_PHASE_COLUMNS = (
    "id",
    "objective_id",
    "phase_type",
    "name",
    "start_date",
    "end_date",
    "focus",
    "weekly_hours_target",
    "notes",
    "created_at",
    "updated_at",
)

PHASE_TYPE_CHECK = "phase_type IN ('base', 'build', 'peak', 'taper', 'recovery', 'competition')"
PHASE_DATE_ORDER_CHECK = "start_date <= end_date"

EXPECTED_PLAN_DRAFT_COLUMNS = (
    "id",
    "week_start_date",
    "week_end_date",
    "content",
    "status",
    "approved_at",
    "github_issue_ref",
    "created_at",
    "updated_at",
)

PLAN_DRAFT_STATUS_CHECK = "status IN ('draft', 'submitted', 'approved', 'rejected', 'superseded')"
PLAN_DRAFT_WEEK_RANGE_CHECK = "week_start_date <= week_end_date"

EXPECTED_DEBRIEF_COLUMNS = (
    "id",
    "athlete_id",
    "session_external_id",
    "plan_session_key",
    "rpe",
    "notes_encrypted",
    "created_at",
    "updated_at",
)

DEBRIEF_RPE_RANGE_CHECK = "rpe BETWEEN 1 AND 10"

EXPECTED_WEEK_ADHERENCE_COLUMNS = (
    "id",
    "athlete_id",
    "week_key",
    "planned_count",
    "completed_count",
    "adherence_pct",
    "computed_at",
)

WEEK_ADHERENCE_PCT_RANGE_CHECK = "adherence_pct BETWEEN 0 AND 100"


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open the per-test database file with the stdlib driver."""
    return sqlite3.connect(db_path)


def _table_names(db_path: Path) -> set[str]:
    """Return every table name in the database."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _column_flags(db_path: Path) -> dict[str, tuple[bool, bool]]:
    """Return ``(notnull, is_pk)`` flags per column of athlete_objectives."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(athlete_objectives)").fetchall()
    return {row[1]: (bool(row[3]), bool(row[5])) for row in rows}


def _table_sql(db_path: Path) -> str:
    """Return the CREATE TABLE statement text of athlete_objectives."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='athlete_objectives'"
        ).fetchone()
    assert row is not None, "athlete_objectives table is missing"
    return str(row[0])


def _phase_table_sql(db_path: Path) -> str:
    """Return the CREATE TABLE statement text of periodization_phases."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='periodization_phases'"
        ).fetchone()
    assert row is not None, "periodization_phases table is missing"
    return str(row[0])


def _phase_column_flags(db_path: Path) -> dict[str, tuple[bool, bool]]:
    """Return ``(notnull, is_pk)`` flags per column of periodization_phases."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(periodization_phases)").fetchall()
    return {row[1]: (bool(row[3]), bool(row[5])) for row in rows}


def _phase_indexed_columns(db_path: Path) -> set[str]:
    """Return every column covered by an index on periodization_phases."""
    with _connect(db_path) as conn:
        indexes = conn.execute("PRAGMA index_list(periodization_phases)").fetchall()
        columns: set[str] = set()
        for index in indexes:
            columns.update(
                row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
    return columns


class TestInitialMigration:
    """Migration 0001 creates exactly the MADR-008 athlete_objectives table."""

    def test_upgrade_0001_creates_only_athlete_objectives(self, db_path: Path) -> None:
        """After upgrade 0001, athlete_objectives is the only domain table."""
        command.upgrade(alembic_config(), "0001")

        domain_tables = _table_names(db_path) - {"alembic_version", "sqlite_sequence"}
        assert domain_tables == {"athlete_objectives"}

    def test_table_columns_match_the_madr008_schema(self, db_path: Path) -> None:
        """The column set matches the MADR-008 schema sketch exactly, in order."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(athlete_objectives)").fetchall()
            ]

        assert tuple(columns) == EXPECTED_COLUMNS

    def test_required_columns_are_not_null(self, db_path: Path) -> None:
        """id/objective_type/title/status/created_at/updated_at are NOT NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _column_flags(db_path)
        for column in ("objective_type", "title", "status", "created_at", "updated_at"):
            assert flags[column][0] is True, column

    def test_optional_columns_are_nullable(self, db_path: Path) -> None:
        """description/target_metric/target_value/target_date/availability_notes allow NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _column_flags(db_path)
        for column in (
            "description",
            "target_metric",
            "target_value",
            "target_date",
            "availability_notes",
        ):
            assert flags[column][0] is False, column

    def test_id_is_the_primary_key(self, db_path: Path) -> None:
        """id is the single INTEGER primary key (AUTOINCREMENT rowid alias)."""
        command.upgrade(alembic_config(), "head")

        flags = _column_flags(db_path)
        assert flags["id"][1] is True
        assert [name for name, (_, pk) in flags.items() if pk] == ["id"]

    def test_check_constraints_are_declared(self, db_path: Path) -> None:
        """objective_type and status carry their MADR-008 CHECK constraints."""
        command.upgrade(alembic_config(), "head")

        sql = _table_sql(db_path)
        assert OBJECTIVE_TYPE_CHECK in sql
        assert STATUS_CHECK in sql

    def test_objective_type_check_rejects_unknown_values(self, db_path: Path) -> None:
        """Inserting an objective_type outside the enum fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="objective_type"):
                conn.execute(
                    "INSERT INTO athlete_objectives "
                    "(objective_type, title, status, created_at, updated_at) "
                    "VALUES ('fantasy', 't', 'active', 'c', 'u')"
                )

    def test_status_check_rejects_unknown_values(self, db_path: Path) -> None:
        """Inserting a status outside the enum fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="status"):
                conn.execute(
                    "INSERT INTO athlete_objectives "
                    "(objective_type, title, status, created_at, updated_at) "
                    "VALUES ('outcome', 't', 'retired', 'c', 'u')"
                )

    def test_status_defaults_to_active(self, db_path: Path) -> None:
        """A row inserted without an explicit status lands as 'active'."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO athlete_objectives "
                "(objective_type, title, created_at, updated_at) "
                "VALUES ('outcome', 't', 'c', 'u')"
            )
            status = conn.execute(
                "SELECT status FROM athlete_objectives WHERE title = 't'"
            ).fetchone()[0]

        assert status == "active"

    def test_downgrade_base_drops_the_table(self, db_path: Path) -> None:
        """Downgrade to base removes athlete_objectives (authored, per ADR)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "base")

        assert "athlete_objectives" not in _table_names(db_path)


class TestPeriodizationMigration:
    """Migration 0002 adds the MADR-008 periodization_phases table (#167)."""

    def test_upgrade_head_creates_both_domain_tables(self, db_path: Path) -> None:
        """After upgrade head, the MADR-008 domain tables exist (#167).

        Superseded for #168: migration 0003 adds the C3 ``plan_drafts``
        table. Superseded for #189: migration 0004 adds the C2 ``debriefs``
        table. Superseded for #234: migration 0005 adds the C3
        ``week_adherence`` metric table, so head now carries five domain
        tables.
        """
        command.upgrade(alembic_config(), "head")

        domain_tables = _table_names(db_path) - {"alembic_version", "sqlite_sequence"}
        assert domain_tables == {
            "athlete_objectives",
            "periodization_phases",
            "plan_drafts",
            "debriefs",
            "week_adherence",
        }

    def test_phase_table_columns_match_the_madr008_schema(self, db_path: Path) -> None:
        """The column set matches the MADR-008 schema sketch exactly, in order."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(periodization_phases)").fetchall()
            ]

        assert tuple(columns) == EXPECTED_PHASE_COLUMNS

    def test_phase_required_columns_are_not_null(self, db_path: Path) -> None:
        """objective_id/phase_type/name/dates/timestamps are NOT NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _phase_column_flags(db_path)
        for column in (
            "objective_id",
            "phase_type",
            "name",
            "start_date",
            "end_date",
            "created_at",
            "updated_at",
        ):
            assert flags[column][0] is True, column

    def test_phase_optional_columns_are_nullable(self, db_path: Path) -> None:
        """focus/weekly_hours_target/notes allow NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _phase_column_flags(db_path)
        for column in ("focus", "weekly_hours_target", "notes"):
            assert flags[column][0] is False, column

    def test_phase_id_is_the_primary_key(self, db_path: Path) -> None:
        """id is the single INTEGER primary key (AUTOINCREMENT rowid alias)."""
        command.upgrade(alembic_config(), "head")

        flags = _phase_column_flags(db_path)
        assert flags["id"][1] is True
        assert [name for name, (_, pk) in flags.items() if pk] == ["id"]

    def test_phase_check_constraints_are_declared(self, db_path: Path) -> None:
        """phase_type and the date order carry their MADR-008 CHECK constraints."""
        command.upgrade(alembic_config(), "head")

        sql = _phase_table_sql(db_path)
        assert PHASE_TYPE_CHECK in sql
        assert PHASE_DATE_ORDER_CHECK in sql

    def test_phase_type_check_rejects_unknown_values(self, db_path: Path) -> None:
        """Inserting a phase_type outside the enum fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO athlete_objectives "
                "(objective_type, title, created_at, updated_at) "
                "VALUES ('outcome', 't', 'c', 'u')"
            )
            objective_id = conn.execute(
                "SELECT id FROM athlete_objectives WHERE title = 't'"
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="phase_type"):
                conn.execute(
                    "INSERT INTO periodization_phases "
                    "(objective_id, phase_type, name, start_date, end_date, "
                    "created_at, updated_at) "
                    "VALUES (?, 'fantasy', 'n', '2026-01-01', '2026-02-01', 'c', 'u')",
                    (objective_id,),
                )

    def test_date_order_check_rejects_inverted_ranges(self, db_path: Path) -> None:
        """Inserting a phase with start_date after end_date fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO athlete_objectives "
                "(objective_type, title, created_at, updated_at) "
                "VALUES ('outcome', 't', 'c', 'u')"
            )
            objective_id = conn.execute(
                "SELECT id FROM athlete_objectives WHERE title = 't'"
            ).fetchone()[0]
            with pytest.raises(sqlite3.IntegrityError, match="date_order"):
                conn.execute(
                    "INSERT INTO periodization_phases "
                    "(objective_id, phase_type, name, start_date, end_date, "
                    "created_at, updated_at) "
                    "VALUES (?, 'base', 'n', '2026-02-01', '2026-01-01', 'c', 'u')",
                    (objective_id,),
                )

    def test_objective_id_foreign_key_rejects_unknown_parent(self, db_path: Path) -> None:
        """Inserting a phase for a nonexistent objective fails the FK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO periodization_phases "
                    "(objective_id, phase_type, name, start_date, end_date, "
                    "created_at, updated_at) "
                    "VALUES (99999, 'base', 'n', '2026-01-01', '2026-02-01', 'c', 'u')"
                )

    def test_objective_id_foreign_key_cascades_deletes(self, db_path: Path) -> None:
        """Deleting the parent objective removes its phases (MADR-008 CASCADE)."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute(
                "INSERT INTO athlete_objectives "
                "(objective_type, title, created_at, updated_at) "
                "VALUES ('outcome', 't', 'c', 'u')"
            )
            objective_id = conn.execute(
                "SELECT id FROM athlete_objectives WHERE title = 't'"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO periodization_phases "
                "(objective_id, phase_type, name, start_date, end_date, "
                "created_at, updated_at) "
                "VALUES (?, 'base', 'n', '2026-01-01', '2026-02-01', 'c', 'u')",
                (objective_id,),
            )
            conn.execute("DELETE FROM athlete_objectives WHERE id = ?", (objective_id,))
            remaining = conn.execute("SELECT COUNT(*) FROM periodization_phases").fetchone()[0]

        assert remaining == 0

    def test_objective_id_is_indexed(self, db_path: Path) -> None:
        """objective_id carries the MADR-008 index (per-objective lookups)."""
        command.upgrade(alembic_config(), "head")

        assert "objective_id" in _phase_indexed_columns(db_path)

    def test_upgrade_head_is_idempotent(self, db_path: Path) -> None:
        """Re-running upgrade head is a no-op and the version stays at 0006.

        Superseded for #168: migration 0003 (plan_drafts) extends the chain.
        Superseded for #189: migration 0004 (debriefs) extends the chain.
        Superseded for #234: migration 0005 (week_adherence) extends the chain.
        Superseded for #243: migration 0006 (encrypted debrief RPE) extends
        the chain.
        """
        command.upgrade(alembic_config(), "head")
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0006"

    def test_downgrade_to_0001_drops_the_phase_table(self, db_path: Path) -> None:
        """Downgrade to 0001 removes periodization_phases (authored, per ADR)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "0001")

        tables = _table_names(db_path)
        assert "periodization_phases" not in tables
        assert "athlete_objectives" in tables


def _plan_draft_table_sql(db_path: Path) -> str:
    """Return the CREATE TABLE statement text of plan_drafts."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plan_drafts'"
        ).fetchone()
    assert row is not None, "plan_drafts table is missing"
    return str(row[0])


def _plan_draft_column_flags(db_path: Path) -> dict[str, tuple[bool, bool]]:
    """Return ``(notnull, is_pk)`` flags per column of plan_drafts."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(plan_drafts)").fetchall()
    return {row[1]: (bool(row[3]), bool(row[5])) for row in rows}


def _plan_draft_indexed_columns(db_path: Path) -> set[str]:
    """Return every column covered by an index on plan_drafts."""
    with _connect(db_path) as conn:
        indexes = conn.execute("PRAGMA index_list(plan_drafts)").fetchall()
        columns: set[str] = set()
        for index in indexes:
            columns.update(
                row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
    return columns


class TestPlanDraftMigration:
    """Migration 0003 adds the MADR-008 plan_drafts table (#168, C3)."""

    def test_plan_draft_table_columns_match_the_madr008_schema(self, db_path: Path) -> None:
        """The column set matches the MADR-008 schema sketch exactly, in order."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(plan_drafts)").fetchall()]

        assert tuple(columns) == EXPECTED_PLAN_DRAFT_COLUMNS

    def test_plan_draft_required_columns_are_not_null(self, db_path: Path) -> None:
        """week bounds, content, status and timestamps are NOT NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _plan_draft_column_flags(db_path)
        for column in (
            "week_start_date",
            "week_end_date",
            "content",
            "status",
            "created_at",
            "updated_at",
        ):
            assert flags[column][0] is True, column

    def test_plan_draft_optional_columns_are_nullable(self, db_path: Path) -> None:
        """approved_at and github_issue_ref allow NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _plan_draft_column_flags(db_path)
        for column in ("approved_at", "github_issue_ref"):
            assert flags[column][0] is False, column

    def test_plan_draft_id_is_the_primary_key(self, db_path: Path) -> None:
        """id is the single INTEGER primary key (AUTOINCREMENT rowid alias)."""
        command.upgrade(alembic_config(), "head")

        flags = _plan_draft_column_flags(db_path)
        assert flags["id"][1] is True
        assert [name for name, (_, pk) in flags.items() if pk] == ["id"]

    def test_plan_draft_check_constraints_are_declared(self, db_path: Path) -> None:
        """status enum and week-range carry their MADR-008 CHECK constraints."""
        command.upgrade(alembic_config(), "head")

        sql = _plan_draft_table_sql(db_path)
        assert PLAN_DRAFT_STATUS_CHECK in sql
        assert PLAN_DRAFT_WEEK_RANGE_CHECK in sql

    def test_plan_draft_status_check_rejects_unknown_values(self, db_path: Path) -> None:
        """Inserting a status outside the enum fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="status"):
                conn.execute(
                    "INSERT INTO plan_drafts "
                    "(week_start_date, week_end_date, content, status, created_at, "
                    "updated_at) VALUES ('2026-10-05', '2026-10-11', 'c', 'lost', "
                    "'t1', 't2')"
                )

    def test_plan_draft_week_range_check_rejects_inverted_ranges(self, db_path: Path) -> None:
        """Inserting a draft with an inverted week range fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="week_range"):
                conn.execute(
                    "INSERT INTO plan_drafts "
                    "(week_start_date, week_end_date, content, status, created_at, "
                    "updated_at) VALUES ('2026-10-11', '2026-10-05', 'c', 'draft', "
                    "'t1', 't2')"
                )

    def test_plan_draft_status_defaults_to_draft(self, db_path: Path) -> None:
        """A row inserted without an explicit status lands as 'draft'."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO plan_drafts "
                "(week_start_date, week_end_date, content, created_at, updated_at) "
                "VALUES ('2026-10-05', '2026-10-11', 'c', 't1', 't2')"
            )
            status = conn.execute("SELECT status FROM plan_drafts").fetchone()[0]

        assert status == "draft"

    def test_week_start_date_is_indexed(self, db_path: Path) -> None:
        """week_start_date carries the MADR-008 index (per-week lookups)."""
        command.upgrade(alembic_config(), "head")

        assert "week_start_date" in _plan_draft_indexed_columns(db_path)

    def test_downgrade_to_0002_drops_the_plan_draft_table(self, db_path: Path) -> None:
        """Downgrade to 0002 removes plan_drafts (authored, per ADR)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "0002")

        tables = _table_names(db_path)
        assert "plan_drafts" not in tables
        assert "periodization_phases" in tables


def _debrief_column_flags(db_path: Path) -> dict[str, tuple[bool, bool]]:
    """Return ``(notnull, is_pk)`` flags per column of debriefs."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(debriefs)").fetchall()
    return {row[1]: (bool(row[3]), bool(row[5])) for row in rows}


def _debrief_table_sql(db_path: Path) -> str:
    """Return the CREATE TABLE statement text of debriefs."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='debriefs'"
        ).fetchone()
    assert row is not None, "debriefs table is missing"
    return str(row[0])


def _debrief_indexed_columns(db_path: Path) -> set[str]:
    """Return every column covered by an index on debriefs."""
    with _connect(db_path) as conn:
        indexes = conn.execute("PRAGMA index_list(debriefs)").fetchall()
        columns: set[str] = set()
        for index in indexes:
            columns.update(
                row[2] for row in conn.execute(f"PRAGMA index_info({index[1]})").fetchall()
            )
    return columns


class TestDebriefMigration:
    """Migration 0004 adds the debriefs table (MADR-008 C2, #189).

    The debrief is health data (nLPD Art. 5(c)): the notes column is
    Fernet ciphertext by contract (``notes_encrypted``), and — since the
    #243 classification-drift fix (migration 0006) — the RPE column is a
    nullable Text carrying the Fernet token too (the plaintext-integer
    CHECK was dropped: a ciphertext column cannot carry a numeric range
    CHECK; the 1–10 bound is enforced at the Pydantic boundary). The
    athlete dimension plus the plan-session key are indexed for the
    per-athlete pending derivation. The contract pins the exact column
    set, the nullable flags, the indexes and the downgrade path.
    """

    def test_debrief_table_columns_match_the_story_schema(self, db_path: Path) -> None:
        """The column set matches the #189 schema exactly, in order."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(debriefs)").fetchall()]

        assert tuple(columns) == EXPECTED_DEBRIEF_COLUMNS

    def test_debrief_required_columns_are_not_null(self, db_path: Path) -> None:
        """athlete_id, plan_session_key and timestamps are NOT NULL.

        #243: ``rpe`` left the NOT NULL set — the column now stores the
        Fernet token under the ``notes_encrypted`` typing/nullable pattern.
        """
        command.upgrade(alembic_config(), "head")

        flags = _debrief_column_flags(db_path)
        for column in (
            "athlete_id",
            "plan_session_key",
            "created_at",
            "updated_at",
        ):
            assert flags[column][0] is True, column

    def test_debrief_optional_columns_are_nullable(self, db_path: Path) -> None:
        """session_external_id (ICU link, 3.2), notes and rpe allow NULL.

        #243: ``rpe`` is nullable like ``notes_encrypted`` — the Fernet
        token pattern's typing (a tampered/absent value stays NULL instead
        of failing the row write).
        """
        command.upgrade(alembic_config(), "head")

        flags = _debrief_column_flags(db_path)
        for column in ("session_external_id", "notes_encrypted", "rpe"):
            assert flags[column][0] is False, column

    def test_debrief_id_is_the_primary_key(self, db_path: Path) -> None:
        """id is the single INTEGER primary key (AUTOINCREMENT rowid alias)."""
        command.upgrade(alembic_config(), "head")

        flags = _debrief_column_flags(db_path)
        assert flags["id"][1] is True
        assert [name for name, (_, pk) in flags.items() if pk] == ["id"]

    def test_debrief_rpe_check_constraint_is_absent(self, db_path: Path) -> None:
        """#243: the numeric RPE range CHECK is gone from the schema.

        A Fernet token cannot satisfy ``rpe BETWEEN 1 AND 10`` — the CHECK
        was dropped with the plaintext column (the 1–10 bound lives at the
        Pydantic boundary, pinned in ``tests/test_debrief.py``).
        """
        command.upgrade(alembic_config(), "head")

        sql = _debrief_table_sql(db_path)
        assert DEBRIEF_RPE_RANGE_CHECK not in sql

    def test_debrief_rpe_column_accepts_fernet_tokens(self, db_path: Path) -> None:
        """#243: a Fernet token inserts cleanly (no numeric CHECK rejects it)."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO debriefs "
                "(athlete_id, plan_session_key, rpe, created_at, updated_at) "
                "VALUES (1, '2026-10-10', 'gAAAAABmXk3test-token-shape', 't1', 't2')"
            )
            stored = conn.execute("SELECT rpe FROM debriefs").fetchone()[0]

        assert stored == "gAAAAABmXk3test-token-shape"

    def test_debrief_athlete_and_session_key_are_indexed(self, db_path: Path) -> None:
        """athlete_id and plan_session_key carry indexes (pending derivation)."""
        command.upgrade(alembic_config(), "head")

        indexed = _debrief_indexed_columns(db_path)
        assert "athlete_id" in indexed
        assert "plan_session_key" in indexed

    def test_downgrade_to_0003_drops_the_debrief_table(self, db_path: Path) -> None:
        """Downgrade to 0003 removes debriefs (data-destructive; documented)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "0003")

        tables = _table_names(db_path)
        assert "debriefs" not in tables
        assert "plan_drafts" in tables


def _week_adherence_column_flags(db_path: Path) -> dict[str, tuple[bool, bool]]:
    """Return ``(notnull, is_pk)`` flags per column of week_adherence."""
    with _connect(db_path) as conn:
        rows = conn.execute("PRAGMA table_info(week_adherence)").fetchall()
    return {row[1]: (bool(row[3]), bool(row[5])) for row in rows}


def _week_adherence_table_sql(db_path: Path) -> str:
    """Return the CREATE TABLE statement text of week_adherence."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='week_adherence'"
        ).fetchone()
    assert row is not None, "week_adherence table is missing"
    return str(row[0])


class TestWeekAdherenceMigration:
    """Migration 0005 adds the week_adherence table (#234, C3).

    The metric row carries COUNTS AND PERCENTAGES ONLY (AC2): the exact
    column set is pinned so no health content (RPE, notes, wellness values)
    can ever join the C3 metric row. The (athlete_id, week_key) pair is
    UNIQUE — the debrief-save upsert is idempotent per athlete and week —
    and the percentage is CHECK-bounded to 0..100.
    """

    def test_week_adherence_table_columns_match_the_story_schema(self, db_path: Path) -> None:
        """The column set is exactly the metric columns, in order (C3 pin)."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            columns = [
                row[1] for row in conn.execute("PRAGMA table_info(week_adherence)").fetchall()
            ]

        assert tuple(columns) == EXPECTED_WEEK_ADHERENCE_COLUMNS

    def test_week_adherence_required_columns_are_not_null(self, db_path: Path) -> None:
        """athlete_id, week_key, both counts, the percentage and computed_at are NOT NULL."""
        command.upgrade(alembic_config(), "head")

        flags = _week_adherence_column_flags(db_path)
        for column in (
            "athlete_id",
            "week_key",
            "planned_count",
            "completed_count",
            "adherence_pct",
            "computed_at",
        ):
            assert flags[column][0] is True, column

    def test_week_adherence_id_is_the_primary_key(self, db_path: Path) -> None:
        """id is the single INTEGER primary key (AUTOINCREMENT rowid alias)."""
        command.upgrade(alembic_config(), "head")

        flags = _week_adherence_column_flags(db_path)
        assert flags["id"][1] is True
        assert [name for name, (_, pk) in flags.items() if pk] == ["id"]

    def test_week_adherence_pct_check_is_declared(self, db_path: Path) -> None:
        """The percentage range CHECK is declared on the table."""
        command.upgrade(alembic_config(), "head")

        sql = _week_adherence_table_sql(db_path)
        assert WEEK_ADHERENCE_PCT_RANGE_CHECK in sql

    def test_week_adherence_pct_check_rejects_out_of_range_values(self, db_path: Path) -> None:
        """Inserting a percentage below 0 or above 100 fails the CHECK."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            for sql in (
                "INSERT INTO week_adherence "
                "(athlete_id, week_key, planned_count, completed_count, adherence_pct, "
                "computed_at) VALUES (1, '2026-W41', 2, 1, -0.5, 't1')",
                "INSERT INTO week_adherence "
                "(athlete_id, week_key, planned_count, completed_count, adherence_pct, "
                "computed_at) VALUES (1, '2026-W41', 2, 1, 100.5, 't1')",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="pct_range"):
                    conn.execute(sql)

    def test_week_adherence_athlete_week_pair_is_unique(self, db_path: Path) -> None:
        """A second metric row for the same (athlete, week) fails the UNIQUE."""
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            conn.execute(
                "INSERT INTO week_adherence "
                "(athlete_id, week_key, planned_count, completed_count, adherence_pct, "
                "computed_at) VALUES (1, '2026-W41', 2, 1, 50.0, 't1')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO week_adherence "
                    "(athlete_id, week_key, planned_count, completed_count, adherence_pct, "
                    "computed_at) VALUES (1, '2026-W41', 2, 2, 100.0, 't2')"
                )

    def test_downgrade_to_0004_drops_the_week_adherence_table(self, db_path: Path) -> None:
        """Downgrade to 0004 removes week_adherence (data-destructive; documented)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "0004")

        tables = _table_names(db_path)
        assert "week_adherence" not in tables
        assert "debriefs" in tables


def _seed_plaintext_debrief(db_path: Path, *, rpe: int) -> int:
    """Insert one plaintext-RPE debrief row at 0005 (the pre-0006 state).

    Returns the row id. (NULL RPEs cannot exist at 0005 — the column is NOT
    NULL there — so the migration's NULL filter is defensive only; the
    nullable-column NULL behavior is pinned at the service level in
    ``tests/test_debrief.py``.)
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO debriefs "
            "(athlete_id, plan_session_key, rpe, notes_encrypted, created_at, updated_at) "
            "VALUES (1, '2026-10-06', ?, NULL, 't1', 't2')",
            (rpe,),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


class TestDebriefRpeEncryptionMigration:
    """Migration 0006 encrypts the debrief RPE in place (#243, MADR-0008 C2).

    The #236 security review found the RPE column drifting from its
    MADR-0008 C2 classification (plaintext Integer while the notes were
    Fernet-encrypted). Migration 0006 closes the drift: upgrade encrypts
    every existing plaintext RPE in place (NULL stays NULL) and drops the
    numeric range CHECK; downgrade decrypts back to the plaintext integer
    and restores the CHECK (dev rollback symmetry).
    """

    def test_upgrade_encrypts_existing_plaintext_rpes(self, db_path: Path) -> None:
        """Upgrade 0005→head turns the seeded plaintext RPE into a token."""
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)

        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            stored = conn.execute("SELECT rpe FROM debriefs WHERE id = ?", (row_id,)).fetchone()[0]
        assert stored != 7
        assert stored != "7"
        assert str(stored).startswith("gAAAA")

    def test_upgrade_stored_token_decrypts_to_the_plaintext_value(self, db_path: Path) -> None:
        """The migrated token decrypts to the original RPE with the lane key."""
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)

        command.upgrade(alembic_config(), "head")

        key = os.environ["DATA_ENCRYPTION_KEY"]
        with _connect(db_path) as conn:
            stored = conn.execute("SELECT rpe FROM debriefs WHERE id = ?", (row_id,)).fetchone()[0]
        plaintext = Fernet(key.encode("ascii")).decrypt(str(stored).encode("ascii"))
        assert plaintext.decode("utf-8") == "7"

    def test_upgrade_without_key_fails_before_any_ddl(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing key → RuntimeError before any DDL; no tmp-table leftover (#306).

        The cipher must be resolved/validated BEFORE the batch alter runs:
        when the key is absent, the migration fails cleanly with the
        debriefs table untouched and no ``_alembic_tmp_debriefs`` residue —
        the lane stays recoverable (a plain re-run with the key present
        completes the upgrade).
        """
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)

        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        with pytest.raises(RuntimeError, match="DATA_ENCRYPTION_KEY is not configured"):
            command.upgrade(alembic_config(), "head")

        tables = _table_names(db_path)
        assert "_alembic_tmp_debriefs" not in tables
        with _connect(db_path) as conn:
            stored = conn.execute("SELECT rpe FROM debriefs WHERE id = ?", (row_id,)).fetchone()[0]
        assert stored == 7

    def test_upgrade_recovers_from_leftover_tmp_table(self, db_path: Path) -> None:
        """A leftover _alembic_tmp_debriefs no longer blocks the upgrade (#306).

        The batch alter's ``CREATE TABLE _alembic_tmp_debriefs`` persisted
        after a previously interrupted (non-atomic) run, bricking every
        subsequent start with ``table already exists``. The migration now
        drops the transient leftover before the batch alter, so recovery is
        automatic.
        """
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)

        # Simulate the interrupted-run residue: the batch alter's tmp table.
        with _connect(db_path) as conn:
            conn.execute("CREATE TABLE _alembic_tmp_debriefs (id INTEGER PRIMARY KEY)")

        command.upgrade(alembic_config(), "head")

        tables = _table_names(db_path)
        assert "_alembic_tmp_debriefs" not in tables
        with _connect(db_path) as conn:
            stored = conn.execute("SELECT rpe FROM debriefs WHERE id = ?", (row_id,)).fetchone()[0]
        assert str(stored).startswith("gAAAA")

    def test_downgrade_restores_plaintext_integers_and_the_check(self, db_path: Path) -> None:
        """Downgrade head→0005 decrypts back to the plaintext int + CHECK."""
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)
        command.upgrade(alembic_config(), "head")

        command.downgrade(alembic_config(), "0005")

        with _connect(db_path) as conn:
            stored = conn.execute("SELECT rpe FROM debriefs WHERE id = ?", (row_id,)).fetchone()[0]
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='debriefs'"
            ).fetchone()
        assert stored == 7
        assert isinstance(stored, int)
        assert DEBRIEF_RPE_RANGE_CHECK in str(sql_row[0])

    def test_upgrade_leaves_no_ascii_plaintext_rpe_residue_in_db_or_wal(
        self, db_path: Path
    ) -> None:
        """The migration path gets the DB+WAL residue scan (#259 AC3).

        The engine connections checkpoint and REMOVE the ``-wal`` side file
        when they close, so the migration's transient WAL would never
        survive to scan time and the residue claim would pass vacuously.
        Holding a second WAL-mode connection open across the upgrade keeps
        the migration's page images materialized — the scan below is
        unconditional (the WAL genuinely materialized in this path; there
        is no skip branch).

        Scan scope, documented: the seeded plaintext is the INTEGER 7 — its
        SQLite record encoding is a binary varint with no distinctive byte
        pattern, so the byte scan targets the ASCII leak forms (the JSON
        API shape the encrypted-at-rest tests pin). Residue of the binary
        form in SQLite free space is residual risk R4, owned by the
        encrypted-backup story (MADR-0008) — deliberately out of scope here.
        """
        command.upgrade(alembic_config(), "0005")
        row_id = _seed_plaintext_debrief(db_path, rpe=7)

        holder = _connect(db_path)
        try:
            holder.execute("PRAGMA journal_mode=WAL").fetchone()
            holder.execute("SELECT count(*) FROM debriefs").fetchone()

            command.upgrade(alembic_config(), "head")

            with _connect(db_path) as conn:
                stored = conn.execute(
                    "SELECT rpe FROM debriefs WHERE id = ?", (row_id,)
                ).fetchone()[0]
            assert str(stored).startswith("gAAAA")
            db_raw = db_path.read_bytes()
            assert b'"rpe": 7' not in db_raw
            wal_path = Path(str(db_path) + "-wal")
            assert wal_path.exists()
            wal_raw = wal_path.read_bytes()
            # Positive control: the migration's own write product is inside
            # the scanned window — the residue scan can never pass vacuously.
            assert str(stored).encode("ascii") in wal_raw
            assert b'"rpe": 7' not in wal_raw
        finally:
            holder.close()
