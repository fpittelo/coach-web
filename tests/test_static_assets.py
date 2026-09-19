"""Frontend smoke tests: static asset serving and Swiss minimalist contract.

These tests guard the single-origin, zero-CDN delivery of the Alpine.js UI and
the Swiss nLPD constraint that no user data is ever rendered as raw HTML.
"""

from fastapi.testclient import TestClient

from coach_web.app import STATIC_DIR, create_app

VENDOR_DIR = STATIC_DIR / "vendor"


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
    """Design-token and privacy constraints on the shipped assets."""

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
        """The stylesheet encodes the EPFL Elements design tokens."""
        css = (STATIC_DIR / "styles.css").read_text(encoding="utf-8").lower()

        assert "@font-face" in css
        assert "inter" in css
        assert "#d5d5d5" in css
        assert "--radius: 2px" in css
        assert "box-shadow" not in css

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
