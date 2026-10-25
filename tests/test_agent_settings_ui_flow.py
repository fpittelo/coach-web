"""Save→next-reply integration for the admin orientation UI (#363, AC2).

The settings modal saves through ``PUT /api/admin/agent-settings``; the NEXT
coach reply must reflect the saved orientation. Proven at the stream boundary
through ``POST /api/agent/stream`` with a stubbed agent echoing its assembled
system prompt (the #362 probe pattern): the UI-shaped payload — the FULL
representation with JSON ``null`` for the "default (unset)" segmented options
— drives the assertions. The backend routes/seam are merged (#361/#362) and
frozen; these tests pin the UI-facing contract end to end, complementing the
static markup tests in :mod:`tests.test_static_assets`.
"""

from datetime import date
from pathlib import Path
from typing import Any

from coach_web.agent import (
    DEFAULT_SYSTEM_PROMPT,
    REASSERT_CLAUSE,
    TONE_DIRECTIVES,
    VERBOSITY_DIRECTIVES,
    apply_temporal_anchor,
)
from tests.test_agent_settings_api import _probe_client, _streamed_text


def _ui_payload(**overrides: Any) -> dict[str, Any]:
    """Return the exact payload the settings modal builds (full representation)."""
    payload: dict[str, Any] = {
        "orientation": "Anchor recovery weeks after races.",
        "verbosity": "detailed",
        "tone": "supportive",
    }
    payload.update(overrides)
    return payload


class TestSaveThenNextReply:
    """AC2: a saved orientation reaches the next reply through the UI path."""

    def test_saved_ui_payload_reaches_the_next_reply(self, migrated_db: Path) -> None:
        """Save → the next streamed reply carries orientation + pref directives."""
        with _probe_client() as client:
            put_response = client.put("/api/admin/agent-settings", json=_ui_payload())
            reply = _streamed_text(client)

        assert put_response.status_code == 200
        assert "| Anchor recovery weeks after races." in reply
        assert VERBOSITY_DIRECTIVES["detailed"] in reply
        assert TONE_DIRECTIVES["supportive"] in reply
        assert REASSERT_CLAUSE in reply

    def test_unset_preferences_emit_no_directives(self, migrated_db: Path) -> None:
        """The "default (unset)" segmented options ride as JSON null.

        A null verbosity/tone emits NO directive lines while the orientation
        block (and the re-assert clause) still applies — the API's null
        semantics that the UI affordance mirrors (MADR-0026).
        """
        with _probe_client() as client:
            put_response = client.put(
                "/api/admin/agent-settings",
                json=_ui_payload(verbosity=None, tone=None),
            )
            reply = _streamed_text(client)

        assert put_response.status_code == 200
        assert put_response.json()["verbosity"] is None
        assert put_response.json()["tone"] is None
        assert "| Anchor recovery weeks after races." in reply
        for directive in (*VERBOSITY_DIRECTIVES.values(), *TONE_DIRECTIVES.values()):
            assert directive not in reply
        assert REASSERT_CLAUSE in reply

    def test_reset_after_save_returns_the_reply_to_the_core_prompt(self, migrated_db: Path) -> None:
        """Reset (the confirm-gated DELETE) clears the orientation from the reply."""
        with _probe_client() as client:
            client.put("/api/admin/agent-settings", json=_ui_payload())
            with_save = _streamed_text(client)
            delete_response = client.delete("/api/admin/agent-settings")
            after_reset = _streamed_text(client)

        assert delete_response.status_code == 200
        assert "| Anchor recovery weeks after races." in with_save
        assert after_reset == apply_temporal_anchor(DEFAULT_SYSTEM_PROMPT, date.today())
