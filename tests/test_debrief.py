"""Debrief service contract — C2-encrypted capture & pending derivation (#189).

Story 3.1 completes the capture half of the debrief loop (epic #161): the
athlete rates a completed planned session (RPE 1–10) with optional free-text
notes, and the debrief persists in the ``debriefs`` table with the notes as
Fernet ciphertext — NEVER plaintext at rest (MADR-008 C2, Swiss nLPD Art.
5(c) health data).

The contract pinned here:

- the cipher is built from ``DATA_ENCRYPTION_KEY`` (SecretStr) and a
  missing/invalid key raises :class:`EncryptionUnavailableError` — the
  boundary maps it to the fixed 503 vocabulary; there is no plaintext
  fallback code path;
- saves are per-athlete (``athlete_id`` scoping on every query — athlete B
  cannot read or write athlete A's debriefs) and idempotent per session key
  (a re-submission upserts, never duplicates);
- the pending derivation is a pure function of (approved plan drafts,
  today, the athlete's debrief rows): completed (strictly past) non-rest
  day slots of approved drafts without a debrief row for THIS athlete;
- the coach digest renders the pending list as prompt lines (metadata only
  — title and date, never notes or RPE).
"""

import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from coach_web.config import get_settings
from coach_web.debrief import (
    DEFAULT_ATHLETE_ID,
    MAX_DEBRIEF_NOTES_LENGTH,
    DebriefSaveRequest,
    PendingDebrief,
    RpeRequest,
    apply_pending_debrief_digest,
    athlete_id_from_email,
    build_debrief_cipher,
    list_pending_debriefs,
    parse_plan_day_rows,
    pending_debrief_digest,
    save_debrief,
)
from coach_web.errors import EncryptionReason, EncryptionUnavailableError
from coach_web.microcycle import (
    DaySlot,
    PlanDraftRow,
    WeeklyPlanDraft,
    build_week_markdown,
    week_bounds,
)

TEST_ENCRYPTION_KEY = Fernet.generate_key()
"""Per-module Fernet key — generated, never a committed literal (gitleaks)."""

TEST_CIPHER = Fernet(TEST_ENCRYPTION_KEY)


@pytest.fixture
def cipher_settings(monkeypatch: pytest.MonkeyPatch):
    """Settings carrying the module's generated test key (env-injected shape)."""
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY.decode("ascii"))
    get_settings.cache_clear()
    return get_settings()


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
            _day(
                day_of_week="wed",
                date="2026-10-07",
                session_title="Rest day",
                planned_tss=0,
                duration_minutes=0,
                rest_day=True,
            ),
        ],
        "total_tss": 140,
        "hard_days": 1,
        "easy_days": 1,
    }
    payload.update(overrides)
    return WeeklyPlanDraft.model_validate(payload)


async def _seed_approved_draft(session, draft: WeeklyPlanDraft) -> None:
    """Persist one approved plan_drafts row (the pending derivation's source)."""
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


def _save_payload(**overrides: object) -> DebriefSaveRequest:
    """Return a valid debrief save payload with optional overrides."""
    base: dict[str, object] = {
        "plan_session_key": "2026-10-06",
        "rpe": 7,
        "notes": "Legs felt heavy on the third interval.",
    }
    base.update(overrides)
    return DebriefSaveRequest.model_validate(base)


class TestAthleteIdResolution:
    """The request → athlete-id mapping (#189 AC4)."""

    def test_missing_email_maps_to_the_default_athlete(self) -> None:
        """Auth-disabled lanes (no session email) use the single local athlete."""
        assert athlete_id_from_email(None) == DEFAULT_ATHLETE_ID
        assert athlete_id_from_email("") == DEFAULT_ATHLETE_ID

    def test_email_maps_to_a_stable_positive_id(self) -> None:
        """The derivation is deterministic and positive for any email."""
        first = athlete_id_from_email("frederic.pitteloud@gmail.com")
        second = athlete_id_from_email("frederic.pitteloud@gmail.com")

        assert first == second
        assert first > 0

    def test_case_and_whitespace_insensitive(self) -> None:
        """The canonical email form (whitelist normalization) feeds the digest."""
        assert athlete_id_from_email("  Athlete@Example.COM ") == athlete_id_from_email(
            "athlete@example.com"
        )

    def test_different_emails_map_to_different_athletes(self) -> None:
        """Athlete B's email never collides with athlete A's id (isolation basis)."""
        assert athlete_id_from_email("owner@example.com") != athlete_id_from_email(
            "other@example.com"
        )

    def test_derived_id_never_equals_the_default(self) -> None:
        """A real email never maps onto the auth-disabled default athlete."""
        for email in ("owner@example.com", "other@example.com", "frederic.pitteloud@gmail.com"):
            assert athlete_id_from_email(email) != DEFAULT_ATHLETE_ID


class TestBuildDebriefCipher:
    """The DATA_ENCRYPTION_KEY → Fernet boundary (#189 AC2)."""

    def test_valid_key_builds_a_working_cipher(self, cipher_settings) -> None:
        """A valid key yields a Fernet that round-trips."""
        cipher = build_debrief_cipher(cipher_settings)

        token = cipher.encrypt(b"round-trip")
        assert cipher.decrypt(token) == b"round-trip"

    def test_missing_key_raises_the_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty key is a service condition — the typed error carries its reason."""
        monkeypatch.delenv("DATA_ENCRYPTION_KEY", raising=False)
        get_settings.cache_clear()

        with pytest.raises(EncryptionUnavailableError) as excinfo:
            build_debrief_cipher(get_settings())

        assert excinfo.value.reason is EncryptionReason.KEY_UNAVAILABLE

    def test_invalid_key_raises_the_typed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-Fernet key (wrong length/encoding) fails closed — never a fallback."""
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "not-a-valid-fernet-key")
        get_settings.cache_clear()

        with pytest.raises(EncryptionUnavailableError) as excinfo:
            build_debrief_cipher(get_settings())

        assert excinfo.value.reason is EncryptionReason.KEY_UNAVAILABLE

    def test_error_text_never_carries_the_key_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The technical message names the env var, never its value (nLPD)."""
        secret_value = "not-a-valid-fernet-key"  # noqa: S105 - invalid-key fixture value
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", secret_value)
        get_settings.cache_clear()

        with pytest.raises(EncryptionUnavailableError) as excinfo:
            build_debrief_cipher(get_settings())

        assert secret_value not in str(excinfo.value)


class TestDebriefSaveRequest:
    """Boundary validation of the POST /api/debrief payload (#189 AC1)."""

    def test_valid_payload_passes(self) -> None:
        request = _save_payload()

        assert request.plan_session_key == "2026-10-06"
        assert request.rpe == 7
        assert request.notes == "Legs felt heavy on the third interval."

    def test_rpe_bounds_are_enforced(self) -> None:
        """RPE is an integer 1–10 — 0 and 11 are rejected."""
        with pytest.raises(ValidationError):
            _save_payload(rpe=0)
        with pytest.raises(ValidationError):
            _save_payload(rpe=11)

    def test_rpe_must_be_an_integer(self) -> None:
        """A float or string RPE is rejected (strict type)."""
        with pytest.raises(ValidationError):
            _save_payload(rpe="7")
        with pytest.raises(ValidationError):
            _save_payload(rpe=7.5)

    def test_plan_session_key_must_be_a_real_iso_date(self) -> None:
        """The key is validated as a calendar date (shared rule)."""
        with pytest.raises(ValidationError):
            _save_payload(plan_session_key="not-a-date")
        with pytest.raises(ValidationError):
            _save_payload(plan_session_key="2026-13-40")

    def test_notes_are_capped(self) -> None:
        """Notes are length-capped (prompt-injection surface bound)."""
        with pytest.raises(ValidationError):
            _save_payload(notes="x" * (MAX_DEBRIEF_NOTES_LENGTH + 1))

    def test_notes_are_optional(self) -> None:
        """A debrief without notes is valid (notes=None)."""
        request = _save_payload(notes=None)

        assert request.notes is None

    def test_mirror_to_icu_defaults_to_false(self) -> None:
        """The mirror is strictly opt-in — the default save never touches ICU."""
        request = _save_payload()

        assert request.mirror_to_icu is False

    def test_mirror_to_icu_accepts_true(self) -> None:
        """The athlete's explicit mirror consent rides the validated payload."""
        request = _save_payload(mirror_to_icu=True)

        assert request.mirror_to_icu is True


class TestSaveDebrief:
    """The encrypted persistence path (#189 AC1/AC2/AC4)."""

    async def test_save_persists_an_encrypted_row(self, db_session_factory, db_path: Path) -> None:
        """The debrief row lands with Fernet ciphertext, never plaintext notes."""
        payload = _save_payload(notes="SECRET-NOTE-CONTENT-XYZ")

        async with db_session_factory() as session:
            row = await save_debrief(session, athlete_id=1, payload=payload, cipher=TEST_CIPHER)

        assert row.id is not None
        assert row.athlete_id == 1
        assert row.plan_session_key == "2026-10-06"
        assert row.rpe == 7
        assert row.notes_encrypted is not None
        assert row.notes_encrypted.startswith("gAAAA")
        # AC1: NO plaintext notes bytes exist anywhere in the database files
        # (main file and WAL side file alike — the plaintext never enters SQL).
        for candidate in (db_path, Path(str(db_path) + "-wal")):
            if candidate.exists():
                assert b"SECRET-NOTE-CONTENT-XYZ" not in candidate.read_bytes()

    async def test_saved_notes_decrypt_round_trip(self, db_session_factory, db_path: Path) -> None:
        """The stored ciphertext decrypts to the original notes with the same key."""
        payload = _save_payload(notes="Round-trip note content")

        async with db_session_factory() as session:
            await save_debrief(session, athlete_id=1, payload=payload, cipher=TEST_CIPHER)

        with sqlite3.connect(db_path) as conn:
            stored = conn.execute("SELECT notes_encrypted FROM debriefs").fetchone()[0]
        assert TEST_CIPHER.decrypt(stored.encode("ascii")).decode("utf-8") == (
            "Round-trip note content"
        )

    async def test_save_without_notes_stores_null(self, db_session_factory) -> None:
        """An optional-notes debrief persists a NULL notes column."""
        payload = _save_payload(notes=None)

        async with db_session_factory() as session:
            row = await save_debrief(session, athlete_id=1, payload=payload, cipher=TEST_CIPHER)

        assert row.notes_encrypted is None

    async def test_resave_upserts_instead_of_duplicating(self, db_session_factory) -> None:
        """A second submission for the same athlete+session updates the row."""
        first = _save_payload(rpe=7, notes="First impression")
        second = _save_payload(rpe=8, notes="Re-rated after reflection")

        async with db_session_factory() as session:
            await save_debrief(session, athlete_id=1, payload=first, cipher=TEST_CIPHER)
            row = await save_debrief(session, athlete_id=1, payload=second, cipher=TEST_CIPHER)

        assert row.rpe == 8
        assert row.notes_encrypted is not None
        assert TEST_CIPHER.decrypt(row.notes_encrypted.encode("ascii")).decode("utf-8") == (
            "Re-rated after reflection"
        )

    async def test_same_session_key_for_another_athlete_is_a_separate_row(
        self, db_session_factory
    ) -> None:
        """Athlete B's debrief for the same session never touches athlete A's row."""
        payload = _save_payload(rpe=7, notes="Athlete A private note")

        async with db_session_factory() as session:
            row_a = await save_debrief(session, athlete_id=1, payload=payload, cipher=TEST_CIPHER)
            row_b = await save_debrief(
                session,
                athlete_id=athlete_id_from_email("other@example.com"),
                payload=_save_payload(rpe=3, notes="Athlete B private note"),
                cipher=TEST_CIPHER,
            )

        assert row_a.id != row_b.id
        assert row_b.athlete_id == athlete_id_from_email("other@example.com")
        # Athlete A's row is untouched by B's save.
        assert row_a.notes_encrypted is not None
        assert TEST_CIPHER.decrypt(row_a.notes_encrypted.encode("ascii")).decode("utf-8") == (
            "Athlete A private note"
        )


class TestParsePlanDayRows:
    """The approved-draft markdown day-table parser (#189 AC3)."""

    def test_parses_the_canonical_week_markdown(self) -> None:
        """Day rows of build_week_markdown output parse to session candidates."""
        content = build_week_markdown(_week_draft())

        sessions = parse_plan_day_rows(content)

        assert [(s.plan_session_key, s.session_title) for s in sessions] == [
            ("2026-10-05", "Easy spin"),
            ("2026-10-06", "Threshold 3x12"),
        ]

    def test_rest_days_are_skipped(self) -> None:
        """A rest day carries no session to rate — it never becomes pending."""
        content = build_week_markdown(_week_draft())

        keys = [s.plan_session_key for s in parse_plan_day_rows(content)]

        assert "2026-10-07" not in keys  # the Wednesday rest day

    def test_malformed_lines_are_skipped(self) -> None:
        """Junk lines (prose, tables of other shapes) fail safe to no session."""
        content = "# Title\n\nSome prose\n\n| broken | row |\n"

        assert parse_plan_day_rows(content) == []

    def test_impossible_dates_are_skipped(self) -> None:
        """A well-shaped row with an impossible calendar date is dropped."""
        line = "| mon | 2026-13-40 | Ghost session | 60 | 45 | 60m |"

        assert parse_plan_day_rows(line) == []

    def test_titles_with_pipes_are_not_split(self) -> None:
        """The title column is matched minimally — later pipes stay in later cells."""
        line = "| tue | 2026-10-06 | Intervals | Z4–Z5 | 95 | 80m |"

        sessions = parse_plan_day_rows(line)

        assert sessions[0].session_title == "Intervals"


class TestListPendingDebriefs:
    """Pending derivation: completed plan sessions without a debrief row (#189 AC3)."""

    async def _pending(self, db_session_factory, *, athlete_id: int, today: date):
        async with db_session_factory() as session:
            return await list_pending_debriefs(session, athlete_id=athlete_id, today=today)

    async def test_completed_undebriefed_sessions_are_pending(self, db_session_factory) -> None:
        """Approved-draft day slots strictly before today with no debrief are pending."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert [(p.plan_session_key, p.session_title) for p in pending] == [
            ("2026-10-05", "Easy spin"),
            ("2026-10-06", "Threshold 3x12"),
        ]

    async def test_today_and_future_sessions_are_not_pending(self, db_session_factory) -> None:
        """Only strictly-past sessions count as completed (local proxy, no ICU)."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 6)
        )

        assert [p.plan_session_key for p in pending] == ["2026-10-05"]

    async def test_debriefed_sessions_are_excluded_for_that_athlete(
        self, db_session_factory
    ) -> None:
        """A session with a debrief row for THIS athlete is no longer pending."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await save_debrief(
                session,
                athlete_id=DEFAULT_ATHLETE_ID,
                payload=_save_payload(plan_session_key="2026-10-05"),
                cipher=TEST_CIPHER,
            )

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert [p.plan_session_key for p in pending] == ["2026-10-06"]

    async def test_another_athletes_debrief_does_not_clear_the_pending_state(
        self, db_session_factory
    ) -> None:
        """Athlete B's debrief rows never mask athlete A's pending sessions (AC4)."""
        async with db_session_factory() as session:
            await _seed_approved_draft(session, _week_draft())
            await save_debrief(
                session,
                athlete_id=athlete_id_from_email("other@example.com"),
                payload=_save_payload(plan_session_key="2026-10-05"),
                cipher=TEST_CIPHER,
            )

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert [p.plan_session_key for p in pending] == ["2026-10-05", "2026-10-06"]

    async def test_non_approved_drafts_never_become_pending(self, db_session_factory) -> None:
        """draft/submitted/superseded/rejected drafts carry no pending sessions."""
        week_start, week_end = week_bounds("2026-W41")
        async with db_session_factory() as session:
            now = datetime.now(UTC).isoformat()
            for status in ("draft", "submitted", "superseded", "rejected"):
                session.add(
                    PlanDraftRow(
                        week_start_date=week_start,
                        week_end_date=week_end,
                        content=build_week_markdown(_week_draft()),
                        status=status,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await session.commit()

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert pending == []

    async def test_pending_is_ordered_by_session_key(self, db_session_factory) -> None:
        """Multiple pending sessions render in chronological order."""
        draft = _week_draft(
            days=[
                _day(day_of_week="tue", date="2026-10-06", session_title="Later"),
                _day(day_of_week="mon", date="2026-10-05", session_title="Earlier"),
            ],
            total_tss=90,
            hard_days=2,
            easy_days=0,
        )
        async with db_session_factory() as session:
            await _seed_approved_draft(session, draft)

        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert [p.plan_session_key for p in pending] == ["2026-10-05", "2026-10-06"]

    async def test_empty_database_yields_no_pending(self, db_session_factory) -> None:
        """No drafts → no pending (graceful empty state)."""
        pending = await self._pending(
            db_session_factory, athlete_id=DEFAULT_ATHLETE_ID, today=date(2026, 10, 8)
        )

        assert pending == []


class TestPendingDebriefDigest:
    """The coach prompt digest for pending debriefs (MADR-0010 greeting hook)."""

    def test_empty_pending_yields_no_digest(self) -> None:
        """No pending sessions → no digest section (graceful empty state)."""
        assert pending_debrief_digest([]) is None

    def test_digest_lists_metadata_only(self) -> None:
        """The digest carries date + title — never notes or RPE (nLPD)."""
        pending = [
            PendingDebrief(plan_session_key="2026-10-05", session_title="Easy spin"),
            PendingDebrief(plan_session_key="2026-10-06", session_title="Threshold 3x12"),
        ]

        digest = pending_debrief_digest(pending)

        assert digest is not None
        assert "2026-10-05: Easy spin" in digest
        assert "2026-10-06: Threshold 3x12" in digest
        assert "request_rpe" in digest

    def test_apply_appends_the_digest_to_the_prompt(self) -> None:
        """apply_pending_debrief_digest appends only when sessions are pending."""
        base = "BASE PROMPT"
        assert apply_pending_debrief_digest(base, []) == base
        extended = apply_pending_debrief_digest(
            base, [PendingDebrief(plan_session_key="2026-10-05", session_title="Easy spin")]
        )
        assert extended.startswith(base)
        assert "2026-10-05: Easy spin" in extended


class TestRpeRequestModel:
    """The coach's request_rpe tool payload (SSE rpe_request event, #189 AC3)."""

    def test_valid_request_passes(self) -> None:
        request = RpeRequest.model_validate(
            {"plan_session_key": "2026-10-06", "session_title": "Threshold 3x12"}
        )

        assert request.plan_session_key == "2026-10-06"
        assert request.session_title == "Threshold 3x12"

    def test_plan_session_key_must_be_a_real_iso_date(self) -> None:
        with pytest.raises(ValidationError):
            RpeRequest.model_validate({"plan_session_key": "junk", "session_title": "X"})

    def test_session_title_is_required_and_capped(self) -> None:
        with pytest.raises(ValidationError):
            RpeRequest.model_validate({"plan_session_key": "2026-10-06", "session_title": ""})
        with pytest.raises(ValidationError):
            RpeRequest.model_validate(
                {"plan_session_key": "2026-10-06", "session_title": "x" * 201}
            )
