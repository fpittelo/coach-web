"""Persistence-failure log lines carry the exception class, never its text (#371).

SQLAlchemy exception strings embed the statement plus ``[parameters: (...)]``
— the pre-#371 ``logger.warning("...: %s", exc)`` lines on the DB-write/read
degrade paths therefore put C2 debrief content (RPE/notes) and plan content
(week drafts) into journald. MADR-0026 (no content in logs) is the governing
standard; the fixed pattern is the sibling ``log_persistence_degrade``
(``conversation_api.py``): log ``type(exc).__name__`` only.

Pinned here for every audited request-facing site in ``app.py``:

- AC1: each site logs the exception CLASS NAME only, never ``str(exc)``;
- AC2: a crafted ``SQLAlchemyError`` whose message carries bound values (and
  the ``[parameters: ...]`` marker SQLAlchemy itself appends) leaves no value
  fragment in any captured record, while the class name still rides the log.
"""

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from coach_web.app import create_app
from tests.test_week_api import WeekHub, _raw_week

MARKER = "persistence-secret-marker-4f7c"
"""Sentinel that stands in for bound parameter values in the crafted error."""


def _leaky_error() -> SQLAlchemyError:
    """Return a SQLAlchemyError whose text carries bound values + the marker.

    SQLAlchemy appends ``[parameters: (...)]`` to every ``DBAPIError`` string;
    the crafted message mirrors that shape so the assertion proves the values
    (debrief notes, plan text) cannot ride the log line.
    """
    return SQLAlchemyError(
        "(sqlite3.IntegrityError) UNIQUE constraint failed: "
        f"plan_drafts.content: 'week plan text {MARKER}' "
        f"[parameters: ('{MARKER}', 7, 'knee pain {MARKER}')]"
    )


class _ExplodingSessionFactory:
    """Session factory whose every call raises the crafted leaky error."""

    def __call__(self) -> Any:
        """Raise the leaky SQLAlchemyError instead of opening a session."""
        raise _leaky_error()


class _FlakySessionFactory:
    """Real session factory that starts raising the leaky error at call N.

    Models the sequential branches: the handler's earlier factory calls must
    succeed so the failure lands on the site under test.
    """

    def __init__(self, real: Any, fail_from: int) -> None:
        self._real = real
        self._fail_from = fail_from
        self._calls = 0

    def __call__(self) -> Any:
        """Delegate the first N-1 calls, then raise the leaky error."""
        self._calls += 1
        if self._calls >= self._fail_from:
            raise _leaky_error()
        return self._real()


def _log_blob(caplog: Any) -> str:
    """Join every captured record's rendered message into one blob."""
    return "\n".join(record.getMessage() for record in caplog.records)


def _client_with_broken_store() -> TestClient:
    """Build an app whose persistence factory always fails."""
    app = create_app()
    app.state.db_session_factory = _ExplodingSessionFactory()
    return TestClient(app)


def _client_with_flaky_store(fail_from: int) -> tuple[TestClient, Any]:
    """Build an app whose real store starts failing at factory call *fail_from*."""
    app = create_app()
    real = app.state.db_session_factory
    app.state.db_session_factory = _FlakySessionFactory(real, fail_from=fail_from)
    return TestClient(app), app


_SINGLE_FAILURE_SITES = [
    pytest.param(
        "POST",
        "/api/week/approve",
        {"week": _raw_week()},
        503,
        id="app.py-1369-week-draft-read",
    ),
    pytest.param(
        "POST",
        "/api/debrief",
        {"plan_session_key": "2026-10-06", "rpe": 7, "notes": f"private {MARKER}"},
        503,
        id="app.py-1616-debrief-save",
    ),
    pytest.param("GET", "/api/debrief/pending", None, 200, id="app.py-1700-pending-debriefs"),
    pytest.param(
        "GET", "/api/admin/agent-settings", None, 200, id="app.py-1727-agent-settings-read"
    ),
    pytest.param(
        "PUT",
        "/api/admin/agent-settings",
        {"orientation": f"focus {MARKER}", "verbosity": "concise", "tone": "strict"},
        503,
        id="app.py-1776-agent-settings-save",
    ),
    pytest.param(
        "DELETE", "/api/admin/agent-settings", None, 503, id="app.py-1811-agent-settings-reset"
    ),
]


class TestPersistenceLogRedaction:
    """AC1/AC2 (#371): every single-failure path logs the class name only."""

    @pytest.mark.parametrize(("method", "path", "json_body", "expected"), _SINGLE_FAILURE_SITES)
    def test_failure_path_logs_class_name_only(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
        expected: int,
        migrated_db: Path,
        caplog: Any,
    ) -> None:
        """The degrade/503 path logs ``SQLAlchemyError`` and no bound values."""
        with (
            _client_with_broken_store() as client,
            caplog.at_level(logging.WARNING, logger="coach_web"),
        ):
            response = client.request(method, path, json=json_body)

        assert response.status_code == expected
        blob = _log_blob(caplog)
        assert MARKER not in blob
        assert "parameters:" not in blob
        assert "SQLAlchemyError" in blob


class TestSequentialPersistenceLogRedaction:
    """AC1/AC2 (#371): branches that fail after earlier store calls succeed."""

    def test_week_draft_upsert_failure_logs_class_name_only(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """The upsert branch (post-read) logs the class name, not the draft text."""
        client, _app = _client_with_flaky_store(fail_from=2)
        with client, caplog.at_level(logging.WARNING, logger="coach_web"):
            response = client.post("/api/week/approve", json={"week": _raw_week()})

        assert response.status_code == 503
        blob = _log_blob(caplog)
        assert MARKER not in blob
        assert "Week draft persistence failed (SQLAlchemyError)" in blob

    def test_week_draft_approval_transition_failure_logs_class_name_only(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """The approval-transition branch degrades to the per-day results."""
        client, app = _client_with_flaky_store(fail_from=3)
        app.state.hub_factory = lambda _settings: WeekHub()
        with client, caplog.at_level(logging.WARNING, logger="coach_web"):
            response = client.post("/api/week/approve", json={"week": _raw_week()})

        assert response.status_code == 200
        blob = _log_blob(caplog)
        assert MARKER not in blob
        assert "Week draft approval transition failed (SQLAlchemyError)" in blob

    def test_week_adherence_upsert_failure_logs_class_name_only(
        self, migrated_db: Path, caplog: Any
    ) -> None:
        """The best-effort adherence upsert keeps the 201 and leaks nothing."""
        client, _app = _client_with_flaky_store(fail_from=2)
        with client, caplog.at_level(logging.WARNING, logger="coach_web"):
            response = client.post(
                "/api/debrief",
                json={
                    "plan_session_key": "2026-10-06",
                    "rpe": 7,
                    "notes": f"private {MARKER}",
                },
            )

        assert response.status_code == 201
        blob = _log_blob(caplog)
        assert MARKER not in blob
        assert "Week adherence upsert failed (SQLAlchemyError)" in blob


class TestPersistenceFailureLogShape:
    """AC1 (#371): the shared shape — message plus class name, never the text."""

    def test_broken_store_factory_is_not_swallowed_by_the_client(self) -> None:
        """Guard: the double really raises the leaky error (test hygiene)."""
        with pytest.raises(SQLAlchemyError) as excinfo:
            _ExplodingSessionFactory()()

        assert MARKER in str(excinfo.value)

    def test_flaky_factory_keeps_essential_fields(self) -> None:
        """Guard: the counting double fails exactly from the configured call."""
        real = MagicMock()
        factory = _FlakySessionFactory(real, fail_from=3)

        assert factory() is real.return_value
        assert factory() is real.return_value
        with pytest.raises(SQLAlchemyError):
            factory()
