"""Divergence digest — DB-derived plan-vs-reality context (#236, epic #213 S2).

The digest is a read-only, per-request view of the athlete's executed week
versus the approved plan, derived from DB rows ONLY (approved plan drafts C3
+ debrief rows C2 — no new ICU reads, no new persistence, no new endpoint).
The @cyber-security controls (verdict APPROVED-WITH-CONTROLS, 2026-10-17)
are pinned here as contract tests:

- C-S2-1 (AC2): the numeric RPE value and any note text never reach the
  system prompt or any SSE payload — only the fixed-vocabulary severity
  band label is emitted (numeric-sentinel seeded).
- C-S2-2 (AC3): the digest path never selects/decrypts ``notes_encrypted``
  (sentinel-seeded: neither the plaintext note nor the stored ciphertext
  appears downstream).
- C-S2-3 (AC4): severity chips are a closed ``Literal`` enum; fact strings
  are assembled server-side from counts + enum labels.
- C-S2-4 (AC5): the digest rides the existing request-scoped digest
  machinery — one ``today``, one DB session, graceful degradation.
- C-S2-5 (AC6): DB-only — the ICU GET costs are unchanged, the digest
  module imports no MCP/ICU client, and no new endpoint exists.
- C-S2-6: no new ORM model/migration — the digest is computed per-request.
- C-S2-7 (AC7): stored-content text passes through ``_digest_single_line``.
- C-S2-8 (AC8): divergence facts are date-keyed row diffs, never title-keyed.

Sentinel note (C-S2-1): the seeded RPE is **9**, not the verdict's example
``rpe=7`` — ``DEFAULT_SYSTEM_PROMPT`` itself contains the fixed literal
"7-day plan", so the digit 7 can never act as a whole-prompt absence
sentinel. The control pins the MECHANISM (a stored RPE numeric must be
absent from the prompt and every SSE payload); the fixture is built 9-free
so any leak of the stored RPE value introduces a 9 that exists nowhere else.
"""

import ast
from collections.abc import AsyncIterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

import coach_web
import coach_web.app
import coach_web.debrief
from coach_web.agent import AgentEvent
from coach_web.analysis import ICU_TOOL_GET_COSTS
from coach_web.app import create_app
from coach_web.config import Settings, get_settings
from coach_web.db import Base
from coach_web.debrief import (
    DEFAULT_ATHLETE_ID,
    DIVERGENCE_DIGEST_MAX_CHARS,
    DebriefRow,
    DivergenceDigest,
    FlaggedExertionFact,
    PendingDebrief,
    apply_divergence_digest,
    build_divergence_digest,
    divergence_digest_text,
    rpe_severity_band,
)
from coach_web.microcycle import (
    DaySlot,
    WeeklyPlanDraft,
    transition_draft_status,
    upsert_week_draft,
)

TEST_ENCRYPTION_KEY = Fernet.generate_key()
"""Per-module Fernet key — generated, never a committed literal (gitleaks)."""

TEST_CIPHER = Fernet(TEST_ENCRYPTION_KEY)

SENTINEL_NOTE = "sentinel-note-quiet-meadow-do-not-leak"
"""Unique debrief-note sentinel: must never reach the prompt or the SSE stream."""

SENTINEL_RPE = 9
"""The seeded RPE value — absent from every fixture string (see module docstring)."""


@pytest.fixture
def cipher_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings carrying the module's generated key (the stream endpoint's cipher).

    #243: the divergence read decrypts the stored RPE tokens, so the app
    needs a valid ``DATA_ENCRYPTION_KEY`` — injected here per test (the
    shared ``settings`` fixture stays key-agnostic).
    """
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY.decode("ascii"))
    get_settings.cache_clear()
    return get_settings()


def _rpe_token(rpe: int) -> str:
    """Encrypt an RPE exactly the way the save path stores it (#243)."""
    return TEST_CIPHER.encrypt(str(rpe).encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures & test doubles
# ---------------------------------------------------------------------------


def _day(**overrides: object) -> DaySlot:
    """Return a valid day slot with optional overrides."""
    payload: dict[str, object] = {
        "day_of_week": "mon",
        "date": "2026-10-12",
        "session_title": "Easy spin",
        "focus": None,
        "zones": [],
        "planned_tss": 40,
        "duration_minutes": 60,
        "rest_day": False,
    }
    payload.update(overrides)
    return DaySlot.model_validate(payload)


def _week_2026_w42() -> WeeklyPlanDraft:
    """A 9-free approved-week fixture for ISO week 2026-W42 (Mon 2026-10-12).

    Every string in the fixture avoids the digit ``9`` so the seeded RPE
    (``SENTINEL_RPE``) can act as a whole-prompt numeric sentinel (C-S2-1):
    any leak of the stored RPE value introduces a 9 the fixture otherwise
    never contains.
    """
    return WeeklyPlanDraft.model_validate(
        {
            "week_id": "2026-W42",
            "title": "Base week 42",
            "summary": "Endurance-focused base week.",
            "rationale": None,
            "days": [
                _day(),
                _day(
                    day_of_week="tue",
                    date="2026-10-13",
                    session_title="Threshold intervals",
                    planned_tss=60,
                    duration_minutes=80,
                ),
                _day(
                    day_of_week="wed",
                    date="2026-10-14",
                    session_title="Recovery ride",
                    planned_tss=30,
                    duration_minutes=45,
                ),
                _day(
                    day_of_week="thu",
                    date="2026-10-15",
                    session_title="Long endurance",
                    planned_tss=80,
                    duration_minutes=120,
                ),
                _day(
                    day_of_week="fri",
                    date="2026-10-16",
                    session_title="Rest day",
                    planned_tss=0,
                    duration_minutes=0,
                    rest_day=True,
                ),
            ],
            "total_tss": 210,
            "hard_days": 0,
            "easy_days": 4,
        }
    )


def _freeze_today(monkeypatch: pytest.MonkeyPatch, today: date) -> None:
    """Pin the request-scoped clock (the boundary owns ``today``, N3)."""

    class _FrozenDate(date):
        @classmethod
        def today(cls) -> "_FrozenDate":
            return _FrozenDate(today.year, today.month, today.day)

    monkeypatch.setattr("coach_web.app.date", _FrozenDate)


class _DigestProbeAgent:
    """Agent stub capturing the assembled system prompt at run time."""

    # The stream endpoint stamps the anchor/digests onto this attribute.
    system_prompt: str = ""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.captured_prompt: str | None = None

    async def __aenter__(self) -> "_DigestProbeAgent":
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
        """Capture the assembled prompt and replay the canned events."""
        self.captured_prompt = self.system_prompt
        for event in self._events:
            yield event


class _ExplodingSessionFactory:
    """Session factory whose sessions always fail (persistence outage)."""

    def __call__(self) -> Any:
        raise SQLAlchemyError("database unavailable")


class _CountingSessionFactory:
    """Session factory wrapper counting how often the boundary opens a session."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self._inner()


# ---------------------------------------------------------------------------
# C-S2-1 — the RPE → severity-band mapping (server-side, fixed vocabulary)
# ---------------------------------------------------------------------------


class TestRpeSeverityBand:
    """The server-side RPE → band mapping (AC2, C-S2-1)."""

    def test_the_full_rpe_domain_maps_into_the_closed_vocabulary(self) -> None:
        """Every RPE 1-10 maps to exactly one of the three fixed bands."""
        assert rpe_severity_band(1) == "low"
        assert rpe_severity_band(3) == "low"
        assert rpe_severity_band(4) == "moderate"
        assert rpe_severity_band(6) == "moderate"
        assert rpe_severity_band(7) == "high"
        assert rpe_severity_band(10) == "high"


# ---------------------------------------------------------------------------
# C-S2-3 — the digest model: closed chips, strict invariants, no RPE slot
# ---------------------------------------------------------------------------


class TestDivergenceDigestModel:
    """The Pydantic v2 digest schema (AC4, C-S2-3)."""

    def test_severity_chips_are_a_closed_enum(self) -> None:
        """The chip vocabulary is exactly the three fixed band labels."""
        schema = FlaggedExertionFact.model_json_schema()

        assert schema["properties"]["severity"]["enum"] == ["low", "moderate", "high"]

    def test_free_text_severity_is_rejected(self) -> None:
        """A chip outside the closed vocabulary can never be constructed."""
        with pytest.raises(ValidationError):
            FlaggedExertionFact.model_validate(
                {
                    "plan_session_key": "2026-10-12",
                    "session_title": "Easy spin",
                    "severity": "brutal",
                }
            )

    def test_flagged_count_must_match_the_fact_list(self) -> None:
        """Counts are server-verified, never trusted (#165 posture)."""
        with pytest.raises(ValidationError):
            DivergenceDigest.model_validate(
                {"missed_count": 0, "flagged_count": 2, "flagged_sessions": []}
            )

    def test_an_all_zero_digest_is_rejected(self) -> None:
        """Zero divergence is modeled as NO digest (None), never an empty one."""
        with pytest.raises(ValidationError):
            DivergenceDigest.model_validate(
                {"missed_count": 0, "flagged_count": 0, "flagged_sessions": []}
            )

    def test_the_digest_schema_carries_no_rpe_field(self) -> None:
        """C-S2-1: the numeric RPE has no slot in the digest schema at all."""
        for schema in (
            DivergenceDigest.model_json_schema(),
            FlaggedExertionFact.model_json_schema(),
        ):
            assert "rpe" not in schema["properties"]


# ---------------------------------------------------------------------------
# AC1 + C-S2-8 — the date-keyed row-diff computation
# ---------------------------------------------------------------------------


class TestBuildDivergenceDigest:
    """The pure row-diff derivation (AC1, AC8/C-S2-8)."""

    def test_missed_and_flagged_counts_come_from_the_row_diff(self) -> None:
        """A past slot without a debrief is missed; a high-RPE debrief is flagged."""
        planned = [
            PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin"),
            PendingDebrief(plan_session_key="2026-10-13", session_title="Threshold intervals"),
            PendingDebrief(plan_session_key="2026-10-14", session_title="Recovery ride"),
        ]

        digest = build_divergence_digest(
            planned=planned,
            debrief_rpes={"2026-10-12": SENTINEL_RPE},
            today=date(2026, 10, 14),
        )

        assert digest is not None
        assert digest.missed_count == 1
        assert digest.flagged_count == 1
        assert digest.flagged_sessions == [
            FlaggedExertionFact(
                plan_session_key="2026-10-12",
                session_title="Easy spin",
                severity="high",
            )
        ]

    def test_zero_divergence_week_yields_no_digest(self) -> None:
        """Every past slot debriefed at low/moderate RPE → no digest, no noise."""
        planned = [PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin")]

        assert (
            build_divergence_digest(
                planned=planned,
                debrief_rpes={"2026-10-12": 4},
                today=date(2026, 10, 14),
            )
            is None
        )

    def test_future_planned_days_are_never_divergence(self) -> None:
        """Slots on/after today have not happened yet — never missed, never flagged."""
        planned = [
            PendingDebrief(plan_session_key="2026-10-14", session_title="Recovery ride"),
            PendingDebrief(plan_session_key="2026-10-15", session_title="Long endurance"),
        ]

        assert (
            build_divergence_digest(planned=planned, debrief_rpes={}, today=date(2026, 10, 14))
            is None
        )

    def test_a_debrief_outside_the_planned_slots_never_counts(self) -> None:
        """Intersection semantics: a debrief for an unplanned key is not divergence."""
        planned = [PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin")]

        digest = build_divergence_digest(
            planned=planned,
            debrief_rpes={"2026-10-06": SENTINEL_RPE},
            today=date(2026, 10, 14),
        )

        assert digest is not None
        assert digest.missed_count == 1
        assert digest.flagged_count == 0

    def test_the_row_diff_is_date_keyed_never_title_keyed(self) -> None:
        """C-S2-8: same-title slots diverge independently — the key is the DATE."""
        planned = [
            PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin"),
            PendingDebrief(plan_session_key="2026-10-13", session_title="Easy spin"),
        ]

        digest = build_divergence_digest(
            planned=planned,
            debrief_rpes={"2026-10-12": SENTINEL_RPE},
            today=date(2026, 10, 14),
        )

        assert digest is not None
        assert digest.missed_count == 1
        assert [fact.plan_session_key for fact in digest.flagged_sessions] == ["2026-10-12"]

    def test_low_and_moderate_bands_are_not_flagged(self) -> None:
        """Only the high band is a divergence fact; low/moderate weeks stay quiet."""
        planned = [
            PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin"),
            PendingDebrief(plan_session_key="2026-10-13", session_title="Threshold intervals"),
        ]

        assert (
            build_divergence_digest(
                planned=planned,
                debrief_rpes={"2026-10-12": 3, "2026-10-13": 6},
                today=date(2026, 10, 14),
            )
            is None
        )


# ---------------------------------------------------------------------------
# AC1 + C-S2-7 — the prompt text: fact shape, hygiene, budget
# ---------------------------------------------------------------------------


class TestDivergenceDigestText:
    """The rendered digest text (AC1, AC7/C-S2-7, DoS budget)."""

    def _digest(self, **overrides: object) -> DivergenceDigest:
        payload: dict[str, object] = {
            "missed_count": 1,
            "flagged_count": 1,
            "flagged_sessions": [
                {
                    "plan_session_key": "2026-10-12",
                    "session_title": "Easy spin",
                    "severity": "high",
                }
            ],
        }
        payload.update(overrides)
        return DivergenceDigest.model_validate(payload)

    def test_the_text_matches_the_ac1_fact_shape(self) -> None:
        """Counts line + per-day flagged facts, exactly AC1's example shape."""
        text = divergence_digest_text(self._digest())

        assert text.startswith("Divergence digest")
        assert "- 1 session missed · 1 session flagged high exertion" in text
        assert "- 2026-10-12: Easy spin — flagged high exertion" in text

    def test_pluralization_tracks_the_counts(self) -> None:
        """The counts line reads naturally at zero and plural counts."""
        text = divergence_digest_text(
            self._digest(
                missed_count=2,
                flagged_count=2,
                flagged_sessions=[
                    {
                        "plan_session_key": "2026-10-12",
                        "session_title": "Easy spin",
                        "severity": "high",
                    },
                    {
                        "plan_session_key": "2026-10-13",
                        "session_title": "Threshold intervals",
                        "severity": "high",
                    },
                ],
            )
        )

        assert "- 2 sessions missed · 2 sessions flagged high exertion" in text

    def test_stored_titles_pass_through_single_line_hygiene(self) -> None:
        """C-S2-7: a stored title carrying newlines/control chars renders safe."""
        digest = build_divergence_digest(
            planned=[
                PendingDebrief(plan_session_key="2026-10-12", session_title="Long\r\nInjected\x1b")
            ],
            debrief_rpes={"2026-10-12": SENTINEL_RPE},
            today=date(2026, 10, 14),
        )

        assert digest is not None
        text = divergence_digest_text(digest)
        assert "Long Injected" in text
        assert "\r" not in text
        assert "\x1b" not in text

    def test_the_numeric_rpe_never_renders(self) -> None:
        """C-S2-1 (unit): the band label replaces the numeric RPE in the text."""
        digest = build_divergence_digest(
            planned=[PendingDebrief(plan_session_key="2026-10-12", session_title="Easy spin")],
            debrief_rpes={"2026-10-12": SENTINEL_RPE},
            today=date(2026, 10, 14),
        )

        assert digest is not None
        text = divergence_digest_text(digest)
        assert "flagged high exertion" in text
        assert str(SENTINEL_RPE) not in text

    def test_the_text_stays_within_the_token_budget(self) -> None:
        """DoS control: the digest is bounded by the approved-week-style cap."""
        week_days = [date.fromisocalendar(2026, 42, day).isoformat() for day in range(1, 8)]
        digest = build_divergence_digest(
            planned=[
                PendingDebrief(plan_session_key=day, session_title="X" * 200) for day in week_days
            ],
            debrief_rpes={day: SENTINEL_RPE for day in week_days},
            today=date(2026, 10, 19),
        )

        assert digest is not None
        text = divergence_digest_text(digest)
        assert len(text) <= DIVERGENCE_DIGEST_MAX_CHARS
        # The counts line always renders; the per-day lines are the truncatable tail.
        assert "- 7 sessions flagged high exertion" in text

    def test_apply_appends_the_digest_and_no_ops_on_none(self) -> None:
        """AC5: the apply helper is a no-op when the week shows no divergence."""
        base = "BASE PROMPT"
        assert apply_divergence_digest(base, None) == base
        extended = apply_divergence_digest(base, self._digest())
        assert extended.startswith(base)
        assert "Divergence digest" in extended


# ---------------------------------------------------------------------------
# AC2/AC3/AC5 — the sentinel contract tests at the SSE boundary
# ---------------------------------------------------------------------------


class TestDivergenceDigestEndpoint:
    """POST /api/agent/stream injects the divergence digest (AC5, C-S2-4)."""

    def _client_with(
        self, settings: Settings, agent: Any, session_factory: Any = None
    ) -> TestClient:
        """Build a TestClient with a stub agent and an optional session factory."""
        app = create_app(settings)
        app.state.agent_factory = MagicMock(return_value=agent)
        if session_factory is not None:
            app.state.db_session_factory = session_factory
        return TestClient(app)

    async def _seed_approved_week(self, factory: Any, week: WeeklyPlanDraft) -> None:
        """Persist an approved draft row for the given week payload."""
        async with factory() as session:
            row = await upsert_week_draft(session, week)
            await transition_draft_status(session, row, "approved")

    async def _seed_debrief(
        self,
        factory: Any,
        *,
        plan_session_key: str,
        rpe: int,
        notes_encrypted: str | None,
    ) -> None:
        """Insert one debrief row directly (fixture state, not path coverage).

        #243: the RPE is seeded the way the save path stores it — as the
        Fernet token, never the plaintext integer.
        """
        async with factory() as session:
            session.add(
                DebriefRow(
                    athlete_id=DEFAULT_ATHLETE_ID,
                    session_external_id=None,
                    plan_session_key=plan_session_key,
                    rpe=_rpe_token(rpe),
                    notes_encrypted=notes_encrypted,
                    created_at="2026-10-12T10:00:00+00:00",
                    updated_at="2026-10-12T10:00:00+00:00",
                )
            )
            await session.commit()

    async def _stream(self, client: TestClient) -> str:
        """POST one agent turn and return the full SSE body."""
        with client.stream(
            "POST", "/api/agent/stream", json={"message": "hi", "history": []}
        ) as response:
            return response.read().decode("utf-8")

    async def test_the_divergence_digest_rides_the_existing_digest_boundary(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC5: the digest joins the anchor/objectives/pending/approved-week boundary."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        await self._seed_debrief(
            db_session_factory,
            plan_session_key="2026-10-12",
            rpe=SENTINEL_RPE,
            notes_encrypted=None,
        )
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        assert "event: done" in body
        prompt = agent.captured_prompt
        assert prompt is not None
        assert "Today is 2026-10-14 (Wednesday)." in prompt
        # The divergence digest is applied AFTER the approved-week digest —
        # it diffs the week that digest announces.
        assert "Approved current week" in prompt
        assert "Divergence digest" in prompt
        assert prompt.index("Approved current week") < prompt.index("Divergence digest")
        assert "- 1 session missed · 1 session flagged high exertion" in prompt
        assert "- 2026-10-12: Easy spin — flagged high exertion" in prompt

    async def test_zero_divergence_week_yields_no_digest_section(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC1: a fully adherent week stays silent — no noise section at all."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        await self._seed_debrief(
            db_session_factory, plan_session_key="2026-10-12", rpe=4, notes_encrypted=None
        )
        await self._seed_debrief(
            db_session_factory, plan_session_key="2026-10-13", rpe=3, notes_encrypted=None
        )
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        assert "event: done" in body
        prompt = agent.captured_prompt
        assert prompt is not None
        assert "Divergence digest" not in prompt

    async def test_rpe_numeric_sentinel_never_reaches_prompt_or_sse(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """C-S2-1 (AC2): the stored RPE numeric is absent everywhere; the band shows."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        await self._seed_debrief(
            db_session_factory,
            plan_session_key="2026-10-12",
            rpe=SENTINEL_RPE,
            notes_encrypted=None,
        )
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        prompt = agent.captured_prompt
        assert prompt is not None
        # The severity band label IS emitted (the positive half of AC2)...
        assert "flagged high exertion" in prompt
        # ...while the numeric RPE value appears NOWHERE — the fixture is
        # 9-free, so any leak of the stored RPE introduces a 9.
        assert str(SENTINEL_RPE) not in prompt
        assert str(SENTINEL_RPE) not in body

    async def test_note_sentinel_never_reaches_prompt_or_sse(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """C-S2-2 (AC3): neither the note plaintext nor its ciphertext leaks."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        await self._seed_debrief(
            db_session_factory,
            plan_session_key="2026-10-12",
            rpe=SENTINEL_RPE,
            notes_encrypted=None,
        )
        # A realistic C2 row: the sentinel note stored as real Fernet ciphertext.
        cipher = Fernet(TEST_ENCRYPTION_KEY)
        ciphertext = cipher.encrypt(SENTINEL_NOTE.encode("utf-8")).decode("ascii")
        async with db_session_factory() as session:
            stored = (
                await session.execute(
                    select(DebriefRow).where(DebriefRow.plan_session_key == "2026-10-12")
                )
            ).scalar_one()
            stored.notes_encrypted = ciphertext
            await session.commit()

        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        prompt = agent.captured_prompt
        assert prompt is not None
        # The plaintext note never reaches the prompt or the SSE stream...
        assert SENTINEL_NOTE not in prompt
        assert SENTINEL_NOTE not in body
        # ...and the stored ciphertext is never even SELECTED (the stronger
        # half of C-S2-2: the digest path does not touch the column).
        assert ciphertext not in prompt
        assert ciphertext not in body

    async def test_db_failure_yields_no_divergence_digest_and_the_turn_survives(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """C-S2-4 (AC5): a persistence outage degrades to no digest — turn intact."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(settings, agent, _ExplodingSessionFactory())

        body = await self._stream(client)

        assert "event: done" in body
        prompt = agent.captured_prompt
        assert prompt is not None
        assert "Divergence digest" not in prompt

    async def test_a_tampered_rpe_token_drops_the_session_from_the_facts(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """A tampered stored token degrades — the turn survives (#259 AC2).

        The tampered row yields no RPE entry, so its session drops out of
        the divergence facts and counts as missed; the digest still renders
        from what remains readable and the SSE turn completes (never a 500).
        """
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        tampered = Fernet(Fernet.generate_key()).encrypt(b"9").decode("ascii")
        async with db_session_factory() as session:
            session.add(
                DebriefRow(
                    athlete_id=DEFAULT_ATHLETE_ID,
                    session_external_id=None,
                    plan_session_key="2026-10-12",
                    rpe=tampered,
                    notes_encrypted=None,
                    created_at="2026-10-12T10:00:00+00:00",
                    updated_at="2026-10-12T10:00:00+00:00",
                )
            )
            await session.commit()

        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        assert "event: done" in body
        prompt = agent.captured_prompt
        assert prompt is not None
        # Both past slots (2026-10-12, 2026-10-13) count as missed — the
        # tampered row contributes no readable RPE — and nothing is flagged.
        assert "- 2 sessions missed" in prompt
        assert "flagged" not in prompt

    async def test_an_undecryptable_projection_degrades_to_no_digest(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """InvalidToken joins the digest degrade boundary (#259 R5) — turn intact.

        The per-row skip inside the projection already contains tampered
        rows; this pins the boundary backstop: an ``InvalidToken`` escaping
        the projection degrades exactly like ``EncryptionUnavailableError``
        (no digest section, the turn survives), never a 500.
        """
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())

        async def _raising_projection(*args: Any, **kwargs: Any) -> dict[str, int]:
            raise InvalidToken("tampered")

        monkeypatch.setattr(coach_web.app, "list_debrief_rpes", _raising_projection)
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, db_session_factory)

        body = await self._stream(client)

        assert "event: done" in body
        prompt = agent.captured_prompt
        assert prompt is not None
        assert "Divergence digest" not in prompt

    async def test_the_digest_shares_the_single_request_scoped_db_session(
        self,
        cipher_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """C-S2-4 (AC5): one DB session serves every digest read of the turn."""
        _freeze_today(monkeypatch, date(2026, 10, 14))
        await self._seed_approved_week(db_session_factory, _week_2026_w42())
        await self._seed_debrief(
            db_session_factory,
            plan_session_key="2026-10-12",
            rpe=SENTINEL_RPE,
            notes_encrypted=None,
        )
        counting = _CountingSessionFactory(db_session_factory)
        agent = _DigestProbeAgent([AgentEvent(type="done", data={"message": "ok"})])
        client = self._client_with(cipher_settings, agent, counting)

        body = await self._stream(client)

        assert "event: done" in body
        assert counting.calls == 1
        prompt = agent.captured_prompt
        assert prompt is not None
        # The shared session served the divergence read too.
        assert "Divergence digest" in prompt


# ---------------------------------------------------------------------------
# AC6 + C-S2-5/C-S2-6 — the scope pins: DB-only, no new surface
# ---------------------------------------------------------------------------


class TestDivergenceDigestScope:
    """The digest adds no ICU read, no endpoint, no persistence (AC6)."""

    def test_icu_tool_get_costs_are_unchanged(self) -> None:
        """C-S2-5 (AC6): the digest adds no ICU read — the cost table is pinned.

        Rewritten by #262 (C-BR-6, finding F1): the readiness dashboard
        GET joined the budget table for the morning briefing. The digest
        itself still adds no ICU read — the pin now guards the table
        WITH the #262 entry so no further silent growth is possible.
        """
        assert ICU_TOOL_GET_COSTS == {
            "icu_get_workout_compliance": 2,
            "icu_get_completed_sessions": 1,
            "icu_get_wellness": 1,
            "icu_get_trends": 1,
            "intervals_get_readiness_dashboard": 1,
        }

    def test_no_new_endpoint_is_added(self, settings: Settings) -> None:
        """C-S2-5 (AC6): the digest introduces no new HTTP endpoint."""
        app = create_app(settings)
        api_paths = {
            getattr(route, "path", "")
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/")
        }

        assert api_paths == {
            "/api/agent/stream",
            "/api/plan/approve",
            "/api/week/approve",
            "/api/objectives",
            "/api/periodization/approve",
            "/api/debrief",
            "/api/debrief/pending",
        }

    def test_no_new_orm_model_or_migration_is_added(self) -> None:
        """C-S2-6: the digest is computed per-request — no new table/migration.

        Superseded for #243: migration 0006 (encrypted debrief RPE) extends
        the chain — the digest itself still adds no persistence of its own.
        """
        import coach_web.app  # noqa: F401 - pulls every model module into Base

        assert sorted(Base.metadata.tables) == [
            "athlete_objectives",
            "debriefs",
            "periodization_phases",
            "plan_drafts",
            "week_adherence",
        ]
        versions = sorted(
            path.name
            for path in Path(coach_web.__file__)
            .parent.joinpath("alembic", "versions")
            .glob("0*.py")
        )
        assert versions == [
            "0001_athlete_objectives.py",
            "0002_periodization_phases.py",
            "0003_plan_drafts.py",
            "0004_debriefs.py",
            "0005_week_adherence.py",
            "0006_encrypt_debrief_rpe.py",
        ]

    def test_the_digest_module_imports_no_mcp_or_icu_client(self) -> None:
        """C-S2-5/C-S2-8: the derivation stays DB-only and row-diff-based."""
        source = Path(coach_web.debrief.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        assert not any("mcp" in name or "icu" in name for name in imported)
        # C-S2-8: no title-keyed move primitive joins the digest path.
        assert not any("moved_session" in name for name in imported)
