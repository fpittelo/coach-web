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

from coach_web.app import create_app
from coach_web.config import Settings
from coach_web.microcycle import WeeklyPlanDraft, build_week_markdown, week_bounds
from coach_web.plan_approval import EVENT_START_TIME

WEEK_START = "2026-10-05"
"""Monday of ISO week 2026-W41 (pinned literal — the tests' calendar anchor)."""


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class WeekHub:
    """Fake hub succeeding per day except the configured failing dates."""

    def __init__(self, failing_dates: set[str] | None = None) -> None:
        self.failing_dates = failing_dates or set()
        self.calls: list[dict[str, Any]] = []
        self.entered = False
        self.exited = False

    async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Record the call; fail the days configured as failing."""
        payload = arguments or {}
        self.calls.append(payload)
        day_date = str(payload.get("start_date_local", ""))[:10]
        if day_date in self.failing_dates:
            return json.dumps({"error": "intervals rejected the event"})
        return json.dumps({"id": f"evt-{day_date}", "status": "created"})

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
        """Every day write carries the validated Intervals.icu event shape."""
        client, hub = week_client

        _post_week(client)

        assert len(hub.calls) == 7
        assert all(call["type"] == "Ride" for call in hub.calls)
        tuesday = next(
            call for call in hub.calls if call["start_date_local"].startswith("2026-10-06")
        )
        assert tuesday["category"] == "WORKOUT"
        assert tuesday["start_date_local"] == f"2026-10-06T{EVENT_START_TIME}"
        assert tuesday["moving_time_seconds"] == 75 * 60
        assert tuesday["icu_training_load"] == 85.0
        rest_day = next(
            call for call in hub.calls if call["start_date_local"].startswith("2026-10-11")
        )
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
        assert "rejected" in failed["error"]

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
        assert len(hub.calls) == 8
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

        ``intervals_create_event`` is not idempotent: re-issuing the events
        would duplicate calendar entries, so the terminal state short-circuits
        the endpoint before any tool call.
        """
        client, hub = week_client

        _post_week(client)
        assert len(hub.calls) == 7

        again = _post_week(client)

        assert again.status_code == 200
        results = again.json()["results"]
        assert len(results) == 7
        assert all(result["success"] for result in results)
        # No additional calendar writes: the tool-call count is unchanged.
        assert len(hub.calls) == 7
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
        assert len(hub.calls) == 7
        rows = _draft_rows(migrated_db)
        assert rows[0]["status"] == "approved"

    def test_a_new_proposal_supersedes_the_previous_draft(
        self, week_client: tuple[TestClient, WeekHub], migrated_db: Path
    ) -> None:
        """A revised proposal supersedes the old draft and lands as its own row."""
        client, _ = week_client

        _post_week(client)
        _post_week(client, title="Revised week 41")

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
