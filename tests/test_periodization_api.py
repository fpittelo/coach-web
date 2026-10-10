"""Periodization API contract: GET/PUT /api/periodization + digest extension (#167).

The phases hang off the active objective (MADR-008 FK CASCADE): GET returns
the active objective's ordered phases (empty when absent), PUT replaces the
set wholesale after the coverage cross-check against the objective's target
date. Malformed plans are rejected 422 with a generic detail (nLPD #142
precedent — validation detail stays server-side). The coach digest gains the
current phase, the countdown and the load hint when phases exist, and the
#166 graceful-degradation boundary is preserved: any persistence failure —
including a phase row tampered into a Pydantic-invalid shape — yields no
digest at all and never breaks the agent turn.
"""

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


class TestPeriodizationEndpoints:
    """GET/PUT /api/periodization contract (AC3/AC4)."""

    def test_get_returns_empty_plan_without_an_objective(self, migrated_db: Path) -> None:
        """No active objective reads back as a null id and no phases."""
        with TestClient(create_app()) as client:
            response = client.get("/api/periodization")

        assert response.status_code == 200
        assert response.json() == {"objective_id": None, "phases": []}

    def test_put_without_an_objective_is_rejected_generically(self, migrated_db: Path) -> None:
        """Phases hang off the active objective; without one the PUT is 422."""
        with TestClient(create_app()) as client:
            response = client.put("/api/periodization", json=_plan_payload())

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL

    def test_put_persists_and_get_returns_the_plan(self, migrated_db: Path) -> None:
        """A valid PUT persists the phases; GET reads them back intact."""
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            put_response = client.put("/api/periodization", json=_plan_payload())
            get_response = client.get("/api/periodization")

        assert put_response.status_code == 200
        saved = put_response.json()
        assert saved["objective_id"] == 1
        assert saved["phases"][0]["name"] == "Aerobic base"
        assert saved["phases"][0]["focus"] == "Long rides, endurance"
        assert saved["phases"][0]["weekly_hours_target"] == 8.0
        assert saved["phases"][0]["notes"] == "Coach note"
        assert saved["phases"][1]["phase_type"] == "build"

        assert get_response.status_code == 200
        assert get_response.json() == saved

    def test_put_replaces_a_previous_plan(self, migrated_db: Path) -> None:
        """A second PUT replaces the phase set (upsert semantics)."""
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
            client.put("/api/periodization", json=_plan_payload())
            put_response = client.put("/api/periodization", json=updated)
            get_response = client.get("/api/periodization")

        assert put_response.status_code == 200
        plan = get_response.json()
        assert [phase["name"] for phase in plan["phases"]] == ["Race peak"]

    def test_put_with_an_empty_plan_clears_the_phases(self, migrated_db: Path) -> None:
        """PUT with no phases removes every persisted phase."""
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            client.put("/api/periodization", json=_plan_payload())
            clear_response = client.put("/api/periodization", json={"phases": []})
            get_response = client.get("/api/periodization")

        assert clear_response.status_code == 200
        assert clear_response.json()["phases"] == []
        assert get_response.json()["phases"] == []

    def test_put_accepts_a_plan_without_a_target_date(self, migrated_db: Path) -> None:
        """Without an objective target date there is no coverage cross-check."""
        profile = _profile_payload(primary_goal=_goal_payload(target_date=None))
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=profile)
            response = client.put("/api/periodization", json=_plan_payload())

        assert response.status_code == 200

    def test_put_rejects_overlapping_phases_with_generic_detail(self, migrated_db: Path) -> None:
        """AC4: overlapping phases are rejected 422 with a generic detail."""
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
            response = client.put("/api/periodization", json=overlapping)

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL
        # nLPD (#142 precedent): the rejected input is never echoed.
        assert "2026-12-15" not in response.text

    def test_put_rejects_a_plan_not_covering_the_target_date(self, migrated_db: Path) -> None:
        """AC4: a plan ending before the objective target date is rejected."""
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
            response = client.put("/api/periodization", json=short_plan)

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL

    def test_put_rejects_malformed_phase_payload_generically(self, migrated_db: Path) -> None:
        """AC4: a phase_type outside the enum is rejected generically."""
        malformed = _plan_payload(phases=[_phase_payload(phase_type="fantasy", notes=None)])
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            response = client.put("/api/periodization", json=malformed)

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL
        assert "fantasy" not in response.text

    def test_put_rejects_an_inverted_date_range_generically(self, migrated_db: Path) -> None:
        """AC4: start_date after end_date is rejected generically."""
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
            response = client.put("/api/periodization", json=inverted)

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL


class TestPeriodizationAuthGating:
    """The periodization endpoints sit behind the whitelist boundary."""

    def test_get_requires_authentication(self, auth_client: TestClient) -> None:
        """GET /api/periodization returns 401 without a session."""
        response = auth_client.get("/api/periodization")

        assert response.status_code == 401

    def test_put_requires_authentication(self, auth_client: TestClient) -> None:
        """PUT /api/periodization returns 401 without a session."""
        response = auth_client.put("/api/periodization", json=_plan_payload())

        assert response.status_code == 401


class TestSystemPromptPhaseInjection:
    """The coach system prompt gains the phase state when phases exist (AC2)."""

    def _seed_plan_relative_to_today(self, client: TestClient) -> None:
        """Persist an objective and a phase plan around the real clock.

        The phase spans [today-5, today+300] and the objective target sits at
        today+300, so the phase contains "today" across a midnight rollover
        between setup and request. Exact countdowns are pinned by the
        frozen-clock domain tests; this integration test asserts presence.
        """
        today = date.today()
        target = (today + timedelta(days=300)).isoformat()
        client.put(
            "/api/objectives", json=_profile_payload(primary_goal=_goal_payload(target_date=target))
        )
        client.put(
            "/api/periodization",
            json={
                "phases": [
                    _phase_payload(
                        start_date=(today - timedelta(days=5)).isoformat(),
                        end_date=target,
                        notes=None,
                    )
                ]
            },
        )

    def test_digest_contains_the_phase_state(self, migrated_db: Path) -> None:
        """A persisted plan surfaces the current phase, hint and countdown."""
        with _probe_client() as client:
            self._seed_plan_relative_to_today(client)
            streamed = _streamed_text(client)

        assert "Current phase: Aerobic base (base)" in streamed
        assert "Phase load focus: aerobic base, high volume, low intensity" in streamed
        assert "Days to target:" in streamed

    def test_digest_reports_no_current_phase_outside_the_plan(self, migrated_db: Path) -> None:
        """A plan that ended before today renders the no-current-phase line."""
        today = date.today()
        target = (today - timedelta(days=10)).isoformat()
        with _probe_client() as client:
            client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(target_date=target)),
            )
            client.put(
                "/api/periodization",
                json={
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
            client.put("/api/periodization", json=_plan_payload())
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE periodization_phases SET name = ''")
            streamed = _streamed_text(client)

        assert streamed == DEFAULT_SYSTEM_PROMPT


class TestPeriodizationOpenApiContract:
    """The periodization endpoints are documented in the OpenAPI schema."""

    def test_periodization_paths_are_registered(self, migrated_db: Path) -> None:
        """GET and PUT /api/periodization appear in the OpenAPI schema."""
        with TestClient(create_app()) as client:
            schema = client.get("/openapi.json").json()

        assert "/api/periodization" in schema["paths"]
        assert set(schema["paths"]["/api/periodization"]) == {"get", "put"}

    def test_extractor_skips_non_json_frames(self) -> None:
        """The SSE text extractor never leaks non-JSON frames into assertions."""
        body = (
            "data: not-json at all\n"
            'data: {"type":"done","data":{"message":"leak","iterations":1}}\n'
            'data: {"type":"token","data":{"text":"kept"}}\n'
        )

        assert _extract_token_texts(body) == "kept"
