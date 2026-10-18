"""Tests for persistence hygiene: VACUUM reclaim + encrypted backup/restore (#274).

Covers the #274 acceptance criteria at the unit level:

- AC1: the VACUUM step reclaims free pages left by deleted plaintext rows
  (``PRAGMA freelist_count`` drops to 0 and the file shrinks).
- AC2: backup produces a Fernet-encrypted artifact; restore into a fresh
  database file round-trips the data intact; wrong/missing keys fail closed.
- CLI: ``python -m coach_web.maintenance`` dispatches the three verbs.
"""

import os
import sqlite3
import stat
from functools import partial
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from coach_web.config import get_settings
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


class TestBackupRefusesMissingPaths:
    """AC1 (N1) — backup/restore/vacuum refuse a missing path instead of failing silently."""

    def test_vacuum_refuses_missing_db(self, tmp_path: Path) -> None:
        """A missing DB path must raise — never ``sqlite3.connect`` an empty DB into being."""
        missing_db = tmp_path / "nope.db"
        with pytest.raises(FileNotFoundError, match="database not found"):
            vacuum_database(str(missing_db))
        # The refusal must not have created anything (no empty DB).
        assert not missing_db.exists()

    def test_backup_refuses_missing_db(self, tmp_path: Path) -> None:
        """A missing DB path must raise — never ``sqlite3.connect`` an empty DB into being."""
        missing_db = tmp_path / "nope.db"
        with pytest.raises(FileNotFoundError, match="database not found"):
            backup_database(str(missing_db), str(tmp_path / "o.backup"), Fernet.generate_key())
        # The refusal must not have created anything (no empty DB, no artifact).
        assert not missing_db.exists()
        assert not (tmp_path / "o.backup").exists()

    def test_backup_cli_reports_missing_db_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from coach_web.maintenance import main

        monkeypatch.setenv("COACH_DB_PATH", str(tmp_path / "nope.db"))
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
        out = tmp_path / "o.backup"
        assert main(["backup", "--output", str(out)]) == 1
        err = capsys.readouterr().err
        assert "maintenance failed" in err
        assert "Traceback" not in err
        assert not out.exists()

    def test_restore_cli_reports_missing_artifact_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from coach_web.maintenance import main

        monkeypatch.setenv("COACH_DB_PATH", str(tmp_path / "coach.db"))
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
        restored = tmp_path / "restored.db"
        assert main(["restore", str(tmp_path / "missing.backup"), "--output", str(restored)]) == 1
        err = capsys.readouterr().err
        assert "maintenance failed" in err
        assert "Traceback" not in err
        assert not restored.exists()


class TestCliSqliteErrorHandling:
    """AC2 (N2) — ``sqlite3.Error`` surfaces as a clean CLI message, no traceback."""

    def test_corrupt_db_file_reports_cleanly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        corrupt = tmp_path / "corrupt.db"
        corrupt.write_bytes(b"this is not a sqlite database")
        monkeypatch.setenv("COACH_DB_PATH", str(corrupt))
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
        from coach_web.maintenance import main

        assert main(["backup", "--output", str(tmp_path / "o.backup")]) == 1
        captured = capsys.readouterr()
        assert "maintenance failed" in captured.err
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out


class TestBackupCapturesWalPages:
    """AC3 (N3) — the WAL checkpoint folds uncheckpointed pages into the artifact."""

    def test_backup_round_trips_rows_living_only_in_the_wal(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        backup_path = tmp_path / "wal.backup"
        key = Fernet.generate_key()
        conn = _connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'wal-k', '2026-09-03T00:00:00+00:00', "
                "'2026-09-03T00:00:00+00:00', '8', 'wal-note')"
            )
            conn.commit()
            # Premise: the row's pages sit in the WAL, uncheckpointed (writer still open).
            wal_file = db_path.parent / f"{db_path.name}-wal"
            assert wal_file.exists() and wal_file.stat().st_size > 0
            backup_database(str(db_path), str(backup_path), key)
        finally:
            conn.close()
        restored_path = tmp_path / "restored.db"
        restore_database(str(backup_path), str(restored_path), key)
        conn = _connect(restored_path)
        try:
            rows = conn.execute("SELECT plan_session_key, notes_encrypted FROM debriefs").fetchall()
        finally:
            conn.close()
        assert rows == [("wal-k", "wal-note")]


class TestCheckpointStatusChecked:
    """AC5 (SEC-296-04) — a busy/failed checkpoint is a loud error, not a partial artifact."""

    def test_busy_checkpoint_fails_the_backup(
        self, db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        backup_path = tmp_path / "busy.backup"
        key = Fernet.generate_key()
        conn = _connect(db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "INSERT INTO debriefs (athlete_id, plan_session_key, created_at, updated_at, "
                "rpe, notes_encrypted) VALUES (1, 'busy-k', '2026-09-03T00:00:00+00:00', "
                "'2026-09-03T00:00:00+00:00', '8', 'busy-note')"
            )
            conn.commit()
            # An open read transaction blocks the TRUNCATE checkpoint (busy=1);
            # shrink the busy timeout so the refusal is fast, not the 5s default.
            conn.execute("BEGIN")
            conn.execute("SELECT COUNT(*) FROM debriefs").fetchone()
            monkeypatch.setattr(sqlite3, "connect", partial(sqlite3.connect, timeout=0.05))
            with pytest.raises(sqlite3.Error, match="checkpoint"):
                backup_database(str(db_path), str(backup_path), key)
        finally:
            conn.rollback()
            conn.close()
        # No silently incomplete artifact may exist.
        assert not backup_path.exists()


class TestBackupArtifactPermissions:
    """AC6 (SEC-296-05) — the artifact is written 0600, independent of the umask."""

    def test_artifact_mode_is_0600_under_restrictive_umask(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        backup_path = tmp_path / "mode.backup"
        key = Fernet.generate_key()
        # A umask that would strip bits off a naive create — the artifact must
        # be exactly 0600 regardless of the process umask.
        old_umask = os.umask(0o027)
        try:
            backup_database(str(db_path), str(backup_path), key)
        finally:
            os.umask(old_umask)
        assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600


class TestCliFailureRedaction:
    """AC7 (SEC-296-06) — CLI failure paths never emit key material or tracebacks."""

    def test_failure_paths_do_not_leak_key_material(
        self,
        db_path: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from alembic import command

        command.upgrade(alembic_config(), "head")
        marker_key = Fernet.generate_key().decode("ascii")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        from coach_web.maintenance import main

        # (a) restore with the wrong key → InvalidToken failure path.
        artifact = tmp_path / "a.backup"
        backup_database(str(db_path), str(artifact), Fernet.generate_key())
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", marker_key)
        assert main(["restore", str(artifact), "--output", str(tmp_path / "r.db")]) == 1

        # (b) backup with the key missing entirely.
        monkeypatch.delenv("DATA_ENCRYPTION_KEY")
        get_settings.cache_clear()
        assert main(["backup", "--output", str(tmp_path / "b.backup")]) == 1

        # (c) backup with the DB missing.
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", marker_key)
        monkeypatch.setenv("COACH_DB_PATH", str(tmp_path / "nope.db"))
        get_settings.cache_clear()
        assert main(["backup", "--output", str(tmp_path / "c.backup")]) == 1

        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert marker_key not in combined
        assert "Traceback" not in combined


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
