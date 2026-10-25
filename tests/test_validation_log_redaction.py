"""The 422 validation-handler log never carries request values (#352).

Pydantic v2 error dicts embed the offending ``input`` (and sometimes
``ctx``), so the validation handler's full-error log writes request values
into journald — for ``POST /api/agent/stream`` that is chat text
(health-adjacent personal content, nLPD) and a violation of epic #346's C3
redaction condition. Pinned here:

- AC1: the handler logs only safe fields — the request path, a per-error
  ``loc``/``type`` projection and the error count — never ``input``,
  ``ctx``, ``msg`` or the body;
- AC2: an oversized ``message`` and an oversized ``history`` entry leave no
  fragment of the submitted value in any captured log record;
- AC3: the settings-reason 422 stays generic (static vocabulary body,
  value-free log) and SSE error events never echo conversation content;
- AC4 is the harness gate itself (see the PR).
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from coach_web.app import _safe_validation_errors, create_app
from coach_web.mcp_hub import MCPHubError

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
        "secondary_goals": [],
        "weekly_availability_hours": 8.5,
        "priority_disciplines": ["road", "crit"],
    }
    payload.update(overrides)
    return payload


def _log_blob(caplog: Any) -> str:
    """Join every captured log record's rendered message into one blob."""
    return "\n".join(record.getMessage() for record in caplog.records)


class _ExplodingAgent:
    """Stub agent whose hub connection fails on enter (SSE error path)."""

    system_prompt = ""

    async def __aenter__(self) -> "_ExplodingAgent":
        """Raise a hub connection error on enter."""
        raise MCPHubError("no MCP server reachable")

    async def __aexit__(self, *args: Any) -> None:
        """Exit the agent context (never reached after the failed enter)."""


class TestValidationLogRedaction:
    """AC1/AC2 (#352): the 422 log line carries loc/type + count, no values."""

    def test_oversized_message_leaves_no_value_in_the_log(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """An oversized chat message never reaches the validation log line."""
        marker = "knee-pain-secret-marker-0f3a"
        with (
            TestClient(create_app()) as client,
            caplog.at_level(logging.WARNING, logger="coach_web"),
        ):
            response = client.post(
                "/api/agent/stream",
                json={"message": marker + "x" * 8100, "history": []},
            )

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL
        blob = _log_blob(caplog)
        assert marker not in blob
        assert "x" * 100 not in blob
        assert "/api/agent/stream" in blob
        assert "1 validation error(s)" in blob
        assert "'loc'" in blob
        assert "'type': 'string_too_long'" in blob

    def test_oversized_history_content_leaves_no_value_in_the_log(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """An oversized replayed history entry never reaches the log line."""
        marker = "history-secret-marker-5b8d"
        with (
            TestClient(create_app()) as client,
            caplog.at_level(logging.WARNING, logger="coach_web"),
        ):
            response = client.post(
                "/api/agent/stream",
                json={
                    "message": "hi",
                    "history": [{"role": "user", "content": marker + "x" * 8100}],
                },
            )

        assert response.status_code == 422
        assert response.json()["detail"] == GENERIC_422_DETAIL
        blob = _log_blob(caplog)
        assert marker not in blob
        assert "x" * 100 not in blob
        assert "'type': 'string_too_long'" in blob

    def test_settings_reason_422_payload_and_log_stay_value_free(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """The settings path answers the static reason body and logs no values.

        The invalid-date rejection rides the Pydantic error's ``msg`` — the
        pre-#352 log line leaked it because the old technical message
        interpolated the rejected value (made value-free in #355).
        """
        marker = "target-date-secret-marker-7b2c"
        with (
            TestClient(create_app()) as client,
            caplog.at_level(logging.WARNING, logger="coach_web"),
        ):
            response = client.put(
                "/api/objectives",
                json=_profile_payload(primary_goal=_goal_payload(target_date=marker)),
            )

        assert response.status_code == 422
        assert response.json() == {
            "code": "invalid_dates",
            "message": "One or more dates are invalid.",
        }
        blob = _log_blob(caplog)
        assert marker not in blob
        assert "/api/objectives" in blob


class TestSseErrorContentRedaction:
    """AC3 (#352): SSE error events never echo conversation content."""

    def test_error_event_never_echoes_conversation_content(self, migrated_db: Path) -> None:
        """A mid-stream failure emits the fixed technical text only."""
        marker = "conversation-secret-marker-9d4e"
        app = create_app()
        app.state.agent_factory = MagicMock(return_value=_ExplodingAgent())
        client = TestClient(app)

        with client.stream(
            "POST",
            "/api/agent/stream",
            json={"message": f"plan around {marker}", "history": []},
        ) as response:
            body = response.read().decode("utf-8")

        assert response.status_code == 200
        assert "event: error" in body
        assert "no MCP server reachable" in body
        assert marker not in body


class TestSafeValidationErrorsProjection:
    """AC1 (#352): the projection keeps only the value-free error fields."""

    def test_projects_every_error_to_loc_and_type_only(self) -> None:
        """``input``/``ctx``/``msg``/``url`` are dropped; bare errors survive."""
        errors: list[dict[str, Any]] = [
            {
                "type": "string_too_long",
                "loc": ("body", "message"),
                "msg": "String should have at most 8000 characters",
                "input": "knee-pain-secret-marker-0f3a",
                "ctx": {"max_length": 8000},
                "url": "https://errors.pydantic.dev/2/v/string_too_long",
            },
            # msg can interpolate the input (validate_iso_date does), so it
            # must be projected away exactly like input/ctx:
            {
                "type": "value_error",
                "loc": ("body", "primary_goal", "target_date"),
                "msg": "Value error, date 'target-date-secret-marker-7b2c' is not a valid"
                " calendar date",
                "input": "target-date-secret-marker-7b2c",
                "ctx": {"error": ValueError()},
            },
            # A minimal error dict (no msg/input/ctx at all) stays intact:
            {"type": "json_invalid", "loc": ("body",)},
        ]

        assert _safe_validation_errors(errors) == [
            {"loc": ("body", "message"), "type": "string_too_long"},
            {"loc": ("body", "primary_goal", "target_date"), "type": "value_error"},
            {"loc": ("body",), "type": "json_invalid"},
        ]

    def test_projection_preserves_the_error_count(self) -> None:
        """One projected entry per error — the count stays loggable."""
        errors: list[dict[str, Any]] = [
            {"type": "string_too_long", "loc": ("body", "message")},
            {"type": "literal_error", "loc": ("body", "history", 0, "role")},
            {"type": "json_invalid", "loc": ("body",)},
        ]

        projected = _safe_validation_errors(errors)

        assert len(projected) == len(errors)
