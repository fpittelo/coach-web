"""Integration-level validation of the local Docker lane topology (ADR-007).

These tests inspect the declared compose files (base + per-lane overrides),
the lane environment templates, the pre-flight script and .gitignore without
requiring a running Docker daemon, so they remain fast and deterministic in
CI as well as local development.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

BASE_COMPOSE = "compose.yaml"
LANE_COMPOSE = {
    "dev": "compose.dev.yml",
    "qa": "compose.qa.yml",
    "prod": "compose.prod.yml",
}
LANE_PORTS = {"dev": 8100, "qa": 8200, "prod": 8000}
LANE_CORS_ORIGINS = {
    "dev": "http://localhost:8100",
    "qa": "http://localhost:8200",
    "prod": "http://localhost:8000",
}
GITHUB_MCP_PIN = "ghcr.io/github/github-mcp-server:v1.12.2"
GITHUB_MCP_DIGEST = "sha256:508a0857ec762b1ab1cece29193345b501fab1dd9d1228a7b617062954cecac6"
GITHUB_MCP_DIGEST_PIN = f"{GITHUB_MCP_PIN}@{GITHUB_MCP_DIGEST}"
GITHUB_MCP_TAG_DEFAULT = f"${{GITHUB_MCP_IMAGE:-{GITHUB_MCP_PIN}}}"
GITHUB_MCP_DIGEST_DEFAULT = f"${{GITHUB_MCP_IMAGE:-{GITHUB_MCP_DIGEST_PIN}}}"
SERVICES = ("coach-web", "coach-mcp", "github-mcp")


def load_yaml(name: str) -> dict[str, Any]:
    """Load a YAML file from the project root."""
    path = PROJECT_ROOT / name
    assert path.is_file(), f"{name} must exist"
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    assert isinstance(data, dict), f"{name} must contain a top-level mapping"
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base (override wins on non-mapping keys).

    Approximates docker compose's file merging for the keys used by the lane
    topology: mappings merge recursively; scalars and lists are replaced. The
    lane files never redefine lists that the base also sets (ports, tmpfs,
    cap_drop, ...), so replacement matches compose's behavior here.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@pytest.fixture(scope="module")
def base_compose() -> dict[str, Any]:
    """Load and return the parsed base compose.yaml."""
    return load_yaml(BASE_COMPOSE)


@pytest.fixture(scope="module")
def lane_compose() -> dict[str, dict[str, Any]]:
    """Load and return each lane override file, keyed by lane."""
    return {lane: load_yaml(name) for lane, name in LANE_COMPOSE.items()}


@pytest.fixture(scope="module")
def effective_compose(
    base_compose: dict[str, Any],
    lane_compose: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Base+lane merged view per lane (approximates docker compose merging)."""
    return {lane: deep_merge(base_compose, override) for lane, override in lane_compose.items()}


class TestBaseComposeTopology:
    """Declarative checks for the shared compose.yaml (AC1/AC4/AC6)."""

    def test_all_three_services_present(self, base_compose: dict[str, Any]) -> None:
        """The sidecar topology contains coach-web, coach-mcp and github-mcp."""
        services = base_compose.get("services", {})
        assert set(services) == set(SERVICES)

    @pytest.mark.parametrize("service", SERVICES)
    def test_no_lane_specific_keys_in_base(
        self, base_compose: dict[str, Any], service: str
    ) -> None:
        """The base file pins no container_name, hostname, ports or image (AC1)."""
        config = base_compose["services"][service]
        assert "container_name" not in config
        assert "hostname" not in config
        assert "ports" not in config
        assert "image" not in config
        assert "build" not in config

    def test_network_has_no_pinned_name(self, base_compose: dict[str, Any]) -> None:
        """coach-net is a bridge whose name derives from the project (AC2)."""
        networks = base_compose.get("networks", {})
        assert "coach-net" in networks
        assert networks["coach-net"].get("driver") == "bridge"
        assert "name" not in networks["coach-net"]

    @pytest.mark.parametrize("service", SERVICES)
    def test_security_hardening_applied(self, base_compose: dict[str, Any], service: str) -> None:
        """Each service drops capabilities, forbids privilege escalation and is read-only."""
        config = base_compose["services"][service]
        assert config.get("read_only") is True
        assert config.get("cap_drop") == ["ALL"]
        assert config.get("security_opt") == ["no-new-privileges:true"]
        tmpfs = config.get("tmpfs", [])
        assert any(entry.startswith("/tmp:") for entry in tmpfs)

    @pytest.mark.parametrize("service", SERVICES)
    def test_resource_limits_applied(self, base_compose: dict[str, Any], service: str) -> None:
        """Each service declares CPU and memory limits (AC6)."""
        limits = base_compose["services"][service]["deploy"]["resources"]["limits"]
        assert limits.get("cpus") == "1.0"
        assert limits.get("memory") == "512M"

    def test_health_checks_defined(self, base_compose: dict[str, Any]) -> None:
        """Every service declares a health check."""
        for service in SERVICES:
            health = base_compose["services"][service].get("healthcheck", {})
            assert "test" in health, f"{service} is missing a healthcheck test"
            assert health.get("interval") is not None

    def test_coach_web_depends_on_healthy_sidecars(self, base_compose: dict[str, Any]) -> None:
        """coach-web waits for both MCP sidecars to be healthy before starting."""
        depends_on = base_compose["services"]["coach-web"].get("depends_on", {})
        assert depends_on.get("coach-mcp", {}).get("condition") == "service_healthy"
        assert depends_on.get("github-mcp", {}).get("condition") == "service_healthy"

    def test_coach_web_service_discovery(self, base_compose: dict[str, Any]) -> None:
        """coach-web points to sidecars via Docker DNS names."""
        env = base_compose["services"]["coach-web"].get("environment", {})
        assert env.get("COACH_MCP_URL") == "http://coach-mcp:8000/sse"
        assert env.get("GITHUB_MCP_URL") == "http://github-mcp:8001/"

    def test_coach_mcp_uses_sse_transport(self, base_compose: dict[str, Any]) -> None:
        """coach-mcp is configured for SSE transport on the internal network."""
        env = base_compose["services"]["coach-mcp"].get("environment", {})
        assert env.get("MCP_TRANSPORT") == "sse"
        assert env.get("MCP_HOST") == "0.0.0.0"
        assert env.get("MCP_PORT") == "8000"

    def test_github_mcp_uses_streamable_http(self, base_compose: dict[str, Any]) -> None:
        """github-mcp uses the official image's streamable HTTP command on port 8001."""
        config = base_compose["services"]["github-mcp"]
        assert config.get("command") == ["http", "--port", "8001", "--listen-host", "0.0.0.0"]

    def test_coach_web_secret_scoping(self, base_compose: dict[str, Any]) -> None:
        """coach-web receives only its own secrets, never the sidecars' (AC4)."""
        env = base_compose["services"]["coach-web"].get("environment", {})
        assert env.get("OPENROUTER_API_KEY") == "${OPENROUTER_API_KEY:-}"
        assert env.get("GOOGLE_OIDC_CLIENT_SECRET") == "${GOOGLE_OIDC_CLIENT_SECRET:-}"
        assert env.get("AUTH_SESSION_SECRET") == "${AUTH_SESSION_SECRET:-}"
        assert env.get("GITHUB_TOKEN") == "${GITHUB_TOKEN:-}"
        assert "INTERVALS_API_KEY" not in env

    def test_coach_web_security_settings_scoping(self, base_compose: dict[str, Any]) -> None:
        """Cookie/Host settings are scoped to coach-web only (AC5/AC6, #112)."""
        env = base_compose["services"]["coach-web"].get("environment", {})
        assert env.get("AUTH_COOKIE_SECURE") == "${AUTH_COOKIE_SECURE:-true}"
        assert env.get("TRUSTED_HOSTS") == '${TRUSTED_HOSTS:-["localhost", "127.0.0.1"]}'
        for service in ("coach-mcp", "github-mcp"):
            foreign = base_compose["services"][service].get("environment", {})
            assert "AUTH_COOKIE_SECURE" not in foreign
            assert "TRUSTED_HOSTS" not in foreign

    def test_coach_mcp_secret_scoping(self, base_compose: dict[str, Any]) -> None:
        """coach-mcp receives only INTERVALS_API_KEY, never web-app secrets (AC4)."""
        env = base_compose["services"]["coach-mcp"].get("environment", {})
        assert env.get("INTERVALS_API_KEY") == "${INTERVALS_API_KEY:-}"
        for foreign in (
            "OPENROUTER_API_KEY",
            "GITHUB_TOKEN",
            "AUTH_SESSION_SECRET",
            "GOOGLE_OIDC_CLIENT_SECRET",
        ):
            assert foreign not in env

    def test_github_mcp_secret_scoping(self, base_compose: dict[str, Any]) -> None:
        """github-mcp receives only the GitHub PAT and its toolsets (AC4)."""
        env = base_compose["services"]["github-mcp"].get("environment", {})
        assert env.get("GITHUB_PERSONAL_ACCESS_TOKEN") == "${GITHUB_TOKEN:-}"
        assert set(env) == {"GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_TOOLSETS"}


class TestLaneOverrides:
    """Per-lane override checks (AC1/AC2/AC3)."""

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_project_name(self, lane_compose: dict[str, dict[str, Any]], lane: str) -> None:
        """Each lane pins its compose project name so lanes run concurrently (AC2)."""
        assert lane_compose[lane].get("name") == f"coach-web-{lane}"

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_coach_web_loopback_port(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """coach-web publishes exactly one port, bound to 127.0.0.1 only (AC1)."""
        ports = effective_compose[lane]["services"]["coach-web"]["ports"]
        assert ports == [f"127.0.0.1:{LANE_PORTS[lane]}:8000"]

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_sidecars_publish_no_ports(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """Sidecars are reachable only inside the lane network (AC1/AC5)."""
        for service in ("coach-mcp", "github-mcp"):
            assert "ports" not in effective_compose[lane]["services"][service]

    def test_dev_lane_builds_from_source(
        self, effective_compose: dict[str, dict[str, Any]]
    ) -> None:
        """The dev lane builds coach-web from source, targeting the runtime stage."""
        build = effective_compose["dev"]["services"]["coach-web"]["build"]
        assert build["context"] == "."
        assert build["dockerfile"] == "Dockerfile"
        assert build["target"] == "runtime"
        assert effective_compose["dev"]["services"]["coach-web"]["image"] == (
            "ghcr.io/fpittelo/coach-web:dev"
        )

    def test_qa_lane_pulls_promoted_image(
        self, effective_compose: dict[str, dict[str, Any]]
    ) -> None:
        """The qa lane pulls the CI-built, zero-warning-gated :qa image."""
        config = effective_compose["qa"]["services"]["coach-web"]
        assert config["image"] == "ghcr.io/fpittelo/coach-web:qa"
        assert "build" not in config

    def test_prod_lane_requires_digest_pinned_image(
        self, effective_compose: dict[str, dict[str, Any]]
    ) -> None:
        """The prod lane requires a digest-pinned COACH_WEB_IMAGE (fails fast)."""
        config = effective_compose["prod"]["services"]["coach-web"]
        assert config["image"].startswith("${COACH_WEB_IMAGE:?")
        assert "sha256" in config["image"]
        assert "build" not in config

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_coach_mcp_lane_tag(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """The coach-mcp sidecar tag matches the lane via COACH_MCP_IMAGE (AC3)."""
        image = effective_compose[lane]["services"]["coach-mcp"]["image"]
        assert image == f"${{COACH_MCP_IMAGE:-ghcr.io/fpittelo/coach:{lane}}}"

    @pytest.mark.parametrize(
        ("lane", "expected"),
        [
            ("dev", GITHUB_MCP_TAG_DEFAULT),
            ("qa", GITHUB_MCP_TAG_DEFAULT),
            ("prod", GITHUB_MCP_DIGEST_DEFAULT),
        ],
    )
    def test_github_mcp_pinned_version(
        self, effective_compose: dict[str, dict[str, Any]], lane: str, expected: str
    ) -> None:
        """github-mcp is tag-pinned on dev/qa and digest-pinned on prod (AC7)."""
        image = effective_compose[lane]["services"]["github-mcp"]["image"]
        assert image == expected

    def test_prod_github_mcp_digest_pinned(
        self, effective_compose: dict[str, dict[str, Any]]
    ) -> None:
        """The prod lane pins github-mcp by tag AND digest (AC7, #113)."""
        image = effective_compose["prod"]["services"]["github-mcp"]["image"]
        assert GITHUB_MCP_DIGEST in image
        assert image.endswith(GITHUB_MCP_DIGEST + "}")

    def test_github_mcp_digest_is_valid_sha256(self) -> None:
        """The pinned digest is a well-formed sha256 reference (AC7)."""
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", GITHUB_MCP_DIGEST)

    @pytest.mark.parametrize(
        ("lane", "auth_enabled"),
        [("dev", "false"), ("qa", "false"), ("prod", "true")],
    )
    def test_auth_mode_per_lane(
        self, effective_compose: dict[str, dict[str, Any]], lane: str, auth_enabled: str
    ) -> None:
        """Auth is disabled on dev/qa and enabled (OIDC) on prod (ADR-007)."""
        env = effective_compose[lane]["services"]["coach-web"]["environment"]
        assert env.get("AUTH_ENABLED") == auth_enabled

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_cors_origin_default(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """Each lane defaults CORS_ORIGINS to its own loopback origin (AC3, #112)."""
        env = effective_compose[lane]["services"]["coach-web"]["environment"]
        origin = LANE_CORS_ORIGINS[lane]
        assert env.get("CORS_ORIGINS") == f'${{CORS_ORIGINS:-["{origin}"]}}'

    def test_no_latest_tag_in_any_compose_file(self) -> None:
        """No compose file references a :latest image (AC3)."""
        for name in (BASE_COMPOSE, *LANE_COMPOSE.values()):
            text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            assert ":latest" not in text, f"{name} still references :latest"


class TestLaneEnvTemplates:
    """Per-lane environment template checks (AC4)."""

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_template_exists(self, lane: str) -> None:
        """Each lane ships an env template."""
        assert (PROJECT_ROOT / f".env.{lane}.example").is_file()

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_template_matches_lane_sidecar_tag(self, lane: str) -> None:
        """The template's coach-mcp image tag matches the lane (AC3)."""
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
        assert f"COACH_MCP_IMAGE=ghcr.io/fpittelo/coach:{lane}" in text

    def test_templates_pin_github_mcp_version(self) -> None:
        """dev/qa templates tag-pin github-mcp; prod digest-pins it (AC7)."""
        for lane in ("dev", "qa"):
            text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
            assert f"GITHUB_MCP_IMAGE={GITHUB_MCP_PIN}" in text
        prod_text = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")
        assert f"GITHUB_MCP_IMAGE={GITHUB_MCP_DIGEST_PIN}" in prod_text
        for lane in ("dev", "qa", "prod"):
            text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
            active = [
                line
                for line in text.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            assert ":latest" not in "\n".join(active)

    def test_prod_env_template_pins_github_mcp_digest(self) -> None:
        """The prod template digest-pins the github-mcp sidecar (AC7, #113)."""
        text = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")
        assert f"GITHUB_MCP_IMAGE={GITHUB_MCP_DIGEST_PIN}" in text

    def test_prod_env_template_pins_digest(self) -> None:
        """The prod template carries the digest-pinned COACH_WEB_IMAGE (AC1)."""
        text = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")
        assert "COACH_WEB_IMAGE=ghcr.io/fpittelo/coach-web@sha256:" in text

    def test_prod_env_template_requires_oidc_credentials(self) -> None:
        """The prod template includes the fail-closed OIDC credentials (ADR-007)."""
        text = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")
        for required in (
            "GOOGLE_OIDC_CLIENT_ID=",
            "GOOGLE_OIDC_CLIENT_SECRET=",
            "AUTH_SESSION_SECRET=",
            "AUTH_WHITELIST_EMAILS=",
        ):
            assert required in text

    def test_prod_env_template_documents_redirect_uri(self) -> None:
        """The prod template documents the loopback redirect URI variants (AC4)."""
        text = (PROJECT_ROOT / ".env.prod.example").read_text(encoding="utf-8")
        assert "http://localhost:8000/auth/callback" in text
        assert "127.0.0.1" in text

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_template_documents_distinct_session_secret(self, lane: str) -> None:
        """Every lane template documents a distinct AUTH_SESSION_SECRET (AC2, #112)."""
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
        assert "AUTH_SESSION_SECRET=" in text
        assert "openssl rand -hex 32" in text

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_template_documents_cookie_and_host_settings(self, lane: str) -> None:
        """Every lane template documents AUTH_COOKIE_SECURE and TRUSTED_HOSTS (AC5/AC6)."""
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
        assert "AUTH_COOKIE_SECURE=true" in text
        assert "TRUSTED_HOSTS=" in text

    def test_env_example_has_no_duplicate_cache_ttl(self) -> None:
        """.env.example declares the app CACHE_TTL_SECONDS exactly once (AC2, #112)."""
        text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        active = [
            line.split("=", 1)[0]
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert active.count("CACHE_TTL_SECONDS") == 1
        assert "COACH_MCP_CACHE_TTL_SECONDS=300" in text

    def test_gitignore_covers_real_env_files(self) -> None:
        """.gitignore ignores real lane env files but keeps the templates (AC4)."""
        text = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert ".env.*" in text
        for template in (
            "!.env.example",
            "!.env.dev.example",
            "!.env.qa.example",
            "!.env.prod.example",
        ):
            assert template in text

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_real_lane_env_files_are_gitignored(self, lane: str) -> None:
        """Real lane env files are ignored; only the templates are tracked (AC2)."""
        git = shutil.which("git")
        assert git is not None, "git must be available to verify ignore rules"
        result = subprocess.run(
            [git, "check-ignore", f".env.{lane}"],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f".env.{lane} must be gitignored"


class TestE2EPreflightScript:
    """Declarative checks for the lane-aware E2E pre-flight script (AC5/AC6)."""

    @pytest.fixture
    def script(self) -> str:
        """Return the pre-flight script contents."""
        path = PROJECT_ROOT / "scripts" / "e2e-preflight.sh"
        assert path.is_file(), "scripts/e2e-preflight.sh must exist"
        return path.read_text(encoding="utf-8")

    def test_script_exists_and_is_executable(self) -> None:
        """scripts/e2e-preflight.sh exists and is executable."""
        script = PROJECT_ROOT / "scripts" / "e2e-preflight.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111, "script must be executable"

    def test_script_passes_bash_syntax_check(self) -> None:
        """bash -n validates the script syntax."""
        result = subprocess.run(
            ["/bin/bash", "-n", str(PROJECT_ROOT / "scripts" / "e2e-preflight.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_defaults_to_dev_lane(self, script: str) -> None:
        """The lane argument defaults to dev."""
        assert 'LANE="${1:-dev}"' in script

    def test_script_dispatches_all_lanes(self, script: str) -> None:
        """The script resolves compose file and port for every lane."""
        for lane, port in LANE_PORTS.items():
            assert f"{lane})" in script
            assert f"LANE_PORT={port}" in script

    def test_script_rejects_unknown_lane(self, script: str) -> None:
        """An unknown lane argument fails with a clear message."""
        assert "unknown lane" in script

    def test_script_scopes_teardown_to_its_own_project(self, script: str) -> None:
        """Teardown targets only the lane's compose project (AC5)."""
        assert 'PROJECT="coach-web-${LANE}"' in script
        assert "down --remove-orphans" in script

    def test_script_uses_lane_env_file(self, script: str) -> None:
        """The script passes the lane env file via --env-file (AC4)."""
        assert "--env-file" in script
        assert ".env.${LANE}" in script

    def test_script_fails_fast_on_missing_lane_image(self, script: str) -> None:
        """The script verifies pulled images exist before up (AC5)."""
        assert "manifest inspect" in script
        assert "not yet published to GHCR" in script

    def test_script_asserts_expected_service_set(self, script: str) -> None:
        """The health gate asserts all three services are present (folded #116 finding)."""
        assert "ps -a" in script
        assert "EXPECTED_SERVICES" in script
        assert "service set mismatch" in script
        for service in SERVICES:
            assert service in script

    def test_script_asserts_loopback_only_publishing(self, script: str) -> None:
        """The script asserts coach-web binds 127.0.0.1 and sidecars publish nothing."""
        assert "127.0.0.1" in script
        assert "Publishers" in script

    def test_script_validates_compose_config(self, script: str) -> None:
        """The script runs docker compose config as a lint step."""
        assert "docker compose" in script
        assert "config --quiet" in script

    def test_script_checks_health_endpoint(self, script: str) -> None:
        """The script validates the coach-web /health endpoint."""
        assert "/health" in script
        assert "coach-web" in script

    def test_script_probes_prod_auth_boundary(self, script: str) -> None:
        """The prod lane probe asserts /api/* returns 401 without a session."""
        assert "401" in script
        assert "/api/agent/stream" in script


class TestCIContainerScan:
    """CI must scan the built image with trivy (AC3, #113)."""

    @pytest.fixture(scope="class")
    def ci(self) -> dict[str, Any]:
        """Load the parsed CI workflow."""
        return load_yaml(".github/workflows/ci.yaml")

    def test_trivy_image_scan_job_present(self, ci: dict[str, Any]) -> None:
        """A dedicated image-scan job exists."""
        assert "image-scan" in ci.get("jobs", {})

    def test_trivy_action_pinned(self, ci: dict[str, Any]) -> None:
        """The trivy action is pinned to a specific version tag."""
        steps = ci["jobs"]["image-scan"]["steps"]
        trivy = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        ]
        assert len(trivy) == 1
        assert trivy[0]["uses"] == "aquasecurity/trivy-action@v0.36.0"

    def test_trivy_fails_on_high_critical(self, ci: dict[str, Any]) -> None:
        """The scan fails on HIGH/CRITICAL and reports lower severities."""
        steps = ci["jobs"]["image-scan"]["steps"]
        trivy = next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        )
        with_ = trivy.get("with", {})
        assert with_.get("severity") == "HIGH,CRITICAL"
        assert str(with_.get("exit-code")) == "1"

    def test_image_scan_builds_and_loads_image(self, ci: dict[str, Any]) -> None:
        """The job builds the image and loads it into the local daemon for scanning."""
        steps = ci["jobs"]["image-scan"]["steps"]
        builds = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
        ]
        assert len(builds) == 1
        assert builds[0]["with"].get("load") is True


class TestSecurityDocsLocalTopology:
    """docs/security.md must carry the local-topology STRIDE + nLPD model (AC4/AC5)."""

    @pytest.fixture(scope="class")
    def security(self) -> str:
        """Return the security policy contents."""
        return (PROJECT_ROOT / "docs" / "security.md").read_text(encoding="utf-8")

    @pytest.mark.parametrize("boundary", ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8"])
    def test_local_stride_boundaries_present(self, security: str, boundary: str) -> None:
        """Every local trust boundary B1-B8 is documented (AC4)."""
        assert f"Boundary {boundary}" in security

    def test_cloud_model_retained_as_historical_appendix(self, security: str) -> None:
        """The retired cloud model is preserved, not silently deleted (AC4)."""
        assert "Historical Appendix" in security
        assert "Cloud Run" in security

    def test_local_nlpd_rows_present(self, security: str) -> None:
        """The local-first nLPD checklist covers the required controls (AC5)."""
        for marker in ("LUKS", "chmod 600", "OpenRouter", "loopback"):
            assert marker in security

    def test_trivy_documented(self, security: str) -> None:
        """The dependency-scanning section documents the trivy image gate (AC3)."""
        assert "trivy" in security
        assert "HIGH/CRITICAL" in security


class TestReleaseGateDocs:
    """The v0.8.0 release gate and manual E2E checklist are documented (AC8/AC9)."""

    def test_admin_guide_defines_release_gate(self) -> None:
        """The admin guide defines the release gate and links the E2E checklist."""
        text = (PROJECT_ROOT / "docs" / "admin_guide.md").read_text(encoding="utf-8")
        assert "Release Gate" in text
        assert "trivy" in text.lower()
        assert "e2e-checklist" in text

    def test_e2e_checklist_exists(self) -> None:
        """The manual prod-lane E2E checklist exists."""
        assert (PROJECT_ROOT / "docs" / "e2e-checklist.md").is_file()

    def test_e2e_checklist_covers_happy_path(self) -> None:
        """The checklist covers OIDC login, agent chat, tool calls and plan approval."""
        text = (PROJECT_ROOT / "docs" / "e2e-checklist.md").read_text(encoding="utf-8")
        for step in ("OIDC login", "Agent chat", "Tool calls", "Plan approval"):
            assert step in text


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

    def test_forwarded_allow_ips_is_loopback_only(self, dockerfile: str) -> None:
        """uvicorn trusts X-Forwarded-* from loopback peers only (AC7, #112)."""
        assert "--forwarded-allow-ips='127.0.0.1,::1'" in dockerfile
        assert "--forwarded-allow-ips='*'" not in dockerfile
