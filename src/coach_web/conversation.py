"""Conversation memory store — server-owned encrypted threads & messages (#348).

Persistence for MADR-0025 (story #348): SQLite tables for the server-owned
conversation threads and their raw messages, so memory survives restarts.
The API surface (#349), context assembly (#350) and the summarizer are
later slices — this module is the encrypted persistence layer only.

Encryption posture (MADR-0025 C1, debriefs precedent): message content and
the rolling thread summary are Fernet-encrypted at rest with the shared
per-lane ``DATA_ENCRYPTION_KEY`` through the SAME helper
(:func:`coach_web.debrief.build_debrief_cipher`) — the ``*_encrypted``
convention. There is NO plaintext content column and no plaintext fallback:
a message row cannot exist without its ciphertext (``content_encrypted``
NOT NULL). Metadata (ids, role, seq, timestamps) stays plaintext — accepted
residual R3 (MADR-0008 F9 pattern). A read with the wrong key fails closed:
``InvalidToken`` surfaces as a failure, never as garbage content.

Per-athlete isolation (AC2, MADR-0025 C4): every athlete-facing read/write
is scoped by ``athlete_id`` — derived from the authenticated session
upstream (#349). Unknown OR non-owned ids fail closed to ``None``/empty
(404-equivalent). ``role`` is server-assigned (the caller declares which
side is speaking; the client never supplies it — #349 enforces that at the
API boundary). The single deliberate exception is :func:`purge_expired` —
the maintenance retention sweep is a global, age-driven DELETE returning
COUNTS ONLY (never rows, never content, C3); it cannot leak another
athlete's data because it returns no data at all.

``seq`` monotonicity: the per-thread order key is computed INSIDE the
INSERT (``max(seq)+1`` scalar subquery over the thread's rows), so SQLite's
single-writer serialization hands every append — concurrent or not — a
strictly increasing value with no retries; the ``(thread_id, seq)`` unique
index backstops it at the schema level.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from cryptography.fernet import Fernet
from pydantic import BaseModel, Field
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    delete,
    func,
    insert,
    select,
)
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from coach_web.db import Base

MessageRole = Literal["user", "assistant"]
"""MADR-0025 ``role`` CHECK enum — server-assigned, never client-supplied (C4)."""

ThreadStatus = Literal["active", "archived"]
"""MADR-0025 ``status`` CHECK enum (archived threads are read-only, C4)."""


class ConversationThreadRow(Base):
    """ORM model for the ``conversation_threads`` table (MADR-0025, #348).

    The rolling summary is stored as Fernet ciphertext (``summary_encrypted``,
    C1) with its update timestamp — both nullable until the fixed-prompt
    summarizer (#350) generates one. ``athlete_id`` is the pseudonymized
    owner id (no ``athletes`` table exists — ids derive from the session
    email upstream, debrief precedent).
    """

    __tablename__ = "conversation_threads"
    # The CHECK and the composite index are the ORM mirror of migration
    # 0008 (both hardcoded deliberately — migrations stay self-contained,
    # never importing application symbols); keep the two in sync.
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'archived')",
            name="ck_conversation_threads_status",
        ),
        Index(
            "ix_conversation_threads_athlete_status_last_active",
            "athlete_id",
            "status",
            "last_active_at",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    last_active_at: Mapped[str] = mapped_column(Text, nullable=False)
    summary_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_updated_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConversationMessageRow(Base):
    """ORM model for the ``conversation_messages`` table (MADR-0025, #348).

    ``content_encrypted`` is NOT NULL — the C1 no-plaintext-fallback pin.
    ``thread_id`` FK-cascades onto its parent thread (deleting a thread
    deletes its messages). ``seq`` is the per-thread monotonic order key,
    UNIQUE per thread via the ``(thread_id, seq)`` index.
    """

    __tablename__ = "conversation_messages"
    __table_args__ = (
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_conversation_messages_role",
        ),
        Index(
            "uq_conversation_messages_thread_seq",
            "thread_id",
            "seq",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("conversation_threads.id", ondelete="CASCADE"),
        nullable=False,
    )
    athlete_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StoredMessage(BaseModel):
    """One decrypted message in the last-N read boundary (#348).

    Plaintext exists only in-process — this model is what the repository
    hands to the caller (API #349 rehydrate, context assembly #350); the
    at-rest shape is always the Fernet token in ``content_encrypted``.
    """

    role: MessageRole = Field(..., description="Server-assigned role (C4)")
    content: str = Field(..., description="Decrypted message content")
    seq: int = Field(..., ge=1, description="Per-thread monotonic order key")
    created_at: str = Field(..., description="ISO-8601 UTC creation timestamp")


class PurgeCounts(BaseModel):
    """Retention sweep outcome — COUNTS ONLY, never content (C3/C6, #348).

    The maintenance CLI prints exactly these numbers; no message or summary
    text can ever ride along because the sweep is a pure DELETE.
    """

    messages_deleted: int = Field(
        ...,
        ge=0,
        description="Raw messages purged (older than the message window)",
    )
    threads_deleted: int = Field(
        ...,
        ge=0,
        description="Inactive threads purged (inactive past the thread window)",
    )


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (MADR-008 convention)."""
    return datetime.now(UTC).isoformat()


async def create_thread(
    session: AsyncSession,
    *,
    athlete_id: int,
) -> ConversationThreadRow:
    """Open a new ACTIVE thread for the athlete and commit.

    The caller decides when to open a thread (first turn, or the reset
    flow's archive-then-create, #349) — this function only persists the
    new active row with ``started_at == last_active_at``.
    """
    now = _utc_now_iso()
    row = ConversationThreadRow(
        athlete_id=athlete_id,
        status="active",
        started_at=now,
        last_active_at=now,
    )
    session.add(row)
    await session.commit()
    return row


async def get_active_thread(
    session: AsyncSession,
    *,
    athlete_id: int,
) -> ConversationThreadRow | None:
    """Return the athlete's most recent ACTIVE thread, or None.

    ``None`` means "no open thread" (404-equivalent, fail-closed). The
    most-recent rule keeps the get-or-create flow (#349) deterministic if
    two active rows ever exist out-of-band; the reset flow archives the
    old one first, so the normal steady state is exactly one.
    """
    result = await session.execute(
        select(ConversationThreadRow)
        .where(
            ConversationThreadRow.athlete_id == athlete_id,
            ConversationThreadRow.status == "active",
        )
        .order_by(ConversationThreadRow.last_active_at.desc(), ConversationThreadRow.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_thread(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
) -> ConversationThreadRow | None:
    """Return the athlete's thread by id, or None (AC2, C4 fail-closed).

    The query is scoped by BOTH the thread id and the athlete id: an
    unknown id and a thread owned by a different athlete are
    indistinguishable from the caller's perspective — both are ``None``
    (404-equivalent).
    """
    result = await session.execute(
        select(ConversationThreadRow).where(
            ConversationThreadRow.id == thread_id,
            ConversationThreadRow.athlete_id == athlete_id,
        )
    )
    return result.scalar_one_or_none()


async def append_message(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
    role: MessageRole,
    content: str,
    cipher: Fernet,
) -> ConversationMessageRow | None:
    """Append one message to the athlete's thread and commit.

    The content is encrypted HERE (C1) — the plaintext never reaches the
    SQL layer, so no database file (main or WAL) can ever contain it. The
    per-thread ``seq`` is computed inside the INSERT (``max(seq)+1``
    scalar subquery scoped to THIS thread and athlete), making the append
    atomic under SQLite's single-writer lock — concurrent appends receive
    strictly increasing seqs without retries. The thread's
    ``last_active_at`` moves in the same transaction.

    Returns ``None`` (nothing written) when the thread is unknown or not
    owned by ``athlete_id`` — fail-closed, 404-equivalent (C4). The
    ``role`` is caller-declared but server-owned: the API layer (#349)
    derives it from the message channel, never from client input.
    """
    result = await session.execute(
        select(ConversationThreadRow).where(
            ConversationThreadRow.id == thread_id,
            ConversationThreadRow.athlete_id == athlete_id,
        )
    )
    thread = result.scalar_one_or_none()
    if thread is None:
        return None
    thread.last_active_at = _utc_now_iso()
    seq_next = (
        select(func.coalesce(func.max(ConversationMessageRow.seq), 0) + 1)
        .where(
            ConversationMessageRow.thread_id == thread_id,
            ConversationMessageRow.athlete_id == athlete_id,
        )
        .scalar_subquery()
    )
    stmt = (
        insert(ConversationMessageRow)
        .values(
            thread_id=thread_id,
            athlete_id=athlete_id,
            role=role,
            content_encrypted=cipher.encrypt(content.encode("utf-8")).decode("ascii"),
            created_at=thread.last_active_at,
            seq=seq_next,
        )
        .returning(ConversationMessageRow)
    )
    row = (await session.execute(stmt)).scalar_one()
    await session.commit()
    return row


async def list_last_messages(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
    limit: int,
    cipher: Fernet,
) -> list[StoredMessage]:
    """Return the thread's last *limit* messages, decrypted, oldest→newest.

    This is the storage primitive behind the MADR-0025 layer-1 window
    (last 20 messages replayed oldest→newest) and the rehydrate listing
    (#349); the caller owns the window/cap policy. Scoped by
    ``thread_id`` AND ``athlete_id`` (C4) — a foreign thread yields an
    empty list. Decryption happens server-side with the lane key; a wrong
    or rotated key raises ``InvalidToken`` — the read FAILS CLOSED, it
    never returns garbage content (AC3).
    """
    result = await session.execute(
        select(ConversationMessageRow)
        .where(
            ConversationMessageRow.thread_id == thread_id,
            ConversationMessageRow.athlete_id == athlete_id,
        )
        .order_by(ConversationMessageRow.seq.desc())
        .limit(limit)
    )
    rows = result.scalars().all()
    messages: list[StoredMessage] = []
    for row in reversed(rows):
        content = cipher.decrypt(row.content_encrypted.encode("ascii")).decode("utf-8")
        messages.append(
            StoredMessage(
                role=row.role,  # type: ignore[arg-type]  # CHECK-enum column
                content=content,
                seq=row.seq,
                created_at=row.created_at,
            )
        )
    return messages


async def count_thread_messages(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
) -> int:
    """COUNT the thread's stored messages — no row read, no decryption.

    The cheap due-check primitive behind the rolling-summary refresh
    (#350, review 5644757392 nit 2): the refresh trigger only needs the
    message count, so the decision runs as one scalar COUNT over
    plaintext metadata — nothing is read, let alone decrypted, until the
    refresh actually fires. Scoped by ``thread_id`` AND ``athlete_id``
    (C4): an unknown or foreign thread counts zero, fail-closed.
    """
    result = await session.execute(
        select(func.count())
        .select_from(ConversationMessageRow)
        .where(
            ConversationMessageRow.thread_id == thread_id,
            ConversationMessageRow.athlete_id == athlete_id,
        )
    )
    return int(result.scalar_one())


async def archive_thread(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
) -> ConversationThreadRow | None:
    """Archive the athlete's thread (the reset flow's primitive, #349/#351).

    Idempotent: re-archiving an archived thread is a no-op returning the
    row. Unknown or non-owned → ``None`` (fail-closed). Archived threads
    become read-only upstream (C4) — enforcing that is the API layer's
    job; the storage layer only records the status.
    """
    thread = await get_thread(session, athlete_id=athlete_id, thread_id=thread_id)
    if thread is None:
        return None
    thread.status = "archived"
    await session.commit()
    return thread


async def set_thread_summary(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
    cipher: Fernet,
    summary: str | None,
) -> ConversationThreadRow | None:
    """Set or clear the thread's rolling summary and commit (#348).

    The SUMMARY GENERATION is #350's job (fixed server-side prompt over
    the stored messages); this is the storage shape only. The plaintext
    summary is encrypted HERE (C1 — ``summary_encrypted``, the debriefs
    ``*_encrypted`` convention) and never logged; ``None`` clears both the
    ciphertext and ``summary_updated_at``. Unknown or non-owned →
    ``None`` (fail-closed).
    """
    thread = await get_thread(session, athlete_id=athlete_id, thread_id=thread_id)
    if thread is None:
        return None
    if summary is None:
        thread.summary_encrypted = None
        thread.summary_updated_at = None
    else:
        thread.summary_encrypted = cipher.encrypt(summary.encode("utf-8")).decode("ascii")
        thread.summary_updated_at = _utc_now_iso()
    await session.commit()
    return thread


async def purge_expired(
    session: AsyncSession,
    *,
    now: datetime,
    message_retention_days: int,
    thread_retention_days: int,
) -> PurgeCounts:
    """Run the MADR-0025 C6 retention sweep and commit — counts only (AC4).

    Deletes (age driven, both windows measured against ``now`` — the
    caller ALWAYS injects the clock, N3):

    - raw messages older than ``message_retention_days`` (default 90 d) —
      from ALL threads: raw turns age out even in the active thread, the
      summary layer is what survives;
    - inactive threads older than ``thread_retention_days`` (default
      365 d) — archived threads (their ``last_active_at`` freezes at
      archive time) and active-but-stale threads alike; the FK CASCADE
      removes their remaining messages.

    This is the ONE athlete-agnostic repository method, by contract: the
    maintenance CLI sweeps the whole lane (it holds no athlete identity).
    It returns counts — never rows, never content (C3) — and never reads
    ``content_encrypted``/``summary_encrypted``, so it cannot leak or log
    conversation data. Idempotent: a second run deletes nothing.

    ``now`` must be UTC-aware (the ISO cutoff strings compare against the
    stored ``+00:00`` timestamps). Purge is LIVE-DB only (C6): previously
    taken encrypted backups retain the ciphertext of purged content until
    they age out of backup rotation.
    """
    message_cutoff = (now - timedelta(days=message_retention_days)).isoformat()
    thread_cutoff = (now - timedelta(days=thread_retention_days)).isoformat()
    # Core DML execution returns a CursorResult at runtime (rowcount carries
    # the deleted-row count); the session's stubs only narrow to Result, so
    # the runtime type is asserted here.
    messages_result = cast(
        CursorResult[Any],
        await session.execute(
            delete(ConversationMessageRow).where(ConversationMessageRow.created_at < message_cutoff)
        ),
    )
    threads_result = cast(
        CursorResult[Any],
        await session.execute(
            delete(ConversationThreadRow).where(
                ConversationThreadRow.last_active_at < thread_cutoff
            )
        ),
    )
    await session.commit()
    return PurgeCounts(
        messages_deleted=messages_result.rowcount or 0,
        threads_deleted=threads_result.rowcount or 0,
    )
