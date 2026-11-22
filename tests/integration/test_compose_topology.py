"""Integration-level validation of the local Docker lane topology (ADR-007).

These tests inspect the declared compose files (base + per-lane overrides),
the lane environment templates, the pre-flight script and .gitignore without
requiring a running Docker daemon, so they remain fast and deterministic in
CI as well as local development.
"""

import copy
import datetime
import json
import os
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
COACH_MCP_PIN = "ghcr.io/fpittelo/coach"
COACH_MCP_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SERVICES = ("coach-web", "coach-mcp", "github-mcp")
CLOUD_RUN_WORKFLOW = ".github/workflows/cloud-run-deploy.yaml"
DEPLOY_SA_MEMBER = "serviceAccount:coach-web-deployer@coach-web-509209.iam.gserviceaccount.com"
RUNTIME_SA_MEMBER = "serviceAccount:coach-web-runtime@coach-web-509209.iam.gserviceaccount.com"
PROJECT_IAM_VIEWER_ROLE = "projects/coach-web-509209/roles/coachWebProjectIamViewer"
# The proven compose invocation (compose.yaml github-mcp command), which under
# K8s/Knative semantics must reach the container as `args`, never as `command`.
GITHUB_MCP_CLOUD_RUN_ARGS = ["http", "--port", "8001", "--listen-host", "0.0.0.0"]


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


def inline_python_blocks(script: str) -> list[str]:
    """Extract every ``python3 -c '`` body from a workflow shell script.

    The bodies are single-quoted bash strings closed by a line containing only
    ``'``. An apostrophe inside the Python (typically a comment) terminates the
    bash string early while leaving both sides balanced, so ``bash -n`` cannot
    see the bug — extracting to the real closing line and compiling is the
    only reliable check (the #417 gate simulation caught exactly this).
    """
    blocks: list[str] = []
    lines = script.splitlines()
    index = 0
    while index < len(lines):
        marker = "python3 -c '"
        if marker in lines[index]:
            tail = lines[index].split(marker, 1)[1]
            if tail.strip():
                # Single-line ``python3 -c '...'`` form — the closing quote is
                # on the same line and the body cannot hide an apostrophe
                # comment the way a multi-line body can.
                index += 1
                continue
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != "'":
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def cloud_run_service_yaml() -> dict[str, Any]:
    """Parse the Knative service YAML heredoc from the Cloud Run workflow."""
    workflow = load_yaml(CLOUD_RUN_WORKFLOW)
    steps = workflow["jobs"]["deploy"]["steps"]
    render = next(step for step in steps if step.get("name") == "Render the service YAML")
    script = render["run"]
    match = re.search(r"<<YAML\n(.*?)\nYAML\n", script, re.DOTALL)
    assert match is not None, "the render step must contain the service YAML heredoc"
    rendered = yaml.safe_load(match.group(1))
    assert isinstance(rendered, dict), "the heredoc must render a YAML mapping"
    return rendered


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

    def test_coach_web_encryption_key_scoping(self, base_compose: dict[str, Any]) -> None:
        """DATA_ENCRYPTION_KEY is declared for coach-web only (MADR-0008 C2/C3, #306).

        The lane env file carries the key, but compose interpolation only
        injects variables the env map declares — an omitted entry never
        reaches the container and migration 0006 hard-fails at startup.
        """
        env = base_compose["services"]["coach-web"].get("environment", {})
        assert env.get("DATA_ENCRYPTION_KEY") == "${DATA_ENCRYPTION_KEY:-}"
        for foreign in ("coach-mcp", "github-mcp"):
            foreign_env = base_compose["services"][foreign].get("environment", {})
            assert "DATA_ENCRYPTION_KEY" not in foreign_env

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


class TestCloudRunDeployWorkflow:
    """The Cloud Run lane maps the compose sidecar contract to K8s semantics (#316).

    In Compose, ``command:`` overrides CMD and is APPENDED to the image
    ENTRYPOINT. In Kubernetes/Knative (Cloud Run), ``command:`` REPLACES the
    ENTRYPOINT and ``args:`` is the CMD-equivalent. The first-deploy failure
    (issue #316, revision coach-web-00001-qnn: "failed to resolve binary
    path: ... \"http\"") came from translating the compose ``command:`` list
    1:1 into a K8s ``command:`` — Cloud Run exec'd a binary named ``http``.
    These tests pin the corrected semantics on the workflow source.
    """

    @pytest.fixture(scope="class")
    def containers(self) -> dict[str, Any]:
        """The rendered service YAML containers keyed by container name."""
        service = cloud_run_service_yaml()
        spec = service["spec"]["template"]["spec"]
        return {container["name"]: container for container in spec["containers"]}

    def test_three_container_pod(self, containers: dict[str, Any]) -> None:
        """The pod carries exactly the compose topology's three containers."""
        assert set(containers) == set(SERVICES)

    def test_github_mcp_passes_the_transport_via_args_not_command(
        self, containers: dict[str, Any]
    ) -> None:
        """github-mcp passes the transport subcommand via ``args:`` (#316).

        The image ENTRYPOINT (/server/github-mcp-server, CMD=stdio) must stay
        intact; a K8s ``command:`` whose first element is not the entrypoint
        binary is exactly the failure class that broke the first deploy.
        """
        container = containers["github-mcp"]
        assert "command" not in container
        assert container["args"] == GITHUB_MCP_CLOUD_RUN_ARGS

    def test_github_mcp_args_match_the_compose_contract(
        self, containers: dict[str, Any], base_compose: dict[str, Any]
    ) -> None:
        """The Cloud Run args mirror the proven compose invocation byte-exact."""
        compose_command = base_compose["services"]["github-mcp"]["command"]
        assert containers["github-mcp"]["args"] == compose_command

    def test_coach_mcp_keeps_the_image_entrypoint(
        self, containers: dict[str, Any], base_compose: dict[str, Any]
    ) -> None:
        """coach-mcp overrides neither command nor args — env-driven contract."""
        container = containers["coach-mcp"]
        assert "command" not in container
        assert "args" not in container
        env = {entry["name"]: entry.get("value") for entry in container["env"]}
        compose_env = base_compose["services"]["coach-mcp"]["environment"]
        for key in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT"):
            assert env[key] == compose_env[key]

    @pytest.fixture(scope="class")
    def smoke_script(self) -> str:
        """The `run` script of the IAP-aware smoke step in the Cloud Run lane."""
        workflow = load_yaml(CLOUD_RUN_WORKFLOW)
        steps = workflow["jobs"]["deploy"]["steps"]
        smoke = next(step for step in steps if step.get("name") == "IAP-aware smoke checks")
        assert isinstance(smoke["run"], str), "the smoke step must have a run script"
        return smoke["run"]

    def test_smoke_id_token_is_minted_via_the_iam_credentials_api(self, smoke_script: str) -> None:
        """The smoke mints its ID token via a direct generateIdToken call (#316).

        The WIF session authenticates with external-account credentials, and
        ``gcloud auth print-identity-token --audiences`` fails for that account
        type ("Invalid account type for --audiences. Requires valid service
        account." — deploy attempt #5). gcloud's ``--impersonate-service-account``
        shortcut is no alternative either: it first drives
        ``generateAccessToken`` (PERMISSION_DENIED — deploy attempt #7), the
        broader tokenCreator capability the deployer's self-grant deliberately
        excludes (cyber C1). The mint must be a direct POST to the IAM
        Credentials API ``generateIdToken`` method — needing exactly the narrow
        getOpenIdToken permission — against the Actions-variable audience, with
        the minted token masked — the bug class must not regress.
        """
        assert (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/${DEPLOY_SA}:generateIdToken"
            in smoke_script
        )
        assert '-H "Authorization: Bearer $(gcloud auth print-access-token)"' in smoke_script
        assert (
            '-d "{\\"audience\\": \\"${GOOGLE_OIDC_CLIENT_ID}\\", \\"includeEmail\\": true}"'
            in smoke_script
        )
        assert "--impersonate-service-account" not in smoke_script
        mint = smoke_script.index("generateIdToken")
        assert smoke_script.index('echo "::add-mask::${token}"') > mint

    def test_smoke_asserts_redirect_client_id_matches_the_oidc_variable(
        self, smoke_script: str
    ) -> None:
        """The scraped IAP client_id must equal the GOOGLE_OIDC_CLIENT_ID variable (#316).

        A mismatch is IAP settings drift (the same OAuth client must be both
        the IAP custom client and the app OIDC client) — the check must fail
        the run before any ID token is minted.
        """
        drift = 'if [ "${client_id}" != "${GOOGLE_OIDC_CLIENT_ID}" ]; then'
        assert drift in smoke_script
        assert smoke_script.index(drift) < smoke_script.index("generateIdToken")


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
    def test_coach_mcp_lane_digest_pinned(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """The coach-mcp sidecar is digest-pinned per lane via COACH_MCP_IMAGE (#275, AC1).

        The upstream lane tag floats (fpittelo/coach is a separate pipeline), so
        the digest is the pin; the CI drift check guards it against upstream
        movement (scripts/check_sidecar_drift.sh).
        """
        image = effective_compose[lane]["services"]["coach-mcp"]["image"]
        prefix = f"${{COACH_MCP_IMAGE:-{COACH_MCP_PIN}@"
        assert image.startswith(prefix), image
        assert image.endswith("}"), image
        assert COACH_MCP_DIGEST_RE.fullmatch(image[len(prefix) : -1]), image

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
    def test_lane_env_template_pins_lane_sidecar_digest(self, lane: str) -> None:
        """The template's coach-mcp image is digest-pinned (#275, AC1)."""
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
        assert f"COACH_MCP_IMAGE={COACH_MCP_PIN}@sha256:" in text

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_template_matches_compose_pin(
        self, effective_compose: dict[str, dict[str, Any]], lane: str
    ) -> None:
        """The template's coach-mcp digest mirrors the compose default (#275).

        The template is what a lane env file is copied from; if it diverged from
        the compose pin, the lane would silently run a different sidecar.
        """
        image = effective_compose[lane]["services"]["coach-mcp"]["image"]
        compose_digest = image.rsplit("@", 1)[1].rstrip("}")
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")
        assert f"COACH_MCP_IMAGE={COACH_MCP_PIN}@{compose_digest}" in text

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
        assert "preflight_loopback_check.py" in script
        assert "coach-web" in script

    def test_loopback_checker_module_enforces_boundary(self) -> None:
        """The extracted publisher validator pins the loopback boundary (#138)."""
        module = PROJECT_ROOT / "scripts" / "preflight_loopback_check.py"
        assert module.is_file(), "scripts/preflight_loopback_check.py must exist"
        text = module.read_text(encoding="utf-8")
        assert "Publishers" in text
        assert "127.0.0.1" in text
        assert "publishes host ports but must publish none" in text

    def test_script_asserts_lane_env_file_permissions(self, script: str) -> None:
        """The script asserts every existing lane env file is mode 600 (MADR-0008 C3, #170)."""
        assert "preflight_env_permissions_check.py" in script
        assert "chmod 600" in script

    def test_env_permissions_checker_module_enforces_mode_600(self) -> None:
        """The extracted permission validator pins the 600 requirement (#170)."""
        module = PROJECT_ROOT / "scripts" / "preflight_env_permissions_check.py"
        assert module.is_file(), "scripts/preflight_env_permissions_check.py must exist"
        text = module.read_text(encoding="utf-8")
        assert "0o600" in text
        assert "must be" in text

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


class TestLaneScript:
    """Minimal tracking assertions for the lane.sh entrypoint (#134)."""

    @pytest.fixture
    def script(self) -> str:
        """Return the lane script contents."""
        path = PROJECT_ROOT / "scripts" / "lane.sh"
        assert path.is_file(), "scripts/lane.sh must exist"
        return path.read_text(encoding="utf-8")

    def test_script_exists_and_is_executable(self) -> None:
        """scripts/lane.sh exists and is executable."""
        script = PROJECT_ROOT / "scripts" / "lane.sh"
        assert script.is_file()
        assert script.stat().st_mode & 0o111, "script must be executable"

    def test_script_passes_bash_syntax_check(self) -> None:
        """bash -n validates the script syntax."""
        result = subprocess.run(
            ["/bin/bash", "-n", str(PROJECT_ROOT / "scripts" / "lane.sh")],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_points_prod_lifecycle_at_systemd(self, script: str) -> None:
        """The prod lifecycle is owned by systemd; lane.sh says so (#328 AC6)."""
        assert "systemctl --user" in script
        assert "coach-web.service" in script
        assert "compose.prod.yml" in script

    def test_script_scopes_teardown_to_its_own_project(self, script: str) -> None:
        """Teardown targets only the lane's compose project (AC2)."""
        assert 'PROJECT="coach-web-${LANE}"' in script
        assert "down --remove-orphans" in script

    def test_script_uses_lane_env_file(self, script: str) -> None:
        """The script passes the lane env file via --env-file (AC2)."""
        assert "--env-file" in script
        assert ".env.${LANE}" in script


class TestSidecarDriftCheck:
    """The coach-mcp sidecar pin drift guard (#275, AC2)."""

    SCRIPT = "scripts/check_sidecar_drift.sh"

    SHIM_DOCKER_SOURCE = '''#!/usr/bin/env python3
"""Test shim for the docker CLI: emits a canned imagetools inspect digest."""
import os
import sys

args = sys.argv[1:]
if args[:3] == ["buildx", "imagetools", "inspect"]:
    print(f"Name:      {args[3]}")
    print(f"Digest:    {os.environ['DRIFT_SHIM_DIGEST']}")
sys.exit(0)
'''

    @pytest.fixture
    def script(self) -> str:
        """Return the drift-check script contents."""
        path = PROJECT_ROOT / self.SCRIPT
        assert path.is_file(), f"{self.SCRIPT} must exist"
        return path.read_text(encoding="utf-8")

    def test_script_exists_and_is_executable(self) -> None:
        """The drift check ships as an executable script."""
        path = PROJECT_ROOT / self.SCRIPT
        assert path.is_file()
        assert path.stat().st_mode & 0o111, "script must be executable"

    def test_script_passes_bash_syntax_check(self) -> None:
        """bash -n validates the script syntax."""
        result = subprocess.run(
            ["/bin/bash", "-n", str(PROJECT_ROOT / self.SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_script_covers_every_lane(self, script: str) -> None:
        """The check resolves the floating tag of every lane."""
        assert "LANES=(dev qa prod)" in script
        for name in LANE_COMPOSE.values():
            assert name in script

    def test_script_fails_loudly_on_drift(self, script: str) -> None:
        """A mismatch is reported and exits non-zero (AC2)."""
        assert "drift" in script
        assert "status=1" in script
        assert 'exit "${status}"' in script

    def test_ci_runs_the_drift_check(self) -> None:
        """The Compose Lane Validation job invokes the drift check (AC2)."""
        ci = load_yaml(".github/workflows/ci.yaml")
        steps = ci["jobs"]["compose-validation"]["steps"]
        runs = [str(step.get("run", "")) for step in steps]
        assert any("check_sidecar_drift.sh" in run for run in runs)

    def _sandbox(self, tmp_path: Path, pinned: str, upstream: str) -> tuple[Path, dict[str, str]]:
        """Build a repo copy with a docker shim emitting the upstream digest."""
        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        script = repo / self.SCRIPT
        shutil.copy2(PROJECT_ROOT / self.SCRIPT, script)
        script.chmod(0o755)
        for name in LANE_COMPOSE.values():
            (repo / name).write_text(
                "services:\n"
                "  coach-mcp:\n"
                f"    image: ${{COACH_MCP_IMAGE:-{COACH_MCP_PIN}@{pinned}}}\n",
                encoding="utf-8",
            )
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "docker"
        shim.write_text(self.SHIM_DOCKER_SOURCE, encoding="utf-8")
        shim.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["DRIFT_SHIM_DIGEST"] = upstream
        return repo, env

    def test_matching_pin_exits_zero(self, tmp_path: Path) -> None:
        """A pin equal to the upstream digest passes."""
        digest = "sha256:" + "a" * 64
        repo, env = self._sandbox(tmp_path, pinned=digest, upstream=digest)
        result = subprocess.run(
            [str(repo / self.SCRIPT)],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "OK" in result.stdout

    def test_drift_exits_non_zero_and_names_the_lane(self, tmp_path: Path) -> None:
        """A moved upstream tag fails loudly and names the offending lane (AC2)."""
        repo, env = self._sandbox(
            tmp_path, pinned="sha256:" + "a" * 64, upstream="sha256:" + "b" * 64
        )
        result = subprocess.run(
            [str(repo / self.SCRIPT)],
            cwd=repo,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert "drift" in result.stderr
        assert "dev" in result.stderr


class TestCIContainerScan:
    """CI must scan the built image with trivy (AC3, #113)."""

    @pytest.fixture(scope="class")
    @classmethod
    def ci(cls) -> dict[str, Any]:
        """Load the parsed CI workflow."""
        return load_yaml(".github/workflows/ci.yaml")

    def test_trivy_image_scan_job_present(self, ci: dict[str, Any]) -> None:
        """A dedicated image-scan job exists."""
        assert "image-scan" in ci.get("jobs", {})

    def test_trivy_action_pinned(self, ci: dict[str, Any]) -> None:
        """Every trivy scan step is pinned to the same action version."""
        steps = ci["jobs"]["image-scan"]["steps"]
        trivy = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("aquasecurity/trivy-action@")
        ]
        assert len(trivy) == 2
        assert all(step["uses"] == "aquasecurity/trivy-action@v0.36.0" for step in trivy)
        # The second scan is the landing surface (MADR-0031, #427).
        landing = next(
            step for step in trivy if step["with"].get("image-ref") == "coach-web-landing:scan"
        )
        assert landing["with"].get("severity") == "HIGH,CRITICAL"

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
        """Both scan builds (console + landing) load into the local daemon."""
        steps = ci["jobs"]["image-scan"]["steps"]
        builds = [
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
        ]
        assert len(builds) == 2
        for build in builds:
            assert build["with"].get("load") is True
        # The second build is the landing surface (MADR-0031, #427).
        landing = next(
            build for build in builds if build["with"].get("file") == "Dockerfile.landing"
        )
        assert landing["with"].get("tags") == "coach-web-landing:scan"


class TestTrivyIgnoreRegister:
    """The .trivyignore accepted-risk register is auditable (issue #113)."""

    @pytest.fixture
    def trivyignore(self) -> str:
        """Return the .trivyignore contents."""
        path = PROJECT_ROOT / ".trivyignore"
        assert path.is_file(), ".trivyignore must exist at the repo root"
        return path.read_text(encoding="utf-8")

    def test_register_exists_at_repo_root(self) -> None:
        """The accepted-risk register exists at the repo root."""
        assert (PROJECT_ROOT / ".trivyignore").is_file()

    def test_every_cve_entry_is_time_boxed(self, trivyignore: str) -> None:
        """Every CVE entry carries an '# exp:' comment within 2 lines above."""
        lines = trivyignore.splitlines()
        for index, line in enumerate(lines):
            if not line.strip().startswith("CVE-"):
                continue
            window = lines[max(0, index - 2) : index]
            assert any(
                entry.strip().startswith("# exp:") for entry in window
            ), f"{line.strip()} is not time-boxed by an '# exp:' comment"

    def test_all_expiry_dates_are_valid_and_future(self, trivyignore: str) -> None:
        """Every '# exp:' date parses as YYYY-MM-DD and lies in the future."""
        today = datetime.date.today()
        expiries = re.findall(r"# exp: (\d{4}-\d{2}-\d{2})", trivyignore)
        assert expiries, "the register must declare at least one expiry date"
        for expiry in expiries:
            parsed = datetime.date.fromisoformat(expiry)
            assert parsed > today, f"expiry {expiry} has passed; re-triage required"


class TestSecurityDocsLocalTopology:
    """docs/security.md must carry the local-topology STRIDE + nLPD model (AC4/AC5)."""

    @pytest.fixture(scope="class")
    @classmethod
    def security(cls) -> str:
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
        """uvicorn trusts X-Forwarded-* from loopback peers only by default (AC7, #112).

        #316 AC2 parameterizes the trusted-peer list as the FORWARDED_ALLOW_IPS
        env var: the baked default stays loopback-only (local lane behavior
        unchanged), the entrypoint passes it through with the same fallback,
        and a literal wildcard is never baked into the image — the Cloud Run
        prod lane sets FORWARDED_ALLOW_IPS=* via the service env (MADR-0027 D4).
        """
        assert "FORWARDED_ALLOW_IPS=127.0.0.1,::1" in dockerfile
        assert "${FORWARDED_ALLOW_IPS:-127.0.0.1,::1}" in dockerfile
        assert "--forwarded-allow-ips='*'" not in dockerfile

    def test_runtime_patches_os_findings_that_have_a_fix(self, dockerfile: str) -> None:
        """Fixable base-image CVEs are patched in the image, not accepted (#351).

        The accepted-risk register is reserved for *unfixed* CVEs (no version
        available upstream); the trivy gate stays strict on everything else.
        zlib1g (CVE-2026-76844 / CVE-2026-85091) is fixed in trixie-security,
        so the runtime stage upgrades it in place instead of ignoring it.
        """
        assert "apt-get install" in dockerfile
        assert "--only-upgrade" in dockerfile
        assert "zlib1g" in dockerfile


class TestPersistenceTopology:
    """Lane volume + DB path wiring for the persistence layer (#166, MADR-008).

    The SQLite database file lives in a lane volume mounted at /data (WAL side
    files alongside); the volume name derives from the compose project name so
    concurrent lanes never share a database. COACH_DB_PATH is scoped to
    coach-web with the container default interpolated.
    """

    def test_coach_web_mounts_the_lane_volume_at_data(self, base_compose: dict[str, Any]) -> None:
        """coach-web mounts the coach-data volume at /data."""
        volumes = base_compose["services"]["coach-web"].get("volumes", [])

        assert "coach-data:/data" in volumes

    def test_sidecars_mount_no_volumes(self, base_compose: dict[str, Any]) -> None:
        """The MCP sidecars stay stateless — no volume mounts."""
        for service in ("coach-mcp", "github-mcp"):
            assert "volumes" not in base_compose["services"][service], service

    def test_volume_is_declared_without_pinned_name(self, base_compose: dict[str, Any]) -> None:
        """coach-data is declared top-level; its name derives from the project."""
        volumes = base_compose.get("volumes", {})

        assert "coach-data" in volumes
        # `coach-data:` with no properties parses as None — equivalent to an
        # empty mapping with no pinned name.
        assert "name" not in (volumes["coach-data"] or {})

    def test_coach_db_path_env_scoped_with_container_default(
        self, base_compose: dict[str, Any]
    ) -> None:
        """COACH_DB_PATH is scoped to coach-web with the /data default."""
        env = base_compose["services"]["coach-web"].get("environment", {})

        assert env.get("COACH_DB_PATH") == "${COACH_DB_PATH:-/data/coach.db}"

    def test_sidecars_do_not_receive_coach_db_path(self, base_compose: dict[str, Any]) -> None:
        """The DB path is coach-web's concern only."""
        for service in ("coach-mcp", "github-mcp"):
            env = base_compose["services"][service].get("environment", {})
            assert "COACH_DB_PATH" not in env, service

    @pytest.mark.parametrize("lane", ["dev", "qa", "prod"])
    def test_lane_env_templates_document_the_db_path_default(self, lane: str) -> None:
        """Every lane template documents the COACH_DB_PATH default (no secrets)."""
        text = (PROJECT_ROOT / f".env.{lane}.example").read_text(encoding="utf-8")

        assert "COACH_DB_PATH=/data/coach.db" in text


class TestDockerfilePersistenceEntrypoint:
    """The container entrypoint runs migrations before uvicorn (#166, MADR-008).

    Migrations run at container start (dev lane: automatic; prod lane: explicit
    step with the lane stopped/idle — same entrypoint command, operational
    discipline differs). A failed migration aborts startup rather than
    half-applying: the ``&&`` chain never reaches uvicorn.
    """

    @pytest.fixture
    def dockerfile(self) -> str:
        """Return the Dockerfile contents."""
        path = PROJECT_ROOT / "Dockerfile"
        assert path.is_file(), "Dockerfile must exist"
        return path.read_text(encoding="utf-8")

    def test_entrypoint_runs_migrations_before_uvicorn(self, dockerfile: str) -> None:
        """alembic upgrade head precedes uvicorn in the entrypoint chain."""
        entrypoint = dockerfile[dockerfile.index("ENTRYPOINT") :]

        assert "alembic" in entrypoint
        assert "upgrade head" in entrypoint
        assert "&&" in entrypoint
        assert entrypoint.index("alembic") < entrypoint.index("uvicorn")

    def test_entrypoint_targets_the_packaged_alembic_ini(self, dockerfile: str) -> None:
        """The entrypoint pins the alembic.ini shipped inside the package."""
        entrypoint = dockerfile[dockerfile.index("ENTRYPOINT") :]

        assert "/app/src/coach_web/alembic.ini" in entrypoint

    def test_data_mountpoint_pre_created_with_runtime_ownership(self, dockerfile: str) -> None:
        """/data exists with coach-web ownership so the named volume inherits it."""
        assert "install -d -o coach-web -g coach-web /data" in dockerfile


class TestSnapshotStoreDeployGates:
    """The durable snapshot store is wired and asserted fail-closed (#417).

    MADR-0032 binding conditions C1/C4/C5/C6 require the production lane to
    (a) render the snapshot configuration onto the serving container, (b)
    verify the dedicated bucket's residency/hardening/retention posture, (c)
    verify the runtime identity holds ZERO project-level roles, and (d) verify
    the bucket IAM is EXACTLY the two intended custom-role bindings. Every
    gate reads live GCP state back and aborts the deploy on drift.
    """

    SNAPSHOT_BUCKET = "coach-web-509209-snapshots"
    SNAPSHOT_OBJECT = "snapshots/coach.db.fernet"
    RUNTIME_ROLE = "projects/coach-web-509209/roles/coachWebSnapshotObjectActor"
    DEPLOYER_ROLE = "projects/coach-web-509209/roles/coachWebSnapshotBucketReader"

    @pytest.fixture(scope="class")
    def workflow(self) -> dict[str, Any]:
        """The Cloud Run deploy workflow mapping."""
        return load_yaml(CLOUD_RUN_WORKFLOW)

    @pytest.fixture(scope="class")
    def steps(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """The deploy job steps."""
        return list(workflow["jobs"]["deploy"]["steps"])

    def _step(self, steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
        """Return the step with the exact name (fails loudly when renamed)."""
        match = next((step for step in steps if step.get("name") == name), None)
        assert match is not None, f"missing workflow step: {name}"
        return match

    def _assert_bash_syntax(self, script: str, tmp_path: Path) -> None:
        """Validate an embedded workflow script with ``bash -n``."""
        path = tmp_path / "step.sh"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["/bin/bash", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_workflow_env_declares_the_bucket_and_object(self, workflow: dict[str, Any]) -> None:
        """SNAPSHOT_BUCKET / SNAPSHOT_OBJECT are workflow-level constants."""
        env = workflow["env"]

        assert env["SNAPSHOT_BUCKET"] == self.SNAPSHOT_BUCKET
        assert env["SNAPSHOT_OBJECT"] == self.SNAPSHOT_OBJECT

    def test_service_yaml_renders_the_snapshot_env(self) -> None:
        """The coach-web container carries bucket, object and 60 s debounce."""
        containers = cloud_run_service_yaml()["spec"]["template"]["spec"]["containers"]
        coach_web = next(c for c in containers if c["name"] == "coach-web")
        env = {entry["name"]: entry.get("value") for entry in coach_web["env"]}

        assert env["COACH_SNAPSHOT_BUCKET"] == "${SNAPSHOT_BUCKET}"
        assert env["COACH_SNAPSHOT_OBJECT"] == "${SNAPSHOT_OBJECT}"
        assert env["COACH_SNAPSHOT_DEBOUNCE_SECONDS"] == "60"

    def test_post_deploy_config_assertion_covers_the_snapshot_env(
        self, steps: list[dict[str, Any]]
    ) -> None:
        """The deployed-env read-back gate asserts the three snapshot values."""
        script = self._step(steps, "Assert deployed config (AUTH_REDIRECT_URI, TRUSTED_HOSTS)")[
            "run"
        ]

        assert "COACH_SNAPSHOT_BUCKET" in script
        assert "COACH_SNAPSHOT_OBJECT" in script
        assert "COACH_SNAPSHOT_DEBOUNCE_SECONDS" in script
        assert "SNAPSHOT_BUCKET_EXPECTED" in script or "coach-web-509209-snapshots" in script

    def test_pre_deploy_gate_asserts_runtime_zero_project_roles(
        self, steps: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """C4: coach-web-runtime must hold no project-level IAM binding."""
        script = self._step(steps, "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)")[
            "run"
        ]

        assert "projects get-iam-policy" in script
        assert "RUNTIME_SA" in script
        assert "zero project-level" in script
        self._assert_bash_syntax(script, tmp_path)

    def test_pre_deploy_gate_asserts_bucket_metadata(self, steps: list[dict[str, Any]]) -> None:
        """C1/C5: residency, UBLA, PAP, versioning, Soft Delete and lifecycle."""
        script = self._step(steps, "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)")[
            "run"
        ]

        for marker in (
            "storage buckets describe",
            "EUROPE-WEST6",
            "locationType",
            "uniformBucketLevelAccess",
            "publicAccessPrevention",
            "versioning",
            "softDeletePolicy",
            "604800",
            "numNewerVersions",
            "daysSinceNoncurrentTime",
        ):
            assert marker in script, marker

    def test_pre_deploy_gate_asserts_the_exact_bucket_iam_allowlist(
        self, steps: list[dict[str, Any]]
    ) -> None:
        """C4: exactly the two custom-role bindings; no allUsers/extra principals."""
        script = self._step(steps, "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)")[
            "run"
        ]

        assert "buckets get-iam-policy" in script
        assert self.RUNTIME_ROLE in script
        assert self.DEPLOYER_ROLE in script
        assert "allUsers" in script
        assert "allAuthenticatedUsers" in script
        # Live evidence 7078137681: the bucket policy (etag CAs=) is exactly
        # those two bindings after the legacyBucket*/legacyObject* scrub.
        assert "legacyBucket" in script
        assert '"RUNTIME_MEMBER"' in script or "RUNTIME_MEMBER" in script
        assert "DEPLOY_MEMBER" in script

    def test_bucket_gate_coerces_the_soft_delete_duration(
        self, steps: list[dict[str, Any]]
    ) -> None:
        """GCP serializes int64 duration fields as str-or-int — int() handles both."""
        script = self._step(steps, "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)")[
            "run"
        ]

        assert 'as_int(soft_delete.get("retentionDurationSeconds"))' in script
        assert "retention != 604800" in script

    def test_workflow_records_the_ratified_project_iam_viewer_role(
        self, workflow: dict[str, Any]
    ) -> None:
        """The nonexistent predefined role is replaced by the custom role (#417)."""
        text = (PROJECT_ROOT / CLOUD_RUN_WORKFLOW).read_text(encoding="utf-8")

        assert "projects/coach-web-509209/roles/coachWebProjectIamViewer" in text
        assert "7078148984" in text
        # The bucket gate reads the project policy (custom role permission).
        assert "projects get-iam-policy" in text

    def test_render_and_assertion_steps_are_valid_bash(
        self, steps: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """Both the render and the snapshot gate scripts parse under bash -n."""
        for name in (
            "Render the service YAML",
            "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)",
            "Assert deployed config (AUTH_REDIRECT_URI, TRUSTED_HOSTS)",
        ):
            script = self._step(steps, name)["run"]
            self._assert_bash_syntax(script, tmp_path)

    def test_dockerfile_runs_boot_restore_before_migrations(self) -> None:
        """The entrypoint restores the snapshot BEFORE alembic upgrade (C2/C6)."""
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        entrypoint = dockerfile[dockerfile.index("ENTRYPOINT") :]

        assert "python -m coach_web.snapshot boot-restore" in entrypoint
        assert entrypoint.index("boot-restore") < entrypoint.index("alembic")
        assert entrypoint.index("alembic") < entrypoint.index("uvicorn")

    def test_embedded_python_blocks_compile(self, workflow: dict[str, Any]) -> None:
        """Every inline ``python3 -c`` body in the deploy workflow compiles.

        ``bash -n`` cannot see inside single-quoted Python bodies; an
        apostrophe in a comment silently truncated the #417 snapshot gate
        while the step still exited 0 (caught by the local gate simulation).
        """
        checked = 0
        for step in workflow["jobs"]["deploy"]["steps"]:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            for body in inline_python_blocks(script):
                compile(body, f"<{step.get('name')}>", "exec")
                checked += 1
        assert checked >= 4, "the deploy workflow must embed compiled gate scripts"

    def test_docs_record_the_implemented_snapshot_store(self) -> None:
        """security.md / admin_guide.md describe the shipped store, not a plan."""
        security = (PROJECT_ROOT / "docs" / "security.md").read_text(encoding="utf-8")
        admin = (PROJECT_ROOT / "docs" / "admin_guide.md").read_text(encoding="utf-8")

        for doc in (security, admin):
            assert "MADR-0032" in doc
            assert "COACH_SNAPSHOT_BUCKET" in doc
            assert "not yet implemented" not in doc
            assert "PLANNED" not in doc
            # The ADR-named predefined role does not exist in GCP: the
            # @architect-ratified 1-permission custom role is the SSOT.
            assert "coachWebProjectIamViewer" in doc

    @pytest.mark.parametrize(
        "adr",
        [
            "docs/adr/0032-durable-longitudinal-memory-snapshot-store.md",
            "docs/adr/0027-cloud-run-production-operating-model.md",
            "docs/adr/0031-public-landing-surface-with-iap-unchanged.md",
        ],
    )
    def test_adrs_carry_the_ratified_iam_viewer_role(self, adr: str) -> None:
        """C4's role reference matches live GCP reality (#417 architect ruling)."""
        text = (PROJECT_ROOT / adr).read_text(encoding="utf-8")

        assert "coachWebProjectIamViewer" in text
        assert "resourcemanager.projects.getIamPolicy" in text

    # ------------------------------------------------------------------
    # Executable gates (M3/C9): the workflow gate bodies run for real.
    #
    # The JSON below is the exact live read-back @devops recorded in #417
    # comment 7078137681 (snake_case ``gcloud storage`` shapes, project
    # policy bindings, bucket policy etag ``CAs=``). A logical inversion of
    # an allowlist comparison cannot pass any more: each drift class below
    # must make the shipped gate exit non-zero.
    # ------------------------------------------------------------------

    GATE_STEP = "Assert snapshot store hardening and snapshot IAM (C1/C4/C5)"

    PROJECT_IAM_BASELINE: dict[str, Any] = {
        "bindings": [
            {"members": [DEPLOY_SA_MEMBER], "role": PROJECT_IAM_VIEWER_ROLE},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/iap.admin"},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/resourcemanager.viewer"},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/serviceusage.serviceUsageConsumer"},
            {"members": ["user:frederic.pitteloud@gmail.com"], "role": "roles/owner"},
        ],
        "etag": "BwZg21q5aJA=",
        "version": 3,
    }

    BUCKET_METADATA_BASELINE: dict[str, Any] = {
        "location": "EUROPE-WEST6",
        "location_type": "region",
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
        "versioning_enabled": True,
        "soft_delete_policy": {
            "retentionDurationSeconds": "604800",
            "effectiveTime": "2026-11-22T10:50:46.535000+00:00",
        },
        "lifecycle_config": {
            "rule": [
                {
                    "action": {"type": "Delete"},
                    "condition": {"isLive": False, "numNewerVersions": 2},
                },
                {
                    "action": {"type": "Delete"},
                    "condition": {"isLive": False, "daysSinceNoncurrentTime": 7},
                },
            ]
        },
        "default_storage_class": "STANDARD",
        "creation_time": "2026-11-22T10:50:46+0000",
    }

    BUCKET_IAM_BASELINE: dict[str, Any] = {
        "bindings": [
            {
                "members": [DEPLOY_SA_MEMBER],
                "role": "projects/coach-web-509209/roles/coachWebSnapshotBucketReader",
            },
            {
                "members": [RUNTIME_SA_MEMBER],
                "role": "projects/coach-web-509209/roles/coachWebSnapshotObjectActor",
            },
        ],
        "etag": "CAs=",
    }

    GCLOUD_SHIM = '''#!/usr/bin/env python3
"""Test shim for gcloud: replays the 7078137681 JSON read-backs."""
import os
import sys

args = sys.argv[1:]
if args[:2] == ["projects", "get-iam-policy"]:
    variable = "SHIM_PROJECT_IAM"
elif args[:3] == ["storage", "buckets", "describe"]:
    variable = "SHIM_BUCKET_METADATA"
elif args[:3] == ["storage", "buckets", "get-iam-policy"]:
    variable = "SHIM_BUCKET_IAM"
else:
    sys.stderr.write("unexpected gcloud invocation: " + " ".join(args))
    sys.exit(2)
with open(os.environ[variable], encoding="utf-8") as handle:
    sys.stdout.write(handle.read())
'''

    def _gate_script(self) -> str:
        """The shipped ``run`` script of the snapshot store gate step."""
        workflow = load_yaml(CLOUD_RUN_WORKFLOW)
        steps = workflow["jobs"]["deploy"]["steps"]
        return str(self._step(steps, self.GATE_STEP)["run"])

    def _run_gate(
        self,
        tmp_path: Path,
        *,
        project_iam: dict[str, Any] | None = None,
        bucket_metadata: dict[str, Any] | None = None,
        bucket_iam: dict[str, Any] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute the extracted gate step against the replayed GCP read-backs."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "gcloud"
        shim.write_text(self.GCLOUD_SHIM, encoding="utf-8")
        shim.chmod(0o755)
        payloads = {
            "SHIM_PROJECT_IAM": project_iam or self.PROJECT_IAM_BASELINE,
            "SHIM_BUCKET_METADATA": bucket_metadata or self.BUCKET_METADATA_BASELINE,
            "SHIM_BUCKET_IAM": bucket_iam or self.BUCKET_IAM_BASELINE,
        }
        env = dict(os.environ)
        for name, value in load_yaml(CLOUD_RUN_WORKFLOW)["env"].items():
            env.setdefault(name, str(value))
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        for name, payload in payloads.items():
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            env[name] = str(path)
        script = tmp_path / "gate.sh"
        script.write_text(self._gate_script(), encoding="utf-8")
        return subprocess.run(
            ["/bin/bash", str(script)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_gate_step_embeds_three_executable_blocks(self) -> None:
        """The gate step carries the project-IAM, bucket and bucket-IAM bodies."""
        assert len(inline_python_blocks(self._gate_script())) == 3

    def test_live_bootstrap_evidence_passes_the_gate(self, tmp_path: Path) -> None:
        """The exact live 7078137681 read-backs exit 0 (positive baseline)."""
        result = self._run_gate(tmp_path)

        assert result.returncode == 0, result.stdout + result.stderr
        assert "C4 verified" in result.stdout

    DRIFT_CASES = {
        "allUsers": lambda project, bucket, policy: (
            policy["bindings"].append(
                {"members": ["allUsers"], "role": "roles/storage.objectViewer"}
            )
        ),
        "allAuthenticatedUsers": lambda project, bucket, policy: (
            policy["bindings"].append(
                {"members": ["allAuthenticatedUsers"], "role": "roles/storage.objectViewer"}
            )
        ),
        "legacyBucketOwner": lambda project, bucket, policy: (
            policy["bindings"].append(
                {
                    "members": ["projectOwner:coach-web-509209"],
                    "role": "roles/storage.legacyBucketOwner",
                }
            )
        ),
        "publicAccessPreventionInherited": lambda project, bucket, policy: bucket.update(
            {"public_access_prevention": "inherited"}
        ),
        "softDeleteDisabled": lambda project, bucket, policy: bucket["soft_delete_policy"].update(
            {"retentionDurationSeconds": "0"}
        ),
        "softDeleteOneDay": lambda project, bucket, policy: bucket["soft_delete_policy"].update(
            {"retentionDurationSeconds": "86400"}
        ),
        "missingNumNewerVersionsRule": lambda project, bucket, policy: bucket[
            "lifecycle_config"
        ].update(
            {
                "rule": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"isLive": False, "daysSinceNoncurrentTime": 7},
                    }
                ]
            }
        ),
        "missingDaysSinceNoncurrentTimeRule": lambda project, bucket, policy: bucket[
            "lifecycle_config"
        ].update(
            {
                "rule": [
                    {
                        "action": {"type": "Delete"},
                        "condition": {"isLive": False, "numNewerVersions": 2},
                    }
                ]
            }
        ),
        "wrongLocation": lambda project, bucket, policy: bucket.update(
            {"location": "EUROPE-WEST4"}
        ),
        "ublaDisabled": lambda project, bucket, policy: bucket.update(
            {"uniform_bucket_level_access": False}
        ),
        "runtimeHoldsProjectRole": lambda project, bucket, policy: project["bindings"].append(
            {
                "members": [RUNTIME_SA_MEMBER],
                "role": "roles/storage.objectViewer",
            }
        ),
        "deployerMissingProjectIamViewer": lambda project, bucket, policy: project.update(
            {
                "bindings": [
                    binding
                    for binding in project["bindings"]
                    if binding["role"] != "projects/coach-web-509209/roles/coachWebProjectIamViewer"
                ]
            }
        ),
    }

    @pytest.mark.parametrize("drift", DRIFT_CASES, ids=list(DRIFT_CASES))
    def test_drift_classes_are_rejected(self, tmp_path: Path, drift: str) -> None:
        """Every injected drift class makes the shipped gate exit non-zero (M3)."""
        project = copy.deepcopy(self.PROJECT_IAM_BASELINE)
        bucket = copy.deepcopy(self.BUCKET_METADATA_BASELINE)
        policy = copy.deepcopy(self.BUCKET_IAM_BASELINE)
        self.DRIFT_CASES[drift](project, bucket, policy)

        result = self._run_gate(
            tmp_path, project_iam=project, bucket_metadata=bucket, bucket_iam=policy
        )

        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout


class TestLandingSurfaceContainerAndGates:
    """The public static landing surface container and its fail-closed gates (#427).

    MADR-0031 (accepted) binds conditions C1-C7 on the second, public Cloud Run
    service: a dedicated ZERO-role runtime identity (C1/C2), per-service
    invoker/IAP posture (C3), resource-scoped deploy grants (C4), a hardened
    digest-pinned container (C5/C6) and content provenance with the canonical
    CTA (C7). The container is pinned declaratively; the shipped
    ``cloud-run-deploy.yaml`` gates are executed for real against replayed
    ``gcloud``/``curl`` read-backs — positive payloads must pass, every drift
    class must exit non-zero.
    """

    LANDING_SERVICE = "coach-web-landing"
    LANDING_SA_MEMBER = "serviceAccount:coach-web-landing@coach-web-509209.iam.gserviceaccount.com"
    LANDING_SA_NAME = "coach-web-landing@coach-web-509209.iam.gserviceaccount.com"
    LANDING_IMAGE = "ghcr.io/fpittelo/coach-web-landing@sha256:" + "c" * 64
    LANDING_URL = "https://coach-web-landing-853194930683.europe-west6.run.app"
    CANONICAL_CTA = "https://coach-web-afff35tqxq-oa.a.run.app/auth/login"
    IAM_GATE_STEP = "Assert landing surface IAM (MADR-0031 C1/C2)"
    C3_GATE_STEP = "Assert landing surface public posture (MADR-0031 C3)"
    SPEC_GATE_STEP = "Assert deployed landing spec (MADR-0031 C5/C6)"
    PROBE_STEP = "Probe the public landing surface (MADR-0031 C7)"
    IAP_GATE_STEP = (
        "Assert no project-level run.admin or iap.admin on the deploy SA"
        " (AC3, Amendments 0002 + 0005)"
    )

    # ------------------------------------------------------------------
    # Declarative container pins (C5/C6)
    # ------------------------------------------------------------------

    @pytest.fixture
    def landing_dockerfile(self) -> str:
        """Return the Dockerfile.landing contents."""
        path = PROJECT_ROOT / "Dockerfile.landing"
        assert path.is_file(), "Dockerfile.landing must exist"
        return path.read_text(encoding="utf-8")

    @pytest.fixture
    def landing_server(self) -> str:
        """Return the landing Go server source."""
        path = PROJECT_ROOT / "landing" / "server.go"
        assert path.is_file(), "landing/server.go must exist"
        return path.read_text(encoding="utf-8")

    def test_landing_dockerfile_pins_every_base_digest(self, landing_dockerfile: str) -> None:
        """Every FROM pins an exact sha256 digest (MADR-0031 C6, #423 lesson)."""
        froms = [line for line in landing_dockerfile.splitlines() if line.startswith("FROM ")]
        assert len(froms) == 2, "the landing image must have builder + runtime stages"
        for line in froms:
            assert "@sha256:" in line, f"unpinned base: {line}"

    def test_landing_dockerfile_runs_nonroot_without_env(self, landing_dockerfile: str) -> None:
        """The runtime user is nonroot (65532) and no env config is baked in."""
        assert "USER nonroot:nonroot" in landing_dockerfile
        assert "gcr.io/distroless/static-debian12:nonroot@sha256:" in landing_dockerfile
        env_lines = [line for line in landing_dockerfile.splitlines() if line.startswith("ENV ")]
        assert env_lines == [], "the landing image must bake zero env vars (MADR-0031 C5)"
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in landing_dockerfile

    def test_landing_dockerfile_asserts_the_canonical_cta_at_build_time(
        self, landing_dockerfile: str
    ) -> None:
        """The build stage rewrites the relative CTA and fails closed on drift."""
        assert "ARG CONSOLE_ORIGIN=https://coach-web-afff35tqxq-oa.a.run.app" in landing_dockerfile
        assert 'href="/auth/login"' in landing_dockerfile
        assert 'grep -F -q "href=\\"${CONSOLE_ORIGIN}/auth/login\\""' in landing_dockerfile
        assert "grep -F -q 'href=\"/auth/login\"'" in landing_dockerfile
        assert "sha256sum landing.html" in landing_dockerfile

    def test_landing_server_is_stdlib_only_and_pins_the_cta(self, landing_server: str) -> None:
        """The Go server pins the canonical CTA, headers and timeouts, and reads nothing."""
        assert f'const canonicalConsoleLoginURL = "{self.CANONICAL_CTA}"' in landing_server
        for header in (
            '"Cache-Control":           "public, max-age=300"',
            '"X-Content-Type-Options":  "nosniff"',
            '"Referrer-Policy":         "no-referrer"',
            "default-src 'none'; style-src 'self'; img-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        ):
            assert header in landing_server, header
        assert "//go:embed landing.html static/landing.css" in landing_server
        for marker in (":8080", "ReadHeaderTimeout", "ReadTimeout", "WriteTimeout", "IdleTimeout"):
            assert marker in landing_server, marker
        for forbidden in ("os.Getenv", "os.LookupEnv", "os.Open", "http.Redirect", "SetCookie"):
            assert forbidden not in landing_server, forbidden

    def test_landing_go_module_is_stdlib_only(self) -> None:
        """landing/go.mod declares the module with zero external dependencies."""
        text = (PROJECT_ROOT / "landing" / "go.mod").read_text(encoding="utf-8")
        assert "module coach_web_landing" in text
        assert "require" not in text

    # ------------------------------------------------------------------
    # Executable gates: the shipped workflow steps run against replayed
    # gcloud/curl read-backs (positive + drift classes).
    # ------------------------------------------------------------------

    PROJECT_IAM_BASELINE: dict[str, Any] = {
        "bindings": [
            {"members": [DEPLOY_SA_MEMBER], "role": PROJECT_IAM_VIEWER_ROLE},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/resourcemanager.viewer"},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/serviceusage.serviceUsageConsumer"},
            {"members": ["user:frederic.pitteloud@gmail.com"], "role": "roles/owner"},
        ],
        "etag": "BwZg3BJ7Uyk=",
        "version": 3,
    }

    LANDING_SA_POLICY_BASELINE: dict[str, Any] = {
        "bindings": [
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/iam.serviceAccountUser"},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/iam.serviceAccountViewer"},
        ],
        "etag": "BwZg3BF9cm4=",
        "version": 1,
    }

    LANDING_KEYS_BASELINE: list[Any] = []

    LANDING_POLICY_BASELINE: dict[str, Any] = {
        "bindings": [
            {"members": ["allUsers"], "role": "roles/run.invoker"},
            {"members": [DEPLOY_SA_MEMBER], "role": "roles/run.admin"},
        ],
        "etag": "CJiE1oEFEPiY2qPw-1UiGhcuUnRCkKxWzsjZj6hcLY=",
        "version": 3,
    }

    LANDING_METADATA_BASELINE: dict[str, Any] = {
        "metadata": {
            "annotations": {
                "run.googleapis.com/description": (
                    "Coach Web public static landing surface (MADR-0031, #427)"
                ),
                "run.googleapis.com/ingress": "all",
                "run.googleapis.com/ingress-status": "all",
                "run.googleapis.com/urls": '["https://coach-web-landing-853194930683.europe-west6.run.app"]',
                "serving.knative.dev/creator": LANDING_SA_NAME,
                "serving.knative.dev/lastModifier": LANDING_SA_NAME,
            }
        }
    }

    LANDING_TEMPLATE_BASELINE: dict[str, Any] = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "autoscaling.knative.dev/maxScale": "2",
                        "autoscaling.knative.dev/minScale": "0",
                    }
                },
                "spec": {
                    "serviceAccountName": LANDING_SA_NAME,
                    "timeoutSeconds": 10,
                    "containers": [
                        {
                            "name": "landing",
                            "image": LANDING_IMAGE,
                            "ports": [{"name": "http1", "containerPort": 8080}],
                            "resources": {"limits": {"cpu": "1000m", "memory": "128Mi"}},
                        }
                    ],
                },
            }
        }
    }

    CTA_HEADERS_BASELINE = (
        "HTTP/2 200\n"
        "content-security-policy: default-src 'none'; style-src 'self'; img-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n"
        "cache-control: public, max-age=300\n"
        "content-type: text/html; charset=utf-8\n"
        "referrer-policy: no-referrer\n"
        "x-content-type-options: nosniff\n"
    )

    CTA_BODY_BASELINE = (
        "<!doctype html>\n"
        '<a class="button" href="https://coach-web-afff35tqxq-oa.a.run.app'
        '/auth/login">Sign in</a>\n'
        "<footer>Swiss nLPD - europe-west6</footer>\n"
    )

    GCLOUD_LANDING_SHIM = '''#!/usr/bin/env python3
"""Test shim for gcloud: replays the #427 landing read-backs."""
import os
import sys

args = sys.argv[1:]
if args[:2] == ["auth", "print-access-token"]:
    sys.stdout.write("shim-access-token\\n")
    sys.exit(0)
if args[:2] == ["projects", "get-iam-policy"]:
    variable = "SHIM_PROJECT_IAM"
elif args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    variable = "SHIM_LANDING_SA_POLICY"
elif args[:4] == ["iam", "service-accounts", "keys", "list"]:
    variable = "SHIM_LANDING_KEYS"
elif args[:3] == ["run", "services", "describe"]:
    if "--format=json(metadata.annotations)" in args:
        variable = "SHIM_LANDING_METADATA"
    elif "--format=json(spec.template)" in args:
        variable = "SHIM_LANDING_TEMPLATE"
    else:
        variable = "SHIM_LANDING_URL"
elif args[:3] == ["run", "services", "get-iam-policy"]:
    variable = "SHIM_LANDING_POLICY"
else:
    sys.stderr.write("unexpected gcloud invocation: " + " ".join(args))
    sys.exit(2)
with open(os.environ[variable], encoding="utf-8") as handle:
    sys.stdout.write(handle.read())
'''

    CURL_LANDING_SHIM = '''#!/usr/bin/env python3
"""Test shim for curl: replays the #427 landing HTTP probes / TestIamPermissions."""
import os
import sys

args = sys.argv[1:]
url = args[-1]
if "testIamPermissions" in url:
    with open(os.environ["SHIM_TESTIAM"], encoding="utf-8") as handle:
        sys.stdout.write(handle.read())
    sys.exit(0)
root = url.endswith("/") and url.count("/") == 3
if root:
    code = os.environ.get("SHIM_CURL_ROOT_CODE", "200")
    body = os.environ["SHIM_CURL_BODY"]
    headers = os.environ["SHIM_CURL_HEADERS"]
else:
    code = os.environ.get("SHIM_CURL_OTHER_CODE", "404")
    body = os.devnull
    headers = None
out = args[args.index("-o") + 1]
with open(body, "rb") as src, open(out, "wb") as dst:
    dst.write(src.read())
if "-D" in args and headers is not None:
    hdr_out = args[args.index("-D") + 1]
    with open(headers, "rb") as src, open(hdr_out, "wb") as dst:
        dst.write(src.read())
sys.stdout.write(code + "\\n")
'''

    @pytest.fixture(scope="class")
    def workflow(self) -> dict[str, Any]:
        """The Cloud Run deploy workflow mapping."""
        return load_yaml(CLOUD_RUN_WORKFLOW)

    @pytest.fixture(scope="class")
    def steps(self, workflow: dict[str, Any]) -> list[dict[str, Any]]:
        """The deploy job steps."""
        return list(workflow["jobs"]["deploy"]["steps"])

    def _step(self, steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
        """Return the step with the exact name (fails loudly when renamed)."""
        match = next((step for step in steps if step.get("name") == name), None)
        assert match is not None, f"missing workflow step: {name}"
        return match

    def _gate_script(self, name: str) -> str:
        """The shipped ``run`` script of the named gate step."""
        workflow = load_yaml(CLOUD_RUN_WORKFLOW)
        steps = workflow["jobs"]["deploy"]["steps"]
        return str(self._step(steps, name)["run"])

    def _shim_env(self, tmp_path: Path, payloads: dict[str, Any]) -> dict[str, str]:
        """Materialize the gcloud/curl shims and payload files; return the env."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        for name, source in (
            ("gcloud", self.GCLOUD_LANDING_SHIM),
            ("curl", self.CURL_LANDING_SHIM),
        ):
            shim = bin_dir / name
            shim.write_text(source, encoding="utf-8")
            shim.chmod(0o755)
        env = dict(os.environ)
        for name, value in load_yaml(CLOUD_RUN_WORKFLOW)["env"].items():
            env.setdefault(name, str(value))
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        env["GITHUB_ENV"] = str(tmp_path / "github_env")
        for name, payload in payloads.items():
            path = tmp_path / f"{name}.payload"
            if isinstance(payload, str):
                path.write_text(payload, encoding="utf-8")
            else:
                path.write_text(json.dumps(payload), encoding="utf-8")
            env[name] = str(path)
        return env

    def _run_step(
        self,
        tmp_path: Path,
        step_name: str,
        payloads: dict[str, Any],
    ) -> subprocess.CompletedProcess[str]:
        """Execute the extracted gate step against the replayed read-backs."""
        env = self._shim_env(tmp_path, payloads)
        script = tmp_path / "gate.sh"
        script.write_text(self._gate_script(step_name), encoding="utf-8")
        return subprocess.run(
            ["/bin/bash", str(script)],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @pytest.mark.parametrize(
        "step_name",
        [IAM_GATE_STEP, C3_GATE_STEP, SPEC_GATE_STEP, PROBE_STEP, IAP_GATE_STEP],
    )
    def test_landing_gate_steps_are_valid_bash(
        self, steps: list[dict[str, Any]], tmp_path: Path, step_name: str
    ) -> None:
        """Every landing gate script parses under ``bash -n``."""
        script = self._step(steps, step_name)["run"]
        path = tmp_path / "step.sh"
        path.write_text(script, encoding="utf-8")
        result = subprocess.run(
            ["/bin/bash", "-n", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    # -- C1/C2: the landing SA IAM gate ---------------------------------

    def test_landing_iam_baseline_passes(self, tmp_path: Path) -> None:
        """The live 7078710086 post-state read-backs exit 0."""
        result = self._run_step(
            tmp_path,
            self.IAM_GATE_STEP,
            {
                "SHIM_PROJECT_IAM": self.PROJECT_IAM_BASELINE,
                "SHIM_LANDING_SA_POLICY": self.LANDING_SA_POLICY_BASELINE,
                "SHIM_LANDING_KEYS": self.LANDING_KEYS_BASELINE,
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "C1 verified" in result.stdout
        assert "C2 verified" in result.stdout

    LANDING_IAM_DRIFTS = {
        "landingHoldsProjectRole": lambda payloads: payloads["SHIM_PROJECT_IAM"]["bindings"].append(
            {
                "members": [TestLandingSurfaceContainerAndGates.LANDING_SA_MEMBER],
                "role": "roles/run.invoker",
            }
        ),
        "landingTokenMintingGrant": lambda payloads: payloads["SHIM_LANDING_SA_POLICY"][
            "bindings"
        ].append(
            {
                "members": [DEPLOY_SA_MEMBER],
                "role": "roles/iam.serviceAccountTokenCreator",
            }
        ),
        "landingForeignRole": lambda payloads: payloads["SHIM_LANDING_SA_POLICY"][
            "bindings"
        ].append({"members": ["user:attacker@example.com"], "role": "roles/editor"}),
        "landingMissingActAs": lambda payloads: payloads["SHIM_LANDING_SA_POLICY"].update(
            {
                "bindings": [
                    binding
                    for binding in payloads["SHIM_LANDING_SA_POLICY"]["bindings"]
                    if binding["role"] != "roles/iam.serviceAccountUser"
                ]
            }
        ),
        "landingUserManagedKey": lambda payloads: payloads.__setitem__(
            "SHIM_LANDING_KEYS",
            [{"name": "bootstrap-key", "keyOrigin": "USER_PROVIDED"}],
        ),
    }

    @pytest.mark.parametrize("drift", LANDING_IAM_DRIFTS, ids=list(LANDING_IAM_DRIFTS))
    def test_landing_iam_drifts_are_rejected(self, tmp_path: Path, drift: str) -> None:
        """Every landing-SA drift class makes the C1/C2 gate exit non-zero."""
        payloads = {
            "SHIM_PROJECT_IAM": copy.deepcopy(self.PROJECT_IAM_BASELINE),
            "SHIM_LANDING_SA_POLICY": copy.deepcopy(self.LANDING_SA_POLICY_BASELINE),
            "SHIM_LANDING_KEYS": copy.deepcopy(self.LANDING_KEYS_BASELINE),
        }
        self.LANDING_IAM_DRIFTS[drift](payloads)
        result = self._run_step(tmp_path, self.IAM_GATE_STEP, payloads)
        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout

    # -- AC3 (Amendments 0002 + 0005): no project-level run/iap admin ----

    def test_no_project_level_run_or_iap_admin_baseline_passes(self, tmp_path: Path) -> None:
        """The post-migration steady state (empty TestIamPermissions) exits 0."""
        result = self._run_step(
            tmp_path,
            self.IAP_GATE_STEP,
            {"SHIM_TESTIAM": "{}"},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "AC3 verified" in result.stdout

    @pytest.mark.parametrize(
        ("drift", "held"),
        [
            (
                "deployerRegainsProjectIapAdmin",
                ["iap.web.setIamPolicy", "iap.projects.updateSettings"],
            ),
            ("deployerRegainsProjectRunAdmin", ["run.services.delete", "run.services.update"]),
        ],
    )
    def test_project_level_admin_drifts_are_rejected(
        self, tmp_path: Path, drift: str, held: list[str]
    ) -> None:
        """A re-granted project-level run/iap admin surface fails the gate."""
        result = self._run_step(
            tmp_path,
            self.IAP_GATE_STEP,
            {"SHIM_TESTIAM": json.dumps({"permissions": held})},
        )
        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout

    # -- C3: the landing public posture gate ----------------------------

    def test_landing_posture_baseline_passes(self, tmp_path: Path) -> None:
        """invoker == exactly allUsers + deployer run.admin, no iap annotation."""
        result = self._run_step(
            tmp_path,
            self.C3_GATE_STEP,
            {
                "SHIM_LANDING_METADATA": self.LANDING_METADATA_BASELINE,
                "SHIM_LANDING_POLICY": self.LANDING_POLICY_BASELINE,
            },
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "C3 verified" in result.stdout

    LANDING_POSTURE_DRIFTS = {
        "landingIapEnabled": lambda payloads: payloads["SHIM_LANDING_METADATA"]["metadata"][
            "annotations"
        ].update({"run.googleapis.com/iap-enabled": "true"}),
        "landingExtraInvoker": lambda payloads: payloads["SHIM_LANDING_POLICY"]["bindings"][0][
            "members"
        ].append(DEPLOY_SA_MEMBER),
        "landingAllAuthenticated": lambda payloads: payloads["SHIM_LANDING_POLICY"]["bindings"][
            0
        ].update({"members": ["allAuthenticatedUsers"]}),
        "landingIapRoleBinding": lambda payloads: payloads["SHIM_LANDING_POLICY"][
            "bindings"
        ].append(
            {
                "members": ["user:frederic.pitteloud@gmail.com"],
                "role": "roles/iap.httpsResourceAccessor",
            }
        ),
        "landingMissingInvoker": lambda payloads: payloads["SHIM_LANDING_POLICY"].update(
            {
                "bindings": [
                    binding
                    for binding in payloads["SHIM_LANDING_POLICY"]["bindings"]
                    if binding["role"] != "roles/run.invoker"
                ]
            }
        ),
        "landingPublicRoleOnAdmin": lambda payloads: payloads["SHIM_LANDING_POLICY"][
            "bindings"
        ].append({"members": ["allUsers"], "role": "roles/run.admin"}),
    }

    @pytest.mark.parametrize("drift", LANDING_POSTURE_DRIFTS, ids=list(LANDING_POSTURE_DRIFTS))
    def test_landing_posture_drifts_are_rejected(self, tmp_path: Path, drift: str) -> None:
        """Every invoker/IAP drift class makes the C3 gate exit non-zero."""
        payloads = {
            "SHIM_LANDING_METADATA": copy.deepcopy(self.LANDING_METADATA_BASELINE),
            "SHIM_LANDING_POLICY": copy.deepcopy(self.LANDING_POLICY_BASELINE),
        }
        self.LANDING_POSTURE_DRIFTS[drift](payloads)
        result = self._run_step(tmp_path, self.C3_GATE_STEP, payloads)
        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout

    # -- C5/C6: the deployed landing spec gate ---------------------------

    def test_landing_spec_baseline_passes(self, tmp_path: Path) -> None:
        """The rendered spec (zero-role SA, digest-pinned image, no env) exits 0."""
        result = self._run_step(
            tmp_path,
            self.SPEC_GATE_STEP,
            {"SHIM_LANDING_TEMPLATE": self.LANDING_TEMPLATE_BASELINE},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "C5/C6 verified" in result.stdout

    LANDING_SPEC_DRIFTS = {
        "landingWrongSA": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"]["template"][
            "spec"
        ].update({"serviceAccountName": "853194930683-compute@developer.gserviceaccount.com"}),
        "landingEnvVar": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"]["template"][
            "spec"
        ]["containers"][0].update({"env": [{"name": "SOMETHING", "value": "leak"}]}),
        "landingTagPinnedImage": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"][
            "template"
        ]["spec"]["containers"][0].update({"image": "ghcr.io/fpittelo/coach-web-landing:latest"}),
        "landingResourceDrift": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"][
            "template"
        ]["spec"]["containers"][0].update(
            {"resources": {"limits": {"cpu": "2000m", "memory": "512Mi"}}}
        ),
        "landingTimeoutDrift": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"][
            "template"
        ]["spec"].update({"timeoutSeconds": 3600}),
        "landingScaleDrift": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"]["template"][
            "metadata"
        ]["annotations"].update({"autoscaling.knative.dev/maxScale": "5"}),
        "landingSecretRef": lambda payloads: payloads["SHIM_LANDING_TEMPLATE"]["spec"]["template"][
            "spec"
        ]["containers"][0].update(
            {
                "env": [
                    {
                        "name": "GOOGLE_APPLICATION_CREDENTIALS",
                        "valueFrom": {"secretKeyRef": {"name": "KEY", "key": "latest"}},
                    }
                ]
            }
        ),
    }

    @pytest.mark.parametrize("drift", LANDING_SPEC_DRIFTS, ids=list(LANDING_SPEC_DRIFTS))
    def test_landing_spec_drifts_are_rejected(self, tmp_path: Path, drift: str) -> None:
        """Every deployed-spec drift class makes the C5/C6 gate exit non-zero."""
        payloads = {"SHIM_LANDING_TEMPLATE": copy.deepcopy(self.LANDING_TEMPLATE_BASELINE)}
        self.LANDING_SPEC_DRIFTS[drift](payloads)
        result = self._run_step(tmp_path, self.SPEC_GATE_STEP, payloads)
        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout

    # -- C7: the unauthenticated content-provenance probe ----------------

    def _probe_payloads(self) -> dict[str, Any]:
        """The positive probe payloads (200 + pinned headers + canonical CTA)."""
        return {
            "SHIM_LANDING_URL": self.LANDING_URL,
            "SHIM_CURL_HEADERS": self.CTA_HEADERS_BASELINE,
            "SHIM_CURL_BODY": self.CTA_BODY_BASELINE,
        }

    def test_landing_probe_baseline_passes(self, tmp_path: Path) -> None:
        """200 + Cache-Control + canonical CTA + zero console path space exits 0."""
        result = self._run_step(tmp_path, self.PROBE_STEP, self._probe_payloads())
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Content provenance verified" in result.stdout

    @pytest.mark.parametrize(
        ("drift", "mutate"),
        [
            (
                "landingMissingCTA",
                lambda payloads: payloads.__setitem__(
                    "SHIM_CURL_BODY", '<a href="/auth/login">Sign in</a>'
                ),
            ),
            (
                "landingSetCookie",
                lambda payloads: payloads.__setitem__(
                    "SHIM_CURL_HEADERS",
                    TestLandingSurfaceContainerAndGates.CTA_HEADERS_BASELINE
                    + "set-cookie: cw_session=poisoned\n",
                ),
            ),
            (
                "landingGoogleSurface",
                lambda payloads: payloads.__setitem__(
                    "SHIM_CURL_BODY",
                    TestLandingSurfaceContainerAndGates.CTA_BODY_BASELINE
                    + "redirect: https://accounts.google.com/o/oauth2/auth\n",
                ),
            ),
            (
                "landingNoCacheHeader",
                lambda payloads: payloads.__setitem__(
                    "SHIM_CURL_HEADERS",
                    TestLandingSurfaceContainerAndGates.CTA_HEADERS_BASELINE.replace(
                        "cache-control: public, max-age=300\n", ""
                    ),
                ),
            ),
            (
                "landingConsolePath200",
                lambda payloads: payloads.__setitem__("SHIM_CURL_OTHER_CODE", "200"),
            ),
            (
                "landingRoot503",
                lambda payloads: payloads.__setitem__("SHIM_CURL_ROOT_CODE", "503"),
            ),
        ],
    )
    def test_landing_probe_drifts_are_rejected(
        self, tmp_path: Path, drift: str, mutate: Any
    ) -> None:
        """Every content-provenance drift class makes the C7 probe exit non-zero."""
        payloads = self._probe_payloads()
        mutate(payloads)
        result = self._run_step(tmp_path, self.PROBE_STEP, payloads)
        assert result.returncode != 0, f"{drift} was NOT rejected: {result.stdout}"
        assert "::error::" in result.stdout
