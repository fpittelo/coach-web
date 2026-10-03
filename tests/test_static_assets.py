"""Frontend smoke tests: static asset serving and the v0.7 visual contract.

These tests guard the single-origin, zero-CDN delivery of the Alpine.js UI and
the Swiss nLPD constraint that no user data is ever rendered as raw HTML.

The stylesheet contract was superseded by ADR-006 (issue #78): the Sprint 03
Swiss minimalist tokens (pure-red accent, 2px radii, zero elevation, 1180px
two-panel grid) are replaced by the v0.7 slate/teal palette, 10px radii, soft
shadows, and a centered 760px reading column. The supersession is explicit —
assertions were rewritten, never silently deleted.
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
        """The component consumes the SSE stream and posts plan approvals."""
        script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")

        assert "EventSource" in script
        assert "/api/agent/stream" in script
        assert "/api/plan/approve" in script
        assert "plan_proposal" in script

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
