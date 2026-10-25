"""Server-owned conversation thread API — wire change, SSE persistence, rehydrate (#349).

The API/SSE slice of MADR-0025 (epic #346): conversation ownership moves to
the server. The client stops replaying history (:func:`coach_web.models.AgentStreamRequest`
retires the field), the stream endpoint persists each turn into the #348
store, and the athlete can rehydrate, reset or purge the transcript. This
module hosts the request/response models and the persistence boundary; the
routes themselves are thin wrappers in :mod:`coach_web.app`.

Wire change (MADR-0025, the epic's core decision)
    ``POST /api/agent/stream`` carries ``message`` plus the optional,
    opaque, SERVER-ISSUED ``thread_id``. The client-owned ``history`` field
    is deleted and never read: forged assistant turns and forged context
    retire as an injection surface (STRIDE #87). Pydantic's default
    ``extra="ignore"`` is deliberate — NOT ``forbid`` — so a cached old
    client that still sends ``history`` keeps working through the
    transition while the injection path stays closed regardless (the field
    is never read, and #350 replaces client replay with server-owned
    context assembly).

Persistence points (AC2)
    The user message is persisted BEFORE streaming; the assistant message
    is persisted EXACTLY ONCE on completion — the agent loop's terminal
    ``done`` event is the completed-turn marker, and the write happens in
    the stream's finalization path (after the event loop ends), never as a
    partial: a typed-error turn persists nothing assistant-side, so no
    truncated reply can ever masquerade as a final one. ``role`` is
    server-assigned from the message channel; the client never supplies it
    (C4). ``seq`` is assigned by the store's atomic max+1 INSERT (#348),
    so per-thread ordering stays strictly monotonic even with concurrent
    tabs — cross-tab interleaving is best-effort by ``seq`` (MADR-0025).

Retry / duplicate guard (the honest minimal design)
    Without a client-issued turn id, the server cannot tag individual
    turns, so retries are recognized by CONTENT against the thread's own
    transcript:

    - **User boundary**: the incoming message matching the thread's most
      recent USER message is treated as a retry of that turn — the append
      is skipped and the turn re-runs. This covers both the in-flight
      retry (the previous attempt died before completion) and the
      completed-but-unreceived retry (the client never saw the reply).
    - **Assistant boundary**: the completion write is skipped when the
      thread's last message is an ASSISTANT message with byte-identical
      content — the deterministic-replay duplicate is suppressed.

    Documented imprecisions (accepted, n=1): (a) an immediate deliberate
    re-send of the identical text is treated as the same turn — the second
    user row is NOT written (the exchange merges into the prior turn's
    user row) while the second reply IS appended whenever it differs, so
    the transcript can hold two consecutive assistant rows
    (``user, assistant, assistant``); #350 consumes this transcript
    as-is, so that shape is what the future memory window will read;
    (b) a non-deterministic reply to a completed-but-unreceived turn
    appends a second assistant row (the user row is still deduplicated,
    same shape as (a)); (c) the guard is check-then-insert, not atomic —
    two concurrent identical writers can both pass, consistent with the
    best-effort-by-``seq`` posture for concurrent tabs. A client turn-id
    wire field would remove the ambiguity and is deliberately out of
    scope (YAGNI, MADR-0025 rejects wire additions beyond ``thread_id``).

C7 failure posture (AC5)
    Persistence failures never abort the SSE stream — the reply is the
    product, memory is convenience (MADR-0025 C7). Every persistence step
    (:func:`open_turn_thread`, :func:`record_user_turn`,
    :func:`record_assistant_turn`) catches the degrade family (OSError,
    SQLAlchemyError, pydantic ValidationError, EncryptionUnavailableError,
    InvalidToken — the same boundary the digest reads use), logs the
    exception CLASS NAME only (C3: no content, no key material, no
    exception text — SQL parameters are ciphertext anyway, but the log
    stays value-free) and reports a ``degraded`` outcome. The endpoint
    maps that to a stable ``warning`` SSE frame the UI can render later;
    the write path cannot raise into the stream generator.

    Disconnect decision: the completion write runs in the generator's
    finalization path but only after the event loop ended NORMALLY with a
    clean ``done`` marker. On a client disconnect the generator is closed
    mid-turn — the turn did not complete, the reply never existed as a
    whole, so nothing assistant-side is persisted; the already-persisted
    user row remains the honest record (and the in-flight marker for the
    retry guard). The write is therefore never attempted during teardown
    and cannot race the cancellation.

Scoping (C4, AC6)
    Every step is scoped by the session-derived ``athlete_id``; an unknown
    or non-owned ``thread_id`` fails closed to HTTP 404 (indistinguishable
    on purpose), an ARCHIVED thread rejects writes with HTTP 409 (a real
    state the legitimate owner can hit after a reset — the body stays
    value-free). Reads with a wrong/rotated key fail closed per #348.

Deliberately out of scope (KIS/YAGNI): summary generation, memory
injection into the prompt (#350), the reset/clear UX (#351), the
opportunistic retention purge on reset (MADR-0025 C6 — deferred to #351;
the ``maintenance purge-conversations`` sweep enforces retention today).
"""

import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from coach_web.conversation import (
    ConversationMessageRow,
    ConversationThreadRow,
    StoredMessage,
    append_message,
    create_thread,
    get_active_thread,
    get_thread,
    list_last_messages,
)
from coach_web.errors import EncryptionUnavailableError

logger = logging.getLogger(__name__)

REHYDRATE_MESSAGE_LIMIT = 20
"""Last-N window served by ``GET /api/conversation`` (bounded rehydrate).

Same magnitude as the MADR-0025 layer-1 memory window: the transcript is a
bounded convenience, not an archive — retention purges raw turns after 90
days regardless (C6).
"""

_RETRY_SCAN_DEPTH = 5
"""How far back the retry guard scans for the thread's most recent user row.

Covers every reachable transcript shape (in-flight marker, normal turn,
degenerate consecutive-assistant rows from the documented imprecision (b)).
"""

DEGRADE_WARNING_CODE = "conversation_persistence_degraded"
"""Stable code of the redacted degrade frame (the UI matches on this)."""

DEGRADE_WARNING_MESSAGE = "Conversation memory is unavailable — the reply was not saved to history."
"""Static, redacted degrade text: no content, no key material, no internals."""

THREAD_NOT_FOUND_DETAIL = "Conversation thread not found"
"""Static 404 body — unknown and non-owned ids are indistinguishable (C4)."""

THREAD_ARCHIVED_DETAIL = "Conversation thread is archived"
"""Static 409 body — archived threads are read-only (C4); value-free."""

PERSISTENCE_DEGRADE_EXCEPTIONS = (
    OSError,
    SQLAlchemyError,
    ValidationError,
    EncryptionUnavailableError,
    InvalidToken,
)
"""The infra failure family that degrades persistence instead of failing
the request (C7) — identical membership to the digest-read boundary in
:mod:`coach_web.app`. Anything else is a bug and must surface."""


class SessionFactory(Protocol):
    """Structural type of the app's async session factory (duck-typed seam).

    Mirrors :func:`coach_web.db.build_session_factory`'s callable shape
    (``async_sessionmaker[AsyncSession]``): calling it yields an async
    context manager handing out one
    :class:`~sqlalchemy.ext.asyncio.AsyncSession`. A protocol keeps the
    boundary honest (test stubs must match the real factory's shape)
    without importing the concrete maker.
    """

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Open one async session (the context closes it on exit)."""
        ...


@dataclass(frozen=True)
class OpenTurnOutcome:
    """Thread resolution outcome for one streaming turn (C7)."""

    thread: ConversationThreadRow | None
    degraded: bool


@dataclass(frozen=True)
class TurnPersistence:
    """Outcome of one persistence write attempt (C7)."""

    persisted: bool
    degraded: bool


class ConversationMessageOut(BaseModel):
    """One decrypted message in the rehydrate listing (AC3)."""

    role: Literal["user", "assistant"] = Field(..., description="Server-assigned role (C4)")
    content: str = Field(..., description="Decrypted message content")
    seq: int = Field(..., ge=1, description="Per-thread monotonic order key")
    created_at: str = Field(..., description="ISO-8601 UTC creation timestamp")


class ConversationResponse(BaseModel):
    """``GET /api/conversation`` body — active thread metadata + last N messages."""

    thread_id: int | None = Field(
        default=None,
        description="The active thread id (server-issued), or None when none is open",
    )
    started_at: str | None = Field(default=None, description="Active thread start timestamp")
    last_active_at: str | None = Field(
        default=None, description="Active thread last-activity timestamp"
    )
    messages: list[ConversationMessageOut] = Field(
        default_factory=list,
        description=f"Last {REHYDRATE_MESSAGE_LIMIT} messages, oldest→newest, decrypted",
    )


class ConversationResetResponse(BaseModel):
    """``POST /api/conversation/reset`` body — the archive/new-thread pair."""

    archived_thread_id: int | None = Field(
        default=None,
        description="The thread that was archived, or None when none was open",
    )
    thread_id: int = Field(..., description="The freshly opened active thread id")


class ConversationPurgeRequest(BaseModel):
    """``POST /api/conversation/purge`` body — explicit-confirm gate (C4/C6)."""

    confirm: bool = Field(
        default=False,
        validate_default=True,  # the default (missing flag) must hit the validator too
        description="Must be true: the purge deletes ALL threads and messages "
        "for the athlete and is irrevocable",
    )

    @field_validator("confirm")
    @classmethod
    def _require_explicit_confirm(cls, value: bool) -> bool:
        """Reject anything but the explicit ``true`` (destructive, C4/C6)."""
        if not value:
            raise ValueError("confirm must be true")
        return value


class ConversationPurgeResponse(BaseModel):
    """``POST /api/conversation/purge`` body — scoped delete counts (C3)."""

    threads_deleted: int = Field(..., ge=0, description="Threads deleted for the athlete")
    messages_deleted: int = Field(..., ge=0, description="Messages deleted for the athlete")


async def open_turn_thread(
    session_factory: SessionFactory,
    *,
    athlete_id: int,
    thread_id: int | None,
) -> OpenTurnOutcome:
    """Resolve or create the turn's thread — degrade-wrapped (C7), fail-closed (C4).

    With an explicit ``thread_id`` the thread must EXIST, be OWNED by
    ``athlete_id`` and be ACTIVE: unknown or non-owned raises 404 (the two
    are indistinguishable on purpose), archived raises 409 (read-only).
    Without one, the athlete's most recent active thread is reused or, if
    none is open, a fresh one is created (first turn). An infra failure in
    the degrade family never raises: the outcome reports ``degraded`` and
    the turn proceeds stateless (the reply is the product, C7).
    """
    try:
        async with session_factory() as session:
            if thread_id is not None:
                thread = await get_thread(session, athlete_id=athlete_id, thread_id=thread_id)
                if thread is None:
                    raise HTTPException(status_code=404, detail=THREAD_NOT_FOUND_DETAIL)
                if thread.status != "active":
                    raise HTTPException(status_code=409, detail=THREAD_ARCHIVED_DETAIL)
                return OpenTurnOutcome(thread=thread, degraded=False)
            thread = await get_active_thread(session, athlete_id=athlete_id)
            if thread is None:
                thread = await create_thread(session, athlete_id=athlete_id)
            return OpenTurnOutcome(thread=thread, degraded=False)
    except PERSISTENCE_DEGRADE_EXCEPTIONS as exc:
        log_persistence_degrade(exc)
        return OpenTurnOutcome(thread=None, degraded=True)


async def record_user_turn(
    session_factory: SessionFactory,
    *,
    athlete_id: int,
    thread: ConversationThreadRow,
    cipher: Fernet,
    content: str,
) -> TurnPersistence:
    """Persist the user message before streaming — degrade-wrapped (C7).

    The retry guard skips the append when the incoming message matches the
    thread's most recent USER message (the in-flight / re-send signature —
    see the module docstring for the honest design and its imprecisions).
    The write itself is the #348 store's scoped, role-server-assigned
    append. The method never raises: infra failures report ``degraded``.
    """
    try:
        async with session_factory() as session:
            recent_user = await _most_recent_user_message(
                session, athlete_id=athlete_id, thread_id=thread.id, cipher=cipher
            )
            if recent_user is not None and recent_user.content == content:
                return TurnPersistence(persisted=False, degraded=False)
            row = await append_message(
                session,
                athlete_id=athlete_id,
                thread_id=thread.id,
                role="user",
                content=content,
                cipher=cipher,
            )
            return TurnPersistence(persisted=row is not None, degraded=False)
    except PERSISTENCE_DEGRADE_EXCEPTIONS as exc:
        log_persistence_degrade(exc)
        return TurnPersistence(persisted=False, degraded=True)


async def record_assistant_turn(
    session_factory: SessionFactory,
    *,
    athlete_id: int,
    thread: ConversationThreadRow,
    cipher: Fernet,
    content: str,
) -> TurnPersistence:
    """Persist the assistant reply exactly once on completion (C7, AC2).

    Called ONLY from the stream's finalization path with the clean
    ``done`` marker's message (never partials, never on disconnect — the
    endpoint owns that gate). The duplicate guard skips the append when
    the thread's last message is an identical assistant row (the
    deterministic-replay duplicate). A thread that vanished mid-stream (a
    concurrent reset archived it) reports ``persisted=False`` without
    degrading — the reset legitimately closed it.
    """
    try:
        async with session_factory() as session:
            last = await list_last_messages(
                session, athlete_id=athlete_id, thread_id=thread.id, limit=1, cipher=cipher
            )
            if last and last[0].role == "assistant" and last[0].content == content:
                return TurnPersistence(persisted=False, degraded=False)
            row = await append_message(
                session,
                athlete_id=athlete_id,
                thread_id=thread.id,
                role="assistant",
                content=content,
                cipher=cipher,
            )
            return TurnPersistence(persisted=row is not None, degraded=False)
    except PERSISTENCE_DEGRADE_EXCEPTIONS as exc:
        log_persistence_degrade(exc)
        return TurnPersistence(persisted=False, degraded=True)


async def purge_athlete_conversations(
    session: AsyncSession,
    *,
    athlete_id: int,
) -> tuple[int, int]:
    """Delete ALL threads and messages of ONE athlete; return counts (AC4, C3).

    Strictly scoped by ``athlete_id`` — the destructive purge behind the
    confirm-gated endpoint. Both deletes run in one transaction; the
    message delete runs first so the reported counts are explicit (the
    thread delete's FK CASCADE would remove the rows too, but the counts
    are the endpoint's only output and must be honest). Never reads
    ``content_encrypted``/``summary_encrypted`` — counts only, no content
    can ride along (C3).
    """
    messages_result = cast(
        CursorResult[Any],
        await session.execute(
            delete(ConversationMessageRow).where(ConversationMessageRow.athlete_id == athlete_id)
        ),
    )
    threads_result = cast(
        CursorResult[Any],
        await session.execute(
            delete(ConversationThreadRow).where(ConversationThreadRow.athlete_id == athlete_id)
        ),
    )
    await session.commit()
    return (
        int(threads_result.rowcount or 0),
        int(messages_result.rowcount or 0),
    )


async def _most_recent_user_message(
    session: AsyncSession,
    *,
    athlete_id: int,
    thread_id: int,
    cipher: Fernet,
) -> StoredMessage | None:
    """The thread's most recent USER message, decrypted, or None (retry guard)."""
    recent = await list_last_messages(
        session,
        athlete_id=athlete_id,
        thread_id=thread_id,
        limit=_RETRY_SCAN_DEPTH,
        cipher=cipher,
    )
    for message in reversed(recent):
        if message.role == "user":
            return message
    return None


def log_persistence_degrade(exc: BaseException) -> None:
    """Log a persistence degrade WITHOUT content or exception text (C3).

    The exception CLASS NAME is the only diagnostic that rides the log:
    SQLAlchemy messages can embed SQL (the parameters are ciphertext, but
    the log stays value-free regardless) and OSError messages can embed
    filesystem paths. Triaging needs the failure class, never the bytes.
    """
    logger.warning(
        "Conversation persistence unavailable (%s) — degrading to stateless chat",
        type(exc).__name__,
    )
