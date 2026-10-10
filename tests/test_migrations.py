"""Alembic migration contract for the persistence layer (#166, MADR-008).

Migration 0001 creates ONLY the ``athlete_objectives`` table (KIS scoping:
the remaining MADR-008 tables arrive with their own stories as incremental
migrations). The contract pins the exact column set, the CHECK constraints,
the ``active`` default and the downgrade path, so the schema the ORM models
against can never drift silently. Migrations are also exercised through the
packaged ``alembic.ini`` (script_location resolved via ``%(here)s``) with the
database URL derived from ``COACH_DB_PATH`` — the same wiring the container
entrypoint uses at startup (MADR-008: a failed migration aborts startup).
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


class TestInitialMigration:
    """Migration 0001 creates exactly the MADR-008 athlete_objectives table."""

    def test_upgrade_head_creates_only_athlete_objectives(self, db_path: Path) -> None:
        """After upgrade head, athlete_objectives is the only domain table."""
        command.upgrade(alembic_config(), "head")

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

    def test_upgrade_head_is_idempotent(self, db_path: Path) -> None:
        """Re-running upgrade head is a no-op and the version stays at 0001."""
        command.upgrade(alembic_config(), "head")
        command.upgrade(alembic_config(), "head")

        with _connect(db_path) as conn:
            version = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        assert version == "0001"

    def test_downgrade_base_drops_the_table(self, db_path: Path) -> None:
        """Downgrade to base removes athlete_objectives (authored, per ADR)."""
        command.upgrade(alembic_config(), "head")
        command.downgrade(alembic_config(), "base")

        assert "athlete_objectives" not in _table_names(db_path)
