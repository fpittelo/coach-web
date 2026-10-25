"""Conversation memory repository contract — encrypted threads & messages (#348).

Pins the persistence half of MADR-0025 (story #348; the server-owned thread
API is #349, context assembly #350, reset UX #351):

- AC1: the ORM models mirror the migration 0008 constraints — FK CASCADE,
  the per-thread seq uniqueness and both CHECK enums (the emitted SQL
  itself is pinned in ``tests/test_migrations.py``);
- AC2: every athlete-facing read/write is scoped by ``athlete_id`` —
  athlete B cannot read or write athlete A's threads or messages, in BOTH
  directions; unknown and non-owned both fail closed (``None``/empty);
- AC3: content round-trips encrypted — ``content_encrypted`` and
  ``summary_encrypted`` hold Fernet ciphertext at rest built with the SAME
  helper/key path as the debriefs (:func:`coach_web.debrief.build_debrief_cipher`,
  C1 — there is no plaintext content column), plaintext exists only
  in-process, and a read with the wrong key fails closed (``InvalidToken``
  surfaces as a failure, never garbage content);
- ``seq`` monotonicity: appends assign strictly increasing per-thread seqs
  via the atomic max+1 INSERT — including concurrent-ish appends across
  independent sessions;
- the purge returns counts only and never reads content (C3); its
  retention windows are caller-injected (tested in
  ``tests/test_conversation_retention.py``);
- no conversation content ever reaches the logs (C3 posture).
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

from coach_web.config import get_settings
from coach_web.conversation import (
    ConversationMessageRow,
    ConversationThreadRow,
    append_message,
    archive_thread,
    count_thread_messages,
    create_thread,
    get_active_thread,
    get_thread,
    list_last_messages,
    set_thread_summary,
)
from coach_web.debrief import build_debrief_cipher

TEST_KEY = Fernet.generate_key()
"""Per-module Fernet key backing the default cipher (generated, never committed)."""

WRONG_KEY = Fernet.generate_key()
"""A different valid Fernet key — the wrong-key fail-closed read scenario."""

ATHLETE_A = 111111111
ATHLETE_B = 222222222

CONTENT_MARKER = "SECRET-KETTLEBELL-CONFESSION"
SUMMARY_MARKER = "SECRET-SUMMARY-TEXT"


@pytest.fixture
def cipher_settings(monkeypatch: pytest.MonkeyPatch):
    """Settings carrying the module's generated test key (env-injected shape)."""
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", TEST_KEY.decode("ascii"))
    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def cipher(cipher_settings):
    """The lane cipher, built through the SAME helper the debriefs use (C1)."""
    return build_debrief_cipher(cipher_settings)


@pytest.fixture
def wrong_cipher():
    """A cipher holding a different valid Fernet key (the wrong-key read)."""
    return Fernet(WRONG_KEY)


def _raw_message_content(db_path: Path, message_id: int) -> str:
    """Read ``content_encrypted`` straight from SQLite (bypassing the app)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT content_encrypted FROM conversation_messages WHERE id = ?", (message_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _raw_summary(db_path: Path, thread_id: int) -> str | None:
    """Read ``summary_encrypted`` straight from SQLite (bypassing the app)."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT summary_encrypted FROM conversation_threads WHERE id = ?", (thread_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    value: str | None = row[0]
    return value


def _message_count(db_path: Path) -> int:
    """Count conversation_messages rows straight from SQLite."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()
    finally:
        conn.close()
    return int(row[0])


class TestThreadLifecycle:
    """create/get active thread — server-owned, per-athlete (C4)."""

    async def test_create_thread_opens_an_active_thread(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)

        assert isinstance(thread, ConversationThreadRow)
        assert thread.athlete_id == ATHLETE_A
        assert thread.status == "active"
        assert thread.started_at == thread.last_active_at
        assert thread.summary_encrypted is None
        assert thread.summary_updated_at is None

    async def test_get_active_thread_returns_the_open_thread(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)
            active = await get_active_thread(session, athlete_id=ATHLETE_A)

        assert active is not None
        assert active.id == created.id

    async def test_the_newest_thread_is_the_active_one(self, db_session_factory) -> None:
        """A second open thread (post-reset flow) supersedes the first."""
        async with db_session_factory() as session:
            first = await create_thread(session, athlete_id=ATHLETE_A)
            second = await create_thread(session, athlete_id=ATHLETE_A)
            active = await get_active_thread(session, athlete_id=ATHLETE_A)

        assert active is not None
        assert active.id == second.id
        assert active.id != first.id

    async def test_get_thread_returns_the_owned_thread(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)
            fetched = await get_thread(session, athlete_id=ATHLETE_A, thread_id=created.id)

        assert fetched is not None
        assert fetched.id == created.id

    async def test_get_unknown_thread_fails_closed(self, db_session_factory) -> None:
        """An unknown thread id is 404-equivalent: None (C4 fail-closed)."""
        async with db_session_factory() as session:
            fetched = await get_thread(session, athlete_id=ATHLETE_A, thread_id=99999)

        assert fetched is None

    async def test_get_active_thread_without_one_fails_closed(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            fetched = await get_active_thread(session, athlete_id=ATHLETE_B)

        assert fetched is None


class TestAthleteScoping:
    """AC2 — every method is scoped by athlete_id, tested in BOTH directions."""

    async def test_another_athlete_cannot_get_the_thread(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)

        async with db_session_factory() as session:
            fetched = await get_thread(session, athlete_id=ATHLETE_B, thread_id=created.id)

        assert fetched is None

    async def test_another_athlete_cannot_append_to_the_thread(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)

        async with db_session_factory() as session:
            result = await append_message(
                session,
                athlete_id=ATHLETE_B,
                thread_id=created.id,
                role="user",
                content=CONTENT_MARKER,
                cipher=cipher,
            )

        assert result is None
        assert _message_count(db_path) == 0

    async def test_another_athlete_cannot_list_the_thread_messages(
        self, db_session_factory, cipher
    ) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=created.id,
                role="user",
                content=CONTENT_MARKER,
                cipher=cipher,
            )

        async with db_session_factory() as session:
            messages = await list_last_messages(
                session, athlete_id=ATHLETE_B, thread_id=created.id, limit=20, cipher=cipher
            )

        assert messages == []

    async def test_another_athlete_cannot_archive_the_thread(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)

        async with db_session_factory() as session:
            result = await archive_thread(session, athlete_id=ATHLETE_B, thread_id=created.id)

        assert result is None
        async with db_session_factory() as session:
            still_active = await get_thread(session, athlete_id=ATHLETE_A, thread_id=created.id)
        assert still_active is not None
        assert still_active.status == "active"

    async def test_another_athlete_cannot_set_the_thread_summary(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            created = await create_thread(session, athlete_id=ATHLETE_A)

        async with db_session_factory() as session:
            result = await set_thread_summary(
                session,
                athlete_id=ATHLETE_B,
                thread_id=created.id,
                cipher=cipher,
                summary=SUMMARY_MARKER,
            )

        assert result is None
        assert _raw_summary(db_path, created.id) is None

    async def test_the_reverse_direction_is_also_scoped(self, db_session_factory, cipher) -> None:
        """A's credentials cannot read B's thread either (both directions)."""
        async with db_session_factory() as session:
            b_thread = await create_thread(session, athlete_id=ATHLETE_B)

        async with db_session_factory() as session:
            fetched = await get_thread(session, athlete_id=ATHLETE_A, thread_id=b_thread.id)
            messages = await list_last_messages(
                session, athlete_id=ATHLETE_A, thread_id=b_thread.id, limit=20, cipher=cipher
            )

        assert fetched is None
        assert messages == []


class TestAppendAndList:
    """append message (server-assigned role, monotonic seq) + last-N listing."""

    async def test_append_assigns_monotonic_seq_starting_at_one(
        self, db_session_factory, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            first = await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content="first",
                cipher=cipher,
            )
            second = await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="assistant",
                content="second",
                cipher=cipher,
            )
            third = await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content="third",
                cipher=cipher,
            )

        assert first is not None and second is not None and third is not None
        assert (first.seq, second.seq, third.seq) == (1, 2, 3)
        assert first.role == "user"
        assert second.role == "assistant"

    async def test_append_stores_ciphertext_at_rest(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            appended = await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content=CONTENT_MARKER,
                cipher=cipher,
            )
            assert appended is not None

        stored = _raw_message_content(db_path, appended.id)
        # Ciphertext at rest: no plaintext in the column, Fernet token shape.
        assert CONTENT_MARKER not in stored
        assert stored.startswith("gAAAA")
        # And it round-trips with the lane key.
        assert cipher.decrypt(stored.encode("ascii")).decode("utf-8") == CONTENT_MARKER

    async def test_append_touches_last_active_at(self, db_session_factory, cipher) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            started = thread.last_active_at
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content="hello",
                cipher=cipher,
            )
            refreshed = await get_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)

        assert refreshed is not None
        assert refreshed.last_active_at >= started

    async def test_append_to_unknown_thread_fails_closed(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            result = await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=99999,
                role="user",
                content=CONTENT_MARKER,
                cipher=cipher,
            )

        assert result is None
        assert _message_count(db_path) == 0

    async def test_list_last_messages_returns_plaintext_oldest_first(
        self, db_session_factory, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            thread_id = thread.id
            for index in range(5):
                await append_message(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread_id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"turn-{index}",
                    cipher=cipher,
                )

        async with db_session_factory() as session:
            last_three = await list_last_messages(
                session, athlete_id=ATHLETE_A, thread_id=thread_id, limit=3, cipher=cipher
            )

        assert [m.seq for m in last_three] == [3, 4, 5]
        assert [m.content for m in last_three] == ["turn-2", "turn-3", "turn-4"]
        assert [m.role for m in last_three] == ["user", "assistant", "user"]

    async def test_list_on_unknown_thread_returns_empty(self, db_session_factory, cipher) -> None:
        async with db_session_factory() as session:
            messages = await list_last_messages(
                session, athlete_id=ATHLETE_A, thread_id=99999, limit=20, cipher=cipher
            )

        assert messages == []

    async def test_read_with_wrong_key_fails_closed(
        self, db_session_factory, db_path: Path, cipher, wrong_cipher
    ) -> None:
        """A wrong-key read raises InvalidToken — never garbage content (AC3)."""
        from cryptography.fernet import InvalidToken

        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content=CONTENT_MARKER,
                cipher=cipher,
            )

        # Premise: the stored ciphertext is intact but undecryptable under
        # the wrong key.
        stored = _raw_message_content(db_path, 1)
        assert stored.startswith("gAAAA")

        async with db_session_factory() as session:
            with pytest.raises(InvalidToken):
                await list_last_messages(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread.id,
                    limit=20,
                    cipher=wrong_cipher,
                )

    async def test_concurrent_appends_stay_monotonic(self, db_session_factory, cipher) -> None:
        """Concurrent-ish appends across sessions produce a strict 1..N seq.

        The seq is computed INSIDE the INSERT (``max(seq)+1`` scalar
        subquery), so SQLite's single-writer serialization hands every
        append a distinct next value — no duplicates, no gaps, no retries.
        """
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            thread_id = thread.id

        async def append_one(index: int) -> int:
            async with db_session_factory() as session:
                row = await append_message(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread_id,
                    role="user",
                    content=f"concurrent-{index}",
                    cipher=cipher,
                )
            assert row is not None
            return row.seq

        seqs = await asyncio.gather(*[append_one(i) for i in range(8)])

        assert sorted(seqs) == list(range(1, 9))


class TestMessageCount:
    """The COUNT primitive behind the summary-refresh due-check (#350)."""

    async def test_counts_all_rows_scoped_to_the_owner(self, db_session_factory, cipher) -> None:
        """Every stored row counts (no window); a foreign athlete counts zero (C4)."""
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            thread_id = thread.id
            for i in range(25):
                await append_message(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread_id,
                    role="user" if i % 2 else "assistant",
                    content=f"turn-{i}",
                    cipher=cipher,
                )

        async with db_session_factory() as session:
            assert (
                await count_thread_messages(session, athlete_id=ATHLETE_A, thread_id=thread_id)
                == 25
            )
            # C4: another athlete's COUNT over the same thread id is zero.
            assert (
                await count_thread_messages(session, athlete_id=ATHLETE_B, thread_id=thread_id) == 0
            )

    async def test_unknown_thread_counts_zero(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            assert await count_thread_messages(session, athlete_id=ATHLETE_A, thread_id=99999) == 0


class TestArchive:
    """archive thread — the reset flow's storage primitive (C4: read-only after)."""

    async def test_archive_thread_sets_status_archived(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            archived = await archive_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)
            active = await get_active_thread(session, athlete_id=ATHLETE_A)

        assert archived is not None
        assert archived.status == "archived"
        assert active is None

    async def test_archive_unknown_thread_fails_closed(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            result = await archive_thread(session, athlete_id=ATHLETE_A, thread_id=99999)

        assert result is None

    async def test_archive_is_idempotent(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            first = await archive_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)
            second = await archive_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)

        assert first is not None and second is not None
        assert second.status == "archived"


class TestSummary:
    """set/clear ``summary_encrypted`` — the storage shape for #350's summarizer.

    Summary GENERATION is not in this slice (fixed-prompt summarizer is
    #350's job); the repository exposes only set (encrypts) and clear.
    """

    async def test_set_thread_summary_stores_ciphertext(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            updated = await set_thread_summary(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                cipher=cipher,
                summary=SUMMARY_MARKER,
            )

        assert updated is not None
        stored = _raw_summary(db_path, thread.id)
        assert stored is not None
        assert SUMMARY_MARKER not in stored
        assert stored.startswith("gAAAA")
        assert cipher.decrypt(stored.encode("ascii")).decode("utf-8") == SUMMARY_MARKER
        assert updated.summary_updated_at is not None

    async def test_clear_summary_nulls_both_columns(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            await set_thread_summary(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                cipher=cipher,
                summary=SUMMARY_MARKER,
            )
            cleared = await set_thread_summary(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                cipher=cipher,
                summary=None,
            )

        assert cleared is not None
        assert _raw_summary(db_path, thread.id) is None
        assert cleared.summary_updated_at is None

    async def test_set_summary_on_unknown_thread_fails_closed(
        self, db_session_factory, cipher
    ) -> None:
        async with db_session_factory() as session:
            result = await set_thread_summary(
                session,
                athlete_id=ATHLETE_A,
                thread_id=99999,
                cipher=cipher,
                summary=SUMMARY_MARKER,
            )

        assert result is None


class TestOrmMirror:
    """AC1 — the ORM models mirror the migration constraints (behavioral)."""

    async def test_orm_rejects_duplicate_thread_seq(self, db_session_factory, cipher) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content="one",
                cipher=cipher,
            )
            session.add(
                ConversationMessageRow(
                    thread_id=thread.id,
                    athlete_id=ATHLETE_A,
                    role="user",
                    content_encrypted="gAAAA-dup",
                    seq=1,
                    created_at="2026-10-25T10:00:00+00:00",
                )
            )
            with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                await session.commit()
            await session.rollback()

    async def test_orm_rejects_unknown_role(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            session.add(
                ConversationMessageRow(
                    thread_id=thread.id,
                    athlete_id=ATHLETE_A,
                    role="system",
                    content_encrypted="gAAAA-x",
                    seq=1,
                    created_at="2026-10-25T10:00:00+00:00",
                )
            )
            with pytest.raises(IntegrityError, match="role"):
                await session.commit()
            await session.rollback()

    async def test_orm_rejects_unknown_status(self, db_session_factory) -> None:
        async with db_session_factory() as session:
            session.add(
                ConversationThreadRow(
                    athlete_id=ATHLETE_A,
                    status="deleted",
                    started_at="s",
                    last_active_at="l",
                )
            )
            with pytest.raises(IntegrityError, match="status"):
                await session.commit()
            await session.rollback()

    async def test_orm_delete_cascades_messages(
        self, db_session_factory, db_path: Path, cipher
    ) -> None:
        async with db_session_factory() as session:
            thread = await create_thread(session, athlete_id=ATHLETE_A)
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="user",
                content="one",
                cipher=cipher,
            )
            await append_message(
                session,
                athlete_id=ATHLETE_A,
                thread_id=thread.id,
                role="assistant",
                content="two",
                cipher=cipher,
            )
            assert _message_count(db_path) == 2

            loaded = await get_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)
            assert loaded is not None
            await session.delete(loaded)
            await session.commit()

        assert _message_count(db_path) == 0


class TestNoContentInLogs:
    """C3 — no conversation content in any log record of this slice's paths."""

    async def test_repository_and_purge_logs_never_carry_content(
        self,
        db_session_factory,
        db_path: Path,
        cipher,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging
        import sqlite3
        from datetime import UTC, datetime

        from coach_web.conversation import purge_expired

        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "INSERT INTO conversation_threads "
                "(athlete_id, status, started_at, last_active_at) VALUES (1, 'active', 's', 'l')"
            )
            conn.commit()
        finally:
            conn.close()

        with caplog.at_level(logging.DEBUG):
            async with db_session_factory() as session:
                thread = await create_thread(session, athlete_id=ATHLETE_A)
                await append_message(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread.id,
                    role="user",
                    content=CONTENT_MARKER,
                    cipher=cipher,
                )
                await list_last_messages(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread.id,
                    limit=20,
                    cipher=cipher,
                )
                await set_thread_summary(
                    session,
                    athlete_id=ATHLETE_A,
                    thread_id=thread.id,
                    cipher=cipher,
                    summary=SUMMARY_MARKER,
                )
                await archive_thread(session, athlete_id=ATHLETE_A, thread_id=thread.id)
            async with db_session_factory() as session:
                await purge_expired(
                    session,
                    now=datetime.now(UTC),
                    message_retention_days=90,
                    thread_retention_days=365,
                )

        logged = "\n".join(record.getMessage() for record in caplog.records)
        assert CONTENT_MARKER not in logged
        assert SUMMARY_MARKER not in logged
