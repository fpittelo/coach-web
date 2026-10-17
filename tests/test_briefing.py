"""Morning briefing card — pull-model greeting extension (#262, epic #215-A).

On the FIRST page load of the day, when no pending debrief claims the
greeting slot, the coach opens with a server-rendered morning briefing card
(readiness band, today's planned session, execution cues). The
@cyber-security verdict (APPROVED-WITH-CONTROLS, 2026-10-17) is pinned here
as contract tests — the controls ARE the acceptance criteria:

- C-BR-1 (AC1): health numerics (resting_hr / hrv_rmssd / sleep_hours) never
  reach any SSE payload or the system prompt — sentinel-seeded; only the
  server-mapped closed-enum band label travels.
- C-BR-2 (AC2): the readiness band is a closed ``Literal`` enum, assembled
  server-side; free text can never become a band.
- C-BR-3 (AC3): the briefing is a server-rendered Pydantic→HTML card on a
  dedicated SSE event — the readiness source is pinned ABSENT from the
  system prompt, and the event payload validates against the strict model.
- C-BR-4 (AC4): a pending-debrief greeting suppresses the briefing
  (both-present → no briefing event).
- C-BR-5 (AC5): stateless/idempotent — repeated loads render the identical
  card and the briefing path performs no DB write.
- C-BR-6 (AC6): ``intervals_get_readiness_dashboard`` is registered in the
  ICU GET budget (finding F1) and the read is TTL-cached so repeated
  loads/reconnects do not multiply ICU calls.
- C-BR-7 (AC7): the briefing joins the degrade boundary — an ICU failure
  yields no briefing event and never breaks the stream bootstrap.
- C-BR-8 (AC8): today's session block carries date/title/focus/duration/
  planned-TSS ONLY — no C2 fields (RPE/notes) exist on the schema.
- C-BR-9 (AC9): stored-content text passes through ``_digest_single_line``.

Sentinel note (C-BR-1): the seeded numerics are 999 / 999.9 / 99.9 — the
string ``"999"`` is a substring of all three, so one absence assertion pins
the whole family. No fixture string contains "999" otherwise, so any leak
of a seeded numeric introduces a match that cannot exist legitimately.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from coach_web.agent import AgentEvent
from coach_web.analysis import ICU_TOOL_GET_COSTS
from coach_web.app import STATIC_DIR, create_app
from coach_web.briefing import (
    BRIEFING_SESSION_FIELDS,
    BriefingSessionBlock,
    MorningBriefingCard,
    briefing_session_block,
    build_morning_briefing,
    clear_readiness_cache,
    fetch_readiness_metrics,
    readiness_band,
)
from coach_web.config import Settings, get_settings
from coach_web.debrief import DEFAULT_ATHLETE_ID, DebriefRow, build_debrief_cipher
from coach_web.mcp_hub import COACH_SERVER
from coach_web.microcycle import (
    DaySlot,
    PlanDraftRow,
    WeeklyPlanDraft,
    build_week_markdown,
    transition_draft_status,
    upsert_week_draft,
)
from coach_web.models import ReadinessMetrics

SENTINEL_RESTING_HR = 999
"""Sentinel resting HR — must never appear in any SSE payload or prompt."""

SENTINEL_HRV_RMSSD = 999.9
"""Sentinel HRV — its string contains "999" (one assertion pins the family)."""

SENTINEL_SLEEP_HOURS = 99.9
"""Sentinel sleep hours — its string contains "999" too."""

SENTINEL_NOTE_TITLE = "Recovery\tInjected\x1b"
"""Stored-title sentinel: control characters must never survive to the card.

The tab and ESC survive the markdown round-trip (neither breaks the table
row the way a newline would) and ``_digest_single_line`` must collapse and
drop them — the card may only carry ``"Recovery Injected"``.
"""


@pytest.fixture(autouse=True)
def _isolate_readiness_cache() -> Iterator[None]:
    """Give every test an empty readiness cache (module-level by design)."""
    clear_readiness_cache()
    yield
    clear_readiness_cache()


# ---------------------------------------------------------------------------
# Fixtures & test doubles
# ---------------------------------------------------------------------------


def _sentinel_metrics(tsb: float = 15.0) -> ReadinessMetrics:
    """Readiness metrics carrying the C-BR-1 sentinel numerics."""
    return ReadinessMetrics(
        ftp=250,
        resting_hr=SENTINEL_RESTING_HR,
        hrv_rmssd=SENTINEL_HRV_RMSSD,
        sleep_hours=SENTINEL_SLEEP_HOURS,
        ctl=75.0,
        atl=60.0,
        tsb=tsb,
    )


def _day(**overrides: object) -> Any:
    """Return a valid day slot with optional overrides (mirrors #236 fixture)."""
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
    """An approved-week fixture for ISO week 2026-W42 (Mon 2026-10-12).

    999-free by construction so the seeded readiness numerics act as
    whole-payload sentinels (C-BR-1).
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
                    focus="Sweet spot",
                    planned_tss=60,
                    duration_minutes=80,
                ),
                _day(
                    day_of_week="wed",
                    date="2026-10-14",
                    session_title=SENTINEL_NOTE_TITLE,
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


class _FakeHub:
    """Hub double serving a canned readiness payload (counting its calls)."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.connected_servers = {COACH_SERVER}
        self.calls = 0

    async def connect(self) -> None:
        """Connect (no-op)."""

    async def close(self) -> None:
        """Close (no-op)."""

    async def execute_tool(self, name: str, arguments: Any = None) -> str:
        """Serve the canned payload, counting the call."""
        self.calls += 1
        return self.payload


class _UnconnectedHub(_FakeHub):
    """Hub double whose coach-mcp server never connects (degrade path)."""

    def __init__(self) -> None:
        super().__init__("{}")
        self.connected_servers: set[str] = set()


class _ExplodingHub:
    """Hub double whose connect always fails (hub outage)."""

    connected_servers: set[str] = set()

    async def connect(self) -> None:
        """Fail the connection."""
        raise RuntimeError("hub unavailable")

    async def close(self) -> None:
        """Close (no-op)."""

    async def execute_tool(self, name: str, arguments: Any = None) -> str:
        """Never reached."""
        raise AssertionError("execute_tool must not be called on a failed connect")


class _CountingHubFactory:
    """Hub factory wrapper counting how often the boundary builds a hub."""

    def __init__(self, hub: Any) -> None:
        self._hub = hub
        self.calls = 0

    def __call__(self, settings: Settings) -> Any:
        self.calls += 1
        return self._hub


def _rpe_token(settings: Settings, rpe: int) -> str:
    """Encrypt an RPE exactly the way the save path stores it (#243)."""
    cipher = build_debrief_cipher(settings)
    return cipher.encrypt(str(rpe).encode("utf-8")).decode("ascii")


def _readiness_payload(metrics: ReadinessMetrics) -> str:
    """Serialize metrics the way the coach-mcp tool result arrives."""
    return json.dumps(metrics.model_dump())


class _BriefingProbeAgent:
    """Agent stub capturing the assembled system prompt at run time."""

    system_prompt: str = ""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events
        self.captured_prompt: str | None = None

    async def __aenter__(self) -> "_BriefingProbeAgent":
        """Enter the agent context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context."""

    async def run(
        self, message: str, *, history: list[Any] | None = None
    ) -> AsyncIterator[AgentEvent]:
        """Capture the assembled prompt and replay the canned events."""
        self.captured_prompt = self.system_prompt
        for event in self._events:
            yield event


def _briefing_events() -> list[AgentEvent]:
    """The canned agent turn used by every endpoint test."""
    return [AgentEvent(type="done", data={"message": "ok"})]


async def _seed_approved_week(factory: Any, week: WeeklyPlanDraft) -> None:
    """Persist an approved draft row for the given week payload."""
    async with factory() as session:
        row = await upsert_week_draft(session, week)
        await transition_draft_status(session, row, "approved")


def _client_with(
    settings: Settings,
    agent: Any,
    session_factory: Any = None,
    hub_factory: Any = None,
) -> TestClient:
    """Build a TestClient with stub agent, store and hub boundaries."""
    app = create_app(settings)
    app.state.agent_factory = MagicMock(return_value=agent)
    if session_factory is not None:
        app.state.db_session_factory = session_factory
    if hub_factory is not None:
        app.state.hub_factory = hub_factory
    return TestClient(app)


def _stream(client: TestClient, **payload: Any) -> str:
    """POST one agent turn and return the full SSE body."""
    body = {"message": "hi", "history": []}
    body.update(payload)
    with client.stream("POST", "/api/agent/stream", json=body) as response:
        return response.read().decode("utf-8")


def _briefing_event_data(body: str) -> str | None:
    """Extract the morning_briefing event's data line, or None when absent."""
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "event: morning_briefing":
            for follower in lines[index + 1 :]:
                if follower.startswith("data:"):
                    return follower[len("data:") :].strip()
                if follower.strip() == "":
                    break
    return None


# ---------------------------------------------------------------------------
# C-BR-2 (AC2) — the server-side band mapping: closed vocabulary
# ---------------------------------------------------------------------------


class TestReadinessBandMapping:
    """The server-side TSB → band mapping (AC2, C-BR-2)."""

    def test_the_full_tsb_domain_maps_into_the_closed_vocabulary(self) -> None:
        """Every TSB value maps to exactly one of the three fixed bands."""
        assert readiness_band(_sentinel_metrics(tsb=20.0)) == "fresh"
        assert readiness_band(_sentinel_metrics(tsb=5.0)) == "fresh"
        assert readiness_band(_sentinel_metrics(tsb=4.9)) == "moderate"
        assert readiness_band(_sentinel_metrics(tsb=-10.0)) == "moderate"
        assert readiness_band(_sentinel_metrics(tsb=-10.1)) == "fatigued"
        assert readiness_band(_sentinel_metrics(tsb=-50.0)) == "fatigued"

    def test_the_band_vocabulary_is_a_closed_enum(self) -> None:
        """The card schema carries exactly the three fixed band labels."""
        schema = MorningBriefingCard.model_json_schema()

        assert schema["properties"]["readiness_band"]["enum"] == [
            "fresh",
            "moderate",
            "fatigued",
        ]

    def test_a_free_text_band_is_rejected(self) -> None:
        """A band outside the closed vocabulary can never be constructed."""
        with pytest.raises(ValidationError):
            MorningBriefingCard.model_validate(
                {"readiness_band": "great", "cues": [], "session": None}
            )


# ---------------------------------------------------------------------------
# C-BR-3/C-BR-8 (AC3/AC8) — the strict card schema: closed surface
# ---------------------------------------------------------------------------


class TestMorningBriefingCardModel:
    """The Pydantic v2 card schema (AC3, AC8, C-BR-3/C-BR-8)."""

    def test_the_card_schema_is_closed(self) -> None:
        """Unknown fields can never ride the card (strict model, C-BR-3)."""
        with pytest.raises(ValidationError):
            MorningBriefingCard.model_validate(
                {
                    "readiness_band": "fresh",
                    "cues": [],
                    "session": None,
                    "resting_hr": SENTINEL_RESTING_HR,
                }
            )

    def test_the_card_carries_no_readiness_numeric_fields(self) -> None:
        """The card surface has no slot for any readiness numeric (C-BR-1)."""
        schema = MorningBriefingCard.model_json_schema()

        assert set(schema["properties"]) == {"readiness_band", "cues", "session"}

    def test_the_session_block_carries_exactly_the_allowed_surface(self) -> None:
        """date/title/focus/duration/planned-TSS only (AC8, C-BR-8)."""
        assert set(BRIEFING_SESSION_FIELDS) == {
            "date",
            "title",
            "focus",
            "duration_minutes",
            "planned_tss",
        }

    def test_the_session_block_carries_no_c2_fields(self) -> None:
        """RPE and notes have no slot on the session block (AC8, C-BR-8)."""

        schema = BriefingSessionBlock.model_json_schema()

        assert "rpe" not in schema["properties"]
        assert "notes" not in schema["properties"]


# ---------------------------------------------------------------------------
# C-BR-1/C-BR-9 (AC1/AC9) — sentinel & hygiene contracts
# ---------------------------------------------------------------------------


class TestSentinelHygiene:
    """Numerics never travel; stored text passes the single-line digest."""

    def test_sentinel_numerics_never_reach_the_card(self) -> None:
        """C-BR-1: the seeded 999-family is absent; only the band travels."""
        card = build_morning_briefing(
            _sentinel_metrics(tsb=15.0),
            build_week_markdown(_week_2026_w42()),
            date(2026, 10, 14),
        )
        rendered = json.dumps(card.model_dump())

        assert "999" not in rendered
        assert card.readiness_band == "fresh"
        assert card.cues

    def test_stored_title_and_focus_pass_through_single_line_hygiene(self) -> None:
        """C-BR-9: control characters never survive to the card."""
        card = build_morning_briefing(
            _sentinel_metrics(),
            build_week_markdown(_week_2026_w42()),
            date(2026, 10, 14),
        )
        rendered = json.dumps(card.model_dump())

        assert card.session is not None
        assert card.session.title == "Recovery Injected"
        assert "\r" not in rendered
        assert "\x1b" not in rendered


# ---------------------------------------------------------------------------
# AC8 — today's session block derivation from the approved draft
# ---------------------------------------------------------------------------


class TestBriefingSessionBlock:
    """Today's approved-draft day → the session block (AC8)."""

    def test_todays_row_becomes_the_session_block(self) -> None:
        """The draft row matching today carries the allowed metadata only."""
        block = briefing_session_block(build_week_markdown(_week_2026_w42()), date(2026, 10, 13))

        assert block is not None
        assert block.date == "2026-10-13"
        assert block.title == "Threshold intervals"
        assert block.focus == "Sweet spot"
        assert block.duration_minutes == 80
        assert block.planned_tss == 60

    def test_a_rest_day_yields_no_session_block(self) -> None:
        """A rest day claims no session block."""
        assert (
            briefing_session_block(build_week_markdown(_week_2026_w42()), date(2026, 10, 16))
            is None
        )

    def test_a_day_outside_the_draft_yields_no_session_block(self) -> None:
        """A date the draft does not cover yields no session block."""
        assert (
            briefing_session_block(build_week_markdown(_week_2026_w42()), date(2026, 11, 2)) is None
        )


# ---------------------------------------------------------------------------
# C-BR-6 (AC6) — the GET-budget entry and the bounded TTL cache
# ---------------------------------------------------------------------------


class TestReadinessCache:
    """The F1 fix and the bounded cache behavior (AC6, C-BR-6)."""

    def test_the_readiness_dashboard_is_registered_in_the_get_budget(self) -> None:
        """F1: the dashboard GET is a budgeted one-GET read."""
        assert ICU_TOOL_GET_COSTS["intervals_get_readiness_dashboard"] == 1

    async def test_repeated_fetches_within_the_ttl_hit_the_cache(self) -> None:
        """Repeated loads reuse the cached metrics — one ICU call, not two."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics()))
        factory = _CountingHubFactory(hub)
        settings = get_settings()

        first = await fetch_readiness_metrics(factory, settings, now=100.0)
        second = await fetch_readiness_metrics(factory, settings, now=101.0)

        assert first == second
        assert factory.calls == 1
        assert hub.calls == 1

    async def test_an_expired_entry_refetches(self) -> None:
        """Past the TTL the cache is stale and the read happens again."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics()))
        factory = _CountingHubFactory(hub)
        settings = get_settings()

        await fetch_readiness_metrics(factory, settings, now=100.0)
        await fetch_readiness_metrics(factory, settings, now=100.0 + settings.CACHE_TTL_SECONDS + 1)

        assert hub.calls == 2

    async def test_a_failed_read_is_never_cached_and_yields_none(self) -> None:
        """A hub outage degrades to None and never poisons the cache."""
        factory = _CountingHubFactory(_ExplodingHub())
        settings = get_settings()

        assert await fetch_readiness_metrics(factory, settings, now=100.0) is None

        good_factory = _CountingHubFactory(_FakeHub(_readiness_payload(_sentinel_metrics())))
        metrics = await fetch_readiness_metrics(good_factory, settings, now=101.0)

        assert metrics is not None
        assert good_factory.calls == 1

    async def test_an_unconnected_coach_server_yields_none(self) -> None:
        """A hub without coach-mcp connected degrades to None (no card)."""
        factory = _CountingHubFactory(_UnconnectedHub())

        assert await fetch_readiness_metrics(factory, get_settings(), now=100.0) is None

    async def test_an_error_payload_yields_none(self) -> None:
        """The hub's ``{"error": ...}`` reply is never mistaken for data."""
        hub = _FakeHub(json.dumps({"error": "upstream failed"}))

        metrics = await fetch_readiness_metrics(_CountingHubFactory(hub), get_settings(), now=100.0)

        assert metrics is None


# ---------------------------------------------------------------------------
# C-BR-3/C-BR-4/C-BR-5/C-BR-7 — the endpoint contracts at the SSE boundary
# ---------------------------------------------------------------------------


class TestBriefingEndpoint:
    """POST /api/agent/stream emits the briefing card (AC1/AC3/AC4/AC5/AC7)."""

    async def _seeded_client(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
        hub: Any,
        today: date = date(2026, 10, 14),
        pending: bool = False,
    ) -> tuple[TestClient, _BriefingProbeAgent]:
        """A client with the approved week seeded and the clock frozen.

        ``pending=False`` debriefs the week's past sessions so no pending
        debrief claims the greeting slot; ``pending=True`` leaves them
        debrief-less (the AC4 suppression state).
        """
        _freeze_today(monkeypatch, today)
        await _seed_approved_week(db_session_factory, _week_2026_w42())
        if not pending:
            async with db_session_factory() as session:
                for key in ("2026-10-12", "2026-10-13"):
                    session.add(
                        DebriefRow(
                            athlete_id=DEFAULT_ATHLETE_ID,
                            session_external_id=None,
                            plan_session_key=key,
                            rpe=_rpe_token(settings, 5),
                            notes_encrypted=None,
                            created_at="2026-10-13T10:00:00+00:00",
                            updated_at="2026-10-13T10:00:00+00:00",
                        )
                    )
                await session.commit()
        agent = _BriefingProbeAgent(_briefing_events())
        client = _client_with(
            settings,
            agent,
            db_session_factory,
            _CountingHubFactory(hub),
        )
        return client, agent

    async def test_the_briefing_event_leads_the_stream_and_carries_the_strict_model(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC3: a dedicated SSE event whose payload validates against the model."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        client, _ = await self._seeded_client(settings, monkeypatch, db_session_factory, hub)

        body = _stream(client, briefing=True)

        data = _briefing_event_data(body)
        assert data is not None
        card = MorningBriefingCard.model_validate(json.loads(data)["data"]["briefing"])
        assert card.readiness_band == "fresh"
        assert card.session is not None
        assert card.session.date == "2026-10-14"
        # The briefing leads the stream — it is the greeting.
        assert body.index("event: morning_briefing") < body.index("event: done")

    async def test_sentinel_numerics_never_reach_the_stream_or_the_prompt(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC1/AC3: the readiness source is absent from prompt and payload."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        client, agent = await self._seeded_client(settings, monkeypatch, db_session_factory, hub)

        body = _stream(client, briefing=True)

        assert "999" not in body
        assert agent.captured_prompt is not None
        assert "999" not in agent.captured_prompt

    async def test_a_pending_debrief_suppresses_the_briefing(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC4: both-present → no briefing event; the greeting slot is claimed.

        The seeded week's past sessions stay debrief-less, so the pending
        derivation fires and claims the greeting slot.
        """
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        client, agent = await self._seeded_client(
            settings, monkeypatch, db_session_factory, hub, pending=True
        )

        body = _stream(client, briefing=True)

        assert _briefing_event_data(body) is None
        assert agent.captured_prompt is not None
        assert "Pending session debriefs" in agent.captured_prompt

    async def test_repeated_loads_render_the_identical_card_without_db_writes(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC5: stateless/idempotent — identical card, no DB write."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        client, _ = await self._seeded_client(settings, monkeypatch, db_session_factory, hub)

        async def _row_counts() -> tuple[int, int]:
            async with db_session_factory() as session:
                drafts = len((await session.execute(select(PlanDraftRow))).scalars().all())
                debriefs = len((await session.execute(select(DebriefRow))).scalars().all())
            return drafts, debriefs

        before = await _row_counts()
        first = _stream(client, briefing=True)
        second = _stream(client, briefing=True)
        after = await _row_counts()

        assert _briefing_event_data(first) is not None
        assert _briefing_event_data(first) == _briefing_event_data(second)
        assert before == after

    async def test_an_icu_failure_degrades_to_no_briefing_event(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """AC7: ICU failure → no briefing event, bootstrap unaffected."""
        client, _ = await self._seeded_client(
            settings, monkeypatch, db_session_factory, _ExplodingHub()
        )

        body = _stream(client, briefing=True)

        assert _briefing_event_data(body) is None
        assert "event: done" in body

    async def test_briefing_defaults_to_off(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        db_session_factory: Any,
    ) -> None:
        """Only the first-load-of-the-day request derives the briefing."""
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        client, _ = await self._seeded_client(settings, monkeypatch, db_session_factory, hub)

        body = _stream(client)

        assert _briefing_event_data(body) is None
        assert hub.calls == 0

    async def test_a_store_outage_suppresses_the_briefing(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC7 family: an unreadable store yields no briefing either."""

        class _ExplodingSessionFactory:
            def __call__(self) -> Any:
                raise SQLAlchemyError("database unavailable")

        _freeze_today(monkeypatch, date(2026, 10, 14))
        hub = _FakeHub(_readiness_payload(_sentinel_metrics(tsb=15.0)))
        agent = _BriefingProbeAgent(_briefing_events())
        client = _client_with(settings, agent, _ExplodingSessionFactory(), _CountingHubFactory(hub))

        body = _stream(client, briefing=True)

        assert _briefing_event_data(body) is None
        assert "event: done" in body


# ---------------------------------------------------------------------------
# C-BR-3 (client half) — the pull-model flag & structured rendering
# ---------------------------------------------------------------------------


class TestMorningBriefingClientContract:
    """Source-level pins on the client flag logic and card rendering."""

    @pytest.fixture
    def app_js(self) -> str:
        """The shipped client script."""
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    @pytest.fixture
    def index_html(self) -> str:
        """The shipped page."""
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_the_client_requests_the_briefing_only_when_the_flag_is_stale(
        self, app_js: str
    ) -> None:
        """R3: the localStorage date flag gates the briefing request."""
        assert "coachBriefingDate" in app_js
        assert "briefingFlagStale" in app_js
        assert "briefing: requestBriefing" in app_js

    def test_the_briefing_event_renders_as_a_structured_card(
        self, app_js: str, index_html: str
    ) -> None:
        """C-BR-3: a dedicated handler pins the card; x-text only."""
        assert "morning_briefing:" in app_js
        assert "morningBriefing" in app_js
        assert 'x-text="morningBriefing.readiness_band"' in index_html
        # The briefing card never renders raw HTML (ADR-006 §9).
        briefing_region = index_html.split('aria-label="Morning briefing"')[1]
        assert "x-html" not in briefing_region

    def test_the_flag_is_marked_shown_at_request_time(self, app_js: str) -> None:
        """Repeated sends on the first load never re-request the briefing."""
        assert "markBriefingShown" in app_js
