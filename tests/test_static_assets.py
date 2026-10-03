"""Frontend smoke tests: static asset serving and the v0.7 visual contract.

These tests guard the single-origin, zero-CDN delivery of the Alpine.js UI and
the Swiss nLPD constraint that no user data is ever rendered as raw HTML.

The stylesheet contract was superseded by ADR-006 (issue #78): the Sprint 03
Swiss minimalist tokens (pure-red accent, 2px radii, zero elevation, 1180px
two-panel grid) are replaced by the v0.7 slate/teal palette, 10px radii, soft
shadows, and a centered 760px reading column. The supersession is explicit —
assertions were rewritten, never silently deleted.

Issue #80 adds the header identity bar contract (inline SVG monogram, product
name, exactly one "New chat" action) and the nLPD guard that the new-chat
reset is a pure client-side operation: nothing transmitted, nothing persisted.

Issue #79 supersedes the SSE transport: EventSource cannot POST, so the
component streams via fetch + ReadableStream against POST /api/agent/stream
with a {message, history} body. Message and history content never appear in
a URL or query string (access-log privacy, Swiss nLPD), and the client
replays the last ≤ 10 transcript messages as history, excluding the
in-flight assistant turn. The per-handler stale-event guards from #80 carry
over unchanged.

The #79 review remediation adds TestSseParserContract: source-level
assertions pinning the three SSE parser correctness properties the PR
claims (whole-buffer CRLF re-normalisation, ping-comment skipping, and the
hasOwnProperty dispatch guard), so a future refactor cannot silently break
them without a test failing (ADR-006 §5 — no Node toolchain; JS contracted
via source assertions).

Issue #81 reverses the x-html ban for assistant messages only (ADR-006 §9,
gated by the STRIDE sign-off #87): assistant bubbles render
DOMPurify.sanitize(marked.parse(text)) through a renderMarkdown helper,
user messages keep x-text forever, and the plan card stays structured
Pydantic→HTML. The STRIDE conditions are pinned at source level: C1
(DOMPurify default config — no ALLOWED_ATTR/ALLOWED_TAGS additions), C3
(every render sanitizes the accumulated raw string, never per-token
fragments), C4 (x-html only on the assistant branch via the helper), and
C6/C7 as regression guards (models.py/app.py untouched — the role Literal,
history caps and approval gate are guarded by the existing API tests).

Issue #84 adds the explicit stream-phase contract: a plain-string `phase`
property (idle | waiting | streaming | tooling | error — KIS, no
state-machine framework) driving the status surfaces. Three genuine v0.5
defects are fixed and pinned here: (1) the typed `error` handler and the
transport-error path are separate code paths, and the typed handler must
NOT finish the stream — agent.py emits a typed `error` FOLLOWED BY `done`
on the OpenRouterError path, so finishing on the typed event would flip
the compound guards and truncate the follow-up done; (2) the transport
catch leaves a visible, non-blocking error banner (the pre-#84 path only
wrote statusText, which finishStream immediately cleared — a mid-stream
transport death was silent once tokens existed); (3) a 90 s no-event
watchdog (backstop above the server's 60 s OpenRouter completion timeout
and 30 s MCP tool-call timeout) is armed on stream start, reset on every
received typed event, cleared on stream end, and its timeout transitions
to a visible error state. Ping comment frames deliberately do NOT reset
the watchdog: a wedged agent behind a live transport must still trip it.

Issue #85 replaces the unconditional auto-scroll with a proximity-armed
follow flag: the log follows the stream only while the viewport sits
within 60 px of the bottom; farther away, token arrivals and transcript
mutations never move the viewport (scrolling up to re-read is never
hijacked), and scrolling back near the bottom re-arms following
automatically (self-healing — the locked KIS cut drops a "jump to latest"
pill). Token-time scrolls are instant (behavior "auto"), and a
prefers-reduced-motion media query disables the looping animations.
"""

import re

from fastapi.testclient import TestClient

from coach_web.app import STATIC_DIR, create_app

VENDOR_DIR = STATIC_DIR / "vendor"

# v0.7 palette (ADR-006) plus one functional addition: --error (red-700). The
# v0.3 contract overloaded #ff0000 as both brand accent and error color; with
# the deep-teal accent, error feedback needs its own token.
V07_PALETTE = frozenset(
    {
        "#f8fafc",  # --surface: off-white page background
        "#ffffff",  # --paper / --accent-ink: card surface, text on accent
        "#1e293b",  # --ink: primary text
        "#475569",  # --muted: secondary text
        "#e2e8f0",  # --line: hairline divider
        "#0f766e",  # --accent: deep teal
        "#b91c1c",  # --error: error feedback text
    }
)


def _rule_block(css: str, selector: str) -> str:
    """Return the declaration block of the first CSS rule matching *selector*."""
    match = re.search(re.escape(selector) + r"\s*\{", css)
    if match is None:
        return ""
    depth = 1
    start = match.end()
    index = start
    while index < len(css) and depth > 0:
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return css[start : index - 1]


def _media_block(css: str, query: str) -> str:
    """Return the body of the first ``@media`` block matching *query*."""
    match = re.search(re.escape(query) + r"\s*\{", css)
    if match is None:
        return ""
    depth = 1
    start = match.end()
    index = start
    while index < len(css) and depth > 0:
        char = css[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return css[start : index - 1]


def _header_block(html: str) -> str:
    """Return the <header>…</header> block of the page."""
    return html[html.index("<header") : html.index("</header>")]


def _has_rule(css: str, selector: str) -> bool:
    """True if any rule's selector list includes *selector* (group-aware)."""
    for match in re.finditer(r"([^{}]+)\{", css):
        selectors = [part.strip() for part in match.group(1).split(",")]
        if selector in selectors:
            return True
    return False


class TestStaticAssetServing:
    """Every frontend asset is served from the FastAPI origin."""

    def test_index_is_served_at_root(self) -> None:
        """GET / serves the single-page interface."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_stylesheet_is_served(self) -> None:
        """GET /static/styles.css serves the Swiss minimalist stylesheet."""
        with TestClient(create_app()) as client:
            response = client.get("/static/styles.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    def test_application_script_is_served(self) -> None:
        """GET /static/app.js serves the Alpine component."""
        with TestClient(create_app()) as client:
            response = client.get("/static/app.js")

        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]

    def test_vendored_alpine_is_served(self) -> None:
        """GET /static/vendor/alpine.min.js serves the local Alpine build."""
        with TestClient(create_app()) as client:
            response = client.get("/static/vendor/alpine.min.js")

        assert response.status_code == 200
        assert len(response.content) > 1000

    def test_vendored_inter_fonts_are_served(self) -> None:
        """The self-hosted Inter WOFF2 weights are served locally."""
        with TestClient(create_app()) as client:
            for weight in ("400", "600", "700"):
                response = client.get(f"/static/vendor/inter-latin-{weight}-normal.woff2")
                assert response.status_code == 200, weight
                assert response.content[:4] == b"wOF2", weight

    def test_vendored_markdown_libraries_are_served(self) -> None:
        """GET /static/vendor/{marked,DOMPurify}.min.js serve the local builds."""
        with TestClient(create_app()) as client:
            for name in ("marked.min.js", "DOMPurify.min.js"):
                response = client.get(f"/static/vendor/{name}")
                assert response.status_code == 200, name
                assert "javascript" in response.headers["content-type"], name
                assert len(response.content) > 1000, name


class TestSwissMinimalistContract:
    """Design-token and privacy constraints on the shipped assets.

    Scope updated per ADR-006: this class now guards the v0.7 visual contract
    that superseded the Sprint 03 Swiss minimalist token set (issue #78).
    """

    def test_index_has_no_external_dependencies(self) -> None:
        """The page references no external CDN or remote origin."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "http://" not in html
        assert "https://" not in html
        assert "//cdn" not in html

    def test_index_references_local_assets(self) -> None:
        """The page wires the local stylesheet, vendor builds and component."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "/static/styles.css" in html
        assert "/static/vendor/marked.min.js" in html
        assert "/static/vendor/DOMPurify.min.js" in html
        assert "/static/vendor/alpine.min.js" in html
        assert "/static/app.js" in html

    def test_index_uses_text_interpolation_only(self) -> None:
        """User data is bound with x-text; x-html is assistant-only + sanitized.

        Rewritten per ADR-006 §9 and STRIDE #87 condition C4: the v0.3
        blanket x-html ban is superseded — exactly one x-html binding is
        allowed, on the assistant message body, through the renderMarkdown
        sanitizer helper. User messages keep x-text forever (untrusted input
        is never HTML-rendered).
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "x-text" in html
        assert html.count("x-html") == 1

        user_branch = re.search(
            r"<template x-if=\"message.role === 'user'\">.*?</template>", html, re.DOTALL
        )
        assistant_branch = re.search(
            r"<template x-if=\"message.role === 'assistant'\">.*?</template>",
            html,
            re.DOTALL,
        )
        assert user_branch is not None, "user message branch missing"
        assert assistant_branch is not None, "assistant message branch missing"

        # User branch: plain text interpolation only — never HTML.
        assert 'x-text="message.content"' in user_branch.group(0)
        assert "x-html" not in user_branch.group(0)

        # Assistant branch: the single x-html binding goes through the
        # sanitizer helper with the accumulated raw content.
        assert 'x-html="renderMarkdown(message.content)"' in assistant_branch.group(0)
        assert "x-text=" not in assistant_branch.group(0)

    def test_script_load_order_vendors_before_app(self) -> None:
        """marked and DOMPurify load before app.js; Alpine boots last."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        for asset in (
            "/static/vendor/marked.min.js",
            "/static/vendor/DOMPurify.min.js",
            "/static/app.js",
            "/static/vendor/alpine.min.js",
        ):
            assert asset in html, asset

        marked_at = html.index("/static/vendor/marked.min.js")
        purify_at = html.index("/static/vendor/DOMPurify.min.js")
        app_at = html.index("/static/app.js")
        alpine_at = html.index("/static/vendor/alpine.min.js")
        assert marked_at < purify_at < app_at < alpine_at

    def test_stylesheet_uses_swiss_tokens(self) -> None:
        """The stylesheet encodes the v0.7 design tokens (ADR-006 supersedes v0.3)."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

        # Inter remains the sole, self-hosted typeface (carried forward from v0.3).
        assert "@font-face" in css
        assert "inter" in css

        # v0.7 palette tokens: slate surfaces, deep teal accent (ADR-006 §9).
        assert "--surface: #f8fafc" in css
        assert "--paper: #ffffff" in css
        assert "--ink: #1e293b" in css
        assert "--muted: #475569" in css
        assert "--line: #e2e8f0" in css
        assert "--accent: #0f766e" in css
        assert "--accent-ink: #ffffff" in css

        # Soft geometry: 10px radius band and soft shadow tokens replace the
        # v0.3 hairline-bordered, zero-elevation surfaces.
        assert "--radius: 10px" in css
        assert "--shadow-sm" in css
        assert "--shadow-md" in css
        assert "box-shadow" in css

        # Panels float on soft elevation — no harsh card borders (ADR-006).
        panel = _rule_block(css, ".panel")
        assert "box-shadow" in panel
        assert "border:" not in panel
        assert "border-radius" in panel

        # The v0.3 token set is gone (superseded, never silently kept).
        assert "#ff0000" not in css
        assert "#111" not in css
        assert "#d5d5d5" not in css
        assert "--radius: 2px" not in css

        # Single centered reading column (epic band 720–800px).
        assert "max-width: 760px" in css

    def test_stylesheet_colors_are_tokenized(self) -> None:
        """Every color literal lives in the :root token block (ADR-006, AC1)."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        root = _rule_block(css, ":root")

        root_hex = set(re.findall(r"#[0-9a-f]{3,8}\b", root))
        assert root_hex == set(V07_PALETTE)

        stray_hex = set(re.findall(r"#[0-9a-f]{3,8}\b", css)) - root_hex
        assert not stray_hex, f"hard-coded colors outside tokens: {sorted(stray_hex)}"

        root_rgba = set(re.findall(r"rgba?\([^)]*\)", root))
        stray_rgba = set(re.findall(r"rgba?\([^)]*\)", css)) - root_rgba
        assert not stray_rgba, f"hard-coded rgba outside tokens: {sorted(stray_rgba)}"

    def test_layout_is_single_centered_column(self) -> None:
        """Masthead, content and footer share one centered 760px column (AC3)."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

        for selector in (".masthead", ".layout", ".footer"):
            block = _rule_block(css, selector)
            assert "max-width: 760px" in block, selector
            assert "margin: 0 auto" in block, selector

        # The v0.3 two-panel grid (1.4fr/1fr) is gone; panels stack in one column.
        layout = _rule_block(css, ".layout")
        assert "grid-template-columns" not in layout
        assert "flex-direction: column" in layout

    def test_message_body_typography_is_readable(self) -> None:
        """Message body line-height sits in the 1.5–1.6 readability band."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

        body = _rule_block(css, ".message-body")
        match = re.search(r"line-height:\s*([0-9.]+)", body)
        assert match is not None, ".message-body must declare an explicit line-height"
        line_height = float(match.group(1))
        assert 1.5 <= line_height <= 1.6

    def test_markdown_css_covers_required_elements(self) -> None:
        """AC2: bold, lists, blockquotes, inline code and headings are styled."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        for selector in (
            ".message-body--markdown strong",
            ".message-body--markdown ul",
            ".message-body--markdown ol",
            ".message-body--markdown li",
            ".message-body--markdown blockquote",
            ".message-body--markdown code",
            ".message-body--markdown h1",
            ".message-body--markdown h2",
            ".message-body--markdown h3",
        ):
            assert _has_rule(css, selector), f"missing markdown rule: {selector}"

    def test_markdown_body_does_not_clip_or_double_space(self) -> None:
        """AC2: block layout replaces pre-wrap; long tokens wrap; code scrolls."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        body = _rule_block(css, ".message-body--markdown")
        assert "white-space: normal" in body
        assert "overflow-wrap: break-word" in body
        pre = _rule_block(css, ".message-body--markdown pre")
        assert "overflow-x: auto" in pre

    def test_application_script_wires_stream_and_approval(self) -> None:
        """The component streams via fetch POST and posts plan approvals (#79)."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        # EventSource cannot POST: the SSE transport is fetch + ReadableStream
        # against POST /api/agent/stream (supersedes the #79 GET transport).
        assert "EventSource" not in script
        assert 'fetch("/api/agent/stream"' in script
        assert 'method: "POST"' in script
        assert "getReader()" in script
        assert "/api/plan/approve" in script
        assert "plan_proposal" in script

    def test_stream_payload_carries_message_and_history_in_body(self) -> None:
        """Message and history travel in the POST body — never in a URL (AC4)."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert "JSON.stringify({ message: message, history: history })" in script
        # Regression guards for the access-log privacy leak (#79): the stream
        # URL is bare and no query-string encoding helper remains.
        assert "/api/agent/stream?" not in script
        assert "encodeURIComponent" not in script

    def test_history_replay_caps_at_ten_excluding_in_flight_turn(self) -> None:
        """History replays the prior turns only, capped at the last 10 (#79)."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        # Prior turns only: the just-submitted user message travels as
        # `message` (never duplicated in history), and the snapshot is taken
        # before the in-flight assistant placeholder is pushed.
        assert "this.messages.slice(0, -1).slice(-10)" in script
        history_at = script.index("this.messages.slice(0, -1).slice(-10)")
        placeholder_at = script.index('this.messages.push({ role: "assistant", content: "" })')
        assert history_at < placeholder_at

    def test_application_script_renders_plan_fields(self) -> None:
        """The plan card surfaces date, title, watts, duration and intervals."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "plan.title" in html
        assert "plan.date" in html
        assert "plan.steps" in html
        assert "target_power_watts" in html
        assert "duration_minutes" in html
        assert "approvePlan()" in html
        assert "rejectPlan()" in html

    def test_vendor_directory_is_self_contained(self) -> None:
        """The vendor directory ships Alpine, marked, DOMPurify and fonts locally."""
        assert (VENDOR_DIR / "alpine.min.js").is_file()
        assert (VENDOR_DIR / "marked.min.js").is_file()
        assert (VENDOR_DIR / "DOMPurify.min.js").is_file()
        assert (VENDOR_DIR / "inter-latin-400-normal.woff2").is_file()
        assert (VENDOR_DIR / "inter-latin-600-normal.woff2").is_file()
        assert (VENDOR_DIR / "inter-latin-700-normal.woff2").is_file()

    def test_vendor_libraries_carry_license_notices(self) -> None:
        """marked and DOMPurify ship their license notices in vendor/ (#81 AC1)."""
        for name in ("marked", "DOMPurify"):
            notice = VENDOR_DIR / f"{name}.LICENSE"
            assert notice.is_file(), name
            text = notice.read_text(encoding="utf-8")
            assert "License" in text, name
            assert len(text) > 200, name

    def test_vendored_marked_is_the_parser_build(self) -> None:
        """The vendored marked.min.js is the real markdown parser build."""
        lib = VENDOR_DIR / "marked.min.js"
        assert lib.is_file()
        content = lib.read_text(encoding="utf-8")
        assert "marked" in content
        assert "parse" in content

    def test_vendored_dompurify_is_the_sanitizer_build(self) -> None:
        """The vendored DOMPurify.min.js is the real sanitizer build."""
        lib = VENDOR_DIR / "DOMPurify.min.js"
        assert lib.is_file()
        content = lib.read_text(encoding="utf-8")
        assert "DOMPurify" in content
        assert "sanitize" in content


class TestIdentityBarContract:
    """Header identity bar & new-chat full client reset (issue #80, ADR-006).

    The identity bar is the compact app chrome at the top of the centered
    column: inline SVG monogram, product name, and exactly one "New chat"
    action. The reset is a pure client-side operation — nothing is
    transmitted and no transcript is persisted (Swiss nLPD posture).

    app.js behavior is contracted through source assertions: the repo ships
    no Node toolchain (ADR-006 §5, KIS), so JS is verified the same way as
    the existing stream/approval wiring tests above.
    """

    # --- AC1: identity bar structure ---------------------------------------

    def test_identity_bar_renders_brand_and_single_new_chat(self) -> None:
        """The bar shows the monogram, the product name and one New chat button."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        header = _header_block(html)

        assert 'class="identity-bar"' in header
        assert '<svg class="monogram"' in header
        assert ">Coach Web</span>" in header
        assert len(re.findall(r">\s*New chat\s*</button>", html)) == 1

    def test_identity_bar_sits_inside_centered_column(self) -> None:
        """The identity bar is a child of the centered 760px masthead."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        masthead = html.index('<header class="masthead">')
        bar = html.index('<div class="identity-bar">')
        close = html.index("</header>")
        assert masthead < bar < close

    def test_legacy_session_buttons_are_gone(self) -> None:
        """KIS cut: the single New chat replaces New Session / Clear Context."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "New Session" not in html
        assert "Clear Context" not in html

    def test_identity_bar_has_no_status_badge(self) -> None:
        """KIS cut: no connectivity/active badge (#84 owns the status surface)."""
        header = _header_block((STATIC_DIR / "index.html").read_text(encoding="utf-8"))
        lowered = header.lower()

        assert "badge" not in lowered
        assert "connected" not in lowered
        # "status" as a raw substring is brittle: it matches any occurrence of
        # the word (prose, URLs, unrelated attributes). Scope the ban to
        # class/id tokens — it still catches a "status-badge" style class.
        class_id_tokens = re.findall(r'(?:class|id)="([^"]*)"', lowered)
        assert not any("status" in token for token in class_id_tokens)

    # --- AC3: accessibility -------------------------------------------------

    def test_new_chat_button_is_accessible(self) -> None:
        """The New chat action is a real button with a labelled, wired click."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        match = re.search(r"<button\b[^>]*new-chat[^>]*>.*?</button>", html, re.DOTALL)
        assert match is not None, "New chat button not found"
        button = match.group(0)

        assert 'type="button"' in button
        assert 'aria-label="New chat' in button
        assert '@click="resetChat()"' in button

    def test_new_chat_button_has_visible_focus_ring(self) -> None:
        """Keyboard focus draws a visible accent outline (AC3)."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        focus = _rule_block(css, ".button:focus-visible")

        assert "outline" in focus
        assert "var(--accent)" in focus

    # --- AC4: monogram -------------------------------------------------------

    def test_monogram_is_single_inline_svg(self) -> None:
        """One hand-authored inline SVG; no sprite, no icon font, no fetch."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert html.count("<svg") == 1
        assert "currentColor" in html
        assert 'viewBox="0 0 32 32"' in html
        assert "<use" not in html
        assert "xlink" not in html

    def test_monogram_is_sized_and_token_colored(self) -> None:
        """The monogram renders at a fixed pixel size in the accent color."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        monogram = _rule_block(css, ".monogram")

        assert "width:" in monogram
        assert "height:" in monogram
        assert "color: var(--accent)" in monogram

    # --- AC2: new-chat reset --------------------------------------------------

    def test_new_chat_resets_all_client_state(self) -> None:
        """Reset clears transcript, activity, plan card, composer and phase."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        reset = _rule_block(script, "resetChat()")

        # Closes any in-flight stream and returns the phase to idle.
        assert "this.finishStream()" in reset
        # Transcript, thoughts and tool activity are cleared.
        assert "this.messages = []" in reset
        assert "this.thoughts = []" in reset
        assert "this.tools = []" in reset
        # Plan card and its approval state are cleared.
        assert "this.plan = null" in reset
        assert 'this.approval = { state: "idle", message: "" }' in reset
        # Composer input is cleared.
        assert 'this.input = ""' in reset

    def test_finish_stream_aborts_controller_and_idles_phase(self) -> None:
        """Aborting the fetch stops the stream and clears the phase (#79).

        Supersedes the EventSource close() contract: the stream handle is now
        an AbortController, and abort() is what cancels an in-flight fetch or
        body read (it is a no-op once the stream already completed).
        """
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        finish = _rule_block(script, "finishStream()")

        assert "this.controller.abort()" in finish
        assert "this.controller = null" in finish
        assert "this.streaming = false" in finish
        assert 'this.statusText = ""' in finish

    def test_stream_handlers_ignore_stale_events_after_reset(self) -> None:
        """Every stream handler bails out once its stream is no longer current.

        "New chat" aborts the fetch mid-flight; frames already buffered can
        still dispatch afterwards. Indexing the cleared transcript would
        throw, and stale activity/plan events would repopulate cleared state,
        so each handler guards on both the phase flag and a per-stream epoch:
        a bare phase check alone would even let stale events through once a
        *new* stream has started (the phase flag is true again), while the
        epoch pins the guard to the stream that registered the handler.

        Superseded for #79: handlers are now entries of the dispatch map
        consumed by the fetch + ReadableStream SSE parser (EventSource cannot
        POST); the guard idiom carries over unchanged.
        """
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        # Each stream captures its own epoch from the shared counter.
        start = _rule_block(script, "startStream(message)")
        assert "this.streamEpoch += 1" in start
        assert "const epoch = this.streamEpoch" in start

        for event in (
            "status",
            "thought",
            "token",
            "tool_call",
            "tool_start",
            "tool_result",
            "plan_proposal",
            "plan",
            "error",
            "done",
        ):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert "if (!this.streaming || epoch !== this.streamEpoch)" in handler, event

    # --- nLPD: client-side only, nothing persisted ----------------------------

    def test_new_chat_reset_is_client_side_only(self) -> None:
        """The reset transmits nothing — no fetch, no beacon, no new stream."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        reset = _rule_block(script, "resetChat()")
        assert reset, "resetChat() must exist"

        assert "fetch" not in reset
        assert "EventSource" not in reset
        assert "XMLHttpRequest" not in reset
        assert "sendBeacon" not in reset

    def test_no_persistent_client_storage(self) -> None:
        """The transcript lives only in memory — no browser persistence."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert "localStorage" not in script
        assert "sessionStorage" not in script
        assert "indexedDB" not in script
        assert "document.cookie" not in script

    # --- AC5: styling contract -------------------------------------------------

    def test_identity_bar_styles_use_tokens(self) -> None:
        """The bar is a token-driven flex row; no new color literals."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        bar = _rule_block(css, ".identity-bar")

        assert "display: flex" in bar
        assert "align-items: center" in bar
        assert "var(--space-" in bar


class TestSseParserContract:
    """SSE parser correctness properties pinned at source level (#79 review).

    The fetch + ReadableStream parser (`consumeSse`/`dispatchSseFrame`) is
    the riskiest JS in the POST transport story, and the repo ships no Node
    toolchain (ADR-006 §5), so — like the rest of the JS — it is contracted
    through source assertions. Three properties the PR claims are pinned so
    a future refactor cannot silently break them:

    1. Whole-buffer ``\\r\\n → \\n`` re-normalisation: sse-starlette
       terminates lines with ``\\r\\n``; a CRLF pair split across two chunk
       boundaries must still be normalised before frames are split. The
       replace therefore runs over the *accumulated* buffer (prior remainder
       + freshly decoded chunk) on every read — not per chunk — and the
       stream tail is normalised too.
    2. Ping-comment skipping: ``": ping …"`` comment frames carry no
       ``event:`` line, so dispatch must require a non-empty event type
       before any handler can run.
    3. Prototype-poisoning dispatch guard: handler lookup must go through
       ``Object.prototype.hasOwnProperty`` so a forged frame with
       ``event: constructor`` / ``event: toString`` cannot dispatch
       inherited Object.prototype keys.
    """

    def test_consume_sse_renormalises_crlf_across_chunk_boundaries(self) -> None:
        """The whole pending buffer is re-normalised on every read (#79)."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        consume = _rule_block(script, "consumeSse(body, handlers)")
        assert consume, "consumeSse(body, handlers) must exist"

        # In-loop normalisation runs over the WHOLE accumulated buffer (prior
        # remainder + freshly decoded chunk) — this is exactly the property
        # that makes a \r\n pair split across two chunk boundaries survive:
        # the second half joins the first half in the buffer before the
        # regex ever runs.
        loop_normalisation = (
            r"(buffer + decoder.decode(value, { stream: true }))" r'.replace(/\r\n/g, "\n")'
        )
        assert loop_normalisation in consume

        # The stream tail (final decode without { stream: true }) is
        # normalised too, so a trailing CRLF cannot leak into the last frame.
        tail_normalisation = r'(buffer + decoder.decode()).replace(/\r\n/g, "\n")'
        assert tail_normalisation in consume

        # Normalisation happens BEFORE frame splitting: the first blank-line
        # boundary search follows the normalising assignment, so no frame is
        # ever split off an un-normalised buffer.
        normalisation_at = consume.index(loop_normalisation)
        first_split_at = consume.index(r'buffer.indexOf("\n\n")')
        assert normalisation_at < first_split_at

        # Frames are delimited by a blank line and the delimiter (2 chars)
        # is consumed — the SSE frame delimiter after \r\n → \n flattening.
        assert r'buffer.indexOf("\n\n")' in consume
        assert "buffer.slice(boundary + 2)" in consume

    def test_dispatch_skips_ping_comment_frames(self) -> None:
        """Comment-only frames (": ping …") never reach any handler."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        dispatch = _rule_block(script, "dispatchSseFrame(frame, handlers)")
        assert dispatch, "dispatchSseFrame(frame, handlers) must exist"

        # Only `event:` lines set the type and only `data:` lines carry
        # payload — a ping comment (": ping …") matches neither, so its type
        # stays "".
        assert 'line.startsWith("event:")' in dispatch
        assert 'line.startsWith("data:")' in dispatch

        # Dispatch requires a non-empty type: the `type &&` half of the
        # guard is what skips comment-only frames, and it precedes the
        # handler invocation.
        guard_at = dispatch.index("if (type &&")
        invocation_at = dispatch.index("handlers[type](")
        assert guard_at < invocation_at

    def test_dispatch_guard_blocks_prototype_keys(self) -> None:
        """Handler lookup goes through hasOwnProperty (prototype poisoning)."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        dispatch = _rule_block(script, "dispatchSseFrame(frame, handlers)")
        assert dispatch, "dispatchSseFrame(frame, handlers) must exist"

        # A forged frame with `event: constructor` / `event: toString` must
        # not dispatch inherited Object.prototype keys: the guard is the
        # canonical hasOwnProperty call, not `in` or a bare map read.
        guard = "Object.prototype.hasOwnProperty.call(handlers, type)"
        assert guard in dispatch
        assert dispatch.index(guard) < dispatch.index("handlers[type](")


class TestMarkdownRenderingContract:
    """Sanitized markdown rendering of assistant messages (#81, ADR-006 §9).

    STRIDE #87 conditions C1/C3 are pinned at source level (no Node
    toolchain, ADR-006 §5): the sanitizer helper composition, the DOMPurify
    default configuration, and the accumulated-string mid-stream render
    path. C4 is pinned by the rewritten test_index_uses_text_interpolation_only;
    C6/C7 (role Literal, history caps, approval gate) are regression guards
    satisfied by not touching models.py/app.py — their API tests must keep
    passing untouched.
    """

    def test_app_js_defines_the_sanitizer_helper(self) -> None:
        """The component exposes renderMarkdown(text) as the only HTML path."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        helper = _rule_block(script, "renderMarkdown(text)")
        assert helper, "renderMarkdown(text) helper must exist"

        # Exact composition (ADR-006 §9): DOMPurify.sanitize(marked.parse(text)).
        assert "window.DOMPurify.sanitize(window.marked.parse(text))" in helper

    def test_sanitizer_uses_dompurify_default_config(self) -> None:
        """C1: the sanitizer call adds no allowlist entries of any kind."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        # The default allowlist strips script/style/iframe/object/embed/form,
        # event handlers, javascript: URIs and every Alpine x-*/@*/:* directive.
        # Any config addition would weaken that contract.
        for forbidden in ("ALLOWED_ATTR", "ALLOWED_TAGS", "ADD_ATTR", "ADD_TAGS"):
            assert forbidden not in script, forbidden

    def test_assistant_binding_sanitizes_the_accumulated_string(self) -> None:
        """C3/AC3: every render re-sanitizes the full accumulated raw text."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        # The binding passes the whole message content — the accumulated raw
        # string — through the helper, so each incremental token render
        # re-sanitizes from scratch (DOMPurify repairs unclosed tags
        # mid-stream; a <scr|ipt> split across tokens is only ever sanitized
        # as a whole).
        assert 'x-html="renderMarkdown(message.content)"' in html

        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        token = _rule_block(script, "token: (event) =>")
        assert token, "token handler must exist"

        # Tokens append RAW text to the accumulated content; no sanitization
        # or HTML construction happens per token.
        assert "this.messages[index].content += data.text" in token
        assert "renderMarkdown" not in token
        assert "sanitize" not in token

    def test_no_html_path_bypasses_the_sanitizer(self) -> None:
        """AC3: no raw HTML injection API outside the DOMPurify helper."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert "innerHTML" not in script
        assert "insertAdjacentHTML" not in script
        assert "document.write" not in script
        # Exactly one sanitize call: the default-config helper.
        assert script.count("DOMPurify.sanitize") == 1

    def test_plan_card_is_not_markdown_rendered(self) -> None:
        """AC5: the plan card stays structured Pydantic→HTML (x-text only)."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        plan_at = html.index('<aside class="panel plan"')
        plan_block = html[plan_at : html.index("</aside>")]

        assert "x-html" not in plan_block
        assert "renderMarkdown" not in plan_block
        # The approve-flow contract is untouched.
        assert "approvePlan()" in plan_block
        assert "rejectPlan()" in plan_block


class TestStreamPhaseContract:
    """Explicit stream phases, error separation & timeout watchdog (#84).

    Three genuine v0.5 defects are fixed here and pinned at source level
    (no Node toolchain, ADR-006 §5 — JS is contracted via source assertions):

    1. Handler collision (AC2): the typed ``error`` handler and the
       transport-error path are separate code paths. The typed handler must
       NOT finish the stream — agent.py emits a typed ``error`` FOLLOWED BY
       ``done`` on its OpenRouterError path, so finishing on the typed event
       would flip the compound guards and truncate the follow-up ``done``.
    2. Silent transport failures (AC5): the transport catch path leaves a
       visible, non-blocking error banner — the pre-#84 path only wrote
       statusText, which finishStream() immediately cleared, so a mid-stream
       transport death was invisible once tokens existed.
    3. No timeout (AC3): a 90 s no-event watchdog (backstop above the
       server's 60 s OpenRouter completion timeout and the 30 s MCP
       tool-call timeout) is armed on stream start, reset on every received
       typed event, cleared on stream end, and its timeout transitions to a
       visible error state with a non-blocking banner. Ping comment frames
       deliberately do NOT reset it: a wedged agent behind a live transport
       must still trip the watchdog.

    The phase is a plain string property — idle | waiting | streaming |
    tooling | error (KIS: no state-machine framework) — and the #80 compound
    stale-event guards in all ten handlers carry over verbatim.
    """

    PHASES = ("idle", "waiting", "streaming", "tooling", "error")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- AC1: explicit phase property ----------------------------------------

    def test_component_declares_the_explicit_phase_property(self) -> None:
        """The component state carries a plain-string phase starting at idle."""
        script = self._script()

        assert 'phase: "idle"' in script
        for phase in self.PHASES:
            assert f'this.phase = "{phase}"' in script, phase

    def test_start_stream_enters_waiting_before_the_fetch(self) -> None:
        """AC4: the phase flips to waiting synchronously, before any event."""
        start = _rule_block(self._script(), "startStream(message)")

        assert 'this.phase = "waiting"' in start
        waiting_at = start.index('this.phase = "waiting"')
        fetch_at = start.index('fetch("/api/agent/stream"')
        assert waiting_at < fetch_at

    def test_first_token_transitions_to_streaming(self) -> None:
        """The first token moves the phase from waiting to streaming."""
        token = _rule_block(self._script(), "token: (event) =>")

        assert 'this.phase = "streaming"' in token

    def test_thinking_status_returns_to_the_waiting_phase(self) -> None:
        """A thinking status (new agent iteration) returns to the waiting dots."""
        status = _rule_block(self._script(), "status: (event) =>")

        assert 'this.phase = "waiting"' in status

    def test_tool_events_display_the_tooling_substate(self) -> None:
        """AC1: tool_call, tool_start and tool_result all show tooling."""
        script = self._script()

        for event in ("tool_call", "tool_start", "tool_result"):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert 'this.phase = "tooling"' in handler, event

    def test_finish_stream_idles_the_phase(self) -> None:
        """Stream end returns the phase to idle (the banner persists)."""
        finish = _rule_block(self._script(), "finishStream()")

        assert 'this.phase = "idle"' in finish

    def test_reset_chat_returns_to_idle(self) -> None:
        """New chat returns the phase to idle."""
        reset = _rule_block(self._script(), "resetChat()")

        assert 'this.phase = "idle"' in reset

    # --- AC2: typed vs transport error separation -----------------------------

    def test_typed_error_does_not_finish_the_stream(self) -> None:
        """A typed error must not truncate the follow-up done (agent.py:274-280).

        The typed handler surfaces the error and keeps the stream alive so
        the subsequent done event still runs its compound guard and finishes
        the stream normally.
        """
        error = _rule_block(self._script(), "error: (event) =>")

        # The exact call syntax: the typed path must never end the stream.
        assert "this.finishStream()" not in error
        assert 'this.phase = "error"' in error
        assert "this.errorBanner" in error

    def test_done_handler_still_finishes_after_a_typed_error(self) -> None:
        """The done handler keeps its finishStream call (error→done intact)."""
        done = _rule_block(self._script(), "done: (event) =>")

        assert "this.finishStream()" in done

    def test_transport_errors_are_a_separate_path_with_visible_state(self) -> None:
        """AC5: the transport catch surfaces a banner; it is not the typed path.

        The transport path lives in startStream's catch (fetch rejection /
        body-read failure), distinct from the typed handler in
        streamHandlers — and it finishes the stream, unlike the typed
        handler, because no done event will follow a dead transport.
        """
        start = _rule_block(self._script(), "startStream(message)")

        assert "AbortError" in start
        assert "Connection failed" in start
        assert 'this.phase = "error"' in start
        assert "this.errorBanner" in start
        assert "this.finishStream()" in start

    # --- AC3: 90 s no-event watchdog -------------------------------------------

    def test_watchdog_constant_is_90_seconds(self) -> None:
        """The watchdog backstop sits above the server's 60 s OpenRouter timeout."""
        script = self._script()

        assert "WATCHDOG_TIMEOUT_MS = 90000" in script

    def test_watchdog_is_armed_when_the_stream_starts(self) -> None:
        """The watchdog is armed before the fetch dispatches (AC3)."""
        start = _rule_block(self._script(), "startStream(message)")

        assert "this.armWatchdog(epoch, index)" in start
        arm_at = start.index("this.armWatchdog(epoch, index)")
        fetch_at = start.index('fetch("/api/agent/stream"')
        assert arm_at < fetch_at

    def test_watchdog_resets_on_every_received_typed_event(self) -> None:
        """Every typed event restarts the watchdog; ping comments do not."""
        dispatch = _rule_block(self._script(), "dispatchSseFrame(frame, handlers)")

        assert "this.resetWatchdog()" in dispatch
        # The reset is gated on a non-empty event type — ping comment frames
        # carry no event line and must not mask a wedged agent.
        gate_at = dispatch.index("if (type)")
        reset_at = dispatch.index("this.resetWatchdog()")
        assert gate_at < reset_at
        # And the reset happens before the handler dispatch.
        invoke_at = dispatch.index("handlers[type](")
        assert reset_at < invoke_at

    def test_watchdog_clears_when_the_stream_ends(self) -> None:
        """finishStream clears the watchdog (no timer survives the stream)."""
        finish = _rule_block(self._script(), "finishStream()")

        assert "this.clearWatchdog()" in finish

    def test_watchdog_timeout_surfaces_a_visible_non_blocking_error(self) -> None:
        """The timeout transitions to error with a banner and releases the composer."""
        handler = _rule_block(self._script(), "handleWatchdogTimeout()")

        assert 'this.phase = "error"' in handler
        assert "this.errorBanner" in handler
        assert "this.finishStream()" in handler
        # Stale-timer guard: a fired timer from an ended or superseded stream
        # must not corrupt the current one (same idiom as the SSE handlers).
        assert "this.streaming" in handler
        assert "this.watchdogEpoch !== this.streamEpoch" in handler

    # --- AC4: immediate thinking indicator --------------------------------------

    def test_thinking_indicator_shows_pulsing_dots_on_waiting(self) -> None:
        """AC4: a pulsing-dots indicator is bound to the waiting phase."""
        html = self._html()

        assert 'class="thinking-indicator"' in html
        assert "x-show=\"phase === 'waiting'\"" in html
        assert html.count('class="dot"') == 3

    def test_status_line_covers_streaming_tooling_and_error(self) -> None:
        """AC1: the status line is visible in streaming, tooling and error."""
        html = self._html()

        assert "phase === 'streaming'" in html
        assert "phase === 'tooling'" in html
        assert "phase === 'error'" in html

    # --- AC3/AC5: non-blocking error banner ---------------------------------------

    def test_error_banner_is_present_and_non_blocking(self) -> None:
        """The banner is a separate element; the composer is never phase-gated."""
        html = self._html()

        assert 'class="error-banner"' in html
        assert 'x-show="errorBanner"' in html
        assert 'x-text="errorBanner"' in html

        banner_at = html.index('class="error-banner"')
        composer_at = html.index('<form class="composer"')
        assert banner_at < composer_at

        composer = html[composer_at : html.index("</form>", composer_at)]
        # Errors never block the composer: the only disable condition is the
        # in-flight flag (plus the empty-input guard on the button).
        assert ':disabled="streaming"' in composer
        assert "phase" not in composer

    def test_error_banner_clears_on_the_next_send_and_reset(self) -> None:
        """A stale banner never survives into a new stream or a reset."""
        script = self._script()
        start = _rule_block(script, "startStream(message)")
        reset = _rule_block(script, "resetChat()")

        assert 'this.errorBanner = ""' in start
        assert 'this.errorBanner = ""' in reset

    # --- AC1: phase styling ---------------------------------------------------------

    def test_phase_styles_are_tokenized(self) -> None:
        """Thinking dots, error status and banner styles use tokens only."""
        css = self._css()

        thinking = _rule_block(css, ".thinking-indicator")
        assert "display: flex" in thinking

        dot = _rule_block(css, ".thinking-dots .dot")
        assert "var(--accent)" in dot
        assert "animation" in dot

        # Staggered dots: the 2nd/3rd dots delay their pulse.
        assert "nth-child(2)" in css
        assert "nth-child(3)" in css
        assert "animation-delay" in css

        error_status = _rule_block(css, ".status-line--error")
        assert "var(--error)" in error_status

        banner = _rule_block(css, ".error-banner")
        assert "var(--error)" in banner


class TestAutoScrollContract:
    """Non-hijacking auto-scroll for streaming chat (#85, ADR-006 §9).

    The v0.5 chat scrolled unconditionally on every token and every
    transcript mutation, yanking the viewport back to the bottom while the
    user scrolled up to re-read. #85 replaces that with a proximity-armed
    follow flag: within 60 px of the bottom the log follows the stream;
    farther away it pauses, and scrolling back near the bottom re-arms
    following automatically (self-healing — the locked KIS cut explicitly
    drops a "jump to latest" pill).

    No Node toolchain (ADR-006 §5): the JS is contracted via source
    assertions, the same way as the SSE parser and stream-phase contracts.
    """

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- AC1: scrolling up is never hijacked ----------------------------------

    def test_component_declares_the_stick_to_bottom_flag(self) -> None:
        """The follow flag defaults to armed (a fresh log sits at the bottom)."""
        script = self._script()

        assert "stickToBottom: true" in script

    def test_scroll_to_bottom_is_gated_by_the_follow_flag(self) -> None:
        """scrollToBottom() is a no-op while following is paused (AC1)."""
        scroll = _rule_block(self._script(), "scrollToBottom()")

        assert "if (!this.stickToBottom)" in scroll, "follow gate missing"
        # The gate precedes the scroll: a paused log is never touched.
        assert scroll.index("if (!this.stickToBottom)") < scroll.index("log.scrollTo(")

    # --- AC2: proximity arming / self-healing ----------------------------------

    def test_proximity_threshold_is_60px(self) -> None:
        """The follow band is the 60 px proximity threshold from the spec."""
        script = self._script()

        assert "STICK_THRESHOLD_PX = 60" in script

    def test_proximity_arming_compares_bottom_distance_to_threshold(self) -> None:
        """Within 60 px of the bottom ⇒ follow; farther ⇒ pause (AC1/AC2)."""
        update = _rule_block(self._script(), "updateStickToBottom()")

        assert update, "updateStickToBottom() must exist"
        assert "log.scrollHeight - log.scrollTop - log.clientHeight <= STICK_THRESHOLD_PX" in update

    def test_scroll_listener_updates_the_flag(self) -> None:
        """A passive scroll listener on the log re-evaluates the flag."""
        init = _rule_block(self._script(), "init()")

        assert 'addEventListener("scroll"' in init, "scroll listener missing"
        assert "this.updateStickToBottom()" in init
        assert "passive: true" in init

    def test_reset_chat_rearms_following(self) -> None:
        """A fresh chat follows again: the emptied log cannot fire a scroll."""
        reset = _rule_block(self._script(), "resetChat()")

        assert "this.stickToBottom = true" in reset

    # --- AC3: instant token-time scrolls, reduced motion ------------------------

    def test_token_time_scrolls_are_instant(self) -> None:
        """Token-time scrolls use behavior "auto" — never eased (AC3)."""
        scroll = _rule_block(self._script(), "scrollToBottom()")

        assert 'log.scrollTo({ top: log.scrollHeight, behavior: "auto" })' in scroll
        # Explicit supersession: the v0.5 bare scrollTop assignment is gone,
        # and no eased scroll behavior exists anywhere in the component —
        # eased scrolls would stack into jank during rapid token streams.
        assert "log.scrollTop = log.scrollHeight" not in self._script()
        assert "smooth" not in self._script()

    def test_reduced_motion_media_query_disables_animations(self) -> None:
        """prefers-reduced-motion kills the looping animations (AC3)."""
        css = self._css()
        media = _media_block(css, "@media (prefers-reduced-motion: reduce)")

        assert media, "prefers-reduced-motion block missing"
        assert ".pulse" in media
        assert ".thinking-dots .dot" in media
        assert "animation: none" in media
        assert "scroll-behavior: auto" in media
