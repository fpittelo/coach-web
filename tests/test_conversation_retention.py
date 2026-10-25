"""Conversation memory retention — the MADR-0025 C6 purge sweep (#348).

Pins the retention half of the persistence slice:

- AC4: the sweep deletes messages past the message window (default 90 d)
  and inactive threads (archived or active-stale) past the thread window
  (default 365 d); the windows are configurable via settings; the sweep is
  IDEMPOTENT; the CLI reports COUNTS ONLY — never content (C3);
- boundary semantics: "older than N days" is strict — a row at exactly N
  days is KEPT (tested at 89/90/91 d and 364/365/366 d);
- C6 boundary honesty: ``purge-conversations --help`` states the live-DB
  only limit — previously-taken encrypted backups retain the ciphertext of
  purged content until they age out of backup rotation;
- the sweep never reads ``content_encrypted``/``summary_encrypted`` (no
  key needed, no plaintext path exists).
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command

from coach_web.config import get_settings
from coach_web.conversation import PurgeCounts, purge_expired
from coach_web.maintenance import main
from tests.conftest import alembic_config  # noqa: I001

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
"""Injected clock for every boundary test (N3 — the caller owns the clock)."""

CONTENT_MARKER = "SECRET-OLD-CONFESSION-TEXT"
SUMMARY_MARKER = "SECRET-OLD-SUMMARY-TEXT"


def _iso(delta_days: int) -> str:
    """Return NOW shifted by *delta_days* as an ISO-8601 UTC string."""
    return (NOW - timedelta(days=delta_days)).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    """Open a plain sync connection (mirrors tests/test_migrations.py)."""
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _seed_thread(
    db_path: Path,
    *,
    athlete_id: int = 1,
    status: str = "active",
    last_active_days_ago: int,
) -> int:
    """Insert one conversation_threads row backdated by *last_active_days_ago*."""
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO conversation_threads (athlete_id, status, started_at, last_active_at) "
            "VALUES (?, ?, ?, ?)",
            (athlete_id, status, _iso(last_active_days_ago), _iso(last_active_days_ago)),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _seed_message(
    db_path: Path,
    *,
    thread_id: int,
    created_days_ago: int,
    content: str = CONTENT_MARKER,
    seq: int = 1,
    athlete_id: int = 1,
) -> int:
    """Insert one backdated message row (plaintext marker — purge never decrypts)."""
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO conversation_messages "
            "(thread_id, athlete_id, role, content_encrypted, seq, created_at) "
            "VALUES (?, ?, 'user', ?, ?, ?)",
            (thread_id, athlete_id, content, seq, _iso(created_days_ago)),
        )
        conn.commit()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _seed_summary(db_path: Path, *, thread_id: int) -> None:
    """Attach a plaintext-marker summary to a thread (purge never decrypts)."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE conversation_threads SET summary_encrypted = ?, "
            "summary_updated_at = '2026-01-01T00:00:00+00:00' WHERE id = ?",
            (SUMMARY_MARKER, thread_id),
        )
        conn.commit()


def _row_counts(db_path: Path) -> tuple[int, int]:
    """Return (thread_count, message_count) straight from SQLite."""
    with _connect(db_path) as conn:
        threads = conn.execute("SELECT COUNT(*) FROM conversation_threads").fetchone()[0]
        messages = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    return int(threads), int(messages)


async def _sweep(
    db_session_factory,
    *,
    message_days: int = 90,
    thread_days: int = 365,
) -> PurgeCounts:
    """Run the retention sweep once with the injected NOW."""
    async with db_session_factory() as session:
        return await purge_expired(
            session,
            now=NOW,
            message_retention_days=message_days,
            thread_retention_days=thread_days,
        )


class TestMessageRetentionBoundary:
    """Raw messages older than the message window are purged (AC4)."""

    async def test_messages_within_the_window_are_kept(self, db_session_factory, db_path) -> None:
        thread_id = _seed_thread(db_path, last_active_days_ago=0)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=0)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=89, seq=2)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=90, seq=3)

        report = await _sweep(db_session_factory)

        assert report.messages_deleted == 0
        assert _row_counts(db_path) == (1, 3)

    async def test_messages_past_the_window_are_purged(self, db_session_factory, db_path) -> None:
        thread_id = _seed_thread(db_path, last_active_days_ago=0)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=0)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=91, seq=2)

        report = await _sweep(db_session_factory)

        assert report.messages_deleted == 1
        assert _row_counts(db_path) == (1, 1)

    async def test_messages_age_out_of_the_active_thread_too(
        self, db_session_factory, db_path
    ) -> None:
        """Raw turns age out even in the open thread — the summary survives."""
        thread_id = _seed_thread(db_path, last_active_days_ago=0)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=120, seq=1)
        _seed_summary(db_path, thread_id=thread_id)

        report = await _sweep(db_session_factory)

        assert report.messages_deleted == 1
        assert _row_counts(db_path) == (1, 0)
        with _connect(db_path) as conn:
            summary = conn.execute(
                "SELECT summary_encrypted FROM conversation_threads WHERE id = ?", (thread_id,)
            ).fetchone()[0]
        assert summary == SUMMARY_MARKER


class TestThreadRetentionBoundary:
    """Inactive (archived or active-stale) threads past the window are purged."""

    async def test_threads_within_the_window_are_kept(self, db_session_factory, db_path) -> None:
        _seed_thread(db_path, last_active_days_ago=0)
        _seed_thread(db_path, last_active_days_ago=364, athlete_id=2)
        _seed_thread(db_path, last_active_days_ago=365, athlete_id=3)

        report = await _sweep(db_session_factory)

        assert report.threads_deleted == 0
        assert _row_counts(db_path) == (3, 0)

    async def test_stale_threads_are_purged(self, db_session_factory, db_path) -> None:
        """Both inactive shapes go: active-stale AND archived-old."""
        _seed_thread(db_path, last_active_days_ago=366)
        _seed_thread(db_path, last_active_days_ago=366, status="archived", athlete_id=2)

        report = await _sweep(db_session_factory)

        assert report.threads_deleted == 2
        assert _row_counts(db_path) == (0, 0)

    async def test_recently_archived_thread_is_kept(self, db_session_factory, db_path) -> None:
        """Archive recency is last_active recency — a fresh archive stays."""
        _seed_thread(db_path, last_active_days_ago=10, status="archived")

        report = await _sweep(db_session_factory)

        assert report.threads_deleted == 0
        assert _row_counts(db_path) == (1, 0)

    async def test_thread_purge_cascades_messages(self, db_session_factory, db_path) -> None:
        """The stale thread's remaining messages die with it (FK CASCADE)."""
        thread_id = _seed_thread(db_path, last_active_days_ago=400)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=10, seq=1)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=10, seq=2)

        report = await _sweep(db_session_factory)

        assert report == PurgeCounts(messages_deleted=0, threads_deleted=1)
        assert _row_counts(db_path) == (0, 0)


class TestPurgeIdempotencyAndConfig:
    """The sweep is idempotent and its windows are caller-injectable (AC4)."""

    async def test_purge_is_idempotent(self, db_session_factory, db_path) -> None:
        thread_id = _seed_thread(db_path, last_active_days_ago=400)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=120, seq=1)

        first = await _sweep(db_session_factory)
        second = await _sweep(db_session_factory)

        assert first.messages_deleted == 1
        assert first.threads_deleted == 1
        assert second == PurgeCounts(messages_deleted=0, threads_deleted=0)
        assert _row_counts(db_path) == (0, 0)

    async def test_windows_are_configurable(self, db_session_factory, db_path) -> None:
        thread_id = _seed_thread(db_path, last_active_days_ago=45)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=45)
        old_thread = _seed_thread(db_path, last_active_days_ago=120, athlete_id=2)
        _seed_message(db_path, thread_id=old_thread, created_days_ago=10, seq=1, athlete_id=2)

        report = await _sweep(db_session_factory, message_days=30, thread_days=100)

        # The 45d thread stays (45 < 100 window) but its 45d message goes;
        # the 120d thread goes, cascading its fresh 10d message.
        assert report == PurgeCounts(messages_deleted=1, threads_deleted=1)
        assert _row_counts(db_path) == (1, 0)

    async def test_purge_reports_counts_only(self, db_session_factory, db_path) -> None:
        """The sweep outcome is two integers — counts only, never content (C3)."""
        thread_id = _seed_thread(db_path, last_active_days_ago=120)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=120)
        _seed_summary(db_path, thread_id=thread_id)

        report = await _sweep(db_session_factory)

        # The 120d thread is well inside the 365d window — kept.
        assert isinstance(report, PurgeCounts)
        assert report.model_dump() == {"messages_deleted": 1, "threads_deleted": 0}


class TestPurgeCli:
    """``maintenance purge-conversations`` — counts only, C6 boundary in --help."""

    def test_cli_purges_and_prints_counts_only(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        thread_id = _seed_thread(db_path, last_active_days_ago=120)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=120)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=10, seq=2)

        assert main(["purge-conversations"]) == 0

        captured = capsys.readouterr()
        assert "Purged 1 message(s) and 0 thread(s)" in captured.out
        # C3: the content markers never appear in any CLI output.
        assert CONTENT_MARKER not in captured.out
        assert CONTENT_MARKER not in captured.err

    def test_cli_purge_is_idempotent(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        thread_id = _seed_thread(db_path, last_active_days_ago=400)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=120)

        assert main(["purge-conversations"]) == 0
        assert main(["purge-conversations"]) == 0

        captured = capsys.readouterr()
        assert "Purged 1 message(s) and 1 thread(s)" in captured.out
        assert "Purged 0 message(s) and 0 thread(s)" in captured.out

    def test_cli_help_reports_the_live_db_only_boundary(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """C6 honesty: --help states the encrypted-backup retention lag."""
        with pytest.raises(SystemExit) as excinfo:
            main(["purge-conversations", "--help"])

        assert excinfo.value.code == 0
        captured = capsys.readouterr()
        assert "live-db only" in captured.out.lower()
        assert "backup rotation" in captured.out.lower()

    def test_cli_refuses_missing_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A missing database is refused — never silently created empty (N1)."""
        missing = tmp_path / "nope.db"
        monkeypatch.setenv("COACH_DB_PATH", str(missing))

        assert main(["purge-conversations"]) == 1

        captured = capsys.readouterr()
        assert "maintenance failed" in captured.err
        assert "Traceback" not in captured.err
        assert "Traceback" not in captured.out
        assert not missing.exists()

    def test_cli_windows_follow_settings(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The retention windows come from Settings (env-overridable)."""
        command.upgrade(alembic_config(), "head")
        monkeypatch.setenv("COACH_DB_PATH", str(db_path))
        thread_id = _seed_thread(db_path, last_active_days_ago=45)
        _seed_message(db_path, thread_id=thread_id, created_days_ago=45)
        monkeypatch.setenv("CONVERSATION_MESSAGE_RETENTION_DAYS", "30")
        get_settings.cache_clear()

        assert main(["purge-conversations"]) == 0

        captured = capsys.readouterr()
        assert "Purged 1 message(s) and 0 thread(s)" in captured.out


class TestRetentionSettings:
    """The two retention windows are Settings fields (configurable, AC4)."""

    def test_default_windows(self) -> None:
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.CONVERSATION_MESSAGE_RETENTION_DAYS == 90
        assert settings.CONVERSATION_THREAD_RETENTION_DAYS == 365

    def test_windows_are_env_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONVERSATION_MESSAGE_RETENTION_DAYS", "30")
        monkeypatch.setenv("CONVERSATION_THREAD_RETENTION_DAYS", "100")
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.CONVERSATION_MESSAGE_RETENTION_DAYS == 30
        assert settings.CONVERSATION_THREAD_RETENTION_DAYS == 100
