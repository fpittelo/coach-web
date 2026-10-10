"""Periodization API contract: POST /api/periodization/approve + digest (#167, #182).

The #182 PO pivot moved phase ownership from the athlete to the coach: the
manual settings editor is gone, so the athlete-facing GET/PUT
``/api/periodization`` endpoints are removed with it. The single write path
is now the approval gate ``POST /api/periodization/approve`` — the browser
posts the coach-proposed plan from the inline card, the server re-validates
it (strict Pydantic boundary + the #167 coverage cross-check against the
active objective's target date) and persists through the existing repository.
Malformed plans are rejected 422. Since #181 the settings approval endpoint
answers with the owner-safe structured reason body ``{code, message}`` from
the fixed vocabulary (:mod:`coach_web.errors`) — the authenticated owner
learns WHY the approval failed (overlap, coverage, dates, values) — while
the chat/model-facing surfaces keep the strict #142 generic detail. The
coach digest gains the current phase, the countdown and the load hint when
phases exist, and the #166 graceful-degradation boundary is preserved: any
persistence failure — including a phase row tampered into a Pydantic-invalid
shape — yields no digest at all and never breaks the agent turn.
"""

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coach_web.agent import DEFAULT_SYSTEM_PROMPT, AgentEvent
from coach_web.app import create_app
from coach_web.db import build_db_engine, build_session_factory
from coach_web.objectives import get_active_objective_row
from coach_web.periodization import (
    PeriodizationPlan,
    list_phases,
    replace_phases,
)

GENERIC_422_DETAIL = "Invalid request payload"


def _goal_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective goal payload with optional overrides."""
    payload: dict[str, Any] = {
        "objective_type": "outcome",
        "title": "Race the Alpenbrevet 2027",
        "description": None,
        "target_metric": None,
        "target_value": None,
        "target_date": "2027-09-11",
    }
    payload.update(overrides)
    return payload


def _profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective profile payload with optional overrides."""
    payload: dict[str, Any] = {
        "primary_goal": _goal_payload(),
        "secondary_goals": [],
        "weekly_availability_hours": 8.5,
        "priority_disciplines": ["road"],
    }
    payload.update(overrides)
    return payload


def _phase_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid periodization phase payload with optional overrides."""
    payload: dict[str, Any] = {
        "phase_type": "base",
        "name": "Aerobic base",
        "start_date": "2026-10-01",
        "end_date": "2026-12-15",
        "focus": "Long rides, endurance",
        "weekly_hours_target": 8.0,
        "notes": "Coach note",
    }
    payload.update(overrides)
    return payload


def _plan_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid periodization plan payload with optional overrides."""
    payload: dict[str, Any] = {
        "phases": [
            _phase_payload(),
            _phase_payload(
                phase_type="build",
                name="Threshold build",
                start_date="2026-12-16",
                end_date="2027-09-11",
                focus=None,
                weekly_hours_target=None,
                notes=None,
            ),
        ]
    }
    payload.update(overrides)
    return payload


async def _seed_phases(db_path: Path, plan_payload: dict[str, Any]) -> None:
    """Persist phases directly through the repository (test seeding helper).

    The athlete-facing PUT write path is gone (#182); tests seed the phase
    table the same way the approval endpoint does — through the #167
    repository — over a dedicated engine bound to the per-test database file.
    """
    engine = build_db_engine(str(db_path))
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            row = await get_active_objective_row(session)
            if row is None:
                raise RuntimeError("seeding phases requires a persisted active objective")
            await replace_phases(session, row.id, PeriodizationPlan.model_validate(plan_payload))
    finally:
        await engine.dispose()


def _seed_phases_sync(db_path: Path, plan_payload: dict[str, Any]) -> None:
    """Run the async seeding helper on a private loop (sync-test bridge)."""
    asyncio.run(_seed_phases(db_path, plan_payload))


async def _persisted_phases(db_path: Path) -> list[dict[str, Any]]:
    """Read the active objective's phases straight from the repository."""
    engine = build_db_engine(str(db_path))
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            row = await get_active_objective_row(session)
            if row is None:
                return []
            phases = await list_phases(session, row.id)
            return [phase.model_dump() for phase in phases]
    finally:
        await engine.dispose()


def _persisted_phases_sync(db_path: Path) -> list[dict[str, Any]]:
    """Run the async read-back helper on a private loop (sync-test bridge)."""
    return asyncio.run(_persisted_phases(db_path))


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
        if not line.startswith("data:"):
            continue
        try:
            parsed = json.loads(line[len("data:") :].strip())
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("type") == "token":
            text = parsed.get("data", {}).get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _streamed_text(client: TestClient) -> str:
    """Consume the SSE stream and return the concatenated token texts."""
    with client.stream(
        "POST", "/api/agent/stream", json={"message": "hi", "history": []}
    ) as response:
        body = response.read().decode("utf-8")
    return _extract_token_texts(body)


class TestPeriodizationApprovalEndpoint:
    """POST /api/periodization/approve contract — the single write path (#182)."""

    def test_approve_without_an_objective_is_rejected_generically(self, migrated_db: Path) -> None:
        """Phases hang off the active objective; without one the approval is 422.

        The no-objective case has no home in the #181 reason vocabulary (the
        plan is not invalid — there is nothing to attach it to), so it keeps
        the generic ``detail`` body; the UI's unknown/missing-code fallback
        renders the generic text (fail-safe).
        """
        with TestClient(create_app()) as client:
            response = client.post("/api/periodization/approve", json={"plan": _plan_payload()})

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL

    def test_approve_persists_the_plan(self, migrated_db: Path, db_path: Path) -> None:
        """A valid approval persists the phases; the repository reads them back."""
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.post("/api/periodization/approve", json={"plan": _plan_payload()})

        assert response.status_code == 200
        saved = response.json()
        assert saved["objective_id"] == 1
        assert saved["phases"][0]["name"] == "Aerobic base"
        assert saved["phases"][0]["focus"] == "Long rides, endurance"
        assert saved["phases"][0]["weekly_hours_target"] == 8.0
        assert saved["phases"][0]["notes"] == "Coach note"
        assert saved["phases"][1]["phase_type"] == "build"

        persisted = _persisted_phases_sync(db_path)
        assert persisted == saved["phases"]

    def test_approve_replaces_a_previous_plan(self, migrated_db: Path, db_path: Path) -> None:
        """A second approval replaces the phase set (repository PUT semantics)."""
        updated = _plan_payload(
            phases=[
                _phase_payload(
                    phase_type="peak",
                    name="Race peak",
                    start_date="2027-08-01",
                    end_date="2027-09-11",
                    focus=None,
                    weekly_hours_target=None,
                    notes=None,
                )
            ]
        )
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            client.post("/api/periodization/approve", json={"plan": _plan_payload()})
            response = client.post("/api/periodization/approve", json={"plan": updated})

        assert response.status_code == 200
        persisted = _persisted_phases_sync(db_path)
        assert [phase["name"] for phase in persisted] == ["Race peak"]

    def test_approve_with_an_empty_plan_clears_the_phases(
        self, migrated_db: Path, db_path: Path
    ) -> None:
        """Approving an empty plan removes every persisted phase (#167 semantics)."""
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            client.post("/api/periodization/approve", json={"plan": _plan_payload()})
            clear_response = client.post(
                "/api/periodization/approve", json={"plan": {"phases": []}}
            )

        assert clear_response.status_code == 200
        assert _persisted_phases_sync(db_path) == []

    def test_approve_accepts_a_plan_without_a_target_date(self, migrated_db: Path) -> None:
        """Without an objective target date there is no coverage cross-check."""
        profile = _profile_payload(primary_goal=_goal_payload(target_date=None))
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=profile)
            response = client.post("/api/periodization/approve", json={"plan": _plan_payload()})

        assert response.status_code == 200

    def test_approve_rejects_overlapping_phases_with_the_overlap_reason(
        self, migrated_db: Path
    ) -> None:
        """AC1 #181: overlapping phases (a shared day) carry the overlap reason."""
        overlapping = _plan_payload(
            phases=[
                _phase_payload(end_date="2026-12-15"),
                _phase_payload(
                    phase_type="build",
                    name="Threshold build",
                    start_date="2026-12-15",
                    end_date="2027-09-11",
                    focus=None,
                    weekly_hours_target=None,
                    notes=None,
                ),
            ]
        )
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.post("/api/periodization/approve", json={"plan": overlapping})

        assert response.status_code == 422
        assert response.json() == {
            "code": "overlap",
            "message": "Phases must not overlap — each phase starts the day after the "
            "previous one ends.",
        }
        # nLPD: the rejected input is never echoed — not even in the reason body.
        assert "2026-12-15" not in response.text

    def test_approve_rejects_a_plan_not_covering_the_target_date(self, migrated_db: Path) -> None:
        """AC1 #181: a plan ending before the target date carries the coverage reason.

        This is the live case from the issue: the coverage cross-check's reason
        reaches the owner instead of the generic "check the dates" mask.
        """
        short_plan = _plan_payload(
            phases=[
                _phase_payload(
                    start_date="2026-10-01",
                    end_date="2026-12-15",
                    focus=None,
                    weekly_hours_target=None,
                    notes=None,
                )
            ]
        )
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.post("/api/periodization/approve", json={"plan": short_plan})

        assert response.status_code == 422
        assert response.json() == {
            "code": "coverage_target_date",
            "message": "Phases must span your objective's target date — extend the last "
            "phase to reach it.",
        }
        # nLPD: neither the dates nor the raw validator text are echoed — the
        # fixed friendly message is the only "target date" occurrence.
        assert "2026-12-15" not in response.text
        assert "must cover the window" not in response.text
        assert response.text.count("target date") == 1

    def test_approve_rejects_malformed_phase_payload_with_a_reason_code(
        self, migrated_db: Path
    ) -> None:
        """AC1 #181: a phase_type outside the enum carries the invalid_values reason."""
        malformed = _plan_payload(phases=[_phase_payload(phase_type="fantasy", notes=None)])
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.post("/api/periodization/approve", json={"plan": malformed})

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }
        assert "fantasy" not in response.text

    def test_approve_rejects_an_inverted_date_range_with_the_dates_reason(
        self, migrated_db: Path
    ) -> None:
        """AC1 #181: start_date after end_date carries the invalid_dates reason."""
        inverted = _plan_payload(
            phases=[
                _phase_payload(
                    start_date="2026-12-16",
                    end_date="2026-12-15",
                    focus=None,
                    weekly_hours_target=None,
                    notes=None,
                )
            ]
        )
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.post("/api/periodization/approve", json={"plan": inverted})

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_dates",
            "message": "One or more dates are invalid.",
        }

    def test_removed_athlete_endpoints_return_404(self, migrated_db: Path) -> None:
        """The #182 pivot removed the athlete-facing GET/PUT periodization paths."""
        with TestClient(create_app()) as client:
            get_response = client.get("/api/periodization")
            put_response = client.put("/api/periodization", json={"plan": _plan_payload()})

        assert get_response.status_code == 404
        assert put_response.status_code == 404


class TestPeriodizationAuthGating:
    """The periodization approval endpoint sits behind the whitelist boundary."""

    def test_approve_requires_authentication(self, auth_client: TestClient) -> None:
        """POST /api/periodization/approve returns 401 without a session."""
        response = auth_client.post("/api/periodization/approve", json={"plan": _plan_payload()})

        assert response.status_code == 401


class TestSystemPromptPhaseInjection:
    """The coach system prompt gains the phase state when phases exist (AC2)."""

    def _seed_plan_relative_to_today(self, client: TestClient, db_path: Path) -> None:
        """Persist an objective and a phase plan around the real clock.

        The phase spans [today-5, today+300] and the objective target sits at
        today+300, so the phase contains "today" across a midnight rollover
        between setup and request. Exact countdowns are pinned by the
        frozen-clock domain tests; this integration test asserts presence.
        Seeding rides the repository directly — the athlete PUT path is gone
        (#182).
        """
        today = date.today()
        target = (today + timedelta(days=300)).isoformat()
        client.put(
            "/api/objectives", json=_profile_payload(primary_goal=_goal_payload(target_date=target))
        )
        _seed_phases_sync(
            db_path,
            {
                "phases": [
                    _phase_payload(
                        start_date=(today - timedelta(days=5)).isoformat(),
                        end_date=target,
                        notes=None,
                    )
                ]
            },
        )

    def test_digest_contains_the_phase_state(self, migrated_db: Path, db_path: Path) -> None:
        """A persisted plan surfaces the current phase, hint and countdown."""
        with _probe_client() as client:
            self._seed_plan_relative_to_today(client, db_path)
            streamed = _streamed_text(client)

        assert "Current phase: Aerobic base (base)" in streamed
        assert "Phase load focus: aerobic base, high volume, low intensity" in streamed
        assert "Days to target:" in streamed

    def test_digest_reports_no_current_phase_outside_the_plan(
        self, migrated_db: Path, db_path: Path
    ) -> None:
        """A plan that ended before today renders the no-current-phase line."""
        today = date.today()
        target = (today - timedelta(days=10)).isoformat()
        with _probe_client() as client:
            client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(target_date=target)),
            )
            _seed_phases_sync(
                db_path,
                {
                    "phases": [
                        _phase_payload(
                            start_date=(today - timedelta(days=300)).isoformat(),
                            end_date=(today - timedelta(days=5)).isoformat(),
                            notes=None,
                        )
                    ]
                },
            )
            streamed = _streamed_text(client)

        assert "Current phase: none (plan spans " in streamed
        assert "Phase load focus" not in streamed

    def test_digest_without_phases_stays_objectives_only(self, migrated_db: Path) -> None:
        """No persisted plan leaves the digest at the #166 objectives state."""
        with _probe_client() as client:
            client.put("/api/objectives", json=_profile_payload())
            streamed = _streamed_text(client)

        assert "Athlete objectives" in streamed
        assert "Current phase" not in streamed

    def test_digest_degrades_when_a_stored_phase_row_is_corrupt(
        self, migrated_db: Path, db_path: Path
    ) -> None:
        """A Pydantic-invalid phase row yields no digest; the stream survives.

        The #166 boundary contract extended: a phase row tampered outside the
        API (raw SQL bypasses nothing here — the blank name has no CHECK)
        fails phase re-validation with ``pydantic.ValidationError``. The
        digest boundary degrades to no-digest like every other persistence
        failure, never breaking the agent turn.
        """
        with _probe_client() as client:
            client.put("/api/objectives", json=_profile_payload())
            _seed_phases_sync(db_path, _plan_payload())
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE periodization_phases SET name = ''")
            streamed = _streamed_text(client)

        assert streamed == DEFAULT_SYSTEM_PROMPT


class TestPeriodizationOpenApiContract:
    """The periodization approval endpoint is documented in the OpenAPI schema."""

    def test_approval_path_is_registered_and_athlete_paths_are_gone(
        self, migrated_db: Path
    ) -> None:
        """POST /api/periodization/approve is registered; GET/PUT /api/periodization are not."""
        with TestClient(create_app()) as client:
            schema = client.get("/openapi.json").json()

        assert "/api/periodization" not in schema["paths"]
        assert "/api/periodization/approve" in schema["paths"]
        assert set(schema["paths"]["/api/periodization/approve"]) == {"post"}

    def test_extractor_skips_non_json_frames(self) -> None:
        """The SSE text extractor never leaks non-JSON frames into assertions."""
        body = (
            "data: not-json at all\n"
            'data: {"type":"done","data":{"message":"leak","iterations":1}}\n'
            'data: {"type":"token","data":{"text":"kept"}}\n'
        )

        assert _extract_token_texts(body) == "kept"
