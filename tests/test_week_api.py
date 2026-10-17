"""POST /api/week/approve — weekly plan approval contract (#168).

The endpoint persists the draft (MADR-008 ``plan_drafts``: create-or-reuse,
replacement rule, submitted → approved only on full success), then writes one
Intervals.icu event per day through ``coach-mcp`` sequentially (#163: the
write is NOT atomic — per-day ``DayWriteResult`` outcomes are returned and a
partial failure stays ``submitted`` for the retry-failed-days affordance).

Every test is hermetic: the MCP hub is replaced by an in-memory fake and the
persistence fixtures run against a per-test SQLite file.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from coach_web.app import (
    WEEK_ADJUSTMENT_AVAILABILITY_DETAIL,
    WEEK_ADJUSTMENT_EMPTY_WRITE_SET_DETAIL,
    WEEK_ADJUSTMENT_PAST_DAYS_DETAIL,
    WEEK_ADJUSTMENT_PEAK_DATE_DETAIL,
    WEEK_ADJUSTMENT_PHASE_BOUNDARY_DETAIL,
    create_app,
)
from coach_web.config import Settings
from coach_web.microcycle import WeeklyPlanDraft, build_week_markdown, week_bounds

WEEK_START = "2026-10-05"
"""Monday of ISO week 2026-W41 (pinned literal — the tests' calendar anchor)."""

ADJUSTMENT_TODAY = date(2026, 10, 8)
"""Frozen clock: the Thursday of 2026-W41 (four locked days, three adjustable)."""


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FrozenDate(date):
    """date subclass whose ``today()`` is pinned to the adjustment clock."""

    @classmethod
    def today(cls) -> "_FrozenDate":
        return _FrozenDate(ADJUSTMENT_TODAY.year, ADJUSTMENT_TODAY.month, ADJUSTMENT_TODAY.day)


class WeekHub:
    """Fake hub implementing the two-phase ``icu_batch_create_events`` contract.

    Preview calls (without ``confirm_token``) return a stub token; execute
    calls return the per-item upsert report — every placed day succeeds as
    ``created`` (or ``updated`` once already placed) except the configured
    failing dates, which report ``failed``.
    """

    def __init__(self, failing_dates: set[str] | None = None) -> None:
        self.failing_dates = failing_dates or set()
        self.calls: list[dict[str, Any]] = []
        self.outcomes: list[list[dict[str, Any]]] = []
        self._written: set[str] = set()
        self.entered = False
        self.exited = False

    async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Record the call and answer per the two-phase batch contract."""
        payload = arguments or {}
        self.calls.append(payload)
        events = payload.get("events") or []
        if payload.get("confirm_token") is None:
            return json.dumps(
                {
                    "phase": "preview",
                    "item_count": len(events),
                    "confirm_token": "preview-token-abc",
                    "events": events,
                    "note": "Re-invoke with confirm_token to place these events.",
                }
            )
        outcomes: list[dict[str, Any]] = []
        for event in events:
            if event["date"] in self.failing_dates:
                outcomes.append(
                    {
                        "external_id": event["external_id"],
                        "outcome": "failed",
                        "event_id": None,
                        "reason": "intervals rejected the event",
                    }
                )
                continue
            created_before = event["external_id"] in self._written
            outcomes.append(
                {
                    "external_id": event["external_id"],
                    "outcome": "updated" if created_before else "created",
                    "event_id": f"evt-{event['date']}",
                    "reason": None,
                }
            )
            self._written.add(event["external_id"])
        self.outcomes.append(outcomes)
        return json.dumps(
            {
                "created": sum(1 for item in outcomes if item["outcome"] == "created"),
                "updated": sum(1 for item in outcomes if item["outcome"] == "updated"),
                "failed": sum(1 for item in outcomes if item["outcome"] == "failed"),
                "outcomes": outcomes,
            }
        )

    @property
    def execute_batches(self) -> list[list[dict[str, Any]]]:
        """The event drafts of every execute-phase call, in call order."""
        return [list(call["events"]) for call in self.calls if call.get("confirm_token")]

    async def __aenter__(self) -> "WeekHub":
        """Mark the hub as entered."""
        self.entered = True
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Mark the hub as exited."""
        self.exited = True


class FailingHub(WeekHub):
    """Hub whose connection always fails."""

    async def __aenter__(self) -> "FailingHub":
        """Raise a hub error to exercise the endpoint failure path."""
        raise RuntimeError("no MCP server reachable")


class ExplodingSessionFactory:
    """Session factory whose sessions always fail (persistence outage)."""

    def __call__(self) -> Any:
        raise SQLAlchemyError("database unavailable")


# ---------------------------------------------------------------------------
# Payload helpers
# ---------------------------------------------------------------------------


def _raw_day(index: int, **overrides: Any) -> dict[str, Any]:
    """Build a raw day-slot payload for the i-th day of 2026-W41."""
    day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    hard = index in (1, 3)
    rest = index == 6
    payload: dict[str, Any] = {
        "day_of_week": day_names[index],
        "date": date.fromisocalendar(2026, 41, index + 1).isoformat(),
        "session_title": "Rest day" if rest else ("VO2 intervals" if hard else "Easy spin"),
        "focus": None if rest else ("Threshold zones" if hard else "Endurance zones"),
        "zones": [] if rest else (["Z4", "Z5"] if hard else ["Z1", "Z2"]),
        "planned_tss": 0.0 if rest else (85.0 if hard else 45.0),
        "duration_minutes": 0.0 if rest else (75.0 if hard else 60.0),
        "rest_day": rest,
    }
    payload.update(overrides)
    return payload


def _raw_week(**overrides: Any) -> dict[str, Any]:
    """Build a raw weekly-plan payload: 2 hard / 4 easy / 1 rest, 350 TSS."""
    payload: dict[str, Any] = {
        "week_id": "2026-W41",
        "title": "Base week 41",
        "summary": "Two hard days after the recovery block.",
        "rationale": "Volume capped at the availability budget.",
        "days": [_raw_day(i) for i in range(7)],
        "total_tss": 350.0,
        "hard_days": 2,
        "easy_days": 4,
    }
    payload.update(overrides)
    return payload


def _client_with(hub: WeekHub, settings: Settings) -> TestClient:
    """Build a TestClient whose hub factory returns the supplied hub."""
    app = create_app(settings)
    app.state.hub_factory = lambda _settings: hub
    return TestClient(app)


def _post_week(client: TestClient, **overrides: Any) -> Any:
    """POST a weekly plan approval request (with optional payload overrides)."""
    payload: dict[str, Any] = {"week": _raw_week(**overrides)}
    return client.post("/api/week/approve", json=payload)


def _draft_rows(db_path: Path) -> list[sqlite3.Row]:
    """Read every plan_drafts row with the stdlib driver."""
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM plan_drafts ORDER BY id").fetchall()


@pytest.fixture
def week_client(settings: Settings) -> Iterator[tuple[TestClient, WeekHub]]:
    """A TestClient paired with its recording hub."""
    hub = WeekHub()
    yield _client_with(hub, settings), hub


# ---------------------------------------------------------------------------
# Approval contract
# ---------------------------------------------------------------------------


class TestWeekApproveEndpoint:
    """POST /api/week/approve contract (AC2/AC5, #163 non-atomic writes)."""

    def test_full_approval_writes_every_day_and_approves_the_draft(
        self,
        week_client: tuple[TestClient, WeekHub],
        migrated_db: Path,
    ) -> None:
        """All 7 days succeed: 7 results, the draft row lands in approved."""
        client, hub = week_client

        response = _post_week(client)

        assert response.status_code == 200
        results = response.json()["results"]
        assert len(results) == 7
        assert all(result["success"] for result in results)
        assert [result["date"] for result in results][0] == WEEK_START
        assert hub.entered is True and hub.exited is True

        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"
        assert rows[0]["approved_at"] is not None

    def test_event_arguments_are_strictly_built(
        self, week_client: tuple[TestClient, WeekHub], migrated_db: Path
    ) -> None:
        """The batch carries the validated Intervals.icu event drafts."""
        client, hub = week_client

        _post_week(client)

        batches = hub.execute_batches
        assert len(batches) == 1
        events = batches[0]
        assert len(events) == 7
        assert all(draft["type"] == "Ride" for draft in events)
        # The batch tool pins its own local start time server-side.
        assert all("start_date_local" not in draft for draft in events)
        tuesday = next(draft for draft in events if draft["date"] == "2026-10-06")
        assert tuesday["external_id"] == "coach-web-2026-W41-tue"
        assert tuesday["category"] == "WORKOUT"
        assert tuesday["moving_time_seconds"] == 75 * 60
        assert tuesday["icu_training_load"] == 85.0
        rest_day = next(draft for draft in events if draft["date"] == "2026-10-11")
        assert rest_day["external_id"] == "coach-web-2026-W41-sun"
        assert rest_day["category"] == "NOTE"
        assert "icu_training_load" not in rest_day

    def test_partial_failure_reports_per_day_results_and_stays_submitted(
        self, settings: Settings, migrated_db: Path
    ) -> None:
        """A failing day is reported; the draft stays submitted for retry (AC5)."""
        thursday = date.fromisocalendar(2026, 41, 4).isoformat()
        hub = WeekHub(failing_dates={thursday})
        client = _client_with(hub, settings)

        response = _post_week(client)

        assert response.status_code == 200
        results = response.json()["results"]
        failed = next(result for result in results if not result["success"])
        assert failed["date"] == thursday
        # C3: the upstream failure reason never reaches the response.
        assert failed["error"] == "Calendar write failed"

        rows = _draft_rows(migrated_db)
        assert rows[0]["status"] == "submitted"
        assert rows[0]["approved_at"] is None

    def test_retry_subset_writes_only_the_failed_days(
        self, settings: Settings, migrated_db: Path
    ) -> None:
        """The retry-failed-days affordance posts only the failed dates subset."""
        thursday = date.fromisocalendar(2026, 41, 4).isoformat()
        hub = WeekHub(failing_dates={thursday})
        client = _client_with(hub, settings)
        _post_week(client)

        hub.failing_dates = set()
        retry = client.post(
            "/api/week/approve",
            json={"week": _raw_week(), "dates": [thursday]},
        )

        assert retry.status_code == 200
        results = retry.json()["results"]
        assert [result["date"] for result in results] == [thursday]
        assert all(result["success"] for result in results)
        # Only the retried day was written on the second request.
        assert hub.execute_batches[-1][0]["date"] == thursday
        assert len(hub.execute_batches) == 2
        # The draft is now fully approved.
        rows = _draft_rows(migrated_db)
        assert rows[0]["status"] == "approved"

    def test_reapproval_reuses_the_draft_row_without_duplicating(
        self,
        week_client: tuple[TestClient, WeekHub],
        migrated_db: Path,
    ) -> None:
        """Approving the same week twice keeps a single draft row (AC4)."""
        client, _ = week_client

        _post_week(client)
        _post_week(client)

        rows = _draft_rows(migrated_db)
        assert len(rows) == 1

    def test_reapproval_of_an_approved_week_short_circuits_without_tool_calls(
        self,
        week_client: tuple[TestClient, WeekHub],
        migrated_db: Path,
    ) -> None:
        """Re-approving an approved week reports success WITHOUT re-writing (F3).

        Re-issuing the batch would rewrite the calendar entries, so the
        terminal state short-circuits the endpoint before any tool call.
        """
        client, hub = week_client

        _post_week(client)
        assert len(hub.execute_batches) == 1

        again = _post_week(client)

        assert again.status_code == 200
        results = again.json()["results"]
        assert len(results) == 7
        assert all(result["success"] for result in results)
        # No additional calendar writes: the tool-call count is unchanged.
        assert len(hub.execute_batches) == 1
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_reapproval_of_a_rejected_week_promotes_and_writes(
        self, settings: Settings, migrated_db: Path
    ) -> None:
        """Re-approving a rejected week promotes it to submitted and writes (F3)."""
        week = _raw_week()
        content = build_week_markdown(WeeklyPlanDraft.model_validate(week))
        week_start, week_end = week_bounds("2026-W41")
        with sqlite3.connect(migrated_db) as conn:
            conn.execute(
                "INSERT INTO plan_drafts (week_start_date, week_end_date, content, "
                "status, created_at, updated_at) VALUES (?, ?, ?, 'rejected', 't1', 't2')",
                (week_start, week_end, content),
            )
        hub = WeekHub()
        client = _client_with(hub, settings)

        response = client.post("/api/week/approve", json={"week": week})

        assert response.status_code == 200
        assert all(result["success"] for result in response.json()["results"])
        assert len(hub.execute_batches) == 1
        assert len(hub.execute_batches[0]) == 7
        rows = _draft_rows(migrated_db)
        assert rows[0]["status"] == "approved"

    def test_a_new_proposal_supersedes_the_previous_draft(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """A revised proposal supersedes the old draft and lands as its own row.

        The clock is frozen mid-week and the revision touches a future day:
        changing an approved draft whose week is already over is an
        illegal adjustment (AC3), so the supersede chain is exercised on a
        live week.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _post_week(client)

        client.post("/api/week/approve", json={"week": _adjusted_week()})

        rows = _draft_rows(migrated_db)
        assert len(rows) == 2
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "approved"

    def test_draft_content_persists_the_markdown_rendition(
        self, week_client: tuple[TestClient, WeekHub], migrated_db: Path
    ) -> None:
        """The draft row carries the readable Markdown rendition of the week."""
        client, _ = week_client

        _post_week(client)

        rows = _draft_rows(migrated_db)
        assert "# Base week 41" in rows[0]["content"]
        assert rows[0]["week_start_date"] == WEEK_START
        assert rows[0]["week_end_date"] == "2026-10-11"


# ---------------------------------------------------------------------------
# Validation & failure surfaces
# ---------------------------------------------------------------------------


class TestWeekApproveValidation:
    """Boundary validation — generic 422 detail, no input echo (nLPD #142)."""

    def test_missing_week_is_rejected(self, settings: Settings) -> None:
        """A payload without a week is rejected with 422."""
        client = _client_with(WeekHub(), settings)

        response = client.post("/api/week/approve", json={})

        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid request payload"

    def test_invalid_week_id_is_rejected(self, settings: Settings) -> None:
        """An invalid ISO week identifier is rejected with 422."""
        client = _client_with(WeekHub(), settings)

        response = _post_week(client, week_id="week-41")

        assert response.status_code == 422

    def test_inconsistent_summary_counts_are_rejected(self, settings: Settings) -> None:
        """A draft whose declared totals contradict its days is rejected."""
        client = _client_with(WeekHub(), settings)

        response = _post_week(client, total_tss=999.0)

        assert response.status_code == 422

    def test_retry_dates_outside_the_week_are_rejected(self, settings: Settings) -> None:
        """A dates subset naming a foreign day is rejected with 422."""
        client = _client_with(WeekHub(), settings)

        response = client.post(
            "/api/week/approve",
            json={"week": _raw_week(), "dates": ["2027-01-01"]},
        )

        assert response.status_code == 422

    def test_more_than_seven_retry_dates_are_rejected(self, settings: Settings) -> None:
        """The dates subset is capped at 7 entries."""
        dates = [date.fromisocalendar(2026, 41, i + 1).isoformat() for i in range(7)]
        client = _client_with(WeekHub(), settings)

        response = client.post(
            "/api/week/approve",
            json={"week": _raw_week(), "dates": [*dates, "2026-10-12"]},
        )

        assert response.status_code == 422

    def test_hub_connection_failure_returns_503(self, settings: Settings) -> None:
        """An unreachable MCP hub is surfaced as a 503."""
        client = _client_with(FailingHub(), settings)

        response = _post_week(client)

        assert response.status_code == 503

    def test_persistence_failure_returns_503_without_writing(self, settings: Settings) -> None:
        """A persistence outage fails the request before any calendar write."""
        hub = WeekHub()
        app = create_app(settings)
        app.state.hub_factory = lambda _settings: hub
        app.state.db_session_factory = ExplodingSessionFactory()
        client = TestClient(app)

        response = _post_week(client)

        assert response.status_code == 503
        assert hub.calls == []

    def test_approval_requires_authentication(self, auth_client: TestClient) -> None:
        """The approval endpoint is auth-gated like the other write endpoints."""
        response = auth_client.post("/api/week/approve", json={"week": _raw_week()})

        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Mid-week adjustment path (#225 — AC1..AC6, D2/D3, C1/C2)
# ---------------------------------------------------------------------------


def _adjusted_week() -> dict[str, Any]:
    """An adjusted draft of 2026-W41: Saturday replaces its session."""
    payload = _raw_week()
    payload["days"][5] = _raw_day(
        5, session_title="Recovery roll", planned_tss=30.0, duration_minutes=45.0
    )
    payload["total_tss"] = 335.0
    return payload


def _seed_approved_week(db_path: Path, week: dict[str, Any]) -> None:
    """Persist an already-approved draft row for the given week payload."""
    content = build_week_markdown(WeeklyPlanDraft.model_validate(week))
    week_start, week_end = week_bounds(week["week_id"])
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO plan_drafts (week_start_date, week_end_date, content, "
            "status, approved_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'approved', 't0', 't0', 't0')",
            (week_start, week_end, content),
        )


@pytest.fixture
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the endpoint's clock inside the current ISO week (D3.5)."""
    monkeypatch.setattr("coach_web.app.date", _FrozenDate)


class TestWeekAdjustmentApproval:
    """POST /api/week/approve on an already-approved current week (#225)."""

    def test_adjusting_future_days_writes_only_future_events(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC3/D3.4: the batch contains only days strictly after today."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())

        response = client.post("/api/week/approve", json={"week": _adjusted_week()})

        assert response.status_code == 200
        results = response.json()["results"]
        assert [result["date"] for result in results] == [
            "2026-10-09",
            "2026-10-10",
            "2026-10-11",
        ]
        assert all(result["success"] for result in results)
        batches = hub.execute_batches
        assert len(batches) == 1
        assert all(draft["date"] > ADJUSTMENT_TODAY.isoformat() for draft in batches[0])

    def test_adjusting_future_days_supersedes_and_reapproves(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC2: the adjusted approval supersedes the approved row, history retained."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())

        response = client.post("/api/week/approve", json={"week": _adjusted_week()})

        assert response.status_code == 200
        rows = _draft_rows(migrated_db)
        assert len(rows) == 2
        assert rows[0]["status"] == "superseded"
        assert rows[1]["status"] == "approved"
        assert rows[1]["approved_at"] is not None
        assert "Recovery roll" in rows[1]["content"]

    def test_altering_a_past_day_is_rejected_with_a_static_body(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC3/C1/C2: rewriting a past day fails closed before any write."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())
        adjusted = _adjusted_week()
        adjusted["days"][0] = _raw_day(0, session_title="Rewritten Monday")

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_PAST_DAYS_DETAIL
        # Fail-closed ordering: no calendar write, no draft supersede.
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_explicit_dates_cannot_target_a_past_day(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC3: even an unchanged past day is never rewritten on request."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())

        response = client.post(
            "/api/week/approve",
            json={"week": _adjusted_week(), "dates": ["2026-10-05"]},
        )

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_PAST_DAYS_DETAIL
        assert hub.execute_batches == []

    def test_an_empty_write_set_is_rejected(
        self,
        settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        migrated_db: Path,
    ) -> None:
        """AC3: an adjustment leaving no day strictly after today is rejected."""

        class _FrozenSunday(date):
            @classmethod
            def today(cls) -> "_FrozenSunday":
                return _FrozenSunday(2026, 10, 11)

        monkeypatch.setattr("coach_web.app.date", _FrozenSunday)
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())
        adjusted = _raw_week()
        # Drop a PAST day so the content differs without touching future days
        # (none exist: every 2026-W41 day is on or before the frozen Sunday).
        adjusted["days"] = [day for day in adjusted["days"] if day["date"] != "2026-10-07"]
        adjusted["total_tss"] = 305.0
        adjusted["easy_days"] = 3

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_EMPTY_WRITE_SET_DETAIL
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_an_adjustment_over_the_weekly_availability_is_rejected(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC6/D2: Σ duration_minutes ≤ weekly_availability_hours × 60."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())
        client.put(
            "/api/objectives",
            json={
                "primary_goal": {"objective_type": "outcome", "title": "A mountain finish"},
                "weekly_availability_hours": 5.0,
            },
        )

        response = client.post("/api/week/approve", json={"week": _adjusted_week()})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_AVAILABILITY_DETAIL
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_without_an_active_objective_the_availability_gate_is_skipped(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """D2 graceful degrade: no active objective → no availability gate."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())
        adjusted = _raw_week()
        adjusted["days"][5] = _raw_day(
            5, session_title="Big ride", planned_tss=120.0, duration_minutes=300.0
        )
        adjusted["total_tss"] = 425.0

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 200
        assert all(result["success"] for result in response.json()["results"])

    def test_reapproval_after_a_partial_failure_re_upserts_the_same_external_ids(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC4/AC5: retry re-upserts the upsert keys — no duplicates, approved end state.

        The failed attempt (adjustment path) writes the FUTURE set only; the
        retry (the approved row is gone, so the plain path runs with
        ``dates=None``) re-upserts the WHOLE week with the same deterministic
        ``coach-web-{week}-{day}`` keys — the failed day is retried under its
        original key and already-written days come back as ``updated``.
        """
        saturday = "2026-10-10"
        hub = WeekHub(failing_dates={saturday})
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())

        first = client.post("/api/week/approve", json={"week": _adjusted_week()})
        assert first.status_code == 200
        failed = next(result for result in first.json()["results"] if not result["success"])
        assert failed["date"] == saturday
        assert failed["error"] == "Calendar write failed"
        rows = _draft_rows(migrated_db)
        assert [row["status"] for row in rows] == ["superseded", "submitted"]

        hub.failing_dates = set()
        retry = client.post("/api/week/approve", json={"week": _adjusted_week()})

        assert retry.status_code == 200
        assert all(result["success"] for result in retry.json()["results"])
        batches = hub.execute_batches
        assert len(batches) == 2
        first_keys = {draft["external_id"] for draft in batches[0]}
        assert first_keys == {
            "coach-web-2026-W41-fri",
            "coach-web-2026-W41-sat",
            "coach-web-2026-W41-sun",
        }
        # The failed day is retried under its ORIGINAL upsert key...
        assert "coach-web-2026-W41-sat" in {d["external_id"] for d in batches[1]}
        # ...and every key written by the first attempt is re-upserted as
        # ``updated`` — never duplicated.
        first_outcomes = {item["external_id"]: item["outcome"] for item in hub.outcomes[0]}
        retry_outcomes = {item["external_id"]: item["outcome"] for item in hub.outcomes[1]}
        assert first_outcomes == {
            "coach-web-2026-W41-fri": "created",
            "coach-web-2026-W41-sat": "failed",
            "coach-web-2026-W41-sun": "created",
        }
        assert retry_outcomes["coach-web-2026-W41-sun"] == "updated"
        assert retry_outcomes["coach-web-2026-W41-sat"] == "created"
        rows = _draft_rows(migrated_db)
        assert len(rows) == 2
        assert rows[1]["status"] == "approved"


# ---------------------------------------------------------------------------
# Objective-preserving guardrails (#233 — AC1/AC2/AC3)
# ---------------------------------------------------------------------------


def _season_week() -> dict[str, Any]:
    """A 2026-W41 week with a distinct session title per day (guardrails).

    2 hard / 5 easy / 0 rest, 380 TSS — unique titles make moved sessions
    unambiguous for the guardrail scenarios.
    """
    return {
        "week_id": "2026-W41",
        "title": "Build week 41",
        "summary": "Two hard days inside the build phase.",
        "rationale": "Key sessions land inside the phase window.",
        "days": [
            _raw_day(0, session_title="Monday endurance"),
            _raw_day(1, session_title="Tuesday intervals"),
            _raw_day(2, session_title="Wednesday endurance"),
            _raw_day(3, session_title="Thursday intervals"),
            _raw_day(4, session_title="Friday endurance"),
            _raw_day(5, session_title="Saturday endurance"),
            _raw_day(
                6,
                session_title="Sunday spin",
                focus="Endurance zones",
                zones=["Z1", "Z2"],
                planned_tss=30.0,
                duration_minutes=45.0,
                rest_day=False,
            ),
        ],
        "total_tss": 380.0,
        "hard_days": 2,
        "easy_days": 5,
    }


def _rest_day(index: int) -> dict[str, Any]:
    """A rest-day slot for the i-th day of 2026-W41."""
    return _raw_day(
        index,
        session_title="Rest day",
        focus=None,
        zones=[],
        planned_tss=0.0,
        duration_minutes=0.0,
        rest_day=True,
    )


def _seed_season(
    client: TestClient,
    *,
    build_end: str,
    taper_start: str,
    taper_end: str,
    target_date: str,
) -> None:
    """Seed an active objective and a build→taper season via the owner APIs.

    The build phase always starts 2026-09-28 so it contains the approved
    week's Monday (2026-10-05); the taper follows contiguously.
    """
    saved = client.put(
        "/api/objectives",
        json={
            "primary_goal": {
                "objective_type": "outcome",
                "title": "A mountain finish",
                "target_date": target_date,
            },
            "weekly_availability_hours": 20.0,
        },
    )
    assert saved.status_code == 200
    plan = client.post(
        "/api/periodization/approve",
        json={
            "plan": {
                "phases": [
                    {
                        "phase_type": "build",
                        "name": "Build",
                        "start_date": "2026-09-28",
                        "end_date": build_end,
                    },
                    {
                        "phase_type": "taper",
                        "name": "Taper",
                        "start_date": taper_start,
                        "end_date": taper_end,
                    },
                ]
            }
        },
    )
    assert plan.status_code == 200


class TestObjectiveGuardrails:
    """Adjustment guardrails against the season structure (#233)."""

    def test_a_session_moved_across_the_phase_boundary_is_rejected(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC1 ruling: a moved session may not leave its original phase.

        The build phase ends Saturday 2026-10-10; Friday's session
        (2026-10-09, the build) moved onto Sunday (2026-10-11, the taper)
        leaves its original date's phase → 422 with the static body,
        before any calendar write or draft supersede.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _season_week())
        _seed_season(
            client,
            build_end="2026-10-10",
            taper_start="2026-10-11",
            taper_end="2026-10-18",
            target_date="2026-10-14",
        )
        adjusted = _season_week()
        adjusted["days"][4] = _rest_day(4)
        adjusted["days"][6] = _raw_day(
            6,
            session_title="Friday endurance",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=45.0,
            duration_minutes=60.0,
            rest_day=False,
        )
        adjusted["total_tss"] = 350.0
        adjusted["easy_days"] = 4

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_PHASE_BOUNDARY_DETAIL
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_a_session_moved_back_across_the_phase_boundary_is_rejected(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC1 ruling: a cross-phase move is rejected in EITHER direction.

        The week spans the season — Monday–Saturday sit in the build phase,
        Sunday (2026-10-11) in the taper. Moving Sunday's session back onto
        Saturday (2026-10-10, the build) leaves the session's original
        date's phase: the new date must fall in the SAME phase as the
        original date → 422 with the static body, before any calendar
        write or draft supersede.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _season_week())
        _seed_season(
            client,
            build_end="2026-10-10",
            taper_start="2026-10-11",
            taper_end="2026-10-18",
            target_date="2026-10-14",
        )
        adjusted = _season_week()
        adjusted["days"][5] = _raw_day(
            5,
            session_title="Sunday spin",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=30.0,
            duration_minutes=45.0,
            rest_day=False,
        )
        adjusted["days"][6] = _rest_day(6)
        adjusted["total_tss"] = 335.0
        adjusted["easy_days"] = 4

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_PHASE_BOUNDARY_DETAIL
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_a_session_moved_within_the_phase_is_accepted(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC1: swapping two future sessions inside the phase window approves."""
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _season_week())
        _seed_season(
            client,
            build_end="2026-10-10",
            taper_start="2026-10-11",
            taper_end="2026-10-18",
            target_date="2026-10-14",
        )
        adjusted = _season_week()
        adjusted["days"][4] = _raw_day(4, session_title="Saturday endurance")
        adjusted["days"][5] = _raw_day(5, session_title="Friday endurance")

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 200
        results = response.json()["results"]
        assert [result["date"] for result in results] == [
            "2026-10-09",
            "2026-10-10",
            "2026-10-11",
        ]
        assert all(result["success"] for result in results)

    def test_a_same_date_intensity_edit_is_not_a_boundary_violation(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC1 ruling: same-date intensity edits are not moves — unaffected.

        Sunday (2026-10-11) lies in the taper portion, outside the build
        phase containing the week's start; lowering its TSS in place keeps
        the session on its date, so the boundary rule never fires.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _season_week())
        _seed_season(
            client,
            build_end="2026-10-10",
            taper_start="2026-10-11",
            taper_end="2026-10-18",
            target_date="2026-10-14",
        )
        adjusted = _season_week()
        adjusted["days"][6] = _raw_day(
            6,
            session_title="Sunday spin",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=20.0,
            duration_minutes=45.0,
            rest_day=False,
        )
        adjusted["total_tss"] = 370.0

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 200
        assert all(result["success"] for result in response.json()["results"])

    def test_a_session_placed_after_the_peak_date_is_rejected(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC2: an adjustment placing a session past the target date is rejected.

        The whole week sits inside the build phase (no boundary crossing),
        but the objective's peak date is Friday 2026-10-09: moving Friday's
        session onto Saturday (2026-10-10) places it after the peak → 422
        with the static body, before any calendar write.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _season_week())
        _seed_season(
            client,
            build_end="2026-10-11",
            taper_start="2026-10-12",
            taper_end="2026-10-18",
            target_date="2026-10-09",
        )
        adjusted = _season_week()
        adjusted["days"][4] = _rest_day(4)
        adjusted["days"][5] = _raw_day(
            5,
            session_title="Friday endurance",
            focus="Endurance zones",
            zones=["Z1", "Z2"],
            planned_tss=45.0,
            duration_minutes=60.0,
            rest_day=False,
        )
        adjusted["total_tss"] = 335.0
        adjusted["easy_days"] = 4

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == WEEK_ADJUSTMENT_PEAK_DATE_DETAIL
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_an_adjustment_with_a_tampered_total_tss_is_rejected(
        self,
        settings: Settings,
        frozen_today: None,
        migrated_db: Path,
    ) -> None:
        """AC3 pin: the weekly TSS is re-derived server-side on the adjustment path.

        The declared ``total_tss`` never reaches approval: the draft model
        re-verifies it against the day sum at the request boundary (the #165
        posture), so a tampered total is rejected with the generic static
        body before any gate or write runs.
        """
        hub = WeekHub()
        client = _client_with(hub, settings)
        _seed_approved_week(migrated_db, _raw_week())
        adjusted = _adjusted_week()
        adjusted["total_tss"] = 999.0

        response = client.post("/api/week/approve", json={"week": adjusted})

        assert response.status_code == 422
        assert response.json()["detail"] == "Invalid request payload"
        assert hub.execute_batches == []
        rows = _draft_rows(migrated_db)
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"
