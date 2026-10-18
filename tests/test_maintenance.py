"""Tests for persistence hygiene: VACUUM reclaim + encrypted backup/restore (#274).

Covers the #274 acceptance criteria at the unit level:

- AC1: the VACUUM step reclaims free pages left by deleted plaintext rows
  (``PRAGMA freelist_count`` drops to 0 and the file shrinks).
- AC2: backup produces a Fernet-encrypted artifact; restore into a fresh
  database file round-trips the data intact; wrong/missing keys fail closed.
- CLI: ``python -m coach_web.maintenance`` dispatches the three verbs.
"""

import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from coach_web.maintenance import (
    backup_database,
    restore_database,
    vacuum_database,
)
from tests.conftest import alembic_config  # noqa: I001


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a plain sync connection (mirrors tests/test_migrations.py)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_and_delete_plaintext(db_path: Path, rows: int = 200) -> None:
    """Migrate to head, insert *rows* plaintext debriefs, then delete them all.

    The deleted rows leave free pages behind (the #274 residue scenario after
    migration 0006's in-place rewrite); VACUUM must reclaim them.
    """
    from alembic import command

    command.upgrade(alembic_config(), "head")
    conn = _connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
            "rpe, notes_encrypted) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    f"2026-W37#1#{i}",
                    "2026-09-01T00:00:00+00:00",
                    "2026-09-01T00:00:00+00:00",
                    "7",
                    f"note-{i}",
                )
                for i in range(rows)
            ],
        )
        conn.commit()
        conn.execute("DELETE FROM debriefs")
        conn.commit()
    finally:
        conn.close()


def _freelist_count(db_path: Path) -> int:
    conn = _connect(db_path)
    try:
        (count,) = conn.execute("PRAGMA freelist_count").fetchone()
        return int(count)
    finally:
        conn.close()


class TestVacuumReclaimsFreePages:
    """AC1 — deleted plaintext pages are reclaimed by the VACUUM step."""

    def test_vacuum_drops_freelist_to_zero(self, db_path: Path) -> None:
        _seed_and_delete_plaintext(db_path)
        assert _freelist_count(db_path) > 0
        vacuum_database(str(db_path))
        assert _freelist_count(db_path) == 0

    def test_vacuum_shrinks_the_database_file(self, db_path: Path) -> None:
        _seed_and_delete_plaintext(db_path)
        size_before = db_path.stat().st_size
        vacuum_database(str(db_path))
        assert db_path.stat().st_size < size_before

    def test_vacuum_preserves_live_data(self, db_path: Path) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'k', '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00', '7', 'keep')"
            )
            conn.commit()
        finally:
            conn.close()
        vacuum_database(str(db_path))
        conn = _connect(db_path)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM debriefs").fetchone()
        finally:
            conn.close()
        assert count == 1


class TestEncryptedBackupRestore:
    """AC2 — encrypted-at-rest backup artifact + tested restore path."""

    def test_backup_artifact_is_fernet_encrypted(self, db_path: Path, tmp_path: Path) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'k', '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00', '7', 'secret-note')"
            )
            conn.commit()
        finally:
            conn.close()
        backup_path = tmp_path / "coach.db.backup"
        key = Fernet.generate_key()
        backup_database(str(db_path), str(backup_path), key)
        raw = backup_path.read_bytes()
        # The artifact must NOT contain the plaintext marker.
        assert b"secret-note" not in raw
        # And it must decode as a valid Fernet token under the backup key.
        Fernet(key).decrypt(raw)

    def test_restore_round_trips_the_data(self, db_path: Path, tmp_path: Path) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        conn = _connect(db_path)
        try:
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'k1', '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00', '7', 'note-a')"
            )
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'k2', '2026-09-02T00:00:00+00:00', "
                "'2026-09-02T00:00:00+00:00', '5', 'note-b')"
            )
            conn.commit()
        finally:
            conn.close()
        backup_path = tmp_path / "coach.db.backup"
        key = Fernet.generate_key()
        backup_database(str(db_path), str(backup_path), key)

        restored_path = tmp_path / "restored.db"
        restore_database(str(backup_path), str(restored_path), key)
        conn = _connect(restored_path)
        try:
            rows = conn.execute(
                "SELECT plan_session_key, rpe, notes_encrypted FROM debriefs ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("k1", "7", "note-a"), ("k2", "5", "note-b")]

    def test_restore_with_wrong_key_fails_closed(self, db_path: Path, tmp_path: Path) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        backup_path = tmp_path / "coach.db.backup"
        backup_database(str(db_path), str(backup_path), Fernet.generate_key())
        with pytest.raises(InvalidToken):
            restore_database(str(backup_path), str(tmp_path / "out.db"), Fernet.generate_key())
        # The failed restore must not leave a half-written database behind.
        assert not (tmp_path / "out.db").exists()

    def test_backup_with_missing_key_fails(self, db_path: Path, tmp_path: Path) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        with pytest.raises(ValueError, match="backup key"):
            backup_database(str(db_path), str(tmp_path / "b"), b"")


class TestMaintenanceCli:
    """The ``python -m coach_web.maintenance`` entrypoint dispatches the verbs."""

    def test_vacuum_verb(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        from coach_web.maintenance import main

        assert main(["vacuum"]) == 0

    def test_backup_and_restore_verbs(
        self, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
        backup_path = tmp_path / "coach.db.backup"
        restored_path = tmp_path / "restored.db"
        from coach_web.maintenance import main

        assert main(["backup", "--output", str(backup_path)]) == 0
        assert backup_path.exists()
        assert main(["restore", str(backup_path), "--output", str(restored_path)]) == 0
        assert restored_path.exists()

    def test_backup_without_key_fails(self, db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        from coach_web.maintenance import main

        assert main(["backup", "--output", "unused.backup"]) == 1
