"""Integration-level validation of the Docker Compose sidecar topology.

These tests inspect the declared compose file, Dockerfile and pre-flight script
without requiring a running Docker daemon, so they remain fast and deterministic
in CI as well as local development.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TestDockerComposeTopology:
    """Declarative checks for docker-compose.yml."""

    @pytest.fixture
    def compose(self) -> dict[str, Any]:
        """Load and return the parsed docker-compose.yml."""
        compose_path = PROJECT_ROOT / "docker-compose.yml"
        assert compose_path.is_file(), "docker-compose.yml must exist"
        with compose_path.open(encoding="utf-8") as handle:
            data: dict[str, Any] = yaml.safe_load(handle)
            return data

    def test_network_is_isolated_bridge(self, compose: dict[str, Any]) -> None:
        """The compose project defines the internal coach-net bridge."""
        networks = compose.get("networks", {})
        assert "coach-net" in networks
        assert networks["coach-net"].get("driver") == "bridge"
        assert networks["coach-net"].get("name") == "coach-net"

    def test_all_three_services_present(self, compose: dict[str, Any]) -> None:
        """The sidecar topology contains coach-web, coach-mcp and github-mcp."""
        services = compose.get("services", {})
        assert set(services) == {"coach-web", "coach-mcp", "github-mcp"}

    @pytest.mark.parametrize(
        ("service", "expected_port"),
        [
            ("coach-web", 8000),
            ("coach-mcp", 8000),
            ("github-mcp", 8001),
        ],
    )
    def test_service_uses_coach_net(
        self, compose: dict[str, Any], service: str, expected_port: int
    ) -> None:
        """Every sidecar attaches to coach-net and exposes the expected port."""
        config = compose["services"][service]
        assert "coach-net" in config.get("networks", [])
        ports = config.get("ports", [])
        if service == "coach-web":
            assert "127.0.0.1:8000:8000" in ports
        else:
            assert ports == []

        if service == "coach-web":
            assert config["environment"].get("APP_PORT") == expected_port
        elif service == "coach-mcp":
            assert config["environment"].get("MCP_PORT") == expected_port
        else:
            assert config["command"][2] == str(expected_port)

    @pytest.mark.parametrize(
        "service",
        ["coach-web", "coach-mcp", "github-mcp"],
    )
    def test_security_hardening_applied(self, compose: dict[str, Any], service: str) -> None:
        """Each service drops capabilities, forbids privilege escalation and is read-only."""
        config = compose["services"][service]
        assert config.get("read_only") is True
        assert config.get("cap_drop") == ["ALL"]
        assert config.get("security_opt") == ["no-new-privileges:true"]
        tmpfs = config.get("tmpfs", [])
        assert any(entry.startswith("/tmp:") for entry in tmpfs)

    def test_coach_web_service_discovery(self, compose: dict[str, Any]) -> None:
        """coach-web points to sidecars via Docker DNS names."""
        env = compose["services"]["coach-web"].get("environment", {})
        assert env.get("COACH_MCP_URL") == "http://coach-mcp:8000/sse"
        assert env.get("GITHUB_MCP_URL") == "http://github-mcp:8001/"

    def test_coach_mcp_uses_sse_transport(self, compose: dict[str, Any]) -> None:
        """coach-mcp is configured for SSE transport on the internal network."""
        env = compose["services"]["coach-mcp"].get("environment", {})
        assert env.get("MCP_TRANSPORT") == "sse"
        assert env.get("MCP_HOST") == "0.0.0.0"
        assert env.get("MCP_PORT") == 8000

    def test_github_mcp_uses_streamable_http(self, compose: dict[str, Any]) -> None:
        """github-mcp uses the official image's streamable HTTP command on port 8001."""
        config = compose["services"]["github-mcp"]
        assert config.get("command") == ["http", "--port", "8001", "--listen-host", "0.0.0.0"]
        env = config.get("environment", {})
        assert env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "${GITHUB_TOKEN:-}"
        assert env.get("GITHUB_TOOLSETS") == "${GITHUB_TOOLSETS:-default}"

    def test_health_checks_defined(self, compose: dict[str, Any]) -> None:
        """Every service declares a health check."""
        for service, config in compose["services"].items():
            health = config.get("healthcheck", {})
            assert "test" in health, f"{service} is missing a healthcheck test"
            assert health.get("interval") is not None

    def test_coach_web_depends_on_healthy_sidecars(self, compose: dict[str, Any]) -> None:
        """coach-web waits for both MCP sidecars to be healthy before starting."""
        depends_on = compose["services"]["coach-web"].get("depends_on", {})
        assert depends_on.get("coach-mcp", {}).get("condition") == "service_healthy"
        assert depends_on.get("github-mcp", {}).get("condition") == "service_healthy"


class TestDockerfileHardening:
    """Declarative checks for the production Dockerfile."""

    @pytest.fixture
    def dockerfile(self) -> str:
        """Return the Dockerfile contents."""
        path = PROJECT_ROOT / "Dockerfile"
        assert path.is_file(), "Dockerfile must exist"
        return path.read_text(encoding="utf-8")

    def test_multi_stage_build(self, dockerfile: str) -> None:
        """The Dockerfile separates build and runtime stages."""
        assert "AS builder" in dockerfile
        assert "AS runtime" in dockerfile

    def test_non_root_user_with_uid_10001(self, dockerfile: str) -> None:
        """A dedicated non-root user with UID 10001 is created and used."""
        assert "-u 10001" in dockerfile
        assert "-g 10001" in dockerfile
        assert "USER coach-web:coach-web" in dockerfile

    def test_exposes_port_8000(self, dockerfile: str) -> None:
        """The runtime image exposes the compose topology port 8000."""
        assert "EXPOSE 8000" in dockerfile

    def test_healthcheck_uses_port_8000(self, dockerfile: str) -> None:
        """The container health check targets localhost:8000/health."""
        assert "http://localhost:8000/health" in dockerfile


class TestE2EPreflightScript:
    """Declarative checks for the E2E pre-flight script."""

    def test_script_exists_and_is_executable(self) -> None:
        """scripts/e2e-preflight.sh exists and is executable."""
        script = PROJECT_ROOT / "scripts" / "e2e-preflight.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111, "script must be executable"

    def test_script_validates_compose_config(self) -> None:
        """The script runs docker compose config as a lint step."""
        script = PROJECT_ROOT / "scripts" / "e2e-preflight.sh"
        contents = script.read_text(encoding="utf-8")
        assert "docker compose" in contents
        assert "config" in contents

    def test_script_checks_health_endpoint(self) -> None:
        """The script validates the coach-web /health endpoint."""
        script = PROJECT_ROOT / "scripts" / "e2e-preflight.sh"
        contents = script.read_text(encoding="utf-8")
        assert "/health" in contents
        assert "coach-web" in contents
