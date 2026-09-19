"""Tests for the Coach Web FastAPI application factory, healthcheck and static serving."""

from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coach_web.app import STATIC_DIR, create_app


class TestCreateApp:
    """Application factory behaviour."""

    def test_returns_fastapi_instance(self) -> None:
        """create_app() returns a configured FastAPI application."""
        app = create_app()

        assert isinstance(app, FastAPI)

    def test_sets_application_metadata(self) -> None:
        """The application exposes the Coach Web title and a resolved version."""
        app = create_app()

        assert app.title == "Coach Web"
        assert app.version
        assert app.version != "0.0.0"

    def test_mounts_static_directory(self) -> None:
        """Static assets are mounted under /static."""
        app = create_app()

        mounted_paths = {getattr(route, "path", None) for route in app.routes}

        assert "/static" in mounted_paths

    def test_registers_health_routes(self) -> None:
        """Both /health and the /healthz alias are registered."""
        app = create_app()

        registered_paths = {getattr(route, "path", None) for route in app.routes}

        assert "/health" in registered_paths
        assert "/healthz" in registered_paths

    def test_index_html_is_present_in_static_dir(self) -> None:
        """The Swiss minimalist placeholder index.html ships with the package."""
        index = STATIC_DIR / "index.html"

        assert index.is_file()
        assert "Coach Web" in index.read_text(encoding="utf-8")

    def test_cors_middleware_allows_configured_origin(self) -> None:
        """CORS middleware echoes the configured origin."""
        with TestClient(create_app()) as client:
            response = client.get("/health", headers={"Origin": "http://localhost:8000"})

        assert response.headers.get("access-control-allow-origin") == "http://localhost:8000"


class TestHealthEndpoint:
    """Healthcheck contract."""

    def test_health_returns_healthy_payload(self) -> None:
        """GET /health returns the canonical status payload."""
        with TestClient(create_app()) as client:
            response = client.get("/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "healthy"
        assert payload["service"] == "coach-web"
        assert payload["version"]
        assert payload["uptime_seconds"] >= 0

    def test_healthz_alias_matches_health(self) -> None:
        """GET /healthz is an alias of GET /health."""
        with TestClient(create_app()) as client:
            health = client.get("/health")
            healthz = client.get("/healthz")

        assert healthz.status_code == 200
        health_payload = health.json()
        healthz_payload = healthz.json()
        assert healthz_payload["status"] == health_payload["status"]
        assert healthz_payload["service"] == health_payload["service"]
        assert healthz_payload["version"] == health_payload["version"]


class TestStaticServing:
    """Static asset serving."""

    def test_root_serves_index_html(self) -> None:
        """GET / serves the single-page interface."""
        with TestClient(create_app()) as client:
            response = client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "Coach Web" in response.text

    def test_static_mount_serves_index_html(self) -> None:
        """GET /static/index.html serves the same placeholder."""
        with TestClient(create_app()) as client:
            response = client.get("/static/index.html")

        assert response.status_code == 200
        assert "Coach Web" in response.text


class TestLifespan:
    """Lifespan startup/shutdown handling."""

    def test_lifespan_populates_application_state(self) -> None:
        """Startup exposes resolved settings and a monotonic start timestamp."""
        app = create_app()

        with TestClient(app):
            assert app.state.settings is not None
            assert app.state.settings.service_name == "coach-web"
            assert app.state.started_at > 0

    def test_lifespan_shutdown_is_clean(self) -> None:
        """The context manager exits without raising."""
        app = create_app()

        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


class TestRun:
    """CLI entrypoint."""

    @patch("coach_web.app.uvicorn")
    def test_run_invokes_uvicorn_factory(self, mock_uvicorn: MagicMock) -> None:
        """run() boots uvicorn against the application factory."""
        from coach_web.app import run
        from coach_web.config import get_settings

        get_settings.cache_clear()
        run()

        mock_uvicorn.run.assert_called_once()
        args, kwargs = mock_uvicorn.run.call_args
        assert args[0] == "coach_web.app:create_app"
        assert kwargs["factory"] is True
        assert kwargs["host"] == "0.0.0.0"  # noqa: S104
        assert kwargs["port"] == 8000
