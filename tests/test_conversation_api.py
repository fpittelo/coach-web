"""Server-owned conversation thread API — wire change, SSE persistence, rehydrate (#349).

Pins the API/SSE slice of MADR-0025 (story #349) against the real app with a
migrated per-test SQLite database:

- AC1 (wire change): ``AgentStreamRequest`` no longer carries ``history`` —
  the field is deleted and never read; a cached old client that still sends
  it is IGNORED (Pydantic default ``extra="ignore"``, deliberately not
  ``forbid`` per MADR-0025) while the injection path stays closed because
  the server never replays client history into the prompt. The request now
  carries the message plus an optional server-issued ``thread_id``.
- AC2 (persistence points): the user message is persisted BEFORE streaming;
  the assistant message is persisted EXACTLY ONCE on completion (the
  ``done`` event is the completed-turn marker); a retry of an in-flight
  turn does not double-append; ``seq`` is monotonic per thread.
- AC3 (rehydrate): ``GET /api/conversation`` returns the active thread's
  metadata plus the last N messages oldest→newest, decrypted.
- AC4 (reset/purge): reset archives the active thread and opens a fresh
  one; purge requires the explicit ``confirm`` flag and deletes ONLY the
  session athlete's rows.
- AC5 (C7 failure posture): persistence failures never abort the SSE
  stream — the reply is the product. A DB failure degrades to stateless
  chat with a redacted visible ``warning`` event (stable code, no content,
  no key material, no exception text in the frame or the logs).
- AC6 (scoping): unknown or non-owned ``thread_id`` → 404 fail-closed; an
  archived thread rejects writes; every route is scoped by the session
  ``athlete_id``.

No prompt-assembly change is pinned here deliberately: memory does NOT
enter the prompt in this slice (#350) — the model keeps receiving only the
current user message.
"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from coach_web.agent import DEFAULT_SYSTEM_PROMPT, AgentEvent
from coach_web.app import create_app
from coach_web.config import Settings, get_settings
from coach_web.conversation import (
    StoredMessage,
    append_message,
    create_thread,
    get_active_thread,
    list_last_messages,
)
from coach_web.db import build_db_engine, build_session_factory
from coach_web.debrief import DEFAULT_ATHLETE_ID, build_debrief_cipher
from coach_web.models import AgentStreamRequest

# Contract constants (duplicated here deliberately — the wire contract is
# pinned by value, so a silent change of the degrade frame fails loudly).
DEGRADE_WARNING_CODE = "conversation_persistence_degraded"
DEGRADE_WARNING_MESSAGE = "Conversation memory is unavailable — the reply was not saved to history."
THREAD_NOT_FOUND_DETAIL = "Conversation thread not found"
THREAD_ARCHIVED_DETAIL = "Conversation thread is archived"
REHYDRATE_MESSAGE_LIMIT = 20

OWNER = DEFAULT_ATHLETE_ID
FOREIGN_ATHLETE = 987654321

SIMULATED_DB_FAILURE = "simulated persistence outage"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScriptedAgent:
    """Stub agent replaying a per-call script of events or exceptions.

    Each ``run()`` call pops the next script entry: a list of events (the
    turn streams them verbatim) or an exception (the turn fails at once).
    The capture lists pin what the endpoint actually forwarded.
    """

    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    def __init__(self, scripts: list[list[AgentEvent] | Exception]) -> None:
        self._scripts = list(scripts)
        self.messages: list[str] = []
        self.histories: list[Any] = []
        self.prompts: list[str] = []

    async def __aenter__(self) -> "_ScriptedAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self,
        message: str,
        *,
        history: list[Any] | None = None,
        pre_emitted_outcomes: Sequence[str] = (),
    ) -> AsyncIterator[AgentEvent]:
        """Replay the next scripted turn, capturing the forwarded inputs."""
        self.messages.append(message)
        self.histories.append(history)
        self.prompts.append(self.system_prompt)
        script = self._scripts.pop(0)
        if isinstance(script, Exception):
            raise script
        for event in script:
            yield event


def _clean_reply_agent(reply: str = "Stub coach reply.") -> _ScriptedAgent:
    """A stub agent whose every turn is a clean one-event completion."""
    return _ScriptedAgent(
        [
            [
                AgentEvent(type="token", data={"text": reply}),
                AgentEvent(type="done", data={"message": reply, "iterations": 1}),
            ]
        ]
        * 4
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(settings: Settings, agent: Any) -> TestClient:
    """A TestClient whose agent factory returns the supplied stub agent."""
    app = create_app(settings)
    app.state.agent_factory = MagicMock(return_value=agent)
    return TestClient(app)


def _parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse an SSE body into ``(event, data)`` pairs.

    The endpoint serialises each typed event with
    ``AgentEvent.model_dump_json()`` (``{"type": ..., "data": {...}}``);
    the parser returns the INNER ``data`` dict so assertions read against
    the event payload itself.
    """
    events: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    data_lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("event:"):
            name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
        elif not line.strip():
            if name is not None:
                raw = "\n".join(data_lines)
                envelope = json.loads(raw) if raw else {}
                events.append((name, envelope.get("data", {})))
            name = None
            data_lines = []
    if name is not None:
        raw = "\n".join(data_lines)
        envelope = json.loads(raw) if raw else {}
        events.append((name, envelope.get("data", {})))
    return events


@pytest.fixture(autouse=True)
def _migrated_store(migrated_db: Path) -> Path:
    """Run every test in this module against the migrated per-test SQLite file.

    The conversation routes and the stream persistence boundary all touch
    the store; the migrated database is the default happy path here (the
    degrade tests patch individual steps to fail, not the whole store).
    """
    return migrated_db


def _stream_once(client: TestClient, payload: dict[str, Any]) -> str:
    """POST one stream request and return the raw SSE body."""
    with client.stream("POST", "/api/agent/stream", json=payload) as response:
        assert response.status_code == 200
        return response.read().decode("utf-8")


def _cipher() -> Fernet:
    """The lane cipher built through the SAME helper the debriefs use."""
    return build_debrief_cipher(get_settings())


def _sql(db_path: Path, statement: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Run one read-only query against the per-test SQLite file."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(statement, params).fetchall()
    finally:
        conn.close()
    return list(rows)


def _seed_thread(
    db_path: Path,
    *,
    athlete_id: int,
    status: str = "active",
    messages: list[tuple[str, str]],
) -> int:
    """Seed a thread with messages directly through the frozen repository."""
    cipher = _cipher()

    async def _seed() -> int:
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                thread = await create_thread(session, athlete_id=athlete_id)
                if status == "archived":
                    thread.status = "archived"
                    await session.commit()
                for role, content in messages:
                    await append_message(
                        session,
                        athlete_id=athlete_id,
                        thread_id=thread.id,
                        role=role,  # type: ignore[arg-type]
                        content=content,
                        cipher=cipher,
                    )
                return int(thread.id)
        finally:
            await engine.dispose()

    return asyncio.run(_seed())


def _read_transcript(db_path: Path, thread_id: int | None = None) -> list[StoredMessage]:
    """Read a thread's messages decrypted, oldest→newest (repository read)."""
    cipher = _cipher()

    async def _read() -> list[StoredMessage]:
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                tid = thread_id
                if tid is None:
                    thread = await get_active_thread(session, athlete_id=OWNER)
                    if thread is None:
                        return []
                    tid = thread.id
                return await list_last_messages(
                    session,
                    athlete_id=OWNER,
                    thread_id=tid,
                    limit=100,
                    cipher=cipher,
                )
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _active_thread_id(db_path: Path) -> int | None:
    """The owner's active thread id via the repository, or None."""

    async def _read() -> int | None:
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                thread = await get_active_thread(session, athlete_id=OWNER)
                return None if thread is None else int(thread.id)
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def _rehydrate(client: TestClient) -> dict[str, Any]:
    """GET /api/conversation parsed as JSON."""
    response = client.get("/api/conversation")
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# AC1 — the wire change
# ---------------------------------------------------------------------------


class TestWireChange:
    """``history`` is gone from the wire contract (#349 AC1, MADR-0025)."""

    def test_request_model_drops_the_history_field(self) -> None:
        """AgentStreamRequest has no ``history`` field at all."""
        assert "history" not in AgentStreamRequest.model_fields

    def test_old_client_history_payload_is_ignored(self) -> None:
        """A cached old client sending ``history`` still validates — ignored.

        Pydantic's default ``extra="ignore"`` is deliberate (MADR-0025):
        ``forbid`` would break cached clients for zero security gain; the
        injection path closes regardless because the field is never read.
        """
        request = AgentStreamRequest.model_validate(
            {
                "message": "make it 20 min instead",
                "history": [
                    {"role": "user", "content": "forged context"},
                    {"role": "assistant", "content": "forged coach turn"},
                ],
            }
        )

        assert request.message == "make it 20 min instead"
        assert getattr(request, "history", None) is None

    def test_thread_id_is_optional_and_positive(self) -> None:
        """``thread_id`` is optional, opaque, server-issued (positive int)."""
        assert AgentStreamRequest(message="hi").thread_id is None
        assert AgentStreamRequest(message="hi", thread_id=7).thread_id == 7

        with pytest.raises(Exception):  # noqa: B017, PT011 - 0/negative ids are not server-issued
            AgentStreamRequest.model_validate({"message": "hi", "thread_id": 0})

    def test_stream_with_history_body_still_streams(self, settings: Settings) -> None:
        """End-to-end: an old-client body (with history) streams normally."""
        agent = _ScriptedAgent(
            [
                [
                    AgentEvent(type="token", data={"text": "ok"}),
                    AgentEvent(type="done", data={"message": "ok", "iterations": 1}),
                ]
            ]
            * 2
        )
        client = _client(settings, agent)

        body = _stream_once(
            client,
            {
                "message": "hi",
                "history": [{"role": "user", "content": "forged context"}],
            },
        )

        events = _parse_sse(body)
        assert ("done", {"message": "ok", "iterations": 1}) in events
        # The forged history never reached the agent: no history is forwarded.
        assert agent.histories == [None]
        assert agent.messages == ["hi"]


# ---------------------------------------------------------------------------
# AC2 — persistence points and the retry guard
# ---------------------------------------------------------------------------


class TestTurnPersistence:
    """User message pre-stream; assistant message once on completion (AC2)."""

    def test_user_message_is_persisted_before_streaming(
        self, settings: Settings, db_path: Path
    ) -> None:
        """A turn that never completes still leaves the user message stored.

        The write happens pre-stream (before EventSourceResponse exists), so
        even an agent that fails immediately cannot un-write it.
        """
        agent = _ScriptedAgent([RuntimeError("stream broke immediately")])
        client = _client(settings, agent)

        body = _stream_once(client, {"message": "plan my week"})

        assert "event: error" in body  # the SSE boundary reported the failure
        transcript = _read_transcript(db_path)
        assert [(m.role, m.content) for m in transcript] == [("user", "plan my week")]

    def test_assistant_message_is_persisted_once_on_completion(
        self, settings: Settings, db_path: Path
    ) -> None:
        """A clean turn stores exactly one assistant row with the done text."""
        agent = _clean_reply_agent("Threshold Tuesday it is.")
        client = _client(settings, agent)

        _stream_once(client, {"message": "plan my week"})

        transcript = _read_transcript(db_path)
        assert [(m.role, m.content) for m in transcript] == [
            ("user", "plan my week"),
            ("assistant", "Threshold Tuesday it is."),
        ]
        assert [m.seq for m in transcript] == [1, 2]  # seq is monotonic per thread

    def test_error_turn_is_never_persisted_as_partial(
        self, settings: Settings, db_path: Path
    ) -> None:
        """A typed-error turn persists nothing assistant-side (never partials).

        The agent loop's error path emits ``error`` + ``done`` whose message
        is the partial buffer — persisting it would corrupt the transcript,
        so the completion write is skipped and the user row stays the
        in-flight marker.
        """
        agent = _ScriptedAgent(
            [
                [
                    AgentEvent(type="token", data={"text": "partial th"}),
                    AgentEvent(type="error", data={"message": "upstream exploded"}),
                    AgentEvent(type="done", data={"message": "partial th", "iterations": 1}),
                ]
            ]
            * 2
        )
        client = _client(settings, agent)

        _stream_once(client, {"message": "plan my week"})

        transcript = _read_transcript(db_path)
        assert [(m.role, m.content) for m in transcript] == [("user", "plan my week")]

    def test_retry_of_inflight_turn_does_not_duplicate(
        self, settings: Settings, db_path: Path
    ) -> None:
        """A retried in-flight turn appends nothing twice (AC2).

        Attempt 1 dies mid-stream after the user write; the client retries
        the SAME message against the SAME thread (as a reconnecting client
        would). The retry guard recognizes the in-flight signature and the
        thread ends with exactly one user row and one assistant row.
        """
        agent = _ScriptedAgent(
            [
                RuntimeError("connection dropped mid-stream"),
                [
                    AgentEvent(type="token", data={"text": "Recovered reply."}),
                    AgentEvent(type="done", data={"message": "Recovered reply.", "iterations": 1}),
                ],
            ]
        )
        client = _client(settings, agent)

        _stream_once(client, {"message": "how did last week go?"})
        thread_id = _rehydrate(client)["thread_id"]
        assert thread_id is not None

        _stream_once(client, {"message": "how did last week go?", "thread_id": thread_id})

        transcript = _read_transcript(db_path, thread_id=thread_id)
        assert [(m.role, m.content) for m in transcript] == [
            ("user", "how did last week go?"),
            ("assistant", "Recovered reply."),
        ]

    def test_completed_turn_resend_is_suppressed(self, settings: Settings, db_path: Path) -> None:
        """A re-send of a completed turn does not duplicate the exchange.

        The deterministic stub replies identically on both calls, so the
        completion write recognizes the already-recorded reply and skips —
        the thread keeps exactly one user/assistant pair.
        """
        agent = _clean_reply_agent("Same reply both times.")
        client = _client(settings, agent)

        _stream_once(client, {"message": "hello again"})
        thread_id = _rehydrate(client)["thread_id"]
        _stream_once(client, {"message": "hello again", "thread_id": thread_id})

        transcript = _read_transcript(db_path, thread_id=thread_id)
        assert [(m.role, m.content) for m in transcript] == [
            ("user", "hello again"),
            ("assistant", "Same reply both times."),
        ]

    def test_distinct_follow_up_turns_persist_in_order(
        self, settings: Settings, db_path: Path
    ) -> None:
        """Two different turns append four ordered rows (seq monotonic)."""
        agent = _ScriptedAgent(
            [
                [
                    AgentEvent(type="token", data={"text": "First reply."}),
                    AgentEvent(type="done", data={"message": "First reply.", "iterations": 1}),
                ],
                [
                    AgentEvent(type="token", data={"text": "Second reply."}),
                    AgentEvent(type="done", data={"message": "Second reply.", "iterations": 1}),
                ],
            ]
        )
        client = _client(settings, agent)

        _stream_once(client, {"message": "first question"})
        _stream_once(client, {"message": "second question"})

        transcript = _read_transcript(db_path)
        assert [(m.role, m.content, m.seq) for m in transcript] == [
            ("user", "first question", 1),
            ("assistant", "First reply.", 2),
            ("user", "second question", 3),
            ("assistant", "Second reply.", 4),
        ]


# ---------------------------------------------------------------------------
# AC6 — thread scoping at the stream boundary
# ---------------------------------------------------------------------------


class TestThreadScoping:
    """C4 fail-closed scoping: 404 unknown/non-owned, 409 archived."""

    def test_unknown_thread_id_fails_closed_404(self, settings: Settings) -> None:
        """A thread id the server never issued is a plain 404."""
        client = _client(settings, _clean_reply_agent())

        response = client.post("/api/agent/stream", json={"message": "hi", "thread_id": 424242})

        assert response.status_code == 404
        assert response.json() == {"detail": THREAD_NOT_FOUND_DETAIL}

    def test_foreign_athlete_thread_id_fails_closed_404(
        self, settings: Settings, db_path: Path
    ) -> None:
        """Another athlete's thread id is indistinguishable from unknown."""
        foreign_thread = _seed_thread(
            db_path,
            athlete_id=FOREIGN_ATHLETE,
            messages=[("user", "foreign secret")],
        )
        client = _client(settings, _clean_reply_agent())

        response = client.post(
            "/api/agent/stream", json={"message": "hi", "thread_id": foreign_thread}
        )

        assert response.status_code == 404
        assert response.json() == {"detail": THREAD_NOT_FOUND_DETAIL}

    def test_archived_thread_write_is_rejected_409(self, settings: Settings, db_path: Path) -> None:
        """An archived thread is read-only: the write is rejected (C4)."""
        archived = _seed_thread(
            db_path,
            athlete_id=OWNER,
            status="archived",
            messages=[("user", "old turn")],
        )
        client = _client(settings, _clean_reply_agent())

        response = client.post("/api/agent/stream", json={"message": "hi", "thread_id": archived})

        assert response.status_code == 409
        assert response.json() == {"detail": THREAD_ARCHIVED_DETAIL}
        # Nothing was written into the archived thread.
        assert len(_read_transcript(db_path, thread_id=archived)) == 1


# ---------------------------------------------------------------------------
# AC3 — rehydrate
# ---------------------------------------------------------------------------


class TestRehydrate:
    """GET /api/conversation — active thread metadata + last N messages."""

    def test_rehydrate_returns_ordered_decrypted_transcript(
        self, settings: Settings, db_path: Path
    ) -> None:
        """The endpoint returns the same ordered transcript that was shown."""
        client = _client(settings, _clean_reply_agent())
        _stream_once(client, {"message": "first question"})
        _stream_once(client, {"message": "second question"})

        payload = _rehydrate(client)

        assert payload["thread_id"] is not None
        assert payload["started_at"] is not None
        assert payload["last_active_at"] is not None
        assert [(m["role"], m["content"], m["seq"]) for m in payload["messages"]] == [
            ("user", "first question", 1),
            ("assistant", "Stub coach reply.", 2),
            ("user", "second question", 3),
            ("assistant", "Stub coach reply.", 4),
        ]
        assert all(m["created_at"] for m in payload["messages"])

    def test_rehydrate_without_active_thread_is_an_empty_state(self, settings: Settings) -> None:
        """No open thread → null metadata and no messages (graceful empty)."""
        client = _client(settings, _clean_reply_agent())

        payload = _rehydrate(client)

        assert payload == {
            "thread_id": None,
            "started_at": None,
            "last_active_at": None,
            "messages": [],
        }

    def test_rehydrate_fails_closed_503_on_an_unreadable_store(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A broken store answers the static 503 — never partial content (AC3).

        The read boundary fails closed: an unreadable store must not serve
        a partial transcript, so the whole rehydrate answers 503 with the
        value-free detail.
        """
        import coach_web.app as app_module

        def failing_read(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError(SIMULATED_DB_FAILURE)

        monkeypatch.setattr(app_module, "get_active_thread", failing_read)
        client = _client(settings, _clean_reply_agent())

        response = client.get("/api/conversation")

        assert response.status_code == 503
        assert response.json() == {"detail": "Service temporarily unavailable"}

    def test_rehydrate_fails_closed_503_on_a_tampered_row(
        self, settings: Settings, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One row failing decryption fails the whole read (wrong-key posture)."""
        thread_id = _seed_thread(
            db_path,
            athlete_id=OWNER,
            messages=[("user", "tampered target")],
        )
        import coach_web.app as app_module

        def failing_decrypt(*args: Any, **kwargs: Any) -> Any:
            raise InvalidToken()

        monkeypatch.setattr(app_module, "list_last_messages", failing_decrypt)
        client = _client(settings, _clean_reply_agent())

        response = client.get("/api/conversation")

        assert response.status_code == 503
        assert response.json() == {"detail": "Service temporarily unavailable"}
        # The row itself is untouched (read-only boundary).
        assert len(_read_transcript(db_path, thread_id=thread_id)) == 1

    def test_rehydrate_is_bounded_to_the_last_n_messages(
        self, settings: Settings, db_path: Path
    ) -> None:
        """The rehydrate window returns the LAST N messages, still ordered."""
        messages = [("user" if i % 2 == 0 else "assistant", f"turn {i}") for i in range(25)]
        _seed_thread(db_path, athlete_id=OWNER, messages=messages)
        client = _client(settings, _clean_reply_agent())

        payload = _rehydrate(client)

        assert len(payload["messages"]) == REHYDRATE_MESSAGE_LIMIT
        assert payload["messages"][0]["seq"] == 25 - REHYDRATE_MESSAGE_LIMIT + 1
        assert payload["messages"][-1]["content"] == "turn 24"


# ---------------------------------------------------------------------------
# AC4 — reset and purge
# ---------------------------------------------------------------------------


class TestResetAndPurge:
    """Reset archives + opens fresh; purge requires confirm and is scoped."""

    def test_reset_archives_the_active_thread_and_opens_a_fresh_one(
        self, settings: Settings, db_path: Path
    ) -> None:
        """Reset archives the old thread and the next GET sees a clean one."""
        client = _client(settings, _clean_reply_agent())
        _stream_once(client, {"message": "old conversation"})
        old_thread_id = _rehydrate(client)["thread_id"]
        assert old_thread_id is not None

        response = client.post("/api/conversation/reset")

        assert response.status_code == 200
        body = response.json()
        assert body["archived_thread_id"] == old_thread_id
        new_thread_id = body["thread_id"]
        assert new_thread_id != old_thread_id

        # The old thread is archived in the store…
        status_rows = _sql(
            db_path,
            "SELECT status FROM conversation_threads WHERE id = ?",
            (old_thread_id,),
        )
        assert status_rows == [("archived",)]
        # …the rehydrate now shows the fresh thread…
        payload = _rehydrate(client)
        assert payload["thread_id"] == new_thread_id
        assert payload["messages"] == []
        # …and the archived thread rejects further writes.
        rejected = client.post(
            "/api/agent/stream", json={"message": "late turn", "thread_id": old_thread_id}
        )
        assert rejected.status_code == 409

    def test_reset_without_a_thread_still_opens_one(self, settings: Settings) -> None:
        """Reset on a blank slate opens the first thread (archived: null)."""
        client = _client(settings, _clean_reply_agent())

        response = client.post("/api/conversation/reset")

        assert response.status_code == 200
        body = response.json()
        assert body["archived_thread_id"] is None
        assert body["thread_id"] is not None

    def test_purge_requires_the_explicit_confirm_flag(self, settings: Settings) -> None:
        """Purge without ``confirm: true`` is rejected (422, value-free)."""
        client = _client(settings, _clean_reply_agent())

        missing = client.post("/api/conversation/purge", json={})
        declined = client.post("/api/conversation/purge", json={"confirm": False})

        assert missing.status_code == 422
        assert missing.json() == {"detail": "Invalid request payload"}
        assert declined.status_code == 422

    def test_purge_deletes_only_the_session_athletes_rows(
        self, settings: Settings, db_path: Path
    ) -> None:
        """Purge deletes ALL of the athlete's threads/messages and nothing else."""
        _seed_thread(
            db_path,
            athlete_id=OWNER,
            messages=[("user", "one"), ("assistant", "two")],
        )
        _seed_thread(
            db_path,
            athlete_id=OWNER,
            status="archived",
            messages=[("user", "three")],
        )
        foreign_thread = _seed_thread(
            db_path,
            athlete_id=FOREIGN_ATHLETE,
            messages=[("user", "keep me")],
        )
        client = _client(settings, _clean_reply_agent())

        response = client.post("/api/conversation/purge", json={"confirm": True})

        assert response.status_code == 200
        assert response.json() == {"threads_deleted": 2, "messages_deleted": 3}
        assert _sql(
            db_path,
            "SELECT COUNT(*) FROM conversation_threads WHERE athlete_id = ?",
            (OWNER,),
        ) == [(0,)]
        assert _sql(
            db_path,
            "SELECT COUNT(*) FROM conversation_messages WHERE athlete_id = ?",
            (OWNER,),
        ) == [(0,)]
        # The foreign athlete's data is untouched (scoped delete only).
        assert _sql(
            db_path,
            "SELECT COUNT(*) FROM conversation_threads WHERE id = ?",
            (foreign_thread,),
        ) == [(1,)]
        assert _sql(
            db_path,
            "SELECT COUNT(*) FROM conversation_messages WHERE athlete_id = ?",
            (FOREIGN_ATHLETE,),
        ) == [(1,)]


# ---------------------------------------------------------------------------
# AC5 — the C7 degrade posture
# ---------------------------------------------------------------------------


class TestC7Degrade:
    """Persistence failure never aborts the stream; the reply is the product."""

    def test_user_write_failure_degrades_to_stateless_with_warning(
        self,
        settings: Settings,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing user write yields a warning frame, then the full reply."""
        import coach_web.conversation_api as conversation_api

        real = append_message

        async def failing_user_append(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("role") == "user":
                raise SQLAlchemyError(SIMULATED_DB_FAILURE)
            return await real(*args, **kwargs)

        monkeypatch.setattr(conversation_api, "append_message", failing_user_append)
        agent = _clean_reply_agent("The reply survives.")
        client = _client(settings, agent)

        with caplog.at_level("WARNING"):
            body = _stream_once(client, {"message": "SECRET user confession"})

        events = _parse_sse(body)
        kinds = [name for name, _ in events]
        # The warning opens the stream; the reply still completes.
        assert kinds == ["warning", "token", "done"]
        assert ("token", {"text": "The reply survives."}) in events
        warning = events[0][1]
        assert warning == {"code": DEGRADE_WARNING_CODE, "message": DEGRADE_WARNING_MESSAGE}
        # Nothing user-side was persisted (stateless chat); the completion write
        # still landed (the patch fails the user role only).
        assert _sql(db_path, "SELECT role, COUNT(*) FROM conversation_messages GROUP BY role") == [
            ("assistant", 1)
        ]
        # C3: no content, no exception text in the logs.
        log_blob = caplog.text
        assert "SECRET user confession" not in log_blob
        assert SIMULATED_DB_FAILURE not in log_blob

    def test_assistant_write_failure_mid_stream_keeps_the_full_reply(
        self,
        settings: Settings,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failing completion write degrades after done — reply intact."""

        import coach_web.conversation_api as conversation_api

        real = append_message

        async def failing_assistant_append(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("role") == "assistant":
                raise SQLAlchemyError(SIMULATED_DB_FAILURE)
            return await real(*args, **kwargs)

        monkeypatch.setattr(conversation_api, "append_message", failing_assistant_append)
        agent = _clean_reply_agent("Full streamed reply.")
        client = _client(settings, agent)

        with caplog.at_level("WARNING"):
            body = _stream_once(client, {"message": "SECRET user confession"})

        events = _parse_sse(body)
        kinds = [name for name, _ in events]
        # done still arrives; the degrade warning is the final frame.
        assert kinds == ["token", "done", "warning"]
        warning = events[-1][1]
        assert warning == {"code": DEGRADE_WARNING_CODE, "message": DEGRADE_WARNING_MESSAGE}
        # The user turn WAS persisted; the assistant turn was not.
        transcript = _read_transcript(db_path)
        assert [(m.role, m.content) for m in transcript] == [("user", "SECRET user confession")]
        # C3: neither the content nor the exception text reached the logs.
        assert "SECRET user confession" not in caplog.text
        assert SIMULATED_DB_FAILURE not in caplog.text

    def test_missing_encryption_key_degrades_to_stateless(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No ``DATA_ENCRYPTION_KEY`` → the turn streams stateless (C7).

        The lane cipher joins the persistence boundary: a missing key is
        an unreadable store by another name — the reply is the product.
        """
        monkeypatch.delenv("DATA_ENCRYPTION_KEY")
        get_settings.cache_clear()
        resolved = get_settings()
        app = create_app(resolved)
        app.state.agent_factory = MagicMock(return_value=_clean_reply_agent("Keyless reply."))
        client = TestClient(app)

        with caplog.at_level("WARNING"):
            body = _stream_once(client, {"message": "SECRET without a key"})

        events = _parse_sse(body)
        kinds = [name for name, _ in events]
        assert kinds == ["warning", "token", "done"]
        assert events[0][1] == {"code": DEGRADE_WARNING_CODE, "message": DEGRADE_WARNING_MESSAGE}
        assert _sql(Path(resolved.COACH_DB_PATH), "SELECT COUNT(*) FROM conversation_threads") == [
            (0,)
        ]
        assert "SECRET without a key" not in caplog.text

    def test_thread_resolve_failure_degrades_to_stateless(
        self,
        settings: Settings,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An unreadable store yields warning + full reply, and no thread."""
        import coach_web.conversation_api as conversation_api

        def failing_resolve(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError(SIMULATED_DB_FAILURE)

        monkeypatch.setattr(conversation_api, "get_active_thread", failing_resolve)
        agent = _clean_reply_agent("Stateless reply.")
        client = _client(settings, agent)

        with caplog.at_level("WARNING"):
            body = _stream_once(client, {"message": "SECRET question"})

        events = _parse_sse(body)
        kinds = [name for name, _ in events]
        assert kinds[0] == "warning"
        assert kinds[-1] == "done"
        assert ("token", {"text": "Stateless reply."}) in events
        assert events[0][1] == {"code": DEGRADE_WARNING_CODE, "message": DEGRADE_WARNING_MESSAGE}
        # No thread was created for the degraded turn.
        assert _sql(db_path, "SELECT COUNT(*) FROM conversation_threads") == [(0,)]
        assert "SECRET question" not in caplog.text
        assert SIMULATED_DB_FAILURE not in caplog.text


# ---------------------------------------------------------------------------
# The #350 boundary — memory does NOT enter the prompt yet
# ---------------------------------------------------------------------------


class TestPromptStaysStateless:
    """This slice persists turns but the prompt contract is unchanged."""

    def test_persisted_turns_are_not_forwarded_to_the_agent(
        self, settings: Settings, db_path: Path
    ) -> None:
        """Two turns against one thread: the agent still gets no history."""
        agents: list[_ScriptedAgent] = []

        def fresh_agent(_settings: Any) -> _ScriptedAgent:
            """Mirror production: a fresh agent instance per request."""
            agent = _ScriptedAgent(
                [
                    [
                        AgentEvent(type="token", data={"text": "replied"}),
                        AgentEvent(type="done", data={"message": "replied", "iterations": 1}),
                    ]
                ]
            )
            agents.append(agent)
            return agent

        app = create_app(settings)
        app.state.agent_factory = MagicMock(side_effect=fresh_agent)
        client = TestClient(app)

        _stream_once(client, {"message": "first question"})
        _stream_once(client, {"message": "second question"})

        assert [a.histories for a in agents] == [[None], [None]]
        assert [a.messages for a in agents] == [
            ["first question"],
            ["second question"],
        ]
        # The assembled system prompt is identical across turns (unchanged
        # assembly; persistence adds no digest, no memory block).
        assert agents[0].prompts[0] == agents[1].prompts[0]
