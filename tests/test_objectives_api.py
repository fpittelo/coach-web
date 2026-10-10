"""Objectives API contract: GET/PUT /api/objectives + system-prompt digest (#166).

AC1 (settings view path), AC2 (persistence), AC3 (digest injection with a
stubbed agent), AC4 (strict validation) and the auth-gating AC are pinned
here against the real app with a migrated per-test SQLite database.

Since #181 the settings PUT answers 422 with the owner-safe structured
reason body ``{code, message}`` from the fixed vocabulary
(:mod:`coach_web.errors`) — the authenticated owner may learn WHY their save
failed. The chat/model-facing endpoints keep the strict #142 generic detail
(``TestGenericValidationHandler`` pins that posture unchanged).
"""

import json
import sqlite3
from collections.abc import AsyncIterator
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
        "description": "Finish the middle route under nine hours.",
        "target_metric": "event_time",
        "target_value": 9.0,
        "target_date": "2027-09-11",
    }
    payload.update(overrides)
    return payload


def _profile_payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid objective profile payload with optional overrides."""
    payload: dict[str, Any] = {
        "primary_goal": _goal_payload(),
        "secondary_goals": [
            _goal_payload(
                objective_type="process",
                title="Three threshold sessions weekly",
                description=None,
                target_metric=None,
                target_value=None,
                target_date=None,
            )
        ],
        "weekly_availability_hours": 8.5,
        "priority_disciplines": ["road", "crit"],
    }
    payload.update(overrides)
    return payload


class _SystemPromptProbeAgent:
    """Stub agent echoing its system prompt back as token + done events (AC3)."""

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
    """Build a TestClient whose agent factory returns a fresh probe per request.

    The factory contract is ``factory(settings) -> agent`` with a NEW agent
    per request (``create_agent`` constructs one), so the stub mirrors that —
    a shared mutable probe would accumulate digest mutations across turns and
    misrepresent the production lifecycle.
    """
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


class TestObjectivesEndpoints:
    """GET/PUT /api/objectives contract (AC1/AC2)."""

    def test_get_returns_null_profile_when_absent(self, migrated_db: Path) -> None:
        """An empty database reads back as a null profile (graceful empty state)."""
        with TestClient(create_app()) as client:
            response = client.get("/api/objectives")

        assert response.status_code == 200
        assert response.json() == {"profile": None}

    def test_put_persists_and_get_returns_the_profile(self, migrated_db: Path) -> None:
        """A valid PUT persists the profile; GET reads it back intact."""
        with TestClient(create_app()) as client:
            put_response = client.put("/api/objectives", json=_profile_payload())
            get_response = client.get("/api/objectives")

        assert put_response.status_code == 200
        saved = put_response.json()["profile"]
        assert saved["primary_goal"]["title"] == "Race the Alpenbrevet 2027"
        assert saved["primary_goal"]["target_date"] == "2027-09-11"
        assert saved["weekly_availability_hours"] == 8.5
        assert saved["priority_disciplines"] == ["road", "crit"]
        assert saved["secondary_goals"][0]["title"] == "Three threshold sessions weekly"

        assert get_response.status_code == 200
        assert get_response.json()["profile"] == saved

    def test_put_replaces_a_previous_profile(self, migrated_db: Path) -> None:
        """A second PUT replaces the active profile (upsert semantics)."""
        updated = _profile_payload(
            primary_goal=_goal_payload(title="Updated goal"),
            weekly_availability_hours=12.0,
        )
        with TestClient(create_app()) as client:
            client.put("/api/objectives", json=_profile_payload())
            put_response = client.put("/api/objectives", json=updated)
            get_response = client.get("/api/objectives")

        assert put_response.status_code == 200
        profile = get_response.json()["profile"]
        assert profile["primary_goal"]["title"] == "Updated goal"
        assert profile["weekly_availability_hours"] == 12.0

    def test_put_rejects_unknown_objective_type_with_a_reason_code(self, migrated_db: Path) -> None:
        """AC1 #181: malformed updates carry the owner-safe invalid_values reason."""
        with TestClient(create_app()) as client:
            response = client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(objective_type="fantasy")),
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }
        # nLPD: the rejected input is never echoed — not even in the reason body.
        assert "fantasy" not in response.text

    def test_put_rejects_missing_primary_goal_with_a_reason_code(self, migrated_db: Path) -> None:
        """AC1 #181: a payload without the primary goal carries the reason body."""
        with TestClient(create_app()) as client:
            response = client.put("/api/objectives", json={"weekly_availability_hours": 8})

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }

    def test_put_rejects_negative_target_value(self, migrated_db: Path) -> None:
        """AC4: target_value below zero is rejected with the reason body."""
        with TestClient(create_app()) as client:
            response = client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(target_value=-5.0)),
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }

    def test_put_rejects_availability_hours_above_168(self, migrated_db: Path) -> None:
        """AC4: weekly availability beyond 168 hours is rejected with the reason body."""
        with TestClient(create_app()) as client:
            response = client.put(
                "/api/objectives",
                json=_profile_payload(weekly_availability_hours=200.0),
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }

    def test_put_rejects_malformed_target_date_with_the_dates_reason(
        self, migrated_db: Path
    ) -> None:
        """AC1 #181: a non-ISO target date carries the invalid_dates reason."""
        with TestClient(create_app()) as client:
            response = client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(target_date="09/2027")),
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_dates",
            "message": "One or more dates are invalid.",
        }
        # nLPD: the rejected input is never echoed.
        assert "09/2027" not in response.text

    def test_put_rejects_a_malformed_json_body_with_a_reason_code(self, migrated_db: Path) -> None:
        """AC1 #181: even a JSON decode failure yields the structured reason body.

        The catch-all mapping keeps the settings 422 shape consistent: the UI
        contract (known code → server message, else generic fallback) never
        meets a bare ``detail`` body on a settings path.
        """
        with TestClient(create_app()) as client:
            response = client.put(
                "/api/objectives",
                content=b"{not json",
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_values",
            "message": "One or more values are out of range.",
        }


class TestObjectivesAuthGating:
    """The objectives endpoints sit behind the whitelist boundary (AC security)."""

    def test_get_requires_authentication(self, auth_client: TestClient) -> None:
        """GET /api/objectives returns 401 without a session."""
        response = auth_client.get("/api/objectives")

        assert response.status_code == 401

    def test_put_requires_authentication(self, auth_client: TestClient) -> None:
        """PUT /api/objectives returns 401 without a session."""
        response = auth_client.put("/api/objectives", json=_profile_payload())

        assert response.status_code == 401


class TestSystemPromptDigestInjection:
    """The coach system prompt gains the objectives digest when goals exist (AC3)."""

    def test_digest_is_injected_when_a_profile_exists(self, migrated_db: Path) -> None:
        """A persisted profile appears in the agent's system prompt."""
        with _probe_client() as client:
            client.put("/api/objectives", json=_profile_payload())
            streamed = _streamed_text(client)

        assert "Athlete objectives" in streamed
        assert "Primary outcome goal: Race the Alpenbrevet 2027" in streamed
        assert "Weekly availability: 8.5 h" in streamed
        assert "Secondary process goal: Three threshold sessions weekly" in streamed

    def test_no_digest_when_objectives_are_absent(self, migrated_db: Path) -> None:
        """Absent objectives leave the system prompt untouched (graceful empty)."""
        with _probe_client() as client:
            streamed = _streamed_text(client)

        assert streamed == DEFAULT_SYSTEM_PROMPT

    def test_digest_degrades_gracefully_when_the_database_is_unavailable(
        self, monkeypatch: Any
    ) -> None:
        """An unreadable database yields no digest and the stream still works."""
        monkeypatch.setenv("COACH_DB_PATH", "/proc/definitely-not-writable/coach.db")

        with _probe_client() as client:
            streamed = _streamed_text(client)

        assert streamed == DEFAULT_SYSTEM_PROMPT

    def test_digest_degrades_when_a_stored_row_is_corrupt(
        self, migrated_db: Path, db_path: Path
    ) -> None:
        """A Pydantic-invalid stored row yields no digest; the stream survives.

        N2 (PR #171 review): a row tampered outside the API (raw SQL bypasses
        the CHECK constraints — e.g. an emptied title, which has no CHECK)
        fails profile re-validation with ``pydantic.ValidationError``. The
        digest boundary must degrade to no-digest like every other
        persistence failure, never break the agent turn.
        """
        with _probe_client() as client:
            client.put("/api/objectives", json=_profile_payload())
            with sqlite3.connect(db_path) as conn:
                conn.execute("UPDATE athlete_objectives SET title = ''")
            streamed = _streamed_text(client)

        assert streamed == DEFAULT_SYSTEM_PROMPT

    def test_digest_reflects_the_latest_saved_profile(self, migrated_db: Path) -> None:
        """The digest is rebuilt per turn from the persisted profile."""
        with _probe_client() as client:
            client.put("/api/objectives", json=_profile_payload())
            first = _streamed_text(client)
            client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(title="Revised goal")),
            )
            second = _streamed_text(client)

        assert "Race the Alpenbrevet 2027" in first
        assert "Revised goal" in second
        assert "Race the Alpenbrevet 2027" not in second


class TestGenericValidationHandler:
    """The generic 422 handler covers the pre-existing endpoints too (#142)."""

    def test_agent_stream_rejects_malformed_payload_generically(self, migrated_db: Path) -> None:
        """The SSE endpoint's 422 carries the generic detail, no input echo."""
        with TestClient(create_app()) as client:
            response = client.post("/api/agent/stream", json={"message": "x" * 9000, "history": []})

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL
        assert "x" * 100 not in response.text

    def test_plan_approval_rejects_malformed_payload_generically(self, migrated_db: Path) -> None:
        """The plan approval endpoint's 422 carries the generic detail."""
        with TestClient(create_app()) as client:
            response = client.post("/api/plan/approve", json={})

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL


class TestOpenApiContract:
    """The objectives endpoints are documented in the OpenAPI schema."""

    def test_objectives_paths_are_registered(self, migrated_db: Path) -> None:
        """GET and PUT /api/objectives appear in the OpenAPI schema."""
        with TestClient(create_app()) as client:
            schema = client.get("/openapi.json").json()

        assert "/api/objectives" in schema["paths"]
        assert set(schema["paths"]["/api/objectives"]) == {"get", "put"}

    def test_extractor_concatenates_only_token_events(self) -> None:
        """The SSE text extractor never leaks non-token events into assertions."""
        body = (
            'data: {"type":"done","data":{"message":"leak","iterations":1}}\n'
            "data: not-json at all\n"
            'data: {"type":"token","data":{"text":"kept"}}\n'
        )

        assert _extract_token_texts(body) == "kept"
