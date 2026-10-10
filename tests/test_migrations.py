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

import sqlite3
from pathlib import Path

import pytest
from alembic import command

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
        """After upgrade head, the MADR-008 C1 tables exist (#167).

        Superseded for #168: migration 0003 adds the C3 ``plan_drafts``
        table, so head now carries three domain tables.
        """
        command.upgrade(alembic_config(), "head")

        domain_tables = _table_names(db_path) - {"alembic_version", "sqlite_sequence"}
        assert domain_tables == {
            "athlete_objectives",
            "periodization_phases",
            "plan_drafts",
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
        """Re-running upgrade head is a no-op and the version stays at 0003.

        Superseded for #168: migration 0003 (plan_drafts) extends the chain.
        """
        command.upgrade(alembic_config(), "head")
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0003"

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
