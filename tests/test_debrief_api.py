"""Debrief API contract: POST /api/debrief + GET /api/debrief/pending (#189).

Story 3.1 wires the encrypted capture path and the pending derivation into
the FastAPI boundary. The contract pinned here:

- ``POST /api/debrief`` answers 201 with metadata only (the notes are never
  echoed — nLPD) and persists the notes as Fernet ciphertext; NO plaintext
  notes bytes ever reach the database files (AC1);
- a missing or invalid ``DATA_ENCRYPTION_KEY`` answers 503 with the fixed
  ``{code, message}`` vocabulary (#189 AC2) — nothing is persisted and there
  is no plaintext fallback;
- malformed payloads keep the strict #142 generic 422 detail (the debrief
  path is NOT an owner-facing settings path — the reason vocabulary stays
  scoped to #181's endpoints and the encryption 503);
- auth middleware covers both routes (401 without a session, AC4) and the
  pending derivation is per-athlete (athlete B's view is computed from B's
  own debrief rows — athlete A's debriefs are invisible to B, AC4);
- the agent stream injects the pending-debrief digest into the system
  prompt when pending sessions exist (MADR-0010 greeting hook, AC3) and
  degrades to no digest on any persistence failure (graceful empty state).
"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from coach_web.agent import DEFAULT_SYSTEM_PROMPT, AgentEvent, apply_temporal_anchor
from coach_web.app import _athlete_id_for_session, create_app
from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import create_session_token
from coach_web.config import get_settings
from coach_web.db import build_db_engine, build_session_factory
from coach_web.debrief import (
    DEFAULT_ATHLETE_ID,
    DebriefMirrorOutcome,
    athlete_id_from_email,
)
from coach_web.errors import MIRROR_REASON_MESSAGES, MirrorReason
from coach_web.microcycle import (
    DaySlot,
    PlanDraftRow,
    WeeklyPlanDraft,
    build_week_markdown,
    week_bounds,
)
from tests.conftest import AUTH_TEST_SESSION_SECRET

GENERIC_422_DETAIL = "Invalid request payload"

ENCRYPTION_503_BODY = {
    "code": "key_unavailable",
    "message": (
        "Encrypted storage is unavailable — the debrief was not saved. "
        "Check the DATA_ENCRYPTION_KEY configuration."
    ),
}

OWNER_EMAIL = "frederic.pitteloud@gmail.com"
OTHER_EMAIL = "other-athlete@example.com"

TEST_ENCRYPTION_KEY = Fernet.generate_key()
"""Per-module Fernet key — generated, never a committed literal (gitleaks)."""


@pytest.fixture
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a valid DATA_ENCRYPTION_KEY for the happy-path save tests."""
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY.decode("ascii"))


def _day(**overrides: object) -> DaySlot:
    """Return a valid day slot with optional overrides."""
    payload: dict[str, object] = {
        "day_of_week": "mon",
        "date": "2026-10-05",
        "session_title": "Easy spin",
        "focus": None,
        "zones": [],
        "planned_tss": 45,
        "duration_minutes": 60,
        "rest_day": False,
    }
    payload.update(overrides)
    return DaySlot.model_validate(payload)


def _week_draft(**overrides: object) -> WeeklyPlanDraft:
    """Return a valid weekly draft whose Monday is 2026-10-05 (ISO 2026-W41)."""
    payload: dict[str, object] = {
        "week_id": "2026-W41",
        "title": "Base week",
        "summary": "Endurance-focused base week.",
        "rationale": None,
        "days": [
            _day(day_of_week="mon", date="2026-10-05", session_title="Easy spin"),
            _day(
                day_of_week="tue",
                date="2026-10-06",
                session_title="Threshold 3x12",
                planned_tss=95,
                duration_minutes=80,
            ),
        ],
        "total_tss": 140,
        "hard_days": 1,
        "easy_days": 1,
    }
    payload.update(overrides)
    return WeeklyPlanDraft.model_validate(payload)


def _seed_approved_draft(db_path: Path, draft: WeeklyPlanDraft) -> None:
    """Persist one approved plan_drafts row through a dedicated engine."""

    async def _seed() -> None:
        engine = build_db_engine(str(db_path))
        factory = build_session_factory(engine)
        try:
            async with factory() as session:
                now = datetime.now(UTC).isoformat()
                week_start, week_end = week_bounds(draft.week_id)
                session.add(
                    PlanDraftRow(
                        week_start_date=week_start,
                        week_end_date=week_end,
                        content=build_week_markdown(draft),
                        status="approved",
                        created_at=now,
                        updated_at=now,
                    )
                )
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_seed())


def _debrief_count(db_path: Path) -> int:
    """Count the debriefs rows straight from the database file."""
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM debriefs").fetchone()[0])


def _debrief_rows(db_path: Path) -> list[tuple[int, str, int]]:
    """Return (athlete_id, plan_session_key, rpe) for every debrief row."""
    with sqlite3.connect(db_path) as conn:
        return [
            (int(row[0]), str(row[1]), int(row[2]))
            for row in conn.execute(
                "SELECT athlete_id, plan_session_key, rpe FROM debriefs ORDER BY id"
            ).fetchall()
        ]


class _SystemPromptProbeAgent:
    """Stub agent echoing its system prompt back as token + done events."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.system_prompt = DEFAULT_SYSTEM_PROMPT

    async def __aenter__(self) -> "_SystemPromptProbeAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self, message: str, *, history: list[Any] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Yield the system prompt so tests can assert on the injected digest."""
        yield AgentEvent(type="token", data={"text": self.system_prompt})
        yield AgentEvent(type="done", data={"message": self.system_prompt, "iterations": 1})


def _probe_client() -> TestClient:
    """Build a TestClient whose agent factory returns a fresh probe per request."""
    app = create_app()
    app.state.agent_factory = MagicMock(side_effect=lambda _settings: _SystemPromptProbeAgent())
    return TestClient(app)


def _extract_token_texts(body: str) -> str:
    """Extract token-event texts from an SSE body (single implementation)."""
    parts: list[str] = []
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[len("data:") :].strip())
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("type") == "token":
                text = payload.get("data", {}).get("text")
                if isinstance(text, str):
                    parts.append(text)
    return "".join(parts)


def _extract_token_texts_tolerant(body: str) -> str:
    """_extract_token_texts over a body polluted with a malformed data line.

    The helper must skip junk frames (keep-alive comments, partial writes)
    without crashing — exercised here so the defensive branch stays covered.
    """
    return _extract_token_texts(body + "\ndata:not-json{{{\n")


class TestPostDebrief:
    """POST /api/debrief — the encrypted capture path (#189 AC1/AC2)."""

    def test_save_answers_201_with_metadata_only(self, migrated_db: Path, encryption_key) -> None:
        """The 201 body carries status/key/rpe — never the notes (nLPD)."""
        client = TestClient(create_app())

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "Felt strong."},
        )

        assert response.status_code == 201
        body = response.json()
        assert body == {"status": "saved", "plan_session_key": "2026-10-06", "rpe": 7}
        assert "Felt strong." not in response.text

    def test_save_persists_ciphertext_never_plaintext(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """The notes land as Fernet ciphertext — no plaintext bytes in the DB file."""
        client = TestClient(create_app())

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "SECRET-NOTE-XYZ"},
        )

        assert response.status_code == 201
        assert _debrief_count(migrated_db) == 1
        for candidate in (migrated_db, Path(str(migrated_db) + "-wal")):
            if candidate.exists():
                assert b"SECRET-NOTE-XYZ" not in candidate.read_bytes()
        with sqlite3.connect(migrated_db) as conn:
            stored = conn.execute("SELECT notes_encrypted FROM debriefs").fetchone()[0]
        assert str(stored).startswith("gAAAA")

    def test_save_without_notes_persists_null(self, migrated_db: Path, encryption_key) -> None:
        """An optional-notes debrief persists a NULL notes column."""
        client = TestClient(create_app())

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 5})

        assert response.status_code == 201
        with sqlite3.connect(migrated_db) as conn:
            stored = conn.execute("SELECT notes_encrypted FROM debriefs").fetchone()[0]
        assert stored is None

    def test_resave_upserts_the_existing_row(self, migrated_db: Path, encryption_key) -> None:
        """A re-submission updates the row — never a duplicate."""
        client = TestClient(create_app())
        client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7})

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 9})

        assert response.status_code == 201
        assert _debrief_rows(migrated_db) == [(1, "2026-10-06", 9)]

    def test_missing_key_answers_503_and_persists_nothing(
        self, migrated_db: Path, monkeypatch
    ) -> None:
        """AC2: missing DATA_ENCRYPTION_KEY → fixed 503 body, nothing persisted."""
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        client = TestClient(create_app())

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "Never stored."},
        )

        assert response.status_code == 503
        assert response.json() == ENCRYPTION_503_BODY
        assert _debrief_count(migrated_db) == 0
        assert "Never stored." not in response.text

    def test_invalid_key_answers_503_and_persists_nothing(
        self, migrated_db: Path, monkeypatch
    ) -> None:
        """AC2: a non-Fernet key fails closed — same fixed body, nothing persisted."""
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "not-a-valid-fernet-key")
        client = TestClient(create_app())

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "Never stored."},
        )

        assert response.status_code == 503
        assert response.json() == ENCRYPTION_503_BODY
        assert _debrief_count(migrated_db) == 0

    def test_503_body_never_carries_key_material(self, monkeypatch) -> None:
        """The 503 body is the fixed vocabulary — no key value can leak."""
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "not-a-valid-fernet-key")
        client = TestClient(create_app())

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7})

        assert response.status_code == 503
        assert "not-a-valid-fernet-key" not in response.text

    def test_malformed_payload_keeps_the_generic_422(self) -> None:
        """The debrief path is not a settings path — 422 stays generic (#142)."""
        client = TestClient(create_app())

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 11})

        assert response.status_code == 422
        assert response.json() == {"detail": GENERIC_422_DETAIL}

    def test_malformed_session_key_keeps_the_generic_422(self) -> None:
        """A non-date plan_session_key is a generic 422 (no reason body)."""
        client = TestClient(create_app())

        response = client.post("/api/debrief", json={"plan_session_key": "junk-key", "rpe": 7})

        assert response.status_code == 422
        assert response.json() == {"detail": GENERIC_422_DETAIL}


class TestDebriefIcuMirror:
    """POST /api/debrief — the opt-in ICU session-comment mirror (#190 AC3).

    The mirror is strictly opt-in (``mirror_to_icu`` consent flag), runs
    AFTER the durable save (the debrief row is never touched by a mirror
    failure) and degrades to the fixed-vocabulary soft warning on every
    failure path — the 201 stands either way. The hook is injectable via
    ``app.state.debrief_mirror`` (mirrors the ``agent_factory`` pattern);
    tests fake it — ZERO network.
    """

    @staticmethod
    def _recording_hook(
        outcome: DebriefMirrorOutcome,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Build an async mirror hook recording its calls, plus the call log."""
        calls: list[dict[str, Any]] = []

        async def hook(
            settings: Any,
            *,
            plan_session_key: str,
            rpe: int,
            notes: str | None,
        ) -> DebriefMirrorOutcome:
            calls.append(
                {
                    "settings": settings,
                    "plan_session_key": plan_session_key,
                    "rpe": rpe,
                    "notes": notes,
                }
            )
            return outcome

        return hook, calls

    def test_mirror_consent_returns_the_mirrored_outcome(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """AC3: consent rides the save; the 201 carries the mirror outcome."""
        outcome = DebriefMirrorOutcome(
            status="mirrored", code=None, message="Debrief mirrored to Intervals.icu."
        )
        hook, calls = self._recording_hook(outcome)
        app = create_app()
        app.state.debrief_mirror = hook
        client = TestClient(app)

        response = client.post(
            "/api/debrief",
            json={
                "plan_session_key": "2026-10-06",
                "rpe": 7,
                "notes": "Felt strong.",
                "mirror_to_icu": True,
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "saved"
        assert body["mirror"] == {
            "status": "mirrored",
            "message": "Debrief mirrored to Intervals.icu.",
        }
        assert len(calls) == 1
        assert calls[0]["plan_session_key"] == "2026-10-06"
        assert calls[0]["rpe"] == 7
        assert calls[0]["notes"] == "Felt strong."

    def test_mirror_failure_is_a_soft_warning_and_the_row_is_intact(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """AC3: a failed mirror keeps the 201 and the fixed soft warning."""
        outcome = DebriefMirrorOutcome(
            status="failed",
            code=MirrorReason.SESSION_NOT_FOUND,
            message=MIRROR_REASON_MESSAGES[MirrorReason.SESSION_NOT_FOUND],
        )
        hook, _calls = self._recording_hook(outcome)
        app = create_app()
        app.state.debrief_mirror = hook
        client = TestClient(app)

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "mirror_to_icu": True},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "saved"
        assert body["mirror"] == {
            "status": "failed",
            "code": "session_not_found",
            "message": MIRROR_REASON_MESSAGES[MirrorReason.SESSION_NOT_FOUND],
        }
        assert _debrief_count(migrated_db) == 1

    def test_mirror_hook_exception_degrades_to_the_fixed_soft_warning(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """AC3: even a raising hook can never break the saved debrief."""

        async def _raising_hook(
            settings: Any,
            *,
            plan_session_key: str,
            rpe: int,
            notes: str | None,
        ) -> DebriefMirrorOutcome:
            raise RuntimeError("hub exploded")

        app = create_app()
        app.state.debrief_mirror = _raising_hook
        client = TestClient(app)

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "mirror_to_icu": True},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "saved"
        assert body["mirror"] == {
            "status": "failed",
            "code": "post_failed",
            "message": MIRROR_REASON_MESSAGES[MirrorReason.POST_FAILED],
        }
        assert _debrief_count(migrated_db) == 1

    def test_no_consent_skips_the_hook_and_keeps_the_body_shape(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """The plain save never touches ICU — body shape is byte-identical."""

        async def _forbidden_hook(
            settings: Any,
            *,
            plan_session_key: str,
            rpe: int,
            notes: str | None,
        ) -> DebriefMirrorOutcome:
            raise AssertionError("the mirror hook must not run without consent")

        app = create_app()
        app.state.debrief_mirror = _forbidden_hook
        client = TestClient(app)

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7})

        assert response.status_code == 201
        assert response.json() == {
            "status": "saved",
            "plan_session_key": "2026-10-06",
            "rpe": 7,
        }
        assert "mirror" not in response.json()

    def test_default_hook_routes_through_the_mirror_service(
        self, migrated_db: Path, encryption_key, monkeypatch
    ) -> None:
        """Without an injected hook the save mirrors through the real service."""
        import coach_web.app as app_module

        captured: dict[str, Any] = {}

        async def _fake_mirror(
            hub_factory: Any,
            settings: Any,
            *,
            plan_session_key: str,
            rpe: int,
            notes: str | None,
        ) -> tuple[DebriefMirrorOutcome, str | None]:
            captured["hub_factory"] = hub_factory
            captured["plan_session_key"] = plan_session_key
            captured["rpe"] = rpe
            captured["notes"] = notes
            return (
                DebriefMirrorOutcome(
                    status="mirrored",
                    code=None,
                    message="Debrief mirrored to Intervals.icu.",
                ),
                "i42",
            )

        monkeypatch.setattr(app_module, "mirror_debrief_to_icu", _fake_mirror)
        client = TestClient(create_app())

        response = client.post(
            "/api/debrief",
            json={
                "plan_session_key": "2026-10-06",
                "rpe": 7,
                "notes": "Felt strong.",
                "mirror_to_icu": True,
            },
        )

        assert response.status_code == 201
        assert response.json()["mirror"]["status"] == "mirrored"
        from coach_web.mcp_hub import MCPClientHub

        # Classmethod attribute access binds a new method object per read —
        # bound-method equality compares the underlying function and target.
        assert captured["hub_factory"] == MCPClientHub.from_settings
        assert captured["plan_session_key"] == "2026-10-06"
        assert captured["rpe"] == 7
        assert captured["notes"] == "Felt strong."


class TestGetPendingDebriefs:
    """GET /api/debrief/pending — the banner derivation (#189 AC3)."""

    def test_empty_store_answers_an_empty_list(self) -> None:
        client = TestClient(create_app())

        response = client.get("/api/debrief/pending")

        assert response.status_code == 200
        assert response.json() == {"pending": []}

    def test_completed_undebriefed_sessions_are_listed(self, migrated_db: Path) -> None:
        """Approved-draft sessions strictly before today are pending, oldest first."""
        _seed_approved_draft(migrated_db, _week_draft())
        client = TestClient(create_app())

        response = client.get("/api/debrief/pending")

        assert response.status_code == 200
        # "Today" is the real clock: both seeded dates (2026-10-05/06) are
        # strictly in the past relative to any run date after 2026-10-06.
        pending = response.json()["pending"]
        assert [item["plan_session_key"] for item in pending] == [
            "2026-10-05",
            "2026-10-06",
        ]
        assert pending[0]["session_title"] == "Easy spin"

    def test_pending_payload_is_metadata_only(self, migrated_db: Path, encryption_key) -> None:
        """The banner payload carries key + title only — never notes or RPE."""
        _seed_approved_draft(migrated_db, _week_draft())
        client = TestClient(create_app())
        # Debrief the Monday session with private notes: the Tuesday session
        # stays pending, and the notes must not leak into the pending payload.
        client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-05", "rpe": 7, "notes": "Private note."},
        )

        response = client.get("/api/debrief/pending")

        assert response.status_code == 200
        pending = response.json()["pending"]
        assert [item["plan_session_key"] for item in pending] == ["2026-10-06"]
        for item in pending:
            assert set(item) == {"plan_session_key", "session_title"}
        assert "Private note." not in response.text

    def test_debriefed_sessions_leave_the_pending_list(
        self, migrated_db: Path, encryption_key
    ) -> None:
        """Saving a debrief clears that session's pending state (same athlete)."""
        _seed_approved_draft(migrated_db, _week_draft())
        client = TestClient(create_app())
        client.post("/api/debrief", json={"plan_session_key": "2026-10-05", "rpe": 6})

        response = client.get("/api/debrief/pending")

        assert [item["plan_session_key"] for item in response.json()["pending"]] == ["2026-10-06"]


class TestDebriefAuth:
    """Auth middleware coverage & per-athlete isolation (#189 AC4)."""

    def _auth_client(self, monkeypatch, emails: list[str]) -> TestClient:
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_ID", "test-client-id")
        monkeypatch.setenv("GOOGLE_OIDC_CLIENT_SECRET", "test-client-secret")
        monkeypatch.setenv("AUTH_SESSION_SECRET", AUTH_TEST_SESSION_SECRET)
        monkeypatch.setenv("AUTH_WHITELIST_EMAILS", json.dumps(emails))
        get_settings.cache_clear()
        return TestClient(create_app(), base_url="https://testserver")

    def _session_cookie(self, email: str) -> str:
        return create_session_token(email, AUTH_TEST_SESSION_SECRET, ttl_seconds=3600)

    def test_unauthenticated_post_answers_401(self, monkeypatch) -> None:
        """AC4: the debrief save is behind the whitelist middleware."""
        client = self._auth_client(monkeypatch, [OWNER_EMAIL])

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7})

        assert response.status_code == 401

    def test_unauthenticated_pending_answers_401(self, monkeypatch) -> None:
        """AC4: the pending derivation is behind the whitelist middleware."""
        client = self._auth_client(monkeypatch, [OWNER_EMAIL])

        response = client.get("/api/debrief/pending")

        assert response.status_code == 401

    def test_athlete_b_cannot_read_athlete_a_debrief_state(
        self, migrated_db: Path, monkeypatch, encryption_key
    ) -> None:
        """AC4: B's pending view is computed from B's rows — A's debrief invisible."""
        client = self._auth_client(monkeypatch, [OWNER_EMAIL, OTHER_EMAIL])
        client.cookies.set(SESSION_COOKIE, self._session_cookie(OWNER_EMAIL))
        saved = client.post(
            "/api/debrief",
            json={"plan_session_key": "2099-01-01", "rpe": 7, "notes": "A private."},
        )
        assert saved.status_code == 201

        client.cookies.set(SESSION_COOKIE, self._session_cookie(OTHER_EMAIL))
        pending = client.get("/api/debrief/pending")

        assert pending.status_code == 200
        # Athlete B has no debrief rows: a future-dated session can never be
        # pending for anyone, and A's row must not leak into B's view either.
        assert pending.json() == {"pending": []}
        assert "A private." not in pending.text

    def test_athlete_b_save_creates_an_isolated_row(
        self, monkeypatch, migrated_db: Path, encryption_key
    ) -> None:
        """AC4: B's save for the same session key never touches A's row."""
        client = self._auth_client(monkeypatch, [OWNER_EMAIL, OTHER_EMAIL])
        client.cookies.set(SESSION_COOKIE, self._session_cookie(OWNER_EMAIL))
        client.post(
            "/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "A."}
        )

        client.cookies.set(SESSION_COOKIE, self._session_cookie(OTHER_EMAIL))
        saved = client.post(
            "/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 3, "notes": "B."}
        )

        assert saved.status_code == 201
        rows = _debrief_rows(migrated_db)
        assert len(rows) == 2
        assert rows[0][2] == 7  # athlete A's RPE untouched
        assert rows[1][2] == 3
        assert rows[0][0] != rows[1][0]

    def test_non_whitelisted_email_answers_403(self, monkeypatch) -> None:
        """The whitelist stays the access-control boundary on the new routes."""
        client = self._auth_client(monkeypatch, [OWNER_EMAIL])
        client.cookies.set(SESSION_COOKIE, self._session_cookie("intruder@example.com"))

        response = client.post("/api/debrief", json={"plan_session_key": "2026-10-06", "rpe": 7})

        assert response.status_code == 403


class TestPendingDebriefDigestHook:
    """The agent-stream greeting hook (MADR-0010, #189 AC3)."""

    def test_pending_sessions_inject_the_digest_into_the_prompt(self, migrated_db: Path) -> None:
        """The coach system prompt gains the pending list when sessions await."""
        _seed_approved_draft(migrated_db, _week_draft())
        client = _probe_client()

        response = client.post("/api/agent/stream", json={"message": "Hello coach", "history": []})

        assert response.status_code == 200
        prompt = _extract_token_texts(response.text)
        assert "Pending session debriefs" in prompt
        assert "2026-10-05: Easy spin" in prompt
        assert "2026-10-06: Threshold 3x12" in prompt

    def test_no_pending_sessions_leave_the_prompt_unchanged(self) -> None:
        """Without pending sessions the prompt stays at the anchored base (no hook).

        #220: the temporal anchor is part of the base prompt applied
        unconditionally, so the no-digest baseline is the anchored prompt.
        """
        client = _probe_client()

        response = client.post("/api/agent/stream", json={"message": "Hello coach", "history": []})

        assert response.status_code == 200
        # The tolerant extractor also exercises the malformed-frame branch.
        assert _extract_token_texts_tolerant(response.text) == apply_temporal_anchor(
            DEFAULT_SYSTEM_PROMPT, date.today()
        )

    def test_digest_hook_degrades_gracefully_on_persistence_failure(
        self, db_path: Path, monkeypatch
    ) -> None:
        """A broken persistence layer yields no digest — the chat never breaks.

        #220 AC2: the degradation keeps the temporal anchor — the date must
        survive the persistence failure even though the digest does not.
        """
        app = create_app()
        app.state.agent_factory = MagicMock(side_effect=lambda _settings: _SystemPromptProbeAgent())
        client = TestClient(app)

        def _broken_session_factory(*args: Any, **kwargs: Any) -> Any:
            raise OSError("db unavailable")

        monkeypatch.setattr(app.state, "db_session_factory", _broken_session_factory)

        response = client.post("/api/agent/stream", json={"message": "Hello coach", "history": []})

        assert response.status_code == 200
        assert _extract_token_texts(response.text) == apply_temporal_anchor(
            DEFAULT_SYSTEM_PROMPT, date.today()
        )


class TestAthleteIdResolutionBoundary:
    """The request-context → athlete-id helper (#189 AC4)."""

    def test_auth_disabled_maps_to_the_default_athlete(self) -> None:
        """Dev/qa lanes (AUTH_ENABLED=false) use the single local athlete."""
        settings = get_settings()

        assert _athlete_id_for_session(settings, None) == DEFAULT_ATHLETE_ID
        assert _athlete_id_for_session(settings, "any-cookie") == DEFAULT_ATHLETE_ID

    def test_auth_enabled_without_cookie_fails_safe_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing cookie degrades to the default (middleware rejects first)."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_SESSION_SECRET", AUTH_TEST_SESSION_SECRET)
        get_settings.cache_clear()

        assert _athlete_id_for_session(get_settings(), None) == DEFAULT_ATHLETE_ID

    def test_auth_enabled_with_unverifiable_cookie_fails_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A garbage cookie degrades to the default instead of raising."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_SESSION_SECRET", AUTH_TEST_SESSION_SECRET)
        get_settings.cache_clear()

        assert _athlete_id_for_session(get_settings(), "garbage-token") == DEFAULT_ATHLETE_ID

    def test_auth_enabled_with_valid_session_derives_the_athlete(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A verified session maps to the email-derived athlete id."""
        monkeypatch.setenv("AUTH_ENABLED", "true")
        monkeypatch.setenv("AUTH_SESSION_SECRET", AUTH_TEST_SESSION_SECRET)
        get_settings.cache_clear()
        cookie = create_session_token(OWNER_EMAIL, AUTH_TEST_SESSION_SECRET, ttl_seconds=3600)

        athlete_id = _athlete_id_for_session(get_settings(), cookie)

        assert athlete_id == athlete_id_from_email(OWNER_EMAIL)
        assert athlete_id != DEFAULT_ATHLETE_ID


class TestDebriefPersistenceFailure:
    """The 503 persistence-outage posture on the debrief routes."""

    def test_save_answers_the_generic_503_on_database_failure(
        self, migrated_db: Path, monkeypatch: pytest.MonkeyPatch, encryption_key
    ) -> None:
        """A broken store answers the static persistence detail — no internals."""
        app = create_app()

        def _broken_session_factory(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("db down")

        monkeypatch.setattr(app.state, "db_session_factory", _broken_session_factory)
        client = TestClient(app)

        response = client.post(
            "/api/debrief",
            json={"plan_session_key": "2026-10-06", "rpe": 7, "notes": "Private."},
        )

        assert response.status_code == 503
        assert response.json() == {"detail": "Service temporarily unavailable"}
        assert "db down" not in response.text
        assert "Private." not in response.text

    def test_pending_degrades_to_an_empty_list_on_database_failure(
        self, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The passive banner derivation degrades to silence (MADR-0010)."""
        app = create_app()

        def _broken_session_factory(*args: Any, **kwargs: Any) -> Any:
            raise SQLAlchemyError("db down")

        monkeypatch.setattr(app.state, "db_session_factory", _broken_session_factory)
        client = TestClient(app)

        response = client.get("/api/debrief/pending")

        assert response.status_code == 200
        assert response.json() == {"pending": []}
