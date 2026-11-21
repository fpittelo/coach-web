"""Reset & clear-memory UX — rehydrate on load, New chat archives, explicit purge (#351).

The client half of MADR-0025 (epic #346). The backend routes are merged and
frozen (#349/#350/#375); this suite pins the SERVER-INJECTED restore opt-in
and the static bundle contract. The repo ships no Node toolchain (ADR-006
§5), so — as everywhere else — the JS is contracted through source
assertions, and the markup is asserted on the shipped file.

- AC1: the page rehydrates the active thread on load (``GET
  /api/conversation``), renders the server transcript in server order without
  duplication, and leaves the composer clean.
- AC2: "New chat" archives the active thread through ``POST
  /api/conversation/reset`` when there is something to archive, then clears
  the view — no thread browser.
- AC3: "Clear memory" is an explicit, keyboard-accessible, confirm-gated
  destructive action (``POST /api/conversation/purge`` with ``confirm:
  true``); Cancel owns the default focus.
- AC4: auth-disabled lanes and unauthenticated pages never issue a restore
  call, and a failed restore degrades silently (no banner, no console
  output).
- AC5: markup/JS contract pins — no inline handlers, token-only styles, the
  shared focus ring.
"""

import re

from fastapi.testclient import TestClient

from coach_web.app import STATIC_DIR, create_app
from coach_web.auth.middleware import SESSION_COOKIE
from coach_web.auth.tokens import create_session_token
from tests.conftest import AUTH_TEST_OWNER_EMAIL, AUTH_TEST_SESSION_SECRET
from tests.test_static_assets import _rule_block

RESTORE_OFF = 'data-conversation-restore="false"'
"""The shipped default: no rehydrate call (auth-disabled / unauthenticated)."""

RESTORE_ON = 'data-conversation-restore="true"'
"""The authenticated-session substitution (server-injected, #351 AC4)."""


def _html() -> str:
    """The shipped single-page markup."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _script() -> str:
    """The shipped Alpine component."""
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _norm(text: str) -> str:
    """Collapse source whitespace so wrapped copy reads as one line."""
    return " ".join(text.split())


def _div_block(source: str, marker: str) -> str:
    """Return the outermost ``<div>`` carrying *marker*, tag-balanced.

    The dialog is asserted in isolation so a destructive control added later
    elsewhere on the page can never satisfy these contract checks by accident.
    """
    anchor = source.index(marker)
    start = source.rindex("<div", 0, anchor)
    depth = 0
    index = start
    while index < len(source):
        next_open = source.find("<div", index)
        next_close = source.find("</div>", index)
        if next_close == -1:
            break
        if next_open != -1 and next_open < next_close:
            depth += 1
            index = next_open + len("<div")
        else:
            depth -= 1
            index = next_close + len("</div>")
            if depth == 0:
                return source[start:index]
    raise AssertionError("unbalanced div block")


class TestRestoreOptIn:
    """AC4: only an authenticated session opts the page into rehydrate."""

    def test_shipped_page_defaults_to_no_restore(self) -> None:
        """The static file is the no-restore default (auth-disabled lanes)."""
        html = _html()

        assert RESTORE_OFF in html
        assert RESTORE_ON not in html

    def test_auth_disabled_lane_never_opts_in(self) -> None:
        """AUTH_ENABLED=false (dev/qa) serves the file unchanged — no call."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert RESTORE_OFF in response.text
        assert RESTORE_ON not in response.text

    def test_unauthenticated_page_never_opts_in(self, auth_client: TestClient) -> None:
        """The landing page carries no console at all — nothing to restore (#398)."""
        response = auth_client.get("/")

        assert response.status_code == 200
        assert "data-conversation-restore=" not in response.text

    def test_authenticated_session_opts_in(self, auth_client: TestClient) -> None:
        """A signed-in session rehydrates: the server swaps the attribute."""
        # One argument per line: the wrapped form trips gitleaks' generic
        # api-key heuristic on the conftest test constant (a false positive
        # on a non-secret test signing key's NAME, never on a value).
        cookie = create_session_token(
            AUTH_TEST_OWNER_EMAIL,
            AUTH_TEST_SESSION_SECRET,
            ttl_seconds=600,
        )
        auth_client.cookies.set(SESSION_COOKIE, cookie)

        response = auth_client.get("/")

        assert response.status_code == 200
        assert RESTORE_ON in response.text
        assert RESTORE_OFF not in response.text
        # One root attribute, never duplicated by the substitution.
        assert response.text.count("data-conversation-restore=") == 1


class TestRehydrateOnLoad:
    """AC1: the transcript comes back on load, in order, without duplicates."""

    def test_init_reads_the_opt_in_before_restoring(self) -> None:
        """init() reads the server flag and only then calls the restore."""
        init = _rule_block(_script(), "init()")
        assert init, "init() must exist"

        assert 'this.$el.dataset.conversationRestore === "true"' in init
        assert "if (this.conversationRestoreEnabled)" in init
        assert "this.restoreConversation();" in init
        assert init.index("this.conversationRestoreEnabled =") < init.index(
            "this.restoreConversation();"
        )

    def test_restore_fetches_the_active_thread_uncached(self) -> None:
        """GET /api/conversation, bypassing the HTTP cache on reload."""
        restore = _rule_block(_script(), "restoreConversation()")
        assert restore, "restoreConversation() must exist"

        assert 'fetch("/api/conversation", { cache: "no-store" })' in restore
        assert "if (!response.ok)" in restore
        assert "await response.json()" in restore

    def test_restored_messages_keep_server_order_and_carry_no_receipt(self) -> None:
        """The rehydrate payload maps to {role, content} in server order."""
        script = _script()
        mapping = _rule_block(script, "restoredMessages(messages)")
        assert mapping, "restoredMessages(messages) must exist"

        assert ".map((message) => ({ role: message.role, content: message.content }))" in mapping
        # Receipts stay client-side only (nLPD): restored rows never gain a
        # fabricated timestamp, and the server order (seq) is never re-sorted.
        assert "receipt" not in mapping
        assert "sort" not in mapping
        assert "this.messages = restoredMessages(body.messages)" in _rule_block(
            script, "restoreConversation()"
        )

    def test_restore_never_duplicates_or_reorders_a_live_transcript(self) -> None:
        """The guard runs before the fetch AND after the await (late restore)."""
        restore = _rule_block(_script(), "restoreConversation()")
        assert restore, "restoreConversation() must exist"
        guard = "if (this.streaming || this.messages.length > 0)"

        assert restore.count(guard) == 2
        assignment_at = restore.index("this.messages = restoredMessages(body.messages)")
        await_at = restore.index("await response.json()")
        assert restore.index(guard, await_at) < assignment_at

    def test_restore_leaves_the_composer_clean(self) -> None:
        """A restore never writes the composer — no draft is clobbered."""
        restore = _rule_block(_script(), "restoreConversation()")
        assert restore, "restoreConversation() must exist"

        assert "this.input" not in restore


class TestNewChatArchives:
    """AC2: "New chat" archives the active thread and starts fresh."""

    def test_new_chat_archives_through_the_reset_endpoint(self) -> None:
        """The header action posts the archive when there is something to save."""
        script = _script()
        reset = _rule_block(script, "resetChat()")
        archive = _rule_block(script, "archiveConversation()")
        assert reset, "resetChat() must exist"
        assert archive, "archiveConversation() must exist"

        assert "const hadConversation = this.messages.length > 0;" in reset
        assert "if (hadConversation && this.conversationRestoreEnabled)" in reset
        assert "this.archiveConversation();" in reset
        assert 'fetch("/api/conversation/reset"' in archive
        assert 'method: "POST"' in archive

    def test_new_chat_clears_the_view_and_composer(self) -> None:
        """The view clears locally; the archive never blocks the reset."""
        reset = _rule_block(_script(), "resetChat()")
        assert reset, "resetChat() must exist"

        assert "this.finishStream();" in reset
        assert "this.messages = [];" in reset
        assert 'this.input = "";' in reset
        assert reset.index("this.messages = [];") < reset.index("this.archiveConversation();")

    def test_archive_failure_stays_visible_and_never_claims_success(self) -> None:
        """A failed archive surfaces the non-blocking banner (no false calm)."""
        archive = _rule_block(_script(), "archiveConversation()")
        assert archive, "archiveConversation() must exist"

        assert "this.errorBanner = ARCHIVE_FAILED_BANNER" in archive
        assert "response.status === 401" in archive
        assert ".catch(" in archive


class TestClearMemory:
    """AC3: explicit, confirm-gated, keyboard-accessible destructive purge."""

    def test_settings_modal_offers_the_destructive_entry(self) -> None:
        """The settings panel carries the labeled, described trigger button."""
        html = _html()
        trigger = re.search(
            r'<button[^>]*x-ref="clearMemoryTrigger"[^>]*>.*?</button>', html, re.DOTALL
        )

        assert trigger is not None, "Clear memory trigger not found"
        button = trigger.group(0)
        assert 'type="button"' in button
        assert '@click="openClearMemory()"' in button
        assert "Clear memory" in button
        assert 'aria-describedby="clear-memory-description"' in button
        # Destructive nature stated in the section copy as well as the dialog.
        assert "permanently deletes every thread and message" in _norm(html)

    def test_confirm_dialog_states_the_destructive_consequence(self) -> None:
        """A real alertdialog names itself, describes itself and warns."""
        dialog = _div_block(_html(), 'class="confirm-dialog"')

        assert 'role="alertdialog"' in dialog
        assert 'aria-modal="true"' in dialog
        assert 'aria-labelledby="clear-memory-title"' in dialog
        assert 'aria-describedby="clear-memory-detail"' in dialog
        assert "Delete all conversation memory?" in dialog
        assert "cannot be undone" in dialog
        assert "> Cancel </button>" in _norm(dialog)
        assert "Delete memory permanently" in dialog
        assert '@click="confirmClearMemory()"' in dialog

    def test_cancel_is_the_default_focus(self) -> None:
        """Opening the dialog moves focus to Cancel — never to the destructive act."""
        opening = _rule_block(_script(), "openClearMemory()")
        assert opening, "openClearMemory() must exist"

        assert "this.$refs.clearMemoryCancel" in opening
        assert ".focus()" in opening
        assert "fetch" not in opening

    def test_cancel_never_purges_and_restores_focus(self) -> None:
        """Cancel is a pure dismissal: no request, focus back on the trigger."""
        cancel = _rule_block(_script(), "cancelClearMemory()")
        assert cancel, "cancelClearMemory() must exist"

        assert "fetch" not in cancel
        assert "this.clearMemoryOpen = false" in cancel
        assert "this.$refs.clearMemoryTrigger" in cancel
        assert ".focus()" in cancel
        # Guarded: the window-level handler cannot steal focus while closed.
        assert "if (!this.clearMemoryOpen)" in cancel

    def test_escape_dismisses_the_dialog_without_closing_the_settings(self) -> None:
        """Escape is handled on the dialog and stops before the window handler."""
        html = _html()

        assert '@keydown.escape.stop="cancelClearMemory()"' in html
        assert "if (this.clearMemoryOpen)" in _rule_block(_script(), "closeSettings()")

    def test_purge_requires_the_explicit_confirm_flag(self) -> None:
        """The destructive POST carries confirm: true, then resets the view."""
        purge = _rule_block(_script(), "confirmClearMemory()")
        assert purge, "confirmClearMemory() must exist"

        assert 'fetch("/api/conversation/purge"' in purge
        assert 'method: "POST"' in purge
        assert "JSON.stringify({ confirm: true })" in purge
        assert "this.clearMemoryOpen = false" in purge
        # The purge already deleted the server transcript: the view reset
        # first empties the transcript so the reset archives nothing.
        assert "this.messages = [];" in purge
        assert "this.resetChat();" in purge
        assert 'this.settingsMessage = "Conversation memory cleared."' in purge
        # Focus never stays on a control inside the closed dialog.
        assert "this.$refs.clearMemoryTrigger" in purge
        assert ".focus()" in purge

    def test_purge_failure_is_reported_in_the_dialog(self) -> None:
        """A failed purge keeps the dialog open with a generic, value-free note."""
        purge = _rule_block(_script(), "confirmClearMemory()")
        assert purge, "confirmClearMemory() must exist"

        assert "this.clearMemoryMessage" in purge
        assert "PURGE_FAILED_MESSAGE" in purge
        assert "response.status === 401" in purge
        # The failure text itself is a static, value-free constant.
        assert 'const PURGE_FAILED_MESSAGE = "Could not clear memory. Try again.";' in _script()


class TestClientContract:
    """AC5: CSP-clean markup and the shared focus/keyboard contract."""

    def test_restore_and_purge_use_no_inline_handlers(self) -> None:
        """Alpine directives only — no onclick/onchange style attributes."""
        html = _html()

        assert re.search(r"\son(?:click|keydown|keyup|change|submit|input)=", html) is None

    def test_no_inline_style_attributes_are_added(self) -> None:
        """style-src 'self': the new surfaces are class-driven (no style=)."""
        assert 'style="' not in _html()

    def test_destructive_controls_use_the_shared_focus_ring(self) -> None:
        """The danger buttons reuse .button's :focus-visible ring (no fork)."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        focus = _rule_block(css, ".button:focus-visible")

        assert "outline" in focus
        assert "var(--accent)" in focus
        # Destructive styling is token-driven; no new color literals anywhere.
        danger = _rule_block(css, ".button--danger")
        assert "var(--error)" in danger
        assert "var(--accent-ink)" in danger

    def test_clear_memory_section_is_token_styled(self) -> None:
        """The confirm surfaces use the design tokens, not ad-hoc values."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        overlay = _rule_block(css, ".confirm-overlay")
        dialog = _rule_block(css, ".confirm-dialog")

        assert "position: fixed" in overlay
        assert "var(--overlay)" in overlay
        assert "var(--paper)" in dialog
        assert "var(--radius)" in dialog

    def test_failure_posture_is_silent(self) -> None:
        """AC4: a passive restore never surfaces an error or writes the console."""
        script = _script()
        restore = _rule_block(script, "restoreConversation()")
        assert restore, "restoreConversation() must exist"

        assert "console." not in script
        assert "errorBanner" not in restore
        assert "statusText" not in restore
