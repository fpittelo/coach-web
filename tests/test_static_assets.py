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

Issue #82 removes the v0.5 plan aside (`.panel.plan`): the proposed plan
renders as a single inline conversation card beneath the assistant message
that proposed it — planMessageIndex pins the card to the proposing message,
and a new proposal replaces the previous card (KIS: no stacked plan
history). The approve/reject contract is untouched (exact PlanProposal JSON
to POST /api/plan/approve; reject dismisses the card and clears plan
state), and the 4-column interval table scrolls inside an overflow wrapper
with a ≤375px stacking fallback so nothing overflows mobile (AC3).

Issue #83 delivers the composer ergonomics: the single-line <input> becomes
a <textarea rows="1"> that auto-grows with content up to a ~5-line cap
(internal scroll beyond) and shrinks back when the input is cleared. Enter
sends, Shift+Enter inserts a newline, and an IME composition guard
(event.isComposing) keeps the composition-confirm Enter from sending. The
composer is the pinned footer of the chat panel's flex column — the log
grows above it (the v0.5 max-height: 30rem log cap is dropped) — and the
send-disabled bindings (empty input / in-flight stream) are preserved
verbatim. Input remains ephemeral client state (nLPD: no retention change).

The PR #130 review remediation pins the AC3 mechanism itself: flex: 1 +
overflow-y: auto only scroll against a container with a definite main
size, so .chat is viewport-height-bounded (a dvh declaration over a vh
fallback, a 26rem masthead allowance, and a 40rem tall-monitor cap) and
the v0.5 min-height: 32rem floor is superseded — on phones it exceeds the
space below the masthead and would itself push the composer below the
fold. The focus ring selector moves to :focus-visible (text-entry
controls match on every focus), and the auto-grow border correction reads
computed border widths (scrollbar-blind).

Issue #86 closes the sprint with the onboarding empty state and the
accessibility pass. The v0.5 one-liner empty state is replaced by an
onboarding block — hero monogram, the one-line welcome ("Hello! I'm your
coach — what should we work on today?") and exactly 3 static endurance
starter chips (PO Q2/Q7: domain-correct, static client-side strings, no
personal data). Chips prefill the composer and focus it — NEVER
auto-send (PO Q8). The accessibility pass (grill T11) moves the
whole-log aria-live="polite" to a visually-hidden status region that
announces phase changes and the completed FINAL message only — never per
token — while the history itself becomes a role="log" region (implicit
polite additions semantics for newly added messages). Focus returns to
the composer when a stream ends (disabling the textarea on send drops
focus to <body>), never stolen from a deliberate target, and the
prefers-reduced-motion contract from #85 carries over with the chips
introducing no animation of their own.

Issue #149 polishes the #86 announcement posture. The visually-hidden
``role="status"`` region announced the completed final message as RAW
markdown source (``**bold**``, list markers, code fences) — syntax noise
for screen readers. The done handler now announces a markdown-stripped
plain-text rendering (stripMarkdown: emphasis/list/heading/fence/link
markers removed, content kept — KIS regex chain, no new dependency),
while the visual x-html path (renderMarkdown → marked + DOMPurify) is
untouched and stays the sole on-screen source of truth. The role="log"
AT verification itself requires human screen-reader execution and is
documented as a pending procedure in docs/accessibility.md.

Issue #150 closes out the #83/#82 merge follow-ups. On viewports shorter
than the 26rem masthead allowance (landscape phones), the .chat height
calc clamps to 0 and the panel degenerates — a 14rem min-height floor
keeps the panel and its pinned composer usable while staying below the
computed height on typical portrait phones, so the #83 behavior is
unchanged there. The plan card's step loop no longer shadows the message
loop's ``index`` binding (renamed ``stepIndex``, behavior-identical), and
the PLAN_CARD_TEMPLATE literal is collapsed to a single literal — the
only black-stable readable form at line-length 100 — without changing
assertions.

Issue #180 adds the sign-in affordance for auth-enabled lanes: the public
landing route injects a "Sign in" link into the identity bar for
unauthenticated visitors (server-side, via the ``signin-slot`` marker
comment — no JS logic), and the #84 error banner gains a sign-in action
when a stream fails with 401 (expired-session defense-in-depth). Both are
real keyboard-reachable links to /auth/login with tokenized styling; the
banner stays non-blocking and the #84 error-separation contract is
untouched.

Issue #199 collapses the MCP tool-status chatter into a single activity
indicator. The v0.9.2 prod-lane test flooded the console with one raw chip
per tool call ("coach-mcp__icu_get_workout_compliance done; …"), crushing
the debrief card and the RPE stepper out of view. The per-tool chip list is
removed entirely (markup + stylesheet rules); the tooling status line —
already the #84/#148 single status surface — carries the friendly static
label "Coach is checking your training data…" instead, driven by in-flight
call tracking (tool_call adds the call id, tool_result releases it, the
last release returns the phase to the waiting dots, and finishStream clears
the tracker so a failed stream can never leave a lingering indicator). Raw
``coach-mcp__*`` tool names are never rendered or stored client-side (Swiss
nLPD / #142 posture); the typed-error and transport-error paths are
untouched, so failures stay visible (AC3).

Issue #200 removes the stale "Local VIDAR" codename from every
    user-visible string of the main page (masthead kicker, footer privacy
    line, settings lede) — leftover internal hostname meaningless to the
    athlete. The privacy statements themselves stay; only the codename goes.

    Issue #235 (epic #214-A) adds the SVG interval step profile to the plan
    card and the weekly-card zone chips. The profile renders one inline SVG
    bar per step — height proportional to the step's target intensity, %
    of FTP preferred over raw watts, normalized against the plan peak,
    targetless steps rendered as a neutral stub — from an HTML-context
    x-for loop: Alpine ``<template>`` directives cannot live INSIDE
    ``<svg>`` (the parser creates an SVG-namespaced element without a
    ``content`` fragment; verified against the vendored 3.14.9 build), so
    each bar is its own inline ``<svg>``. CSP-clean by construction
    (precedent #207): bar geometry rides presentation attributes and
    classes — no inline style attributes, no event handlers — and the
    profile block carries ONE accessible name (role="img") summarizing
    steps/duration/peak while the interval table stays as the accessible
    per-step alternative (AC3). The week card gains a per-day intensity
    zone chip (Rest/Hard/Easy) derived purely from the delivered
    zones/rest flags (AC4) — word + color, never color alone (#165
    posture). No new I/O anywhere (AC2): both features are pure functions
    of the data the SSE payloads already deliver.

    Issue #249 compresses the chat chrome — PO feedback on the dev lane
    (2026-10-17): the console is height-starved, the fixed chrome above
    the log (masthead, greeting block, status band) consuming too much
    vertical space without value. The masthead paddings/margins are
    trimmed and the h1 clamp capped at 2rem (the identity bar already
    carries the brand), the transient status band becomes a slim inline
    row (band border/padding geometry removed), and the greeting block
    drops to a compact rhythm (space-4 padding, space-2 gap, 32px hero
    monogram, 1.15rem salutation) — content preserved everywhere. The
    measured arithmetic (old 19.85rem @1280 / ~22.9rem worst case under a
    26rem allowance → new 15.05rem @1280 / ~17.5rem worst case under a
    20rem allowance) is documented in the .chat comment per the #207
    honest-recomputation precedent, and the log's computed height at
    1280×800 rises 24rem → 30rem. The composer capsule, single-dispatch
    path, stop-morph and _SIGNIN_SLOT pins are untouched; the floor/cap
    mechanics are rewritten to the new allowance, never deleted.

Issue #279 replaces the terminal-style message presentation (uppercase
USER/ASSISTANT role labels, thin left-border separators) with a
Sendbird-inspired bubble grammar: assistant messages render as
left-aligned rounded neutral cards with the coach monogram avatar on the
left margin, a "Coach" sender label above the bubble and a client-side
receipt time (HH:MM) below it; user messages render as right-aligned
accent bubbles with high text contrast; date dividers render as centered
chips between messages whose client receipt dates differ. The architect
decision is binding: NO chat component library — the vendored CSP-hardened
stack is retained, bubbles are pure CSS + markup restructuring. nLPD
posture: receipt timestamps are client-side only — stamped from the client
clock into in-memory message state at push time, stripped from the history
replay (the wire contract stays {role, content} pairs), never persisted
and never transmitted. Markdown rendering (renderMarkdown + DOMPurify) is
untouched; user messages stay x-text-only (ADR-006 §9, STRIDE #87); all
six structured cards keep rendering beneath/inside the assistant bubble
per the #82 single-active-card rule.
"""

import re

from fastapi.testclient import TestClient

from coach_web.app import _SIGNIN_SLOT, STATIC_DIR, create_app

VENDOR_DIR = STATIC_DIR / "vendor"

# The inline plan card template (#82): pinned to the proposing assistant
# message via planMessageIndex (AC4 — single active card). One literal
# (#150): the v0.5 mid-attribute implicit concatenation was easy to
# misread, and black collapses any concat of this literal onto a single
# line anyway (line-length 100), so the readable form is the literal itself.
PLAN_CARD_TEMPLATE = (
    "<template x-if=\"message.role === 'assistant' && plan && planMessageIndex === index\">"
)

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


def _iter_media_blocks(css: str) -> list[str]:
    """Return the body of every ``@media`` block in the stylesheet."""
    blocks = []
    for match in re.finditer(r"@media[^{]+\{", css):
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
        blocks.append(css[start : index - 1])
    return blocks


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

    def test_index_carries_no_vidar_codename(self) -> None:
        """AC1 (#200): the stale "Local VIDAR" codename is absent from the page.

        Leftover internal codename meaningless to the athlete (PO request,
        v0.9.2 prod-lane test). No VIDAR reference may remain in any
        user-visible UI text of the rendered main page.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "vidar" not in html.lower()

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

        # Single centered column — widened to the #207 chat container band
        # (~72rem; supersedes the v0.7 720–800px reading column, ADR-006 §2).
        assert "max-width: 72rem" in css

    def test_stylesheet_colors_are_tokenized(self) -> None:
        """Every color literal lives in the :root token block (ADR-006, AC1).

        CSS comments are stripped before the scan (#207): issue-reference
        markers like "#207" in commentary are prose, not color literals —
        the contract governs colors in rules, and comment text can never
        render.
        """
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        root = _rule_block(css, ":root")

        root_hex = set(re.findall(r"#[0-9a-f]{3,8}\b", root))
        assert root_hex == set(V07_PALETTE)

        stray_hex = set(re.findall(r"#[0-9a-f]{3,8}\b", css)) - root_hex
        assert not stray_hex, f"hard-coded colors outside tokens: {sorted(stray_hex)}"

        root_rgba = set(re.findall(r"rgba?\([^)]*\)", root))
        stray_rgba = set(re.findall(r"rgba?\([^)]*\)", css)) - root_rgba
        assert not stray_rgba, f"hard-coded rgba outside tokens: {sorted(stray_rgba)}"

    def test_layout_is_single_centered_column(self) -> None:
        """Header, content and footer share one centered column (AC3).

        Rewritten for #278: the compact sticky conversation header replaces
        the .masthead; it still sits in the same centered column as the chat
        panel and footer. Explicit supersession (ADR-006 §2).
        """
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

        for selector in (".conversation-header", ".layout", ".footer"):
            block = _rule_block(css, selector)
            assert "max-width: 72rem" in block, selector
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
        """Message and history travel in the POST body — never in a URL (AC4).

        Rewritten by #262: the body gains the client-derived briefing flag
        (R3 — a boolean, never message content); message and history still
        travel in the body only.
        """
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert "message: message," in script
        assert "history: history," in script
        assert "briefing: requestBriefing," in script
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
        """The identity bar is the content of the centered sticky header."""
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        header = html.index('<header class="conversation-header"')
        bar = html.index('<div class="identity-bar">')
        close = html.index("</header>")
        assert header < bar < close

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

    def test_icons_are_an_inline_svg_sprite(self) -> None:
        """Inline SVGs only: monograms + the vendored Lucide icon sprite (#207).

        Superseded for #207 (explicit, per ADR-006 §2 — never silent
        deletion): the page still carries the two hand-authored monogram
        SVGs, and the composer's icon set ships as an inline SVG sprite —
        hand-vendored Lucide symbols referenced via ``<use href="#i-…">``,
        with the ISC license comment beside the sprite. Still no icon font,
        no JS icon library, no external request, no xlink.
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

        assert "currentColor" in html
        assert 'viewBox="0 0 32 32"' in html
        assert "xlink" not in html
        # The sprite: exactly the six vendored Lucide symbols, each
        # referenced through <use> — no icon font, no external icon request.
        for symbol_id in (
            "i-paperclip",
            "i-sliders-horizontal",
            "i-mic",
            "i-arrow-up",
            "i-square",
            "i-x",
        ):
            assert f'<symbol id="{symbol_id}"' in html, symbol_id
            assert f'href="#{symbol_id}"' in html, symbol_id
        assert html.count("<symbol") == 6
        # The ISC license notice rides the sprite block (the vendor/*.LICENSE
        # pattern mirrored inline — the icons live in the markup itself).
        assert "ISC License" in html
        assert "Lucide" in html

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
        # Plan card and its approval state are cleared (the card pin too, #82).
        assert "this.plan = null" in reset
        assert "this.planMessageIndex = -1" in reset
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
            "week_plan",
            "periodization_plan",
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
        """The transcript lives only in memory — no browser persistence.

        Rewritten by #262 (R3, accepted): the ONE sanctioned localStorage
        key is the morning-briefing date flag — a bare YYYY-MM-DD string,
        no athlete content (a forged flag only re-renders the same
        read-only card). sessionStorage, indexedDB and cookies stay banned.
        """
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert 'localStorage.getItem("coachBriefingDate")' in script
        assert 'localStorage.setItem("coachBriefingDate"' in script
        # No other localStorage use: every occurrence must be the flag key.
        for line in script.splitlines():
            if "localStorage" in line:
                assert "coachBriefingDate" in line
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
        """AC5: the plan card stays structured Pydantic→HTML (x-text only).

        Rewritten for #82: the card is the inline conversation card beneath
        the proposing assistant message (the v0.5 aside is removed).
        """
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        card_at = html.index(PLAN_CARD_TEMPLATE)
        card_block = html[card_at : html.index("</article>", card_at)]

        assert "x-html" not in card_block
        assert "renderMarkdown" not in card_block
        # The approve-flow contract is untouched.
        assert "approvePlan()" in card_block
        assert "rejectPlan()" in card_block


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
        """AC1 (#199): tool events drive the tooling sub-state while in flight.

        Superseded for #199: tool_call and tool_start show tooling, and
        tool_result keeps tooling only while OTHER calls are still in
        flight — once the last in-flight call finishes, the indicator
        clears and the phase returns to the waiting dots (the coach
        reasons over the results next). The accumulating per-tool chips
        are gone.
        """
        script = self._script()

        for event in ("tool_call", "tool_start"):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert 'this.phase = "tooling"' in handler, event

        result = _rule_block(script, "tool_result: (event) =>")
        assert 'this.phase = "tooling"' in result, "tooling while calls remain in flight"
        assert 'this.phase = "waiting"' in result, "the indicator clears once every call finished"

    def test_tool_events_surface_the_friendly_activity_label(self) -> None:
        """AC1/nLPD (#199): tool events surface ONE friendly activity label.

        Supersedes the #148 "Running tools…" label: the tooling status line
        is the single compact activity indicator and its text is static,
        athlete-friendly prose — raw ``coach-mcp__*`` tool names are never
        rendered anywhere. The phase contract itself is pinned by
        test_tool_events_display_the_tooling_substate.
        """
        script = self._script()

        for event in ("tool_call", "tool_start", "tool_result"):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert 'this.statusText = "Coach is checking your training data…"' in handler, event

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


class TestToolStatusIndicatorContract:
    """Single tool activity indicator replaces the accumulating chips (#199).

    The v0.9.2 prod-lane test flooded the console with one raw chip per MCP
    tool call ("coach-mcp__icu_get_workout_compliance done; …"), crushing
    the debrief card and the RPE stepper out of view. The contract now:

    - the per-tool chip list is gone (markup + stylesheet rules) — at most
      ONE compact activity indicator exists, the tooling status line, with
      friendly static text (pinned by TestStreamPhaseContract);
    - raw ``coach-mcp__*`` tool names are never rendered nor stored
      client-side (Swiss nLPD / #142 posture: internal tool schemas stay
      internal);
    - the indicator is driven by in-flight call tracking: ``tool_call``
      adds the call id, ``tool_result`` releases it, and the tracker
      empties when the calls finish — with a ``finishStream`` safety net
      so a failed stream can never leave a lingering indicator;
    - failures stay visible: the typed-error and transport-error paths are
      untouched (pinned by TestStreamPhaseContract) — no silent hiding.

    Source-level assertions only (ADR-006 §5 — no Node toolchain).
    """

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- AC1: no accumulating chips, one indicator -----------------------------

    def test_per_tool_chip_list_is_gone(self) -> None:
        """AC1: the accumulating tool chip list is removed from the page."""
        html = self._html()

        assert 'class="tool-list"' not in html
        assert 'x-for="tool in tools"' not in html
        assert "tool--done" not in html

    def test_per_tool_chip_styles_are_gone(self) -> None:
        """AC1: the chip stylesheet rules are removed (superseded, not dead)."""
        css = self._css()

        assert ".tool-list" not in css
        assert ".tool-name" not in css
        assert ".tool-state" not in css
        assert ".tool--done" not in css
        assert ".tool {" not in css

    def test_tooling_status_line_is_the_single_indicator(self) -> None:
        """AC1: exactly one tool-status surface — the tooling status line."""
        html = self._html()

        # The status line (streaming | tooling | error) remains the single
        # activity indicator; no second tool-specific element was added.
        assert 'class="status-line"' in html
        assert "tool-indicator" not in html
        assert "tool-list" not in html

    # --- nLPD: internal tool names never surface --------------------------------

    def test_raw_tool_names_are_never_rendered_or_stored(self) -> None:
        """nLPD/#142: no raw coach-mcp__* tool name reaches the page or state."""
        html = self._html()
        script = self._script()

        assert "tool.name" not in html
        assert "coach-mcp__" not in html
        # The client no longer even stores the name (nothing to leak later).
        assert "name: data.name" not in script

    # --- in-flight tracking ------------------------------------------------------

    def test_tool_call_tracks_the_in_flight_id(self) -> None:
        """tool_call adds the call id to the in-flight tracker (deduped)."""
        handler = _rule_block(self._script(), "tool_call: (event) =>")

        assert "this.tools.push(data.id)" in handler
        assert "this.tools.includes(data.id)" in handler

    def test_tool_result_releases_the_in_flight_id(self) -> None:
        """tool_result releases the id; an empty tracker clears the indicator."""
        handler = _rule_block(self._script(), "tool_result: (event) =>")

        assert "this.tools = this.tools.filter(" in handler
        assert "this.tools.length === 0" in handler
        assert 'this.statusText = "Thinking"' in handler

    def test_finish_stream_clears_the_in_flight_tracker(self) -> None:
        """A finished or failed stream can never leave a lingering indicator."""
        finish = _rule_block(self._script(), "finishStream()")

        assert "this.tools = []" in finish

    # --- AC3: failures stay visible ----------------------------------------------

    def test_tool_failure_surfaces_stay_untouched(self) -> None:
        """AC3: the error surfaces are independent of the tool tracker.

        The typed-error handler still writes the banner (the #84 contract)
        and the tool handlers never touch it — a failing call cannot be
        silently absorbed by the indicator.
        """
        script = self._script()

        error = _rule_block(script, "error: (event) =>")
        assert "this.errorBanner" in error
        for event in ("tool_call", "tool_start", "tool_result"):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert "errorBanner" not in handler, event


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


class TestInlinePlanCardContract:
    """Inline plan-approval conversation card (#82, ADR-006 single column).

    The v0.5 plan aside (``.panel.plan``) is removed: the proposed plan
    renders as a single conversation card beneath the assistant message that
    proposed it. KIS cut: one active card — a new proposal replaces the
    previous one (no stacked plan history). The approve/reject POST contract
    is untouched (exact ``PlanProposal`` JSON to ``POST /api/plan/approve``),
    reject dismisses the card and clears plan state, and the 4-column
    interval table scrolls inside an overflow wrapper with a ≤375px stacking
    fallback so nothing overflows mobile.

    No Node toolchain (ADR-006 §5): JS is contracted via source assertions,
    the same way as the stream, phase and auto-scroll contracts above.
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- AC1: aside removed, card inline in the chat flow ----------------------

    def test_plan_aside_is_removed(self) -> None:
        """The v0.5 `.panel.plan` aside and its placeholder are gone (AC1)."""
        html = self._html()

        assert '<aside class="panel plan"' not in html
        assert "Plan approval</h2>" not in html
        assert "plan-placeholder" not in html

    def test_plan_card_renders_beneath_the_proposing_message(self) -> None:
        """AC1: the card is a child of the message loop, after the bubble."""
        html = self._html()

        assert PLAN_CARD_TEMPLATE in html, "inline plan card template missing"

        log_at = html.index('<div class="chat-log"')
        loop_at = html.index('<template x-for="(message, index) in messages"')
        bubble_at = html.index('x-html="renderMarkdown(message.content)"')
        card_at = html.index(PLAN_CARD_TEMPLATE)
        composer_at = html.index('<form class="composer"')

        # The card lives inside the chat log's message loop, beneath the
        # assistant bubble it belongs to, and above the composer.
        assert log_at < loop_at < bubble_at < card_at < composer_at

    def test_plan_step_loop_does_not_shadow_the_message_index(self) -> None:
        """AC2 (#150): the step loop index no longer shadows the message index.

        The plan card is nested inside the message loop, whose ``index``
        pins the card via ``planMessageIndex === index``; the step loop's
        own binding shadowed that name. Renaming it to ``stepIndex`` is
        behavior-identical — the loop body only used it as the x-for key.
        """
        html = self._html()

        assert (
            '<template x-for="(step, stepIndex) in plan.steps" :key="stepIndex">' in html
        ), "step loop must bind its own stepIndex"
        assert "(step, index)" not in html, "shadowed step index still present"

    # --- AC4: single active card pinned to the proposing message ---------------

    def test_card_is_pinned_to_the_proposing_message_index(self) -> None:
        """AC4: planMessageIndex pins the card to the proposing message."""
        html = self._html()
        script = self._script()

        assert "planMessageIndex === index" in html
        assert "planMessageIndex: -1" in script

        for event in ("plan_proposal", "plan"):
            handler = _rule_block(script, f"{event}: (event) =>")
            assert "this.planMessageIndex = index" in handler, event

    def test_new_proposal_replaces_the_previous_card(self) -> None:
        """AC4: one plan slot — a new stream clears and re-pins the card."""
        script = self._script()
        start = _rule_block(script, "startStream(message)")

        assert "this.plan = null" in start
        assert "this.planMessageIndex = -1" in start
        # The card clears before the fetch dispatches: the new turn starts
        # with no card, and the next proposal re-pins a fresh one.
        assert start.index("this.planMessageIndex = -1") < start.index('fetch("/api/agent/stream"')

    # --- AC2: approve/reject end-to-end contract preserved ----------------------

    def test_approve_post_contract_is_unchanged(self) -> None:
        """AC2: approve posts the exact PlanProposal JSON wrapper (#82)."""
        script = self._script()
        html = self._html()

        assert 'fetch("/api/plan/approve"' in script
        assert "body: JSON.stringify({ plan: this.plan })" in script
        assert '@click="approvePlan()"' in html
        assert (
            ":disabled=\"approval.state === 'submitting'"
            " || approval.state === 'approved'\"" in html
        )

    def test_reject_dismisses_the_card_and_clears_state(self) -> None:
        """AC2: reject clears the plan, its pin and the approval state."""
        reject = _rule_block(self._script(), "rejectPlan()")

        assert "this.plan = null" in reject
        assert "this.planMessageIndex = -1" in reject
        assert 'this.approval = { state: "idle", message: "" }' in reject

    # --- AC3: interval table never overflows mobile ------------------------------

    def test_interval_table_scrolls_inside_its_wrapper(self) -> None:
        """AC3: the 4-column table scrolls in an overflow-x wrapper."""
        html = self._html()
        css = self._css()

        wrap = _rule_block(css, ".plan-table-wrap")
        assert "overflow-x: auto" in wrap

        wrap_at = html.index('class="plan-table-wrap"')
        table_at = html.index('<table class="interval-table"')
        assert wrap_at < table_at, "interval table must live inside the scroll wrapper"

    def test_plan_card_stacks_at_375px(self) -> None:
        """AC3: at ≤375px the plan meta grid stacks to a single column."""
        css = self._css()
        media = _media_block(css, "@media (max-width: 375px)")

        assert media, "≤375px media query missing"
        assert ".plan-meta" in media
        assert "grid-template-columns: 1fr" in media


class TestComposerErgonomicsContract:
    """Auto-growing textarea composer with Enter-to-send (#83, ADR-006 §9).

    The v0.5 single-line ``<input>`` is replaced by a ``<textarea rows="1">``
    that auto-grows with content up to a ~5-line cap (internal scroll
    beyond) and shrinks back when the input is cleared. Enter sends,
    Shift+Enter inserts a newline, and an IME composition guard
    (``event.isComposing``) keeps the composition-confirm Enter from
    sending. The composer is the pinned footer of the chat panel's flex
    column — the log grows above it (the v0.5 ``max-height: 30rem`` log cap
    is dropped) — and the send-disabled bindings (empty input / in-flight
    stream) are preserved verbatim. Input remains ephemeral client state
    (nLPD: no retention change). The panel itself is viewport-height-bounded
    (PR #130 review remediation): a definite main size is what makes
    ``flex: 1`` + ``overflow-y: auto`` on the log actually pin the composer
    footer.

    No Node toolchain (ADR-006 §5): JS is contracted via source assertions,
    the same way as the stream, phase, auto-scroll and plan-card contracts.
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _composer(self) -> str:
        html = self._html()
        composer_at = html.index('<form class="composer"')
        return html[composer_at : html.index("</form>", composer_at)]

    # --- AC1: auto-growing textarea ---------------------------------------------

    def test_composer_is_a_single_row_textarea(self) -> None:
        """AC1: the single-line input is replaced by a rows="1" textarea."""
        composer = self._composer()

        assert "<textarea" in composer
        assert 'rows="1"' in composer
        assert "<input" not in composer
        # The x-model / disabled bindings carry over verbatim (#79/#84).
        assert 'x-model="input"' in composer
        assert ':disabled="streaming"' in composer

    def test_auto_grow_resets_height_before_measuring(self) -> None:
        """AC1: autoGrowTextarea() resets to auto, then sizes to scrollHeight."""
        grow = _rule_block(self._script(), "autoGrowTextarea()")

        assert grow, "autoGrowTextarea() must exist"
        assert 'textarea.style.height = "auto"' in grow
        assert "textarea.scrollHeight" in grow

    def test_auto_grow_border_correction_is_scrollbar_blind(self) -> None:
        """AC1: the border correction reads computed border widths.

        A client/offset box delta also absorbs a horizontal scrollbar's
        height (~15px) when one appears, over-sizing the box past the
        fitted size (PR #130 review nit). Computed border widths are
        scrollbar-blind and drift-free (they follow the stylesheet).
        """
        grow = _rule_block(self._script(), "autoGrowTextarea()")

        assert "offsetHeight" not in grow, "box delta absorbs horizontal scrollbars"
        assert "borderTopWidth" in grow, "computed top border missing"
        assert "borderBottomWidth" in grow, "computed bottom border missing"

    def test_auto_grow_follows_every_input_change(self) -> None:
        """AC1: the grow hook watches the input model (type, paste, clear).

        The measurement must run on the next tick so the x-model DOM write
        lands before scrollHeight is read — otherwise a programmatic clear
        (send / reset) would measure the stale value and never shrink.
        """
        init = _rule_block(self._script(), "init()")

        assert (
            'this.$watch("input", () => this.$nextTick(() => this.autoGrowTextarea()));' in init
        ), "$watch(input) → $nextTick → autoGrowTextarea() hook missing"

    def test_textarea_caps_at_eight_lines_and_scrolls_internally(self) -> None:
        """AC1 (#207): the CSS cap is ~8 lines; overflow scrolls inside the box.

        Supersedes the #83 five-line cap (explicit, per ADR-006 §2): the
        floating capsule grows the composer to ~8 lines before the internal
        scroll engages.
        """
        css = self._css()
        textarea = _rule_block(css, ".composer textarea")

        assert "max-height:" in textarea, "textarea max-height cap missing"
        assert "12em" in textarea, "cap must encode 8 lines × 1.5 line-height"
        assert "overflow-y: auto" in textarea, "internal scroll beyond the cap missing"
        assert "resize: none" in textarea, "manual resize handle must be gone"

    def test_send_clears_input_and_shrinks_via_the_watcher(self) -> None:
        """AC1: clearing the input after send shrinks the box (same watcher).

        No parallel height-reset mechanism is added in send() — the shrink
        is the $watch("input") → autoGrowTextarea() path (KIS).
        """
        send = _rule_block(self._script(), "send()")

        assert 'this.input = ""' in send
        assert "style.height" not in send

    # --- AC2: Enter-to-send keyboard contract ------------------------------------

    def test_keydown_handler_sends_on_plain_enter(self) -> None:
        """AC2 (#207 rewrite): Enter (no Shift) prevents the newline and sends.

        The dispatch route changes with the single-dispatch guard: the
        keydown handler no longer calls send() directly — it routes through
        the form's submit event via requestSubmit(), the ONE send() dispatch
        path (AC 1.3). The observable contract is unchanged: plain Enter
        sends, the newline is prevented.
        """
        handler = _rule_block(self._script(), "onComposerKeydown(event)")

        assert handler, "onComposerKeydown(event) must exist"
        assert 'event.key === "Enter" && !event.shiftKey' in handler
        assert "event.preventDefault()" in handler
        assert "requestSubmit()" in handler
        assert "this.send()" not in handler, "keydown must not be a second send() path"

    def test_keydown_handler_guards_ime_composition(self) -> None:
        """AC2: the composition-confirm Enter never sends (IME guard)."""
        handler = _rule_block(self._script(), "onComposerKeydown(event)")

        assert "event.isComposing" in handler
        # The guard precedes the dispatch branch: a composing Enter returns
        # early — never reaching the requestSubmit() route into send().
        assert handler.index("event.isComposing") < handler.index("requestSubmit()")

    def test_textarea_wires_the_keydown_handler(self) -> None:
        """AC2: the composer textarea binds onComposerKeydown($event)."""
        composer = self._composer()

        assert '@keydown="onComposerKeydown($event)"' in composer

    # --- AC3: pinned composer footer ----------------------------------------------

    def test_chat_log_grows_without_max_height_cap(self) -> None:
        """AC3: the v0.5 max-height: 30rem log cap is dropped (supersession)."""
        css = self._css()
        log = _rule_block(css, ".chat-log")

        assert "flex: 1" in log, "the log must remain the growing flex child"
        assert "overflow-y: auto" in log, "the log must still scroll internally"
        assert "max-height" not in log, "the 30rem log cap must be gone"
        assert "max-height: 30rem" not in css

    def test_composer_is_the_pinned_panel_footer(self) -> None:
        """AC3: the composer is the last child of a height-bounded flex column.

        DOM order and flex direction alone pin nothing: ``flex: 1`` +
        ``overflow-y: auto`` on the log only scroll against a container
        with a DEFINITE main size — an auto-height column grows with the
        transcript and pushes the composer below the fold (PR #130 review
        blocker). The panel is therefore viewport-height-bounded: a dvh
        declaration (tracks mobile browser chrome) overriding a vh
        fallback, plus a max-height cap for very tall monitors. The
        header allowance is the #278 compact sticky header value (3rem).
        """
        html = self._html()
        css = self._css()

        panel_at = html.index('<section class="panel chat"')
        close_at = html.index("</section>", panel_at)
        log_at = html.index('<div class="chat-log"')
        composer_at = html.index('<form class="composer"')

        assert panel_at < log_at < composer_at < close_at
        chat = _rule_block(css, ".chat")
        assert "display: flex" in chat
        assert "flex-direction: column" in chat
        # The actual pinning mechanism: a definite, viewport-relative
        # height on the flex column (dvh over vh fallback) + a cap for
        # very tall monitors. Without it the panel grows with the
        # transcript and the footer scrolls away.
        assert "height: calc(100vh - 3rem)" in chat, "vh fallback missing"
        assert "height: calc(100dvh - 3rem)" in chat, "dvh override missing"
        assert "max-height:" in chat, "tall-monitor cap missing"

    def test_chat_has_a_short_viewport_height_floor(self) -> None:
        """AC1 (#150, #278 rewrite): the panel keeps a usable floor on short viewports.

        On viewports shorter than the 3rem sticky header allowance (landscape
        phones), ``calc(100dvh - 3rem)`` clamps to 0 and the panel — log
        AND composer — degenerates. A min-height floor resolves the used
        height to ``max(calc(100dvh - 3rem), floor)``; at 14rem it stays
        below the computed height on typical portrait phones (≥ 34rem
        tall), so the #83 portrait behavior is unchanged there.
        """
        chat = _rule_block(self._css(), ".chat")

        assert "min-height: 14rem" in chat, "short-viewport floor missing"

    # --- AC4: send-disabled bindings preserved ------------------------------------

    def test_send_button_disabled_when_empty_or_streaming(self) -> None:
        """AC4 (#207 rewrite): the submit control's disable binding.

        Supersedes the #83 verbatim binding: the morphing submit control is
        disabled only while IDLE with a whitespace-only input — during a
        stream it is the enabled Stop control (AC 1.5). The streaming send
        guard lives in send() itself (unchanged below).
        """
        composer = self._composer()

        assert ":disabled=\"!streaming && input.trim() === ''\"" in composer

    def test_send_guard_still_blocks_empty_and_in_flight(self) -> None:
        """AC4: send() keeps its empty-input and streaming guards."""
        send = _rule_block(self._script(), "send()")

        assert "if (!message || this.streaming)" in send

    # --- AC5: token-based focus ring ------------------------------------------------

    def test_textarea_has_token_based_focus_ring(self) -> None:
        """AC5 (#207 rewrite): the composer focus ring is drawn from the accent token.

        Supersedes the per-textarea ring: the borderless textarea inside the
        capsule hands the focus ring to the capsule itself —
        ``:focus-within`` lights the whole card while any child (textarea
        included) holds focus, so the ring stays visible for keyboard AND
        pointer users.
        """
        css = self._css()
        focus = _rule_block(css, ".composer:focus-within")

        assert "outline" in focus, "focus ring missing"
        assert "var(--accent)" in focus, "focus ring must use the accent token"


class TestFloatingComposerContract:
    """Floating composer capsule, stop-morph & single dispatch (#207).

    Phase 1 of epic #206 (LibreChat aesthetic): the composer becomes an
    enclosed rounded capsule — textarea on top, a single utility dock row
    inside the capsule (clip + tools left, mic + submit right) — with the
    submit control morphing into a Stop square while a stream is in flight.

    The four study landmines are contracted here:
    1. Double-send: the form's submit event is the ONLY send() dispatch
       path; the keydown handler routes Enter through requestSubmit() into
       the same handler, so keydown and implicit submit can never both fire
       send().
    2. Stop-morph: the Stop control drives the existing client-side
       AbortController (no new server endpoint), finalizes the partial
       assistant message and produces NO error banner — the AbortError
       swallow in the transport catch is explicit, not accidental.
    3. Height budget: the capsule lives INSIDE the height-bounded .chat
       panel (the pinned-footer mechanism is untouched); the height-budget
       comment carries the honest recomputation.
    4. _SIGNIN_SLOT: the literal marker comment survives byte-exact
       (cross-checked against app.py's constant).

    app.js behavior is contracted through source assertions (ADR-006 §5 —
    no Node toolchain), same as every stream/composer wiring test above.
    """

    DOCK_ARIA_LABELS = ("Attach file", "Configure tools", "Voice input")
    SUBMIT_ARIA_LABEL = "Send message"
    STOP_ARIA_LABEL = "Stop generating"

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _composer(self) -> str:
        html = self._html()
        composer_at = html.index('<form class="composer"')
        return html[composer_at : html.index("</form>", composer_at)]

    # --- AC 1.1: enclosed floating capsule -------------------------------------

    def test_composer_capsule_is_an_enclosed_rounded_card(self) -> None:
        """AC 1.1: the capsule is a rounded, shadowed, surfaced card."""
        css = self._css()
        capsule = _rule_block(css, ".composer")

        assert "border-radius: 1.25rem" in capsule, "capsule radius missing"
        assert "box-shadow" in capsule, "subtle shadow missing"
        assert "background: var(--paper)" in capsule, "surface background missing"
        assert "border: 1px solid var(--line)" in capsule, "enclosing border missing"

    def test_capsule_floats_inside_the_max_width_container(self) -> None:
        """AC 1.1: the capsule sits in the centered ~72rem container."""
        css = self._css()
        layout = _rule_block(css, ".layout")

        assert "max-width: 72rem" in layout
        assert "margin: 0 auto" in layout

    def test_height_budget_is_preserved_with_an_honest_comment(self) -> None:
        """AC 1.1 (#278 rewrite): the .chat height budget survives compaction.

        The pinned-footer mechanism is untouched: the panel stays
        viewport-height-bounded (dvh over vh fallback, the 3rem sticky
        conversation-header allowance from #278, 14rem floor, 40rem tall-
        monitor cap) and the capsule is its last flex child. The budget
        comment carries the honest recomputation — the #207 capsule
        arithmetic, the #249 old-vs-new allowance arithmetic, and the #278
        compact-header allowance.
        """
        css = self._css()
        chat = _rule_block(css, ".chat")

        assert "height: calc(100vh - 3rem)" in chat, "vh fallback missing"
        assert "height: calc(100dvh - 3rem)" in chat, "dvh override missing"
        assert "min-height: 14rem" in chat, "short-viewport floor missing"
        assert "max-height: 40rem" in chat, "tall-monitor cap missing"

        # The comment above the .chat rule documents the #207, #249 and
        # #278 recomputations.
        css_raw = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        comment_at = css_raw.index(".chat {")
        comment_start = css_raw.rindex("/*", 0, comment_at)
        comment = css_raw[comment_start:comment_at]
        assert "#207" in comment, "height-budget comment must carry the #207 recomputation"
        assert "#249" in comment, "height-budget comment must carry the #249 arithmetic"
        assert "#278" in comment, "height-budget comment must carry the #278 allowance"

    # --- AC 1.4: action dock — single row inside the capsule --------------------

    def test_dock_is_a_single_row_inside_the_capsule(self) -> None:
        """AC 1.4: textarea on top, one utility row below, inside the capsule."""
        composer = self._composer()

        # The textarea precedes the dock; the dock is a single row holding
        # both groups — clip/tools left, mic/submit right.
        textarea_at = composer.index("<textarea")
        dock_at = composer.index('class="composer-dock"')
        tools_at = composer.index('class="dock-tools"')
        actions_at = composer.index('class="dock-actions"')
        assert textarea_at < dock_at < tools_at < actions_at
        clip_at = composer.index("i-paperclip")
        sliders_at = composer.index("i-sliders-horizontal")
        mic_at = composer.index("i-mic")
        arrow_at = composer.index("i-arrow-up")
        assert clip_at < sliders_at < mic_at < arrow_at

    def test_dock_degrades_only_via_flex_wrap(self) -> None:
        """AC 1.4: flex-wrap: wrap is the only narrow-viewport degradation."""
        css = self._css()
        dock = _rule_block(css, ".composer-dock")

        assert "display: flex" in dock
        assert "flex-wrap: wrap" in dock
        # No stacking media query may target the composer/dock.
        for media in _iter_media_blocks((STATIC_DIR / "styles.css").read_text(encoding="utf-8")):
            assert ".composer-dock" not in media, "stacking media query added"
            assert ".composer " not in media and ".composer{" not in media

    def test_placeholder_buttons_are_disabled_with_coming_soon_tooltips(self) -> None:
        """AC 1.4: clip/tools/mic are disabled placeholders — no dummy handlers."""
        composer = self._composer()

        for label in self.DOCK_ARIA_LABELS:
            button_at = composer.index(f'aria-label="{label}"')
            tag_start = composer.rindex("<button", 0, button_at)
            tag = composer[tag_start : composer.index(">", button_at) + 1]
            assert "disabled" in tag, f"{label} must be a disabled placeholder"
            assert 'title="Coming soon"' in tag, f"{label} must carry the honest tooltip"
            # No dummy handlers: the placeholder buttons carry no @click.
            button = composer[button_at : composer.index("</button>", button_at)]
            assert "@click" not in button, label

    def test_icon_buttons_carry_the_aria_labels(self) -> None:
        """AC 1.6: every icon button is labeled for assistive tech."""
        composer = self._composer()

        for label in self.DOCK_ARIA_LABELS:
            assert f'aria-label="{label}"' in composer, label
        # The morphing submit control carries both labels as the dynamic
        # binding's branches (one control, two states).
        assert ":aria-label=\"streaming ? 'Stop generating' : 'Send message'\"" in composer

    def test_icon_buttons_have_visible_focus_rings(self) -> None:
        """AC 1.6: icon buttons and the submit control draw accent focus rings."""
        css = self._css()

        for selector in (".icon-button:focus-visible", ".composer-submit:focus-visible"):
            focus = _rule_block(css, selector)
            assert "outline" in focus, selector
            assert "var(--accent)" in focus, selector

    # --- AC 1.5: submit/stop morph ----------------------------------------------

    def test_submit_control_morphs_into_stop_while_streaming(self) -> None:
        """AC 1.5: the submit button becomes the Stop square during a stream."""
        composer = self._composer()

        assert ":class=\"{ 'composer-submit--stop': streaming }\"" in composer
        assert 'href="#i-arrow-up"' in composer, "send glyph missing"
        assert 'href="#i-square"' in composer, "stop glyph missing"
        # The stop glyph is visible only while streaming (and the send glyph
        # only while idle) — one morphing control, not two buttons.
        assert 'x-show="streaming"' in composer
        assert 'x-show="!streaming"' in composer

    def test_stop_drives_the_existing_abort_path(self) -> None:
        """AC 1.5: Stop drives the client-side AbortController — no new endpoint."""
        script = self._script()
        stop = _rule_block(script, "stopStreaming()")

        assert stop, "stopStreaming() must exist"
        assert "this.finishStream()" in stop, "Stop must reuse the existing finalize path"
        # No new server endpoint, no second transport: the stop is local.
        assert "fetch" not in stop
        assert "EventSource" not in stop

        # The submit handler branches to the stop while streaming.
        submit = _rule_block(script, "onComposerSubmit()")
        assert "this.stopStreaming()" in submit

    def test_stop_finalizes_the_partial_message_without_an_error_banner(self) -> None:
        """AC 1.5: the partial assistant message stays; no banner is raised."""
        script = self._script()
        stop = _rule_block(script, "stopStreaming()")

        # The accumulated partial content is never overwritten or cleared by
        # the stop path (finishStream leaves messages untouched).
        assert "this.messages" in stop, "stop must reason about the partial bubble"

        # The AbortError swallow is explicit: the transport catch documents
        # that the stream was ended on purpose (resetChat / Stop) — no error
        # banner. The #207 marker pins the deliberate (not accidental)
        # swallow contract.
        catch = _rule_block(script, ".catch((error) =>")
        assert 'error.name === "AbortError"' in catch
        assert "#207" in catch, "the swallow must be documented as deliberate"

    def test_send_has_exactly_one_dispatch_path(self) -> None:
        """AC 1.3: keydown and form submit can never both fire send()."""
        script = self._script()
        html = self._html()

        # Exactly ONE send() call site in the component: the submit handler.
        assert script.count("this.send()") == 1, "send() must have exactly one call site"
        submit = _rule_block(script, "onComposerSubmit()")
        assert "this.send()" in submit

        # The form's submit event is the single dispatch entry; the keydown
        # handler routes into it via requestSubmit().
        assert '@submit.prevent="onComposerSubmit()"' in html
        assert "this.$refs.composerForm.requestSubmit()" in script
        assert 'x-ref="composerForm"' in html

    # --- AC 1.2: greeting visibility & x-text binding ----------------------------

    def test_greeting_renders_only_when_empty_and_no_pending_banner(self) -> None:
        """AC 1.2: the greeting yields to an active pending-debrief banner."""
        html = self._html()
        greeting_at = html.index('<div class="empty-state"')
        greeting = html[greeting_at : html.index('x-for="chip in starterChips"', greeting_at)]

        assert 'x-show="messages.length === 0 && pendingDebriefs.length === 0"' in greeting

    def test_greeting_line_is_bound_via_x_text_only(self) -> None:
        """AC 1.2: the salutation is plain x-text — no new HTML path (ADR-006 §9)."""
        html = self._html()
        script = self._script()

        greeting_at = html.index('<div class="empty-state"')
        greeting = html[greeting_at : html.index('x-for="chip in starterChips"', greeting_at)]
        assert 'x-text="greetingLine"' in greeting
        assert "x-html" not in greeting

        # The getter exists and is time-of-day based (client-side, no fetch).
        getter = _rule_block(script, "get greetingLine()")
        assert getter, "greetingLine getter missing"
        assert "getHours" in getter
        assert "fetch" not in getter

    # --- AC 1.5: reduced motion ---------------------------------------------------

    def test_morph_animation_honors_reduced_motion(self) -> None:
        """AC 1.5: the submit/stop morph transition is disabled under reduce."""
        css_raw = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
        media = _media_block(css_raw, "@media (prefers-reduced-motion: reduce)")

        assert media, "prefers-reduced-motion block missing"
        assert ".composer-submit" in media, "morph transition must be disabled"
        assert "transition: none" in media

    # --- AC 1.7: markup invariants --------------------------------------------------

    def test_signin_slot_survives_byte_exact(self) -> None:
        """AC 1.7: the _SIGNIN_SLOT marker survives the redesign byte-exact.

        Cross-checked against app.py's constant (the / route does a literal
        string replace for the auth landing variant) — drift on either side
        fails here.
        """
        html = self._html()

        assert _SIGNIN_SLOT in html

    def test_stop_stays_client_side_no_new_server_endpoint(self) -> None:
        """AC 1.7: the stop path adds no fetch target beyond the known API."""
        script = self._script()

        # The only fetch targets are the pre-existing API surface.
        endpoints = set(re.findall(r'fetch\("([^"]+)"', script))
        assert endpoints <= {
            "/api/agent/stream",
            "/api/plan/approve",
            "/api/week/approve",
            "/api/periodization/approve",
            "/api/debrief",
            "/api/debrief/pending",
            "/api/objectives",
        }, sorted(endpoints)


class TestCompactStickyHeaderContract:
    """Compact sticky conversation header — reclaim viewport (#278).

    The masthead kicker ("Endurance Cockpit"), h1 and lede are removed
    from the chat view. The existing identity bar (monogram + product
    name + Settings + New chat) becomes the compact sticky conversation
    header pinned above the chat panel. The empty-state greeting already
    carries the welcome copy, so the lede is dropped (not duplicated).

    The dvh-bounded log+composer layout is preserved; only the header
    allowance shrinks from the #249 20rem masthead budget to a 3rem
    sticky-header budget, pushing the primary viewport share to the
    conversation.
    """

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _css_raw(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # --- AC1: kicker, h1 and lede removed --------------------------------------

    def test_masthead_kicker_h1_and_lede_are_removed(self) -> None:
        """AC1: the static banner chrome above the chat panel is gone."""
        html = self._html()
        css = self._css()

        assert '<header class="masthead"' not in html
        assert "Endurance Cockpit" not in html
        assert "<h1>Coach Web</h1>" not in html
        assert '<p class="lede">' not in html
        assert ".kicker" not in css
        assert ".lede" not in css
        assert ".rule" not in css
        assert ".masthead h1" not in css

    def test_identity_bar_is_the_conversation_header(self) -> None:
        """AC1: the identity bar now forms the conversation header."""
        html = self._html()
        header = _header_block(html)

        assert '<header class="conversation-header"' in html
        assert 'class="identity-bar"' in header
        assert 'aria-label="Coach Web"' in header
        assert 'aria-label="Open objectives settings"' in header
        assert 'aria-label="New chat' in header

    # --- AC2: sticky header + viewport-height chat panel -----------------------

    def test_header_is_sticky_above_the_chat_panel(self) -> None:
        """AC2: the header stays visible while the page/log scrolls."""
        css = self._css()
        header = _rule_block(css, ".conversation-header")

        assert "position: sticky" in header
        assert "top: 0" in header
        assert "z-index:" in header

    def test_chat_uses_compact_header_allowance(self) -> None:
        """AC2: the log+composer budget rises to (viewport - 3rem header)."""
        chat = _rule_block(self._css(), ".chat")

        assert "height: calc(100vh - 3rem)" in chat, "vh fallback missing"
        assert "height: calc(100dvh - 3rem)" in chat, "dvh override missing"
        assert "min-height: 14rem" in chat, "short-viewport floor missing"
        assert "max-height: 40rem" in chat, "tall-monitor cap missing"

    def test_height_budget_comment_documents_the_new_allowance(self) -> None:
        """AC2: the .chat comment documents the #278 allowance change."""
        css_raw = self._css_raw()
        comment_at = css_raw.index(".chat {")
        comment_start = css_raw.rindex("/*", 0, comment_at)
        comment = css_raw[comment_start:comment_at]

        assert "#278" in comment, "the #278 allowance must be documented"
        assert "#249" in comment, "the #249 precedent must stay referenced"
        assert "#207" in comment, "the #207 precedent must stay referenced"
        assert "20rem" in comment, "the old allowance must be stated"
        assert "3rem" in comment, "the new allowance must be stated"

    # --- AC5: accessibility landmarks/labels -----------------------------------

    def test_header_carries_an_accessible_name(self) -> None:
        """AC5: the new header landmark has an explicit accessible name."""
        html = self._html()
        header = _header_block(html)

        assert 'aria-label="Coach Web"' in header

    def test_chat_log_keeps_its_log_role(self) -> None:
        """AC5: role='log' and the conversation label are untouched."""
        html = self._html()
        log_at = html.index('<div class="chat-log"')
        log_tag = html[log_at : html.index(">", log_at) + 1]

        assert 'role="log"' in log_tag
        assert 'aria-label="Conversation with your coach"' in log_tag


class TestConsoleChromeCompactionContract:
    """Chat chrome compaction — reclaim log height (#249).

    #278 removes the masthead kicker/h1/lede and folds the identity bar into
    the sticky conversation header, so the masthead-specific #249 assertions
    are superseded by TestCompactStickyHeaderContract. The status band and
    empty-state greeting compaction from #249 are untouched by #278 and are
    retained here to keep their specific value-based regression protection.
    """

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    # --- AC2: slim transient status band -------------------------------------

    def test_status_band_is_a_slim_inline_row(self) -> None:
        """AC2: the activity band loses its border/padding band geometry."""
        activity = _rule_block(self._css(), ".activity")

        assert "margin-top: var(--space-2)" in activity, "slim top margin missing"
        assert "border-top" not in activity, "the band separator must go"
        assert "padding-top" not in activity, "the band padding must go"
        assert "gap: var(--space-2)" in activity, "slim inner rhythm missing"

    def test_status_band_stays_transient(self) -> None:
        """AC2: the band is x-show gated — it never reserves a permanent row."""
        html = self._html()
        activity_at = html.index('<div class="activity"')
        tag = html[activity_at : html.index(">", activity_at) + 1]

        assert "x-show=" in tag, "the band must stay transient (x-show)"

    # --- AC3: slimmed greeting block -----------------------------------------

    def test_greeting_block_padding_and_gap_are_slimmed(self) -> None:
        """AC3: the greeting column drops to the space-4/space-2 rhythm."""
        empty = _rule_block(self._css(), ".empty-state")

        assert "padding: var(--space-4)" in empty
        assert "gap: var(--space-2)" in empty

    def test_hero_monogram_is_compacted(self) -> None:
        """AC3: the hero monogram shrinks 48px → 32px (content preserved)."""
        hero = _rule_block(self._css(), ".monogram--hero")

        assert "width: 32px" in hero
        assert "height: 32px" in hero

    def test_salutation_is_compacted(self) -> None:
        """AC3: the salutation drops 1.5rem → 1.15rem (x-text content unchanged)."""
        line = _rule_block(self._css(), ".greeting-line")

        assert "font-size: 1.15rem" in line


class TestOnboardingEmptyStateContract:
    """Onboarding empty state, starter chips & accessibility pass (#86).

    The v0.5 one-liner empty state is replaced by an onboarding block: a
    hero monogram, the one-line welcome ("Hello! I'm your coach — what
    should we work on today?") and exactly 3 static endurance starter
    chips (PO Q2/Q7 — domain-correct, static client-side strings, no
    personal data; Swiss nLPD). Clicking a chip prefills the composer and
    focuses it — NEVER auto-send (PO Q8).

    The accessibility pass (grill T11) restructures the live-region
    posture: the whole-log ``aria-live="polite"`` is removed — a
    token-by-token stream would spam screen readers — and replaced by

    1. ``role="log"`` on the history: structural chat semantics with
       implicit polite additions announcements for newly added messages;
    2. a visually-hidden ``role="status"`` region announcing phase
       changes (short static labels) and the completed FINAL message —
       never per token.

    Focus returns to the composer when a stream ends (disabling the
    textarea on send drops focus to ``<body>``), and focus is never
    stolen from a deliberate target such as a plan-card button. The
    prefers-reduced-motion contract from #85 carries over; the chips
    introduce no animation of their own.

    No Node toolchain (ADR-006 §5): JS is contracted via source
    assertions, the same way as the stream, phase, auto-scroll, plan-card
    and composer contracts above.
    """

    WELCOME_LINE = "Hello! I'm your coach — what should we work on today?"

    STARTER_CHIPS = (
        "Plan my next training week",
        "How ready am I today?",
        "Review last week's training load",
    )

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _empty_state(self) -> str:
        html = self._html()
        assert '<div class="empty-state"' in html, "empty-state block missing"
        start = html.index('<div class="empty-state"')
        end = html.index("</div>", html.index("</button>", start))
        return html[start:end]

    # --- AC1: onboarding empty state structure ---------------------------------

    def test_v05_one_liner_is_superseded_by_the_welcome_line(self) -> None:
        """AC1: the v0.5 one-liner is gone; the welcome line replaces it.

        Explicit supersession (ADR-006 §2): the old empty-state copy is
        asserted ABSENT so it cannot silently return next to the new one.
        """
        html = self._html()

        assert (
            "Start the conversation with a question about your training." not in html
        ), "v0.5 one-liner must be removed"
        assert self.WELCOME_LINE in html, "welcome line missing"

    def test_empty_state_shows_monogram_welcome_and_chips(self) -> None:
        """AC1 (#207 rewrite): monogram + greeting + welcome + chips.

        The onboarding block becomes the #207 greeting: it renders only when
        the thread is empty AND no pending-debrief banner is active (AC 1.2
        — the banner owns the empty thread's attention), and the time-of-day
        salutation is bound via x-text (ADR-006 §9).
        """
        empty = self._empty_state()

        # The block is the empty-transcript onboarding state, suppressed
        # while a pending-debrief banner is active (AC 1.2, #207).
        assert 'x-show="messages.length === 0 && pendingDebriefs.length === 0"' in empty
        # Hero monogram mark — the coach avatar of the greeting.
        assert 'class="monogram monogram--hero"' in empty
        assert 'aria-hidden="true"' in empty
        # The time-of-day salutation rides x-text (never an HTML path).
        assert 'x-text="greetingLine"' in empty
        # The one-line welcome.
        assert self.WELCOME_LINE in empty
        # The starter chips render from the static array.
        assert 'x-for="chip in starterChips"' in empty
        assert 'x-text="chip"' in empty

    def test_exactly_three_starter_chips_with_exact_strings(self) -> None:
        """AC1: exactly 3 static endurance chips with the exact PO strings."""
        script = self._script()
        match = re.search(r"starterChips:\s*\[(.*?)\]", script, re.DOTALL)
        assert match is not None, "starterChips array missing"

        chips = re.findall(r'"([^"]+)"', match.group(1))
        assert chips == list(self.STARTER_CHIPS)

    def test_hero_monogram_is_sized_and_token_colored(self) -> None:
        """AC1: the hero mark scales the base monogram, accent-token colored."""
        css = self._css()
        hero = _rule_block(css, ".monogram--hero")

        assert "width:" in hero
        assert "height:" in hero
        base = _rule_block(css, ".monogram")
        assert "color: var(--accent)" in base

    # --- AC2: prefill-and-focus, never auto-send ---------------------------------

    def test_chip_click_prefills_and_focuses_never_sends(self) -> None:
        """AC2: a chip fills the composer and focuses it — nothing is sent."""
        script = self._script()
        prefill = _rule_block(script, "prefillChip(chip)")

        assert prefill, "prefillChip(chip) must exist"
        assert "this.input = chip" in prefill
        assert "this.$refs.composer.focus()" in prefill
        # PO Q8: never auto-send — no send/startStream call in the handler.
        assert "this.send()" not in prefill
        assert "startStream" not in prefill

    def test_chips_are_real_buttons_wired_to_prefill(self) -> None:
        """AC2/AC4: chips are keyboard-operable <button>s wired to prefill."""
        empty = self._empty_state()

        assert '<button class="chip" type="button"' in empty
        assert '@click="prefillChip(chip)"' in empty

    # --- AC3: role="log" + visually-hidden status region --------------------------

    def test_chat_log_is_a_log_region_without_whole_log_live(self) -> None:
        """AC3: the history is role="log"; the whole-log aria-live is gone."""
        html = self._html()
        log_at = html.index('<div class="chat-log"')
        log_tag = html[log_at : html.index(">", log_at) + 1]

        assert 'role="log"' in log_tag
        assert "aria-live" not in log_tag, "whole-log aria-live must be removed"

    def test_status_region_is_visually_hidden_and_live(self) -> None:
        """AC3: a visually-hidden role="status" region carries the announcements."""
        html = self._html()
        css = self._css()

        assert 'class="visually-hidden"' in html
        assert 'role="status"' in html
        assert 'aria-live="polite"' in html
        assert 'x-text="liveAnnouncement"' in html

        hidden = _rule_block(css, ".visually-hidden")
        assert "position: absolute" in hidden
        assert "clip" in hidden, "clip pattern missing"
        # display: none would remove the region from the accessibility tree.
        assert "display: none" not in hidden

    def test_phase_changes_are_announced_not_tokens(self) -> None:
        """AC3: phase transitions announce short labels; tokens never do."""
        script = self._script()

        init = _rule_block(script, "init()")
        assert 'this.$watch("phase"' in init, "phase watcher missing"

        announce_phase = _rule_block(script, "announcePhase(phase)")
        assert announce_phase, "announcePhase(phase) must exist"
        for phase in ("waiting", "streaming", "tooling", "error"):
            assert f"{phase}:" in announce_phase, phase

        token = _rule_block(script, "token: (event) =>")
        assert "announce" not in token, "tokens must never announce"

        # Exactly two announce call sites: the phase label and the final
        # message — nothing else may write the live region.
        assert script.count("this.announce(") == 2

    def test_final_message_is_announced_on_done(self) -> None:
        """AC3: the completed FINAL message is announced once, on done."""
        done = _rule_block(self._script(), "done: (event) =>")

        assert "this.announce(" in done
        assert "this.messages[index].content" in done

    def test_reset_chat_clears_the_live_announcement(self) -> None:
        """New chat clears the announcement channel with every state slice."""
        reset = _rule_block(self._script(), "resetChat()")

        assert 'this.liveAnnouncement = ""' in reset

    # --- AC4: keyboard usability & focus management --------------------------------

    def test_focus_returns_to_the_composer_after_send(self) -> None:
        """AC4: stream end refocuses the composer (disable dropped it to body)."""
        finish = _rule_block(self._script(), "finishStream()")

        assert "this.$refs.composer.focus()" in finish
        # Focus is never stolen from a deliberate target (e.g. a plan-card
        # button tabbed to while streaming): the refocus is gated on the
        # current focus having fallen back to <body>.
        assert "document.activeElement" in finish

    # --- AC4: reduced motion & chip styling contract ---------------------------------

    def test_chip_styles_are_tokenized_and_motion_free(self) -> None:
        """AC4: chips use tokens only and introduce no animation/transition."""
        css = self._css()

        chip = _rule_block(css, ".chip")
        assert "border: 1px solid var(--line)" in chip
        assert "var(--radius)" in chip
        assert "animation" not in chip
        assert "transition" not in chip

        focus = _rule_block(css, ".chip:focus-visible")
        assert "outline" in focus
        assert "var(--accent)" in focus

    def test_empty_state_styles_are_centered_and_tokenized(self) -> None:
        """AC1: the onboarding block is a centered, token-driven column."""
        css = self._css()

        empty = _rule_block(css, ".empty-state")
        assert "display: flex" in empty
        assert "flex-direction: column" in empty
        assert "align-items: center" in empty
        assert "var(--space-" in empty

        chips_row = _rule_block(css, ".starter-chips")
        assert "display: flex" in chips_row
        assert "flex-wrap: wrap" in chips_row

    # --- nLPD: onboarding is static, personal-data-free ------------------------------

    def test_onboarding_uses_no_personal_data(self) -> None:
        """Chips are static client-side strings — no fetch, no user data."""
        script = self._script()
        prefill = _rule_block(script, "prefillChip(chip)")
        empty = self._empty_state()

        assert "fetch" not in prefill
        assert "localStorage" not in empty
        assert "sessionStorage" not in empty


class TestAnnouncementPlainTextContract:
    """Markdown-stripped live-region announcement (#149 AC2).

    The visually-hidden ``role="status"`` region announced the completed
    final assistant message as RAW markdown source — ``**bold**``, list
    markers, code fences — which screen readers read as syntax noise.
    #149 strips the common markdown markers from the ANNOUNCED text only:
    the visual rendering path (``renderMarkdown`` → marked + DOMPurify,
    #81) is untouched and remains the sole on-screen source of truth.
    Source-level assertions only (no Node toolchain, ADR-006 §5).
    """

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_strip_markdown_helper_exists(self) -> None:
        """A top-level stripMarkdown(text) helper exists beside parsePayload."""
        helper = _rule_block(self._script(), "stripMarkdown(text)")

        assert helper, "stripMarkdown(text) helper must exist"

    def test_done_handler_announces_the_stripped_text(self) -> None:
        """AC2: the done handler announces stripMarkdown(message.content)."""
        done = _rule_block(self._script(), "done: (event) =>")

        assert "this.announce(stripMarkdown(this.messages[index].content))" in done

    def test_stripper_covers_the_common_markdown_markers(self) -> None:
        """The stripper handles emphasis, lists, headings, fences and links."""
        helper = _rule_block(self._script(), "stripMarkdown(text)")

        # Marker families from #149 AC2: fenced/inline code, links,
        # strong/emphasis, bullet + ordered list markers, headings.
        assert "```" in helper, "fenced code markers"
        assert "`" in helper, "inline code markers"
        assert "\\]\\(" in helper, "link syntax"
        assert "**" in helper, "strong emphasis markers"
        assert "~~" in helper, "strikethrough markers"
        assert "[-*+]" in helper, "bullet list markers"
        assert "\\d" in helper, "ordered list markers"
        assert "#{1,6}" in helper, "heading markers"

    def test_exactly_two_announce_call_sites_are_preserved(self) -> None:
        """AC3 (#149): still exactly two announce() call sites after #149."""
        script = self._script()

        assert script.count("this.announce(") == 2

    def test_visual_rendering_path_is_untouched(self) -> None:
        """AC2: the stripped text is announcement-only; x-html path unchanged."""
        script = self._script()
        html = self._html()
        renderer = _rule_block(script, "renderMarkdown(text)")

        # The visual path keeps its exact #81 composition...
        assert "window.DOMPurify.sanitize(window.marked.parse(text))" in renderer
        # ...and never routes through the announcement stripper.
        assert "stripMarkdown" not in renderer
        # The assistant x-html binding still renders the RAW content.
        assert 'x-html="renderMarkdown(message.content)"' in html
        assert "stripMarkdown" not in html
        # Still exactly one sanitize call: the default-config helper.
        assert script.count("DOMPurify.sanitize") == 1


class TestObjectivesSettingsContract:
    """Objectives settings panel toggled from the identity bar (#166, AC1).

    The settings view is the AC1 delivery path for the Athletic Objective
    Profile (KIS: the conversational onboarding flow is a recorded follow-up,
    not part of this diff). The panel is a modal dialog toggled from the
    header identity bar (#80 patterns): structured Pydantic→HTML bindings
    only (x-text/x-model — the page keeps exactly ONE x-html binding, the
    assistant message body), design tokens throughout, keyboard accessible
    (Escape closes, focus moves into the dialog on open and returns to the
    toggle on close), and it introduces no animation of its own so the
    prefers-reduced-motion contract (#85) is respected trivially.

    app.js behavior is contracted through source assertions (no Node
    toolchain, ADR-006 §5).
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- identity bar toggle ---------------------------------------------------

    def test_settings_toggle_sits_in_the_identity_bar(self) -> None:
        """The toggle lives in the header identity bar (#80 patterns)."""
        header = _header_block(self._html())

        assert 'aria-label="Open objectives settings"' in header
        assert 'aria-controls="settings-panel"' in header
        assert '@click="toggleSettings()"' in header

    def test_settings_toggle_tracks_the_expanded_state(self) -> None:
        """aria-expanded is bound to the open state for assistive tech."""
        header = _header_block(self._html())

        assert ":aria-expanded" in header
        assert "settingsOpen" in header

    def test_single_new_chat_button_is_preserved(self) -> None:
        """Regression guard: the Settings toggle does not disturb #80's contract."""
        html = self._html()

        assert len(re.findall(r">\s*New chat\s*</button>", html)) == 1

    def test_settings_form_root_identifier_is_consistent_across_files(self) -> None:
        """The form-state root declared in app.js is the one bound in index.html.

        B1/B2 regression pin (PR #171 review): the HTML bindings and the
        Alpine component state must share ONE root identifier. A rename on
        one side only leaves the panel runtime-dead (Alpine binds to
        undefined) while per-file string assertions stay green — so the
        declared root, every HTML binding root and every component reference
        are pinned to the same identifier here.
        """
        script = self._script()
        html = self._html()
        form_roots = r"(objectivesForm|settingsForm)"

        declared = re.findall(form_roots + r"\s*:\s*\{", script)
        assert len(declared) == 1, "app.js must declare exactly one form-state root"
        root = declared[0]

        html_roots = set(re.findall(form_roots + r"\.", html))
        assert html_roots, "index.html must bind the form state"
        assert html_roots == {root}, f"HTML binds {html_roots}, app.js declares {root}"

        js_refs = set(re.findall(r"this\." + form_roots, script))
        assert js_refs == {root}, f"app.js references {js_refs}, declares {root}"

    # --- dialog structure --------------------------------------------------------

    def test_settings_panel_is_a_modal_dialog(self) -> None:
        """The panel is a labelled modal dialog."""
        html = self._html()

        assert 'role="dialog"' in html
        assert 'aria-modal="true"' in html
        assert 'aria-labelledby="settings-title"' in html
        assert 'id="settings-title"' in html

    def test_settings_panel_is_hidden_until_toggled(self) -> None:
        """The overlay is x-show-gated and x-cloak'd (no flash before Alpine)."""
        html = self._html()
        class_at = html.index('class="settings-overlay"')
        tag_start = html.rindex("<div", 0, class_at)
        overlay_tag = html[tag_start : html.index(">", class_at) + 1]

        assert 'x-show="settingsOpen"' in overlay_tag
        assert "x-cloak" in overlay_tag

    def test_escape_closes_the_settings_panel(self) -> None:
        """Keyboard users can dismiss the dialog with Escape."""
        html = self._html()

        assert '@keydown.escape.window="closeSettings()"' in html

    # --- form bindings (structured Pydantic→HTML) ---------------------------------

    def test_goal_fields_bind_with_x_model(self) -> None:
        """Every goal field binds via x-model — no HTML construction."""
        html = self._html()

        assert 'x-model="goal.objective_type"' in html
        assert 'x-model="goal.title"' in html
        assert 'x-model="goal.description"' in html
        assert 'x-model="goal.target_metric"' in html
        assert 'x-model="goal.targetValue"' in html
        assert 'x-model="goal.target_date"' in html

    def test_profile_fields_bind_with_x_model(self) -> None:
        """Availability hours and disciplines bind via x-model."""
        html = self._html()

        assert 'x-model="objectivesForm.weeklyAvailabilityHours"' in html
        assert 'x-model="objectivesForm.priorityDisciplines"' in html

    def test_settings_panel_introduces_no_x_html(self) -> None:
        """The page keeps exactly one x-html binding (assistant messages)."""
        html = self._html()

        assert html.count("x-html") == 1

    def test_goals_render_through_a_single_loop(self) -> None:
        """Primary + secondary goals share one loop; the first entry is primary."""
        html = self._html()

        assert '<template x-for="(goal, goalIndex) in objectivesForm.goals"' in html
        assert "goalIndex === 0" in html
        assert "addGoal()" in html
        assert "removeGoal(goalIndex)" in html

    def test_settings_form_initializes_with_a_blank_primary_goal(self) -> None:
        """N4: the form is never empty — a blank primary goal renders pre-load."""
        script = self._script()

        assert "goals: [blankGoalForm()]" in script
        # The blank-goal factory is a top-level function so the state literal
        # and the empty-profile load path share one definition.
        assert "function blankGoalForm()" in script

    def test_goal_type_select_offers_the_madr008_enum(self) -> None:
        """The type select offers exactly the MADR-008 objective_type enum."""
        html = self._html()

        for value in ("outcome", "process", "milestone"):
            assert f'value="{value}"' in html, value

    # --- load/save wiring -----------------------------------------------------------

    def test_settings_load_on_open(self) -> None:
        """Opening the panel loads the persisted profile via GET."""
        script = self._script()
        open_block = _rule_block(script, "openSettings()")
        load = _rule_block(script, "loadObjectives()")

        assert "this.loadObjectives()" in open_block
        assert 'fetch("/api/objectives")' in load
        assert 'method: "PUT"' not in load

    def test_load_surfaces_the_signin_prompt_on_401(self) -> None:
        """A 401 load surfaces "Please sign in." in the settings note (#181 AC2)."""
        load = _rule_block(self._script(), "loadObjectives()")

        assert "Please sign in." in load
        assert "this.settingsSignin = true" in load

    def test_settings_save_via_put(self) -> None:
        """Saving posts the form to PUT /api/objectives."""
        save = _rule_block(self._script(), "saveObjectives()")

        assert 'fetch("/api/objectives"' in save
        assert 'method: "PUT"' in save

    def test_save_normalizes_empty_optionals_to_null(self) -> None:
        """Empty optional numerics serialize as null, not empty strings."""
        script = self._script()
        payload = _rule_block(script, "payloadFromForm()")

        assert 'goal.targetValue === ""' in payload
        assert "Number(goal.targetValue)" in payload
        # Disciplines: comma-separated input → trimmed, empties filtered.
        assert '.split(",")' in payload
        assert ".trim()" in payload
        assert '.filter((item) => item !== "")' in payload

    def test_save_surfaces_the_server_reason_for_known_codes(self) -> None:
        """422 saves render the server's structured reason message (#181).

        The owner-safe reason body {code, message} reaches the settings note:
        the shared helper gates the server message on the client's fixed code
        vocabulary, and the generic text stays as the fallback.
        """
        save = _rule_block(self._script(), "saveObjectives()")

        assert "settingsReasonMessage(" in save
        assert "Could not save objectives. Check the values and retry." in save
        # The generic detail body is never read — the structured body replaced it.
        assert "body.detail" not in save

    def test_save_surfaces_the_signin_prompt_on_401(self) -> None:
        """A 401 save surfaces "Please sign in." — never "check the values" (#181 AC2)."""
        save = _rule_block(self._script(), "saveObjectives()")

        assert "Please sign in." in save
        assert "this.settingsSignin = true" in save

    def test_settings_signin_flag_is_declared_once(self) -> None:
        """The sign-in flag is component state, defaulting off."""
        script = self._script()

        assert "settingsSignin: false" in script
        assert script.count("settingsSignin: ") == 1

    def test_settings_signin_flag_resets_with_the_message(self) -> None:
        """Opening the panel and saving clear the sign-in action (#180 posture).

        The action never outlives its 401 cause: every writer that clears the
        settings message clears the flag with it.
        """
        script = self._script()
        open_block = _rule_block(script, "openSettings()")
        save = _rule_block(script, "saveObjectives()")

        assert "this.settingsSignin = false" in open_block
        assert "this.settingsSignin = false" in save

    def test_settings_modal_renders_the_signin_link(self) -> None:
        """The settings message area carries a real /auth/login link (#181 AC2).

        Pairs with the #180 affordance: the link is a sibling anchor gated on
        the flag — never x-html inside the x-text note — and it sits in the
        message area, before the form's action row.
        """
        html = self._html()
        panel_at = html.index('id="settings-panel"')
        panel = html[panel_at:]
        link_at = panel.index('x-show="settingsSignin"')

        assert 'class="signin-link" x-show="settingsSignin" href="/auth/login"' in panel
        assert ">Sign in</a>" in panel
        # The affordance lives in the message area, above the action row.
        assert link_at < panel.index('class="settings-actions"')

    # --- focus management -------------------------------------------------------------

    def test_focus_moves_into_the_panel_on_open(self) -> None:
        """Opening the dialog moves focus into the panel (behavior contract)."""
        open_block = _rule_block(self._script(), "openSettings()")

        assert "this.$refs.settingsPanel" in open_block
        assert ".focus()" in open_block

    def test_focus_returns_to_the_toggle_on_close(self) -> None:
        """Closing returns focus to the toggle, guarded against steal-when-closed."""
        close_block = _rule_block(self._script(), "closeSettings()")

        assert "this.$refs.settingsToggle" in close_block
        assert ".focus()" in close_block
        assert "if (!this.settingsOpen)" in close_block

    def test_settings_panel_traps_tab_focus(self) -> None:
        """Tab cycles within the dialog (N3: keyboard-only, motion-free trap)."""
        html = self._html()
        script = self._script()

        assert '@keydown.tab="trapSettingsFocus($event)"' in html

        trap = _rule_block(script, "trapSettingsFocus(event)")
        assert trap, "trapSettingsFocus(event) must exist"
        # Focusables are queried within the dialog only — never the page.
        assert "this.$refs.settingsPanel" in trap
        assert 'querySelectorAll("button, input, select, textarea, a[href]")' in trap
        # Hidden controls (x-show, e.g. the primary goal's remove button) and
        # disabled buttons are not Tab stops and never take the wrap.
        assert "offsetParent" in trap
        assert "disabled" in trap
        # Shift+Tab on the first focusable wraps to the last; Tab on the last
        # wraps to the first — preventDefault keeps focus inside the dialog.
        assert "event.shiftKey" in trap
        assert "event.preventDefault()" in trap

    # --- styling contract ----------------------------------------------------------------

    def test_settings_styles_use_tokens(self) -> None:
        """Overlay and panel are token-driven; the overlay covers the viewport."""
        css = self._css()
        overlay = _rule_block(css, ".settings-overlay")
        panel = _rule_block(css, ".settings-panel")

        assert "position: fixed" in overlay
        assert "var(--overlay)" in overlay
        assert "var(--paper)" in panel
        assert "var(--radius)" in panel
        assert "box-shadow" in panel

    def test_settings_introduce_no_animation(self) -> None:
        """No transition/animation of its own — reduced-motion respected (#85)."""
        css = self._css()
        overlay = _rule_block(css, ".settings-overlay")
        panel = _rule_block(css, ".settings-panel")

        assert "transition" not in overlay
        assert "animation" not in overlay
        assert "transition" not in panel
        assert "animation" not in panel

    def test_settings_fields_are_labelled(self) -> None:
        """Every form control sits inside a labelled .field wrapper."""
        html = self._html()

        # The chat UI uses no .field-label class; all occurrences belong to
        # the settings form (8+ labelled controls: type, title, description,
        # metric, value, date per goal template + availability + disciplines).
        assert html.count('class="field-label"') >= 8
        assert "x-text=\"goalIndex === 0 ? 'Primary goal' : 'Secondary goal'\"" in html


class TestPhasesEditorRemoval:
    """The manual phases editor is removed from the settings modal (#182).

    PO design pivot on #167: phase ownership moved from the athlete to the
    coach — *"I'm the athlete, not the coach."* Phases are coach output (the
    ``propose_periodization`` approval card), not athlete input. The settings
    modal returns to the #166 state (objectives only): the ``phasesForm``
    state root, the section markup and the ``.phases-title`` style are all
    gone, while the #166 objectives form and its binding-consistency contract
    stay intact (the mirror test above keeps passing with the remaining
    roots).

    app.js behavior is contracted through source assertions (no Node
    toolchain, ADR-006 §5).
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    # --- structure --------------------------------------------------------------

    def test_settings_modal_has_no_phases_editor(self) -> None:
        """No phases form, heading or add/remove actions remain in the modal."""
        html = self._html()

        assert "savePhases" not in html
        assert "phasesForm" not in html
        assert "addPhase" not in html
        assert "removePhase" not in html
        assert "Periodization phases" not in html

    def test_app_js_has_no_phases_editor_state_or_methods(self) -> None:
        """The phasesForm root and its load/save/normalize methods are gone."""
        script = self._script()

        assert "phasesForm" not in script
        assert "phasesSaving" not in script
        assert "phasesMessage" not in script
        assert "blankPhaseForm" not in script
        assert "loadPhases" not in script
        assert "savePhases" not in script
        assert "payloadFromPhasesForm" not in script
        assert "formFromPlan" not in script
        assert "addPhase" not in script
        assert "removePhase" not in script

    def test_open_settings_loads_only_the_objectives(self) -> None:
        """Opening the panel loads the profile — no phases fetch remains."""
        open_block = _rule_block(self._script(), "openSettings()")

        assert "this.loadObjectives()" in open_block
        assert "loadPhases" not in open_block

    def test_no_athlete_periodization_fetch_remains_in_app_js(self) -> None:
        """The only periodization HTTP call left in app.js is the approval POST."""
        script = self._script()

        assert script.count('"/api/periodization') == 1
        assert 'fetch("/api/periodization/approve"' in script

    # --- styling ------------------------------------------------------------------

    def test_phases_title_style_is_gone(self) -> None:
        """The .phases-title rule is removed from the stylesheet."""
        css = self._css()

        assert ".phases-title" not in css


class TestWeeklyPlanCardContract:
    """Weekly plan card — state pair, chips, approval & adjust contract (#168).

    The weekly microcycle (epic #161 story 2.1) renders as a single
    conversation card pinned to the proposing assistant message via the
    ``weekPlan`` / ``weekMessageIndex`` state pair (#82 replacement rule —
    a new ``week_plan`` event replaces the card). Per-day status chips are
    word + color, never color alone (#165); Adjust prefills the composer and
    NEVER auto-sends (PO Q8); the approval POST carries the validated draft
    plus the optional retry-failed-days dates subset (#163 non-atomic
    writes). JS is contracted via source assertions (no Node toolchain,
    ADR-006 §5).
    """

    WEEK_CARD_TEMPLATE = (
        "<template x-if=\"message.role === 'assistant' && weekPlan && "
        'weekMessageIndex === index">'
    )

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _week_card(self) -> str:
        html = self._html()
        card_at = html.index(self.WEEK_CARD_TEMPLATE)
        return html[card_at : html.index("</article>", card_at)]

    # --- state pair & card pinning -------------------------------------------

    def test_week_state_pair_is_declared_once(self) -> None:
        """The weekPlan/weekMessageIndex pair is declared exactly once."""
        script = self._script()

        assert script.count("weekPlan: null") == 1
        assert script.count("weekMessageIndex: -1") == 1

    def test_week_card_is_pinned_to_the_proposing_message(self) -> None:
        """The card template binds the weekPlan/weekMessageIndex pair."""
        html = self._html()

        assert self.WEEK_CARD_TEMPLATE in html

    def test_week_plan_handler_pins_and_resets_day_statuses(self) -> None:
        """The week_plan handler pins the card and seeds per-day statuses."""
        script = self._script()
        handler = _rule_block(script, "week_plan: (event) =>")

        assert "this.weekPlan = data.draft" in handler
        assert "this.weekMessageIndex = index" in handler
        assert '"proposed"' in handler
        assert "this.scrollToBottom()" in handler

    def test_week_card_is_a_labelled_region(self) -> None:
        """The card is a role=region with the #165 aria-label."""
        card = self._week_card()

        assert 'role="region"' in card
        assert 'aria-label="Proposed weekly plan"' in card

    # --- structured rendering (no markdown) -----------------------------------

    def test_week_card_is_structured_pydantic_to_html(self) -> None:
        """The week card binds x-text only — never markdown/x-html."""
        card = self._week_card()

        assert "x-html" not in card
        assert "renderMarkdown" not in card
        assert "weekPlan.title" in card
        assert "weekPlan.summary" in card
        assert "day.session_title" in card
        assert "day.planned_tss" in card

    def test_week_card_renders_the_week_summary_line(self) -> None:
        """The week summary (total TSS, hard/easy/rest) is rendered."""
        card = self._week_card()

        assert "weekSummaryLine" in card

    # --- per-day status chips: word + color, never color alone -----------------

    def test_day_status_words_are_pinned(self) -> None:
        """Every chip state carries a word — color is never the sole carrier."""
        script = self._script()

        assert '"proposed"' in script
        assert '"pushing…"' in script
        assert '"approved ✓"' in script
        assert '"write failed"' in script

    def test_day_chip_classes_map_to_tokens(self) -> None:
        """Chip color classes exist and resolve to design tokens only."""
        css = self._css()

        assert ".day-chip--proposed" in css
        assert ".day-chip--pushing" in css
        assert ".day-chip--approved" in css
        assert ".day-chip--failed" in css
        approved = _rule_block(css, ".day-chip--approved")
        failed = _rule_block(css, ".day-chip--failed")
        assert "var(--accent)" in approved
        assert "var(--error)" in failed

    # --- approval flow (#163 non-atomic writes) --------------------------------

    def test_approve_week_posts_the_week_and_dates_contract(self) -> None:
        """Approve All posts {week, dates} to /api/week/approve."""
        script = self._script()
        approve = _rule_block(script, "approveWeek(dates)")

        assert 'fetch("/api/week/approve"' in approve
        assert 'method: "POST"' in approve
        assert "JSON.stringify({ week: this.weekPlan, dates: dates || null })" in approve

    def test_approve_week_marks_target_days_pushing_before_the_post(self) -> None:
        """Target days flip to pushing synchronously, before the fetch."""
        approve = _rule_block(self._script(), "approveWeek(dates)")

        pushing_at = approve.index('"pushing"')
        fetch_at = approve.index('fetch("/api/week/approve"')
        assert pushing_at < fetch_at

    def test_partial_failure_surfaces_the_retry_affordance(self) -> None:
        """A partial failure sets the partially_failed state and retry button."""
        script = self._script()
        approve = _rule_block(script, "approveWeek(dates)")

        assert '"partially_failed"' in approve
        retry = _rule_block(script, "retryFailedDays()")
        assert "this.approveWeek(" in retry
        assert '"failed"' in retry

    def test_retry_posts_only_the_failed_dates(self) -> None:
        """The retry affordance collects the failed dates as the subset."""
        retry = _rule_block(self._script(), "retryFailedDays()")

        assert '"failed"' in retry

    def test_transport_error_returns_pushing_days_to_proposed(self) -> None:
        """A failed request never leaves days stuck in pushing."""
        approve = _rule_block(self._script(), "approveWeek(dates)")

        assert '"proposed"' in approve

    # --- Adjust: prefill, never auto-send (PO Q8) -------------------------------

    def test_adjust_day_prefills_the_composer_without_sending(self) -> None:
        """Adjust fills the composer and focuses it — never auto-send."""
        adjust = _rule_block(self._script(), "adjustDay(day)")

        assert "this.input = " in adjust
        assert "this.$refs.composer.focus()" in adjust
        assert "this.send()" not in adjust
        assert "this.startStream(" not in adjust

    def test_rest_days_offer_no_adjust_button(self) -> None:
        """The Adjust button is hidden for rest days."""
        card = self._week_card()

        assert 'x-show="!day.rest_day"' in card

    # --- reject & reset ---------------------------------------------------------

    def test_reject_week_clears_the_card_state(self) -> None:
        """Reject dismisses the card and clears every week state slice."""
        reject = _rule_block(self._script(), "rejectWeek()")

        assert "this.weekPlan = null" in reject
        assert "this.weekMessageIndex = -1" in reject
        assert "this.weekDayStatus = {}" in reject

    def test_reset_chat_clears_the_week_state(self) -> None:
        """New chat clears the weekly card state too."""
        reset = _rule_block(self._script(), "resetChat()")

        assert "this.weekPlan = null" in reset
        assert "this.weekMessageIndex = -1" in reset
        assert "this.weekDayStatus = {}" in reset

    def test_start_stream_clears_the_week_state(self) -> None:
        """A new stream clears the weekly card (the #82 replacement posture)."""
        start = _rule_block(self._script(), "startStream(message)")

        assert "this.weekPlan = null" in start
        assert "this.weekMessageIndex = -1" in start

    # --- accessibility -----------------------------------------------------------

    def test_week_note_is_a_live_status_region(self) -> None:
        """The approval note is a role=status region (WCAG 4.1.3, #165)."""
        card = self._week_card()

        assert 'role="status"' in card

    def test_week_table_headers_are_scoped(self) -> None:
        """The day table declares scope=col headers."""
        card = self._week_card()

        assert 'scope="col"' in card

    # --- mobile stacking (≤375px, #165 wireframe) --------------------------------

    def test_week_table_stacks_on_narrow_viewports(self) -> None:
        """At ≤375px the day table stacks into per-day blocks."""
        css = self._css()
        media = _media_block(css, "@media (max-width: 375px)")

        assert ".week-table" in media
        assert "display: block" in media

    def test_periodization_table_stacks_on_narrow_viewports(self) -> None:
        """At ≤375px the phase table stacks into per-phase blocks.

        Mirrors the .week-table pattern (PR #184 review finding): the
        data-label attributes in the markup become the per-cell labels, so
        the phase table stays readable on narrow viewports.
        """
        css = self._css()
        media = _media_block(css, "@media (max-width: 375px)")

        assert ".periodization-table" in media
        assert "display: block" in media
        assert "attr(data-label)" in media

    def test_week_table_scrolls_in_an_overflow_wrapper(self) -> None:
        """The desktop day table rides the existing overflow-x wrapper."""
        card = self._week_card()

        assert "plan-table-wrap" in card


class TestPeriodizationCardContract:
    """Periodization approval card — state pair, rendering & approval contract (#182).

    The coach-proposed season phase plan renders as a single conversation
    card pinned to the proposing assistant message via the
    ``periodizationPlan`` / ``periodizationMessageIndex`` state pair (#82
    replacement rule — a new ``periodization_plan`` event replaces the card).
    The card shows the phases (type, name, dates, focus) plus the coach's
    rationale; Approve posts ``{plan}`` to POST /api/periodization/approve —
    the server re-validates and persists through the #167 repository, so the
    human-in-the-loop gate is preserved (#87 C7: no phase persistence without
    explicit athlete approval); Reject dismisses without persisting. JS is
    contracted via source assertions (no Node toolchain, ADR-006 §5).
    """

    PERIODIZATION_CARD_TEMPLATE = (
        "<template x-if=\"message.role === 'assistant' && periodizationPlan && "
        'periodizationMessageIndex === index">'
    )

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _card(self) -> str:
        html = self._html()
        card_at = html.index(self.PERIODIZATION_CARD_TEMPLATE)
        return html[card_at : html.index("</article>", card_at)]

    # --- state pair & card pinning -------------------------------------------

    def test_periodization_state_pair_is_declared_once(self) -> None:
        """The periodizationPlan/periodizationMessageIndex pair is declared exactly once."""
        script = self._script()

        assert script.count("periodizationPlan: null") == 1
        assert script.count("periodizationMessageIndex: -1") == 1

    def test_periodization_card_is_pinned_to_the_proposing_message(self) -> None:
        """The card template binds the periodizationPlan/periodizationMessageIndex pair."""
        html = self._html()

        assert self.PERIODIZATION_CARD_TEMPLATE in html

    def test_periodization_plan_handler_pins_the_card(self) -> None:
        """The periodization_plan handler pins the card with the FULL payload.

        Replacement rule (#82): the card is re-pinned per proposal. The
        handler must consume BOTH the plan and the coach's rationale into
        state — storing only the plan would silently drop the rationale line
        the card renders (PR #184 review finding).
        """
        script = self._script()
        handler = _rule_block(script, "periodization_plan: (event) =>")

        assert "this.periodizationPlan = " in handler
        assert "data.plan" in handler
        assert "data.rationale" in handler
        assert "this.periodizationMessageIndex = index" in handler
        assert "this.scrollToBottom()" in handler

    def test_periodization_card_is_a_labelled_region(self) -> None:
        """The card is a role=region with a static aria-label."""
        card = self._card()

        assert 'role="region"' in card
        assert 'aria-label="Proposed periodization plan"' in card

    # --- structured rendering (no markdown) -----------------------------------

    def test_periodization_card_is_structured_pydantic_to_html(self) -> None:
        """The card binds x-text only — never markdown/x-html."""
        card = self._card()

        assert "x-html" not in card
        assert "renderMarkdown" not in card
        assert "phase.phase_type" in card
        assert "phase.name" in card
        assert "phase.start_date" in card
        assert "phase.end_date" in card
        assert "phase.focus" in card
        assert "periodizationPlan.rationale" in card

    def test_periodization_card_loop_does_not_shadow_the_message_index(self) -> None:
        """The phases loop binds phaseIndex — the message index stays intact."""
        card = self._card()

        assert 'x-for="(phase, phaseIndex)' in card
        assert ':key="phaseIndex"' in card

    # --- approval flow (#87 C7 human-in-the-loop gate) --------------------------

    def test_approve_periodization_posts_the_plan_contract(self) -> None:
        """Approve posts {plan} to /api/periodization/approve."""
        script = self._script()
        approve = _rule_block(script, "approvePeriodization()")

        assert 'fetch("/api/periodization/approve"' in approve
        assert 'method: "POST"' in approve
        assert "JSON.stringify({ plan: this.periodizationPlan.plan })" in approve

    def test_approve_periodization_guard_blocks_double_submit(self) -> None:
        """approvePeriodization guards on its own in-flight state."""
        approve = _rule_block(self._script(), "approvePeriodization()")

        assert 'this.periodizationApproval.state === "submitting"' in approve

    def test_approve_periodization_surfaces_the_server_reason(self) -> None:
        """Approval failures render the structured reason, generic fallback kept (#181).

        Extends the #167/PR #184 pin: the server's structured {code, message}
        reason body reaches the card note for known codes (the live case —
        the coverage rule), while the generic text stays the fallback and the
        generic detail body is never read, not even via an error.message
        round-trip (nLPD posture for everything but the fixed reason body).
        """
        approve = _rule_block(self._script(), "approvePeriodization()")

        assert "settingsReasonMessage(" in approve
        assert "Could not save the phase plan. Try again." in approve
        assert "body.detail" not in approve
        assert "error.message" not in approve

    def test_approve_periodization_surfaces_the_signin_prompt_on_401(self) -> None:
        """A 401 approval surfaces "Please sign in." — not "try again" (#181 AC2)."""
        approve = _rule_block(self._script(), "approvePeriodization()")

        assert "Please sign in." in approve

    # --- reject & reset ---------------------------------------------------------

    def test_reject_periodization_clears_the_card_state(self) -> None:
        """Reject dismisses the card without persisting."""
        reject = _rule_block(self._script(), "rejectPeriodization()")

        assert "this.periodizationPlan = null" in reject
        assert "this.periodizationMessageIndex = -1" in reject

    def test_reset_chat_clears_the_periodization_state(self) -> None:
        """New chat clears the periodization card state too."""
        reset = _rule_block(self._script(), "resetChat()")

        assert "this.periodizationPlan = null" in reset
        assert "this.periodizationMessageIndex = -1" in reset

    def test_start_stream_clears_the_periodization_state(self) -> None:
        """A new stream clears the periodization card (the #82 replacement posture)."""
        start = _rule_block(self._script(), "startStream(message)")

        assert "this.periodizationPlan = null" in start
        assert "this.periodizationMessageIndex = -1" in start

    # --- accessibility -----------------------------------------------------------

    def test_periodization_note_is_a_live_status_region(self) -> None:
        """The approval note is a role=status region (WCAG 4.1.3)."""
        card = self._card()

        assert 'role="status"' in card


class TestSettingsReasonRenderingContract:
    """Owner-safe reason rendering — the client side of the #181 vocabulary.

    The server answers settings 422s with ``{code, message}`` from a fixed
    enum (:mod:`coach_web.errors`). The client mirrors that vocabulary and
    renders the server-provided message ONLY when the code is known — an
    unknown or missing code (a future server vocabulary drift, a non-settings
    error shape) falls back to the current generic text (fail-safe). The
    message is bound with x-text — structured data, never x-html (nLPD).
    """

    REASON_CODES = (
        "coverage_target_date",
        "overlap",
        "invalid_dates",
        "invalid_values",
    )

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_reason_vocabulary_is_pinned(self) -> None:
        """The client vocabulary mirrors the server enum exactly — four codes."""
        script = self._script()
        vocabulary = _rule_block(script, "SETTINGS_REASON_CODES = ")

        for code in self.REASON_CODES:
            assert code in vocabulary
        # The vocabulary is a plain frozen map — no dynamic construction.
        assert "Object.keys" not in vocabulary

    def test_reason_helper_renders_only_known_codes(self) -> None:
        """The helper gates the server message on the vocabulary (fail-safe)."""
        helper = _rule_block(self._script(), "settingsReasonMessage(body, fallback)")

        assert "hasOwnProperty" in helper
        assert "typeof body.message" in helper

    def test_reason_helper_takes_the_generic_fallback(self) -> None:
        """Unknown/missing codes return the caller's generic text, unchanged."""
        helper = _rule_block(self._script(), "settingsReasonMessage(body, fallback)")

        assert "return fallback" in helper

    def test_reason_message_is_bound_with_x_text_only(self) -> None:
        """The settings note keeps x-text — the reason body is structured data."""
        html = self._html()
        note_at = html.index('class="settings-note"')
        note = html[note_at : html.index("</p>", note_at)]

        assert 'x-text="settingsMessage"' in note
        assert "x-html" not in note


class TestSignInAffordanceContract:
    """Sign-in affordance for auth-enabled lanes (#180).

    Two surfaces lead an unauthenticated (or expired-session) visitor to
    /auth/login:

    1. Identity bar (server-side): the public landing route swaps the
       ``signin-slot`` marker comment for a real sign-in link when it
       renders an unauthenticated session — no JS logic, no new endpoint.
       The raw static file keeps the inert marker, so /static/index.html
       and auth-disabled lanes are byte-identical to today (AC3).
    2. Error banner (defense-in-depth): a stream failure with status 401
       surfaces a sign-in action next to the #84 banner message, covering
       sessions that expire mid-conversation. The banner stays
       non-blocking and the typed/transport error separation (#84) is
       untouched.

    app.js behavior is contracted through source assertions (ADR-006 §5 —
    no Node toolchain), same as every stream/approval wiring test above.
    """

    SIGNIN_SLOT = "<!-- #180 signin-slot:"

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    # --- identity bar slot (server-injected link) ----------------------------

    def test_identity_bar_declares_the_signin_slot(self) -> None:
        """The marker comment lives inside the identity bar's actions group."""
        html = self._html()
        actions_at = html.index('<div class="identity-actions">')
        slot_at = html.index(self.SIGNIN_SLOT)
        actions_close = html.index("</div>", actions_at)

        assert actions_at < slot_at < actions_close

    def test_signin_link_styles_are_tokenized(self) -> None:
        """The link rides the v0.7 tokens with a visible keyboard focus ring."""
        css = self._css()
        link = _rule_block(css, ".signin-link")

        assert "var(--accent)" in link
        assert "text-decoration: underline" in link

        focus = _rule_block(css, ".signin-link:focus-visible")
        assert "outline" in focus
        assert "var(--accent)" in focus

    # --- 401 banner sign-in action ---------------------------------------------

    def test_error_banner_surfaces_signin_on_401(self) -> None:
        """The banner carries a sign-in link gated on the 401 flag."""
        html = self._html()
        banner_at = html.index('class="error-banner"')
        banner = html[banner_at : html.index("</p>", banner_at)]

        assert 'x-show="errorBannerSignin"' in banner
        assert 'href="/auth/login"' in banner
        assert ">Sign in</a>" in banner

    def test_component_declares_the_banner_signin_flag(self) -> None:
        """The flag is explicit component state, defaulting to hidden."""
        script = self._script()

        assert "errorBannerSignin: false" in script

    def test_stream_failure_carries_the_response_status(self) -> None:
        """The !ok branch attaches the HTTP status to the thrown error."""
        start = _rule_block(self._script(), "startStream(message)")

        assert "failure.status = response.status" in start

    def test_401_failure_sets_the_banner_signin_flag(self) -> None:
        """Only a 401 transport failure arms the banner sign-in action."""
        start = _rule_block(self._script(), "startStream(message)")

        assert "this.errorBannerSignin = error.status === 401" in start

    def test_banner_signin_flag_resets_with_the_banner(self) -> None:
        """A stale sign-in action never survives into a new stream or a reset."""
        script = self._script()
        start = _rule_block(script, "startStream(message)")
        reset = _rule_block(script, "resetChat()")

        assert "this.errorBannerSignin = false" in start
        assert "this.errorBannerSignin = false" in reset

    def test_non_401_banner_writers_clear_the_signin_flag(self) -> None:
        """Every other banner writer keeps the flag off (no misleading action)."""
        script = self._script()
        typed = _rule_block(script, "error: (event) =>")
        watchdog = _rule_block(script, "handleWatchdogTimeout()")

        assert "this.errorBannerSignin = false" in typed
        assert "this.errorBannerSignin = false" in watchdog

    def test_banner_signin_keeps_the_84_contracts(self) -> None:
        """The banner stays non-blocking: composer gating and clears unchanged."""
        script = self._script()
        start = _rule_block(script, "startStream(message)")
        reset = _rule_block(script, "resetChat()")

        assert 'this.errorBanner = ""' in start
        assert 'this.errorBanner = ""' in reset
        # The typed handler still never ends the stream (#84 AC2).
        typed = _rule_block(script, "error: (event) =>")
        assert "this.finishStream()" not in typed


# ---------------------------------------------------------------------------
# Debrief capture UI (#189 — Story 3.1): RPE stepper card + pending banner
# ---------------------------------------------------------------------------


class TestDebriefUiContract:
    """RPE stepper card & pending-debrief banner — UI contract (#189, #165).

    The check-in card follows the #182 approval-card interaction pattern
    pinned to the proposing assistant message via the ``rpe`` /
    ``rpeMessageIndex`` state pair; the stepper is two real buttons plus a
    live-region value (no range input — #165 a11y spec); LOG CHECK-IN posts
    ``{plan_session_key, rpe, notes}`` to POST /api/debrief and the card
    collapses to a one-line summary. The pending-debrief banner is a passive,
    dismissible, non-blocking reminder fetched once at init (#165 component
    4, MADR-0010); LOG IT prefills the composer and never auto-sends (PO Q8).
    JS is contracted via source assertions (no Node toolchain, ADR-006 §5).
    """

    RPE_CARD_TEMPLATE = (
        "<template x-if=\"message.role === 'assistant' && rpe && " 'rpeMessageIndex === index">'
    )

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    def _card(self) -> str:
        html = self._html()
        card_at = html.index(self.RPE_CARD_TEMPLATE)
        return html[card_at : html.index("</article>", card_at)]

    # --- state pair & card pinning -------------------------------------------

    def test_rpe_state_pair_is_declared_once(self) -> None:
        """The rpe/rpeMessageIndex pair is declared exactly once (#165 naming)."""
        script = self._script()

        assert script.count("rpe: null") == 1
        assert script.count("rpeMessageIndex: -1") == 1
        assert script.count('rpeApproval: { state: "idle", message: "" }') == 1

    def test_rpe_card_is_pinned_to_the_proposing_message(self) -> None:
        """The card template binds the rpe/rpeMessageIndex pair."""
        html = self._html()

        assert self.RPE_CARD_TEMPLATE in html

    def test_rpe_request_handler_pins_the_card(self) -> None:
        """The rpe_request handler pins the card with the check-in payload."""
        script = self._script()
        handler = _rule_block(script, "rpe_request: (event) =>")

        assert "this.rpe = " in handler
        assert "data.checkin" in handler
        assert "this.rpeMessageIndex = index" in handler
        assert "this.scrollToBottom()" in handler

    def test_rpe_card_is_a_labelled_region(self) -> None:
        """The card is a role=region with a static aria-label (#165 a11y)."""
        card = self._card()

        assert 'role="region"' in card
        assert 'aria-label="Session check-in"' in card

    # --- structured rendering (no markdown) -----------------------------------

    def test_rpe_card_is_structured_pydantic_to_html(self) -> None:
        """The card binds x-text only — never markdown/x-html."""
        card = self._card()

        assert "x-html" not in card
        assert "renderMarkdown" not in card
        assert "rpe.session_title" in card
        assert "rpe.plan_session_key" in card

    # --- stepper (#165: buttons, not a slider) ---------------------------------

    def test_stepper_is_two_real_buttons_with_a_live_value(self) -> None:
        """The stepper is −/+ buttons around an aria-live value — no range input."""
        card = self._card()

        assert 'type="range"' not in card
        assert 'aria-label="Decrease RPE"' in card
        assert 'aria-label="Increase RPE"' in card
        assert 'aria-live="polite"' in card
        assert "of 10" in card

    def test_step_rpe_clamps_to_the_1_10_range(self) -> None:
        """stepRpe clamps the value into 1..10 (RPE domain)."""
        step = _rule_block(self._script(), "stepRpe(delta)")

        assert "Math.min(10" in step
        assert "Math.max(1" in step

    # --- notes & submission -----------------------------------------------------

    def test_notes_textarea_is_labelled(self) -> None:
        """The notes textarea carries an explicit aria-label (#165 a11y)."""
        card = self._card()

        assert 'aria-label="Session notes"' in card

    def test_log_rpe_posts_the_save_contract(self) -> None:
        """LOG CHECK-IN posts {plan_session_key, rpe, notes} to /api/debrief."""
        script = self._script()
        log = _rule_block(script, "logRpe()")

        assert 'fetch("/api/debrief"' in log
        assert 'method: "POST"' in log
        assert "plan_session_key: this.rpe.plan_session_key" in log
        assert "rpe: this.rpeValue" in log
        assert "notes: this.rpeNotes" in log

    def test_log_rpe_guard_blocks_double_submit(self) -> None:
        """logRpe guards on its own in-flight and completed states."""
        log = _rule_block(self._script(), "logRpe()")

        assert 'this.rpeApproval.state === "submitting"' in log
        assert 'this.rpeApproval.state === "submitted"' in log

    def test_log_rpe_surfaces_the_signin_prompt_on_401(self) -> None:
        """A 401 save surfaces "Please sign in." — not "try again" (#180/#181)."""
        log = _rule_block(self._script(), "logRpe()")

        assert "Please sign in." in log

    def test_log_rpe_surfaces_the_encryption_reason_on_503(self) -> None:
        """A 503 save renders the fixed encryption vocabulary, generic fallback kept.

        The server's {code, message} body reaches the card note only when the
        code is in the client's DEBRIEF_ERROR_CODES mirror; unknown codes and
        transport failures keep the generic text, and no error text is ever
        echoed (nLPD #142 posture).
        """
        log = _rule_block(self._script(), "logRpe()")

        assert "debriefReasonMessage(" in log
        assert "Could not save the check-in. Try again." in log
        assert "body.detail" not in log
        assert "error.message" not in log

    def test_debrief_error_vocabulary_mirrors_the_server_enum(self) -> None:
        """The client mirror carries exactly the server's encryption reason code."""
        script = self._script()

        assert "key_unavailable: true" in script

    def test_log_rpe_success_collapses_to_a_summary_line(self) -> None:
        """A 201 save flips the card to the submitted summary state (#165 §4.2)."""
        log = _rule_block(self._script(), "logRpe()")

        assert 'state: "submitted"' in log
        assert "Logged: RPE" in log

    def test_log_rpe_note_carries_the_week_adherence(self) -> None:
        """The collapsed note appends the ISO-week adherence when present (#234 AC3).

        The debrief-save response is the payload that already carries the
        metric (computed on the same save path), so the check-in card's
        collapsed summary renders it — counts and percentage, no new route
        and no new fetch.
        """
        log = _rule_block(self._script(), "logRpe()")

        assert "body.week_adherence" in log
        assert "Week adherence" in log
        assert "body.week_adherence.adherence_pct" in log
        assert "body.week_adherence.completed_count" in log
        assert "body.week_adherence.planned_count" in log

    def test_skip_rpe_dismisses_the_card(self) -> None:
        """Skip clears the card and its pin without persisting anything."""
        skip = _rule_block(self._script(), "skipRpe()")

        assert "this.rpe = null" in skip
        assert "this.rpeMessageIndex = -1" in skip

    def test_reset_chat_clears_the_debrief_state(self) -> None:
        """resetChat clears every debrief state slice (#165 state machine)."""
        reset = _rule_block(self._script(), "resetChat()")

        assert "this.rpe = null" in reset
        assert "this.rpeMessageIndex = -1" in reset
        assert 'this.rpeApproval = { state: "idle", message: "" }' in reset

    def test_rpe_card_renders_the_submitted_summary_state(self) -> None:
        """The card swaps the editing controls for the one-line summary.

        The collapsed summary (#165 §4.2) is the role="status" note bound to
        ``rpeApproval.message`` — logRpe fills it with the "Logged: RPE …"
        line (pinned in the logRpe contract above) — and the editing
        controls hide once the state flips to submitted.
        """
        card = self._card()

        assert "rpeApproval.state !== 'submitted'" in card
        assert "rpeApproval.message" in card

    def test_rpe_card_has_a_status_note(self) -> None:
        """The card's outcome note is a role=status live region (WCAG 4.1.3)."""
        card = self._card()

        assert 'role="status"' in card
        assert "rpeApproval.message" in card

    # --- pending-debrief banner (#165 component 4, MADR-0010) -------------------

    def test_pending_banner_sits_between_the_error_banner_and_the_composer(
        self,
    ) -> None:
        """The banner markup lives in the composer slot, after the error banner."""
        html = self._html()
        error_at = html.index('class="error-banner"')
        banner_at = html.index('class="pending-banner"')
        composer_at = html.index('class="composer"')

        assert error_at < banner_at < composer_at

    def test_pending_banner_is_a_passive_status_region(self) -> None:
        """The banner is role=status (announces on populate) and non-blocking."""
        html = self._html()
        banner_at = html.index('class="pending-banner"')
        banner = html[banner_at : html.index("</template>", banner_at)]

        assert 'role="status"' in banner

    def test_pending_banner_renders_metadata_only(self) -> None:
        """The banner binds session title + key — never notes or RPE (nLPD)."""
        html = self._html()
        banner_at = html.index('class="pending-banner"')
        banner = html[banner_at : html.index("</template>", banner_at)]

        assert "item.session_title" in banner
        assert "item.plan_session_key" in banner

    def test_init_fetches_the_pending_list_once(self) -> None:
        """init() calls loadPendingDebriefs — the only non-SSE data path (#165)."""
        init = _rule_block(self._script(), "init()")

        assert "this.loadPendingDebriefs()" in init

    def test_load_pending_debriefs_fetches_the_derivation_endpoint(self) -> None:
        """The loader GETs /api/debrief/pending and stores the list."""
        load = _rule_block(self._script(), "loadPendingDebriefs()")

        assert 'fetch("/api/debrief/pending"' in load
        assert "this.pendingDebriefs = " in load

    def test_load_pending_debriefs_degrades_to_silence(self) -> None:
        """Failures (401, transport) never surface an error banner (MADR-0010)."""
        load = _rule_block(self._script(), "loadPendingDebriefs()")

        assert "this.errorBanner" not in load

    def test_pending_banner_log_it_prefills_never_auto_sends(self) -> None:
        """LOG IT prefills the composer and focuses it (PO Q8 — never auto-send)."""
        prefill = _rule_block(self._script(), "prefillDebrief(item)")

        assert "this.input = " in prefill
        assert "this.$refs.composer.focus()" in prefill
        assert "this.send()" not in prefill

    def test_pending_banner_dismiss_is_labelled_and_client_only(self) -> None:
        """The × button carries its aria-label; dismissal clears the local list."""
        html = self._html()
        banner_at = html.index('class="pending-banner"')
        banner = html[banner_at : html.index("</template>", banner_at)]

        assert 'aria-label="Dismiss pending debrief reminder"' in banner
        dismiss = _rule_block(self._script(), "dismissPendingDebrief(key)")
        assert "this.pendingDebriefs" in dismiss

    def test_pending_debriefs_state_is_declared(self) -> None:
        """The pendingDebriefs list is declared once in the Alpine state."""
        script = self._script()

        assert script.count("pendingDebriefs: []") == 1

    # --- stylesheet --------------------------------------------------------------

    def test_pending_banner_and_stepper_are_styled_with_tokens(self) -> None:
        """The new components carry stylesheet rules (tokens only — hex guard)."""
        css = self._css()

        assert ".pending-banner" in css
        assert ".rpe-stepper" in css


class TestDebriefSummaryCardContract:
    """Debrief summary card + ICU mirror consent (#190, #165 wireframes).

    Source-level contract pins (ADR-006 §5 — no Node toolchain):

    - the debrief_summary SSE event renders a single conversation card
      pinned to the message that fetched it (#82 replacement rule);
    - the card is structured Pydantic→HTML (x-text only — never markdown,
      ADR-006 §9 / STRIDE #87) with word+token severity chips (#165:
      never color alone);
    - the ICU mirror consent is an opt-in checkbox folded into the LOG
      CHECK-IN card (no second confirmation card — #190 design decision);
    - a mirror failure surfaces the server's fixed soft warning in the
      submitted note while the debrief stays saved (AC3).
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8")

    def _card_block(self) -> str:
        html = self._html()
        marker = "debriefSummary && debriefSummaryMessageIndex === index"
        card_at = html.index(marker)
        start = html.rindex("<template", 0, card_at)
        return html[start : html.index("</template>", card_at)]

    # --- debrief summary card (AC4) ----------------------------------------------

    def test_summary_card_is_pinned_to_the_proposing_message(self) -> None:
        """AC4: the card renders beneath the assistant message that fetched it."""
        assert (
            "message.role === 'assistant' && debriefSummary && debriefSummaryMessageIndex === index"
            in self._html()
        )

    def test_summary_card_is_structured_xtext_never_markdown(self) -> None:
        """AC4/nLPD: the card stays structured Pydantic→HTML (x-text only)."""
        block = self._card_block()

        assert "x-html" not in block
        assert "renderMarkdown" not in block

    def test_summary_card_renders_headline_range_and_counts(self) -> None:
        """AC1: the plain-language read, the range and the report counts render."""
        block = self._card_block()

        assert "debriefSummary.headline" in block
        assert "debriefSummary.oldest" in block
        assert "debriefSummary.newest" in block
        assert "debriefSummary.total_planned" in block
        assert "debriefSummary.completed" in block
        assert "debriefSummary.missed" in block
        assert "debriefSummary.upcoming" in block
        assert "debriefSummary.adherence_rate" in block

    def test_summary_card_renders_per_session_rows_with_severity_words(self) -> None:
        """AC1/AC4: every session row renders with a word+token severity chip."""
        block = self._card_block()
        script = self._script()

        assert "row.session_date" in block
        assert "row.session_title" in block
        assert "row.status" in block
        assert "row.compliance_pct" in block
        assert "row.structure_diff" in block
        assert "row.executed_tss" in block
        assert "severityWord(row.severity)" in block
        assert "'debrief-chip--' + row.severity" in block
        # The word map is static client-side vocabulary — one entry per token.
        for severity in ("ok", "warn", "critical"):
            assert f"{severity}:" in script

    def test_severity_chips_have_stylesheet_rules(self) -> None:
        """The three severity tokens carry stylesheet rules (tokens only)."""
        css = self._css()

        assert ".debrief-chip--ok" in css
        assert ".debrief-chip--warn" in css
        assert ".debrief-chip--critical" in css

    def test_debrief_summary_handler_pins_the_card(self) -> None:
        """The debrief_summary SSE event feeds the pinned card state."""
        handler = _rule_block(self._script(), "debrief_summary: (event) =>")

        assert "this.debriefSummary = data.summary || null" in handler
        assert "this.debriefSummaryMessageIndex = index" in handler

    def test_reset_chat_clears_the_summary_and_consent(self) -> None:
        """A chat reset clears the summary card and the mirror consent."""
        reset = _rule_block(self._script(), "resetChat()")

        assert "this.debriefSummary = null" in reset
        assert "this.debriefSummaryMessageIndex = -1" in reset
        assert "this.mirrorToIcu = false" in reset

    # --- ICU mirror consent (AC3) -------------------------------------------------

    def test_mirror_consent_is_a_checkbox_in_the_checkin_card(self) -> None:
        """AC3: the consent is an opt-in checkbox inside the check-in card."""
        html = self._html()
        card_at = html.index('class="plan-card rpe-card"')
        card = html[card_at : html.index("</template>", card_at)]

        assert "rpe-mirror-field" in card
        assert 'x-model="mirrorToIcu"' in card
        assert 'type="checkbox"' in card

    def test_mirror_consent_defaults_to_off(self) -> None:
        """AC3: strictly opt-in — a new check-in resets the consent to off."""
        handler = _rule_block(self._script(), "rpe_request: (event) =>")

        assert "this.mirrorToIcu = false" in handler

    def test_log_checkin_posts_the_mirror_consent(self) -> None:
        """AC3: the save POST carries the athlete's consent flag."""
        body = _rule_block(self._script(), "logRpe()")

        assert "mirror_to_icu: this.mirrorToIcu" in body

    def test_mirror_soft_warning_surfaces_in_the_submitted_note(self) -> None:
        """AC3: a failed mirror appends the server's fixed soft warning."""
        body = _rule_block(self._script(), "logRpe()")

        assert "body.mirror" in body
        assert "body.mirror.status === " in body
        assert "body.mirror.message" in body

    def test_mirror_field_is_styled(self) -> None:
        """The consent checkbox carries a stylesheet rule (tokens only)."""
        assert ".rpe-mirror-field" in self._css()


class TestSvgStepProfileContract:
    """SVG interval step profile & week-card zone chips (#235, epic #214-A).

    The structured workout plan renders as an inline SVG step profile inside
    the existing plan card — one bar per step, bar height proportional to
    the step's target intensity, with the target watts/% and cadence labels
    bound as x-text beside each bar. The weekly plan card gains a per-day
    intensity zone chip derived from the already-delivered payload.

    Hard repo posture pinned here:

    1. CSP (precedent #207): the profile is sized/positioned via SVG
       presentation attributes and classes — NO inline style attributes and
       NO event handlers anywhere in the new markup; the visualization is
       static.
    2. No new I/O (AC2): the profile derives every value from the plan data
       already carried by the SSE plan payloads — no fetch, no new
       endpoint, no CSP change.
    3. Accessibility (AC3): the profile block carries ONE accessible name
       (role="img" + aria-label summary); the existing interval table stays
       as the accessible per-step alternative.
    4. Word + color chips (AC4 / #165 posture): the zone chip renders its
       word via x-text next to its color class — never color alone.

    Alpine SVG limitation (verified against the vendored Alpine 3.14.9
    build): ``<template>`` directives cannot live INSIDE ``<svg>`` — the
    HTML parser creates an SVG-namespaced element without a ``content``
    document fragment, so Alpine cannot iterate it. The profile therefore
    renders one inline ``<svg>`` bar per step from an HTML-context x-for
    loop, with rect geometry bound via Alpine attribute bindings
    (SVG presentation attributes — CSP-clean).

    Source-level assertions only (ADR-006 §5 — no Node toolchain).
    """

    WEEK_CARD_TEMPLATE = (
        "<template x-if=\"message.role === 'assistant' && weekPlan && "
        'weekMessageIndex === index">'
    )

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _plan_card(self) -> str:
        html = self._html()
        card_at = html.index(PLAN_CARD_TEMPLATE)
        return html[card_at : html.index("</article>", card_at)]

    def _week_card(self) -> str:
        html = self._html()
        card_at = html.index(self.WEEK_CARD_TEMPLATE)
        return html[card_at : html.index("</article>", card_at)]

    def _profile_block(self) -> str:
        """The step-profile region: from its container to the action row."""
        card = self._plan_card()
        start = card.index('class="step-profile"')
        return card[start : card.index('class="plan-actions"', start)]

    # --- AC1: inline SVG step profile inside the plan card ----------------------

    def test_step_profile_renders_inline_svg_bars_in_the_plan_card(self) -> None:
        """AC1: one inline SVG bar per step inside the plan card markup."""
        profile = self._profile_block()

        assert 'class="step-profile"' in profile
        assert 'x-for="(bar, barIndex) in stepProfileRows(plan)"' in profile
        assert 'class="step-profile-bar"' in profile
        assert "<rect" in profile
        assert 'viewBox="0 0 24 100"' in profile
        assert 'preserveAspectRatio="none"' in profile
        # Bar geometry rides SVG presentation attributes (CSP-clean) —
        # never the style attribute.
        assert 'x="0"' in profile
        assert 'width="24"' in profile
        assert ':y="100 - bar.height"' in profile
        assert ':height="bar.height"' in profile
        # The profile hides itself when the payload carries no steps.
        assert 'x-show="plan.steps && plan.steps.length > 0"' in profile

    def test_step_profile_sits_above_the_existing_step_table(self) -> None:
        """AC1/AC3: visual profile first, detailed table second; both stay."""
        card = self._plan_card()

        profile_at = card.index('class="step-profile"')
        wrap_at = card.index('class="plan-table-wrap"')
        table_at = card.index('<table class="interval-table"')
        assert profile_at < wrap_at < table_at

    def test_step_profile_height_is_proportional_to_target_intensity(self) -> None:
        """AC1: bar height ∝ target intensity via the stepProfileRows helper.

        % of FTP is the primary signal (the bounded 0–250 domain; a step
        carrying both watts and % maps by its %). Explicit watts are the
        fallback. Steps without any target render a neutral stub instead of
        inventing a value, and heights normalize against the plan's peak
        intensity.
        """
        rows = _rule_block(self._script(), "stepProfileRows(plan)")
        assert rows, "stepProfileRows(plan) helper must exist"

        # Reads the delivered payload only (AC2) ...
        assert "plan.steps" in rows
        assert "fetch" not in rows
        # ... prefers % of FTP, with watts as the fallback ...
        assert rows.index("target_power_pct") < rows.index("target_power_watts")
        # ... normalizes each bar against the plan peak ...
        assert "/ peak" in rows
        # ... and stubs targetless steps without inventing a value.
        assert "STEP_PROFILE_STUB_PCT" in rows

    def test_step_profile_labels_render_target_and_cadence(self) -> None:
        """AC1: the per-bar labels carry the target watts/% and cadence."""
        profile = self._profile_block()
        rows = _rule_block(self._script(), "stepProfileRows(plan)")

        assert 'x-text="bar.target"' in profile
        assert 'x-text="bar.cadence"' in profile

        target = _rule_block(self._script(), "stepTargetText(step)")
        assert target, "stepTargetText(step) helper must exist"
        # Mirrors the server's plan_approval._format_target vocabulary:
        # watts preferred, with the % of FTP alongside when both are present.
        assert '" W ("' in target
        assert '" W"' in target
        assert '"%"' in target
        # Cadence labels ride the rows helper (same payload, no new I/O).
        assert "cadence_rpm" in rows
        assert '" rpm"' in rows

    # --- AC2: no new I/O, CSP untouched ------------------------------------------

    def test_step_profile_introduces_no_new_io_or_transport(self) -> None:
        """AC2: the profile derives from SSE payload state — no new transport.

        The fetch surface of the component is pinned by
        TestFloatingComposerContract (the known endpoint set); this pin
        brings the profile helpers under the same no-transport posture.
        """
        script = self._script()
        rules = (
            _rule_block(script, "stepProfileRows(plan)"),
            _rule_block(script, "stepTargetText(step)"),
            _rule_block(script, "stepProfileLabel(plan)"),
        )
        assert all(rules), rules

        for transport in ("fetch", "EventSource", "XMLHttpRequest"):
            for block in rules:
                assert transport not in block, transport

    def test_step_profile_adds_no_inline_style_or_event_handlers(self) -> None:
        """CSP (precedent #207): the profile is style-attribute and handler free.

        Inline style attributes are blocked by style-src — bar geometry must
        ride presentation attributes and classes. The visualization is
        static: no Alpine event bindings inside the profile markup at all.
        """
        profile = self._profile_block()

        assert 'style="' not in profile
        # No event handlers anywhere in the profile markup (static SVG).
        assert "@" not in profile

    # --- AC3: accessible name + retained table ------------------------------------

    def test_step_profile_carries_an_accessible_name(self) -> None:
        """AC3: the profile block is one image with one summarizing name."""
        profile = self._profile_block()

        assert 'role="img"' in profile
        assert ':aria-label="stepProfileLabel(plan)"' in profile
        # The decorative bars are hidden from the accessibility tree; the
        # block-level name is the single summary (never per-bar noise).
        assert 'aria-hidden="true"' in profile

    def test_step_profile_label_summarizes_the_workout(self) -> None:
        """AC3: the aria-label summarizes steps, duration and peak target."""
        label = _rule_block(self._script(), "stepProfileLabel(plan)")
        assert label, "stepProfileLabel(plan) helper must exist"

        assert '"Step profile: "' in label
        assert "plan.steps.length" in label
        assert "duration_minutes" in label
        assert '" · peak "' in label
        # An empty plan yields no name — the block hides anyway.
        assert 'return ""' in label

    def test_step_table_remains_the_accessible_alternative(self) -> None:
        """AC3: the interval table stays inside the card; the SVG is additive."""
        card = self._plan_card()

        assert '<table class="interval-table"' in card
        assert '<th scope="col">Interval</th>' in card
        assert '<th scope="col">Cadence</th>' in card
        # The approve/reject contract is untouched.
        assert '@click="approvePlan()"' in card
        assert '@click="rejectPlan()"' in card

    # --- AC4: weekly-card zone chips ------------------------------------------------

    def test_week_card_renders_per_day_zone_chips(self) -> None:
        """AC4: every day row carries a per-day intensity zone chip."""
        card = self._week_card()

        assert 'class="zone-chip"' in card
        assert ':class="weekDayIntensityClass(day)"' in card
        assert 'x-text="weekDayIntensityText(day)"' in card
        # The chip rides the Focus/Zones cell — that header already owns the
        # zone semantics (no new table column, KIS).
        focus_at = card.index('data-label="Focus"')
        chip_at = card.index('class="zone-chip"', focus_at)
        assert chip_at < card.index("</td>", focus_at)

    def test_week_day_intensity_is_derived_from_the_delivered_payload(self) -> None:
        """AC4/AC2: intensity derives from zones/rest flags — no new I/O."""
        script = self._script()
        text = _rule_block(script, "weekDayIntensityText(day)")
        klass = _rule_block(script, "weekDayIntensityClass(day)")

        assert text, "weekDayIntensityText(day) helper must exist"
        assert "day.rest_day" in text
        assert "day.zones" in text
        assert "fetch" not in text
        assert klass, "weekDayIntensityClass(day) helper must exist"
        # Word + color (never color alone, #165): the color class is derived
        # from the SAME word the chip renders.
        assert '"zone-chip--" +' in klass

    # --- styling contract ------------------------------------------------------------

    def test_step_profile_and_zone_chips_are_styled_with_tokens(self) -> None:
        """The new surfaces carry stylesheet rules (tokens only — hex guard)."""
        css = self._css()

        profile = _rule_block(css, ".step-profile")
        assert "display: flex" in profile

        rect = _rule_block(css, ".step-profile-rect")
        assert "var(--accent)" in rect

        chip = _rule_block(css, ".zone-chip")
        assert "text-transform: uppercase" in chip

        hard = _rule_block(css, ".zone-chip--hard")
        assert "var(--accent)" in hard


class TestMessageBubbleGrammarContract:
    """Message bubble grammar — bubbles, avatars, timestamps, dividers (#279).

    The terminal-style presentation (uppercase USER/ASSISTANT role labels,
    thin left-border separators) is replaced by a Sendbird-inspired bubble
    grammar: assistant messages render as left-aligned rounded neutral cards
    with the coach monogram avatar on the left margin, a "Coach" sender label
    above the bubble and a client-side receipt time (HH:MM) below it; user
    messages render as right-aligned accent bubbles with high text contrast.
    Date dividers render as centered chips between messages whose client
    receipt dates differ.

    Architect decision (binding): NO chat component library — the vendored
    CSP-hardened stack is retained (supply-chain + CSP posture); bubbles are
    pure CSS + markup restructuring.

    nLPD posture (AC2): receipt timestamps are client-side only — stamped
    from the client clock into in-memory message state at push time,
    stripped from the history replay (the wire contract stays {role,
    content} pairs), never persisted and never transmitted.

    Source-level assertions only (ADR-006 §5 — no Node toolchain).
    """

    def _html(self) -> str:
        return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def _script(self) -> str:
        return (STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def _css(self) -> str:
        return (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

    def _message_loop(self) -> str:
        """The message loop region: from the x-for template to the composer."""
        html = self._html()
        loop_at = html.index('<template x-for="(message, index) in messages"')
        return html[loop_at : html.index('<form class="composer"')]

    # --- AC1: bubble grammar structure ---------------------------------------

    def test_assistant_messages_render_avatar_sender_and_bubble(self) -> None:
        """AC1: the assistant grammar carries avatar, sender label, bubble."""
        loop = self._message_loop()

        assert 'class="message-avatar"' in loop
        assert 'class="message-sender"' in loop
        assert ">Coach</span>" in loop
        assert 'class="message-bubble"' in loop
        # The sanitized markdown body renders inside the assistant bubble.
        assert loop.index('class="message-bubble"') < loop.index(
            'x-html="renderMarkdown(message.content)"'
        )

    def test_user_messages_render_right_aligned_distinct_bubbles(self) -> None:
        """AC1: user messages keep x-text and right-align as accent bubbles."""
        html = self._html()
        css = self._css()

        assert "message--user" in html
        assert "align-items: flex-end" in _rule_block(css, ".message--user .message-main")
        user_bubble = _rule_block(css, ".message--user .message-bubble")
        assert "background: var(--accent)" in user_bubble
        assert "color: var(--accent-ink)" in user_bubble

    def test_uppercase_role_label_is_removed(self) -> None:
        """AC1: the uppercase .message-role label is gone (markup + styles)."""
        html = self._html()
        css = self._css()

        assert "message-role" not in html
        assert "message-role" not in css

    def test_assistant_bubble_is_a_rounded_neutral_card(self) -> None:
        """AC1: the assistant bubble is a rounded, tokenized neutral card."""
        css = self._css()
        bubble = _rule_block(css, ".message-bubble")

        assert "border-radius" in bubble
        assert "background: var(--surface)" in bubble
        assert "padding" in bubble

    # --- AC2: client-side receipts (nLPD) --------------------------------------

    def test_receipts_are_stamped_from_the_client_clock_at_push(self) -> None:
        """AC2: both push sites stamp a client-clock receipt at push time."""
        script = self._script()

        stamp = _rule_block(script, "stampReceipt(message)")
        assert stamp, "stampReceipt(message) helper must exist"
        assert "Date.now()" in stamp

        assert "this.stampReceipt(" in _rule_block(script, "send()")
        assert "this.stampReceipt(" in _rule_block(script, "startStream(message)")

    def test_history_replay_strips_receipts_before_transmission(self) -> None:
        """AC2/nLPD: the history replay transmits {role, content} pairs only."""
        script = self._script()

        # The #79 snapshot expression is preserved and every replayed entry
        # is mapped through the receipt-stripping helper before the POST.
        assert "this.messages.slice(0, -1).slice(-10).map(stripReceipt)" in script
        strip = _rule_block(script, "stripReceipt(message)")
        assert "{ role: message.role, content: message.content }" in strip

    def test_receipt_helpers_touch_no_storage_and_no_network(self) -> None:
        """AC2/nLPD: the receipt/divider helpers are pure client formatters."""
        script = self._script()

        for signature in (
            "stampReceipt(message)",
            "stripReceipt(message)",
            "receiptTime(message)",
            "dateDividerLabel(index)",
        ):
            block = _rule_block(script, signature)
            assert block, signature
            for banned in (
                "fetch",
                "localStorage",
                "sessionStorage",
                "indexedDB",
                "document.cookie",
                "sendBeacon",
                "XMLHttpRequest",
            ):
                assert banned not in block, f"{signature}: {banned}"

    # --- AC3: date dividers ------------------------------------------------------

    def test_date_divider_renders_as_a_centered_chip(self) -> None:
        """AC3: a centered divider chip renders inside the message loop."""
        loop = self._message_loop()
        css = self._css()

        assert 'class="date-divider"' in loop
        divider = _rule_block(css, ".date-divider")
        assert "align-self: center" in divider

    def test_date_divider_label_is_a_plain_localized_date(self) -> None:
        """AC3: the divider text is a plain localized date of the receipt day."""
        helper = _rule_block(self._script(), "dateDividerLabel(index)")

        assert "toLocaleDateString" in helper
        assert 'month: "long"' in helper
        # The divider renders only when the previous message's receipt day
        # differs (or there is no previous message).
        assert "this.messages[index - 1]" in helper
        assert "toDateString" in helper

    # --- AC4: markdown unchanged, long content scrolls internally ----------------

    def test_markdown_tables_scroll_inside_the_bubble(self) -> None:
        """AC4: wide markdown tables scroll internally, never overflow."""
        table = _rule_block(self._css(), ".message-body--markdown table")
        assert "overflow-x: auto" in table

    def test_bubble_chain_keeps_the_shrink_to_scroll_contract(self) -> None:
        """AC4: min-width: 0 on the bubble chain lets long content scroll."""
        css = self._css()

        assert "min-width: 0" in _rule_block(css, ".message-main")
        assert "min-width: 0" in _rule_block(css, ".message-bubble")

    # --- AC5: structured cards keep rendering -------------------------------------

    def test_all_six_structured_cards_still_render_in_the_loop(self) -> None:
        """AC5: all six structured cards stay pinned inside the message loop."""
        loop = self._message_loop()

        for pin in (
            "plan && planMessageIndex === index",
            "weekPlan && weekMessageIndex === index",
            "periodizationPlan && periodizationMessageIndex === index",
            "debriefSummary && debriefSummaryMessageIndex === index",
            "morningBriefing && morningBriefingMessageIndex === index",
            "rpe && rpeMessageIndex === index",
        ):
            assert pin in loop, pin

    def test_cards_span_the_full_width_beneath_the_bubble(self) -> None:
        """AC5: cards render beneath the bubble across the full message row."""
        card = _rule_block(self._css(), ".message .plan-card")
        assert "grid-column: 1 / -1" in card

    # --- AC6: CSP-safe, never-color-alone, reduced motion -------------------------

    def test_bubble_grammar_introduces_no_inline_styles(self) -> None:
        """AC6: the bubble grammar is class-only (CSP style-src 'self')."""
        assert "style=" not in self._message_loop()

    def test_bubble_grammar_introduces_no_animation(self) -> None:
        """AC6/#85: the new surfaces animate nothing (reduced-motion safe)."""
        css = self._css()

        for selector in (
            ".message-bubble",
            ".date-divider",
            ".message-avatar",
            ".message-sender",
            ".message-time",
        ):
            block = _rule_block(css, selector)
            assert block, selector
            assert "animation" not in block, selector
            assert "transition" not in block, selector

    def test_sender_identity_is_never_color_alone(self) -> None:
        """AC6/#165: sender identity rides position + label, not color alone."""
        css = self._css()

        # The user variant is structurally distinct: right-aligned column,
        # no avatar and no sender label — color only reinforces the split.
        assert "display: none" in _rule_block(css, ".message--user .message-avatar")
        assert "display: none" in _rule_block(css, ".message--user .message-sender")
