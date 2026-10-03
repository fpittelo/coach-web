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


def _header_block(html: str) -> str:
    """Return the <header>…</header> block of the page."""
    return html[html.index("<header") : html.index("</header>")]


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
        """The page wires the local stylesheet, Alpine build and component."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "/static/styles.css" in html
        assert "/static/vendor/alpine.min.js" in html
        assert "/static/app.js" in html

    def test_index_uses_text_interpolation_only(self) -> None:
        """User data is bound with x-text; x-html is never used."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "x-text" in html
        assert "x-html" not in html

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
        """The vendor directory ships Alpine and the Inter fonts locally."""
        assert (VENDOR_DIR / "alpine.min.js").is_file()
        assert (VENDOR_DIR / "inter-latin-400-normal.woff2").is_file()
        assert (VENDOR_DIR / "inter-latin-600-normal.woff2").is_file()
        assert (VENDOR_DIR / "inter-latin-700-normal.woff2").is_file()


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
