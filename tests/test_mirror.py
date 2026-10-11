"""Contract tests for the debrief ICU session-comment mirror (#190).

Story 3.2 completes PO decision 1a (#163): a saved debrief is mirrored to
Intervals.icu as a session comment via the coach-mcp ``icu_add_session_comment``
tool. The contract pinned here:

- the mirror is NON-BLOCKING on the debrief save: it never raises, and every
  failure degrades to a fixed-vocabulary soft-warning outcome (AC3) — the
  debrief row is already durable when the mirror starts and is never touched
  by mirror failures;
- the comment is SERVER-GENERATED from the validated debrief data only
  (RPE + short notes excerpt) in a fixed format, sanitized with the upstream
  semantics (script/style blocks and HTML tags stripped, control characters
  removed) and hard-capped below the upstream 2000-character limit — the
  tool is never a raw-LLM passthrough channel (upstream security condition 4);
- the athlete's explicit confirmation (folded into the LOG CHECK-IN action,
  the documented #190 decision) is the ONLY path to ``confirmed=True``;
- the ICU activity is resolved by date from the bounded completed-sessions
  read: zero matches, several matches, and read failures each map to their
  own fixed reason — nothing is ever posted to a guessed activity.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from coach_web.config import get_settings
from coach_web.errors import MirrorReason
from coach_web.mcp_hub import MCPHubError
from coach_web.mirror import (
    MIRROR_COMMENT_MAX_LENGTH,
    MIRROR_NOTES_EXCERPT_LENGTH,
    MIRROR_SUCCESS_MESSAGE,
    build_mirror_comment,
    mirror_debrief_to_icu,
)

COMPLETED_SESSIONS_TOOL = "icu_get_completed_sessions"
ADD_COMMENT_TOOL = "icu_add_session_comment"


class FakeMirrorHub:
    """In-memory stand-in for the MCPClientHub on the mirror path."""

    def __init__(
        self,
        *,
        connected: set[str] | None = None,
        results: dict[str, str] | None = None,
        connect_error: MCPHubError | None = None,
    ) -> None:
        self._connected = connected if connected is not None else {"coach-mcp", "github-mcp"}
        self._results = results or {}
        self._connect_error = connect_error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.connected_called = False
        self.closed = False

    @property
    def connected_servers(self) -> set[str]:
        """Return the set of currently connected server names."""
        return set(self._connected)

    async def connect(self) -> None:
        """Connect, or raise the configured hub-level failure."""
        self.connected_called = True
        if self._connect_error is not None:
            raise self._connect_error

    async def close(self) -> None:
        """Mark the hub as closed."""
        self.closed = True

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> str:
        """Record the call and return the configured payload."""
        self.calls.append((name, arguments))
        return self._results.get(name, "{}")


def _page_payload(*sessions: dict[str, Any]) -> str:
    """Build a completed-sessions page JSON with the supplied sessions."""
    import json

    return json.dumps(
        {
            "oldest": "2026-10-06",
            "newest": "2026-10-06",
            "activity_type": None,
            "fetched": len(sessions),
            "returned": len(sessions),
            "sessions": list(sessions),
        }
    )


def _session(**overrides: Any) -> dict[str, Any]:
    """Return one upstream-shaped completed-session summary."""
    payload: dict[str, Any] = {
        "id": "12345678",
        "date": "2026-10-06",
        "type": "Ride",
        "title": "Threshold 3x12",
        "duration_seconds": 4500.0,
        "load": 91.0,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def settings():
    """Return settings configured for the test environment."""
    return get_settings()


# ---------------------------------------------------------------------------
# Comment construction (server-generated, sanitized, capped)
# ---------------------------------------------------------------------------


class TestBuildMirrorComment:
    """The fixed-format debrief comment built from validated data only."""

    def test_comment_carries_the_fixed_format(self) -> None:
        """Header, RPE line and notes line — nothing else."""
        comment = build_mirror_comment(plan_session_key="2026-10-06", rpe=7, notes="Felt strong.")

        assert comment == ("Coach Web session debrief — 2026-10-06\nRPE: 7/10\nNotes: Felt strong.")

    def test_missing_notes_render_the_fixed_placeholder(self) -> None:
        """A notes-free debrief still renders a complete comment."""
        comment = build_mirror_comment(plan_session_key="2026-10-06", rpe=5, notes=None)

        assert comment.endswith("Notes: (none submitted)")

    def test_script_blocks_are_stripped(self) -> None:
        """Complete script/style blocks (content included) never reach ICU."""
        comment = build_mirror_comment(
            plan_session_key="2026-10-06",
            rpe=7,
            notes="<script>alert('x')</script>Felt strong.",
        )

        assert "script" not in comment.lower()
        assert "alert" not in comment
        assert "Felt strong." in comment

    def test_html_tags_are_stripped(self) -> None:
        """Inline markup (and its event handlers) is removed."""
        comment = build_mirror_comment(
            plan_session_key="2026-10-06",
            rpe=7,
            notes="<img src=x onerror=alert(1)>Legs heavy",
        )

        assert "<" not in comment
        assert "onerror" not in comment
        assert "Legs heavy" in comment

    def test_control_characters_are_removed(self) -> None:
        """C0 controls (except newline/tab) and DEL are stripped."""
        comment = build_mirror_comment(
            plan_session_key="2026-10-06",
            rpe=7,
            notes="Legs\x08 heavy\x7f.",
        )

        assert "\x08" not in comment
        assert "\x7f" not in comment
        assert "Legs heavy." in comment

    def test_notes_are_excerpted(self) -> None:
        """Only a short excerpt of the notes travels to ICU."""
        long_notes = "x" * (MIRROR_NOTES_EXCERPT_LENGTH + 500)

        comment = build_mirror_comment(plan_session_key="2026-10-06", rpe=7, notes=long_notes)

        notes_line = comment.rsplit("Notes: ", 1)[1]
        assert len(notes_line) == MIRROR_NOTES_EXCERPT_LENGTH

    def test_comment_never_exceeds_the_upstream_cap(self) -> None:
        """The assembled comment is hard-capped at the upstream 2000 limit."""
        long_notes = "y" * 5000

        comment = build_mirror_comment(plan_session_key="2026-10-06", rpe=7, notes=long_notes)

        assert len(comment) <= MIRROR_COMMENT_MAX_LENGTH
        assert MIRROR_COMMENT_MAX_LENGTH == 2000

    def test_comment_is_never_empty(self) -> None:
        """Even hostile notes leave a non-empty, structured comment."""
        comment = build_mirror_comment(
            plan_session_key="2026-10-06", rpe=7, notes="<script></script>"
        )

        assert comment
        assert "RPE: 7/10" in comment


# ---------------------------------------------------------------------------
# The mirror flow (non-blocking, fixed-vocabulary degradation)
# ---------------------------------------------------------------------------


class TestMirrorDebriefToIcu:
    """The post-save mirror orchestration (#190 AC3)."""

    async def test_success_mirrors_the_comment_with_explicit_confirmation(self, settings) -> None:
        """One session that day → the comment is posted with confirmed=True."""
        hub = FakeMirrorHub(
            results={
                COMPLETED_SESSIONS_TOOL: _page_payload(_session()),
                ADD_COMMENT_TOOL: (
                    "Successfully posted session comment on activity " "'12345678': {\"id\": 1}"
                ),
            }
        )

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes="Felt strong.",
        )

        assert outcome.status == "mirrored"
        assert outcome.code is None
        assert outcome.message == MIRROR_SUCCESS_MESSAGE
        assert activity_id == "12345678"
        assert len(hub.calls) == 2
        resolve_name, resolve_args = hub.calls[0]
        assert resolve_name == COMPLETED_SESSIONS_TOOL
        assert resolve_args == {"oldest": "2026-10-06", "newest": "2026-10-06"}
        post_name, post_args = hub.calls[1]
        assert post_name == ADD_COMMENT_TOOL
        assert post_args is not None
        assert post_args["activity_id"] == "12345678"
        assert post_args["confirmed"] is True
        assert "RPE: 7/10" in post_args["comment"]
        assert hub.closed is True

    async def test_hub_connect_failure_degrades_to_hub_unavailable(self, settings) -> None:
        """No MCP server reachable → fixed soft warning, no tool calls."""
        hub = FakeMirrorHub(connect_error=MCPHubError("no server reachable"))

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.HUB_UNAVAILABLE
        assert outcome.message == (
            "Debrief saved — but the Intervals.icu mirror is unavailable right now."
        )
        assert activity_id is None
        assert hub.calls == []
        assert hub.closed is True

    async def test_coach_server_not_connected_degrades_to_hub_unavailable(self, settings) -> None:
        """A partial connect (github only) never reaches for the coach tools."""
        hub = FakeMirrorHub(connected={"github-mcp"})

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.HUB_UNAVAILABLE
        assert activity_id is None
        assert hub.calls == []
        assert hub.closed is True

    async def test_no_session_that_day_degrades_to_session_not_found(self, settings) -> None:
        """Zero matching sessions → nothing is posted, fixed reason."""
        hub = FakeMirrorHub(results={COMPLETED_SESSIONS_TOOL: _page_payload()})

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.SESSION_NOT_FOUND
        assert activity_id is None
        assert len(hub.calls) == 1

    async def test_several_sessions_that_day_degrade_to_session_ambiguous(self, settings) -> None:
        """Multiple same-day sessions are never guessed between."""
        hub = FakeMirrorHub(
            results={
                COMPLETED_SESSIONS_TOOL: _page_payload(
                    _session(id="11111111"),
                    _session(id="22222222"),
                )
            }
        )

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.SESSION_AMBIGUOUS
        assert activity_id is None
        assert len(hub.calls) == 1

    async def test_failed_resolve_read_degrades_to_hub_unavailable(self, settings) -> None:
        """A rate-limited or errored sessions read cannot resolve an activity."""
        hub = FakeMirrorHub(results={COMPLETED_SESSIONS_TOOL: '{"error": "rate limited"}'})

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.HUB_UNAVAILABLE
        assert activity_id is None
        assert len(hub.calls) == 1

    async def test_failed_comment_post_degrades_to_post_failed(self, settings) -> None:
        """The upstream Error reply maps to the post_failed soft warning."""
        hub = FakeMirrorHub(
            results={
                COMPLETED_SESSIONS_TOOL: _page_payload(_session()),
                ADD_COMMENT_TOOL: "Error posting session comment on activity "
                "'12345678': upstream unavailable",
            }
        )

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.POST_FAILED
        assert activity_id is None

    async def test_unexpected_exception_degrades_to_post_failed(self, settings) -> None:
        """The mirror never raises — any crash is a fixed soft warning."""
        hub = FakeMirrorHub(results={COMPLETED_SESSIONS_TOOL: _page_payload(_session())})
        hub.execute_tool = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

        outcome, activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert outcome.status == "failed"
        assert outcome.code is MirrorReason.POST_FAILED
        assert activity_id is None
        assert hub.closed is True

    async def test_every_failure_message_is_the_fixed_vocabulary(self, settings) -> None:
        """No exception text ever reaches the outcome message (nLPD)."""
        hub = FakeMirrorHub(connect_error=MCPHubError("secret internals leaked here"))

        outcome, _activity_id = await mirror_debrief_to_icu(
            lambda _settings: hub,
            settings,
            plan_session_key="2026-10-06",
            rpe=7,
            notes=None,
        )

        assert "secret internals" not in outcome.message
        assert outcome.message.startswith("Debrief saved")
