"""Behavioral tests for scripts/lane.sh — the agent lane-lifecycle entrypoint (#134).

Two layers, both daemon-free:

- ``TestArgContract`` runs the real script and pins the argument contract
  (prod rejection with the manual-procedure pointer, unknown lane/action,
  missing and extra arguments). A PATH-shim ``docker`` stands in for the real
  CLI so a buggy script can never reach the host daemon from these tests; the
  shim log staying empty additionally proves rejection happens before any
  docker invocation.
- ``TestLaneLifecycle`` drives the script against the shim, which records
  argv and emits canned ``compose ps --format json`` output. This pins the
  per-lane ``-p``/``-f``/``--env-file`` flags, the health wait loop, the
  ``--tail=100`` logs default and the project-scoped ``down --remove-orphans``
  teardown without requiring a running daemon.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SERVICES = ("coach-web", "coach-mcp", "github-mcp")
LANE_PORTS = {"dev": 8100, "qa": 8200}


def _ps_entries(health_by_service: dict[str, str]) -> list[dict[str, str]]:
    """Build a compose ps --format json payload with the given health map."""
    return [
        {"Service": service, "Health": health_by_service.get(service, "healthy")}
        for service in sorted(SERVICES)
    ]


HEALTHY_PS = _ps_entries({})
UNHEALTHY_PS = _ps_entries({"coach-mcp": "starting"})
INCOMPLETE_PS = [entry for entry in HEALTHY_PS if entry["Service"] != "github-mcp"]

SHIM_DOCKER_SOURCE = '''#!/usr/bin/env python3
"""Test shim for the docker CLI: records argv, emits canned compose output."""
import json
import os
import sys

with open(os.environ["LANE_SHIM_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")

args = sys.argv[1:]
if "ps" in args and "--format" in args:
    sequence_path = os.environ.get("LANE_SHIM_PS_SEQUENCE", "")
    if sequence_path:
        with open(sequence_path, encoding="utf-8") as handle:
            payloads = json.load(handle)
        count_path = sequence_path + ".count"
        index = 0
        if os.path.exists(count_path):
            with open(count_path, encoding="utf-8") as handle:
                index = int(handle.read().strip() or "0")
        print(json.dumps(payloads[min(index, len(payloads) - 1)]))
        with open(count_path, "w", encoding="utf-8") as handle:
            handle.write(str(index + 1))
    else:
        print(os.environ["LANE_SHIM_PS_DEFAULT"])
sys.exit(0)
'''


class LaneShim:
    """Sandbox holding a copy of lane.sh plus a recording ``docker`` shim."""

    def __init__(self, tmp_path: Path) -> None:
        self.bin_dir = tmp_path / "bin"
        self.repo = tmp_path / "repo"
        self.script = self.repo / "scripts" / "lane.sh"
        self.log = tmp_path / "shim.log"
        self.bin_dir.mkdir()
        self.repo.mkdir()
        (self.repo / "scripts").mkdir()
        shutil.copy2(PROJECT_ROOT / "scripts" / "lane.sh", self.script)
        self.script.chmod(0o755)
        # Stand-ins for the compose files the script resolves relative to its
        # own location; the shim never parses them.
        for name in ("compose.yaml", "compose.dev.yml", "compose.qa.yml"):
            (self.repo / name).touch()
        self.log.touch()
        shim = self.bin_dir / "docker"
        shim.write_text(SHIM_DOCKER_SOURCE, encoding="utf-8")
        shim.chmod(0o755)

    def write_env_file(self, lane: str) -> None:
        """Create the lane env file inside the sandbox (never the real one)."""
        payload = f"COACH_MCP_IMAGE=ghcr.io/fpittelo/coach:{lane}\n"
        (self.repo / f".env.{lane}").write_text(payload, encoding="utf-8")

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return the process environment with the shim first on PATH."""
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["LANE_SHIM_LOG"] = str(self.log)
        env["LANE_SHIM_PS_DEFAULT"] = json.dumps(HEALTHY_PS)
        if extra:
            env.update(extra)
        return env

    def invocations(self) -> list[list[str]]:
        """Return the recorded argv of every docker invocation, in order."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def shim(tmp_path: Path) -> LaneShim:
    """A sandboxed lane.sh copy with the recording docker shim first on PATH."""
    return LaneShim(tmp_path)


def run_lane(
    shim: LaneShim,
    *args: str,
    lane: str = "dev",
    with_env_file: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the sandboxed lane.sh copy with the shim first on PATH."""
    if with_env_file:
        shim.write_env_file(lane)
    return subprocess.run(
        [str(shim.script), *args],
        cwd=shim.repo,
        env=shim.env(extra_env),
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestArgContract:
    """Argument validation happens up front, before any docker invocation."""

    @pytest.mark.parametrize("action", ["up", "status", "logs", "down"])
    def test_prod_lane_is_rejected_with_manual_procedure_pointer(
        self, shim: LaneShim, action: str
    ) -> None:
        """The prod lane is manual-only; lane.sh refuses it with exit code 2 (AC1)."""
        result = run_lane(shim, "prod", action)
        assert result.returncode == 2
        assert "manual" in result.stderr.lower()
        assert "compose.prod.yml" in result.stderr
        assert shim.invocations() == [], "prod must be rejected before any docker call"

    def test_unknown_lane_is_rejected_with_usage(self, shim: LaneShim) -> None:
        """An unknown lane fails with exit code 2 and usage text on stderr."""
        result = run_lane(shim, "staging", "up")
        assert result.returncode == 2
        assert "unknown lane" in result.stderr
        assert "Usage:" in result.stderr
        assert shim.invocations() == []

    def test_unknown_action_is_rejected_with_usage(self, shim: LaneShim) -> None:
        """An unknown action fails with exit code 2 and usage text on stderr."""
        result = run_lane(shim, "dev", "redeploy")
        assert result.returncode == 2
        assert "unknown action" in result.stderr
        assert "Usage:" in result.stderr
        assert shim.invocations() == []

    def test_missing_arguments_are_rejected_with_usage(self, shim: LaneShim) -> None:
        """Missing lane and/or action arguments fail with exit code 2."""
        cases: list[list[str]] = [[], ["dev"]]
        for args in cases:
            result = run_lane(shim, *args)
            assert result.returncode == 2, args
            assert "Usage:" in result.stderr
        assert shim.invocations() == []

    @pytest.mark.parametrize("action", ["up", "status", "down"])
    def test_non_log_actions_reject_extra_arguments(self, shim: LaneShim, action: str) -> None:
        """Only logs accepts an optional service argument; the rest take none."""
        result = run_lane(shim, "dev", action, "coach-web")
        assert result.returncode == 2
        assert "takes no extra arguments" in result.stderr
        assert shim.invocations() == []

    def test_logs_rejects_more_than_one_service(self, shim: LaneShim) -> None:
        """logs accepts at most one optional service argument."""
        result = run_lane(shim, "dev", "logs", "coach-web", "coach-mcp")
        assert result.returncode == 2
        assert "at most one service" in result.stderr
        assert shim.invocations() == []


class TestLaneLifecycle:
    """Actions delegate to compose with strictly lane-scoped flags (shimmed docker)."""

    def test_up_invokes_compose_with_lane_scoped_flags(self, shim: LaneShim) -> None:
        """dev up builds/starts the coach-web-dev project and reports its URL (AC2)."""
        result = run_lane(shim, "dev", "up")
        assert result.returncode == 0, result.stderr
        ups = [inv for inv in shim.invocations() if "up" in inv]
        assert len(ups) == 1
        assert ups[0][-3:] == ["up", "-d", "--build"]
        assert ups[0][:-3] == [
            "compose",
            "-p",
            "coach-web-dev",
            "-f",
            str(shim.repo / "compose.yaml"),
            "-f",
            str(shim.repo / "compose.dev.yml"),
            "--env-file",
            str(shim.repo / ".env.dev"),
        ]
        assert f"http://127.0.0.1:{LANE_PORTS['dev']}" in result.stdout

    def test_up_qa_uses_qa_project_files_and_url(self, shim: LaneShim) -> None:
        """qa up targets the coach-web-qa project with the qa files and URL (AC2)."""
        result = run_lane(shim, "qa", "up", lane="qa")
        assert result.returncode == 0, result.stderr
        ups = [inv for inv in shim.invocations() if "up" in inv]
        assert len(ups) == 1
        assert "coach-web-qa" in ups[0]
        assert str(shim.repo / "compose.qa.yml") in ups[0]
        assert str(shim.repo / ".env.qa") in ups[0]
        assert f"http://127.0.0.1:{LANE_PORTS['qa']}" in result.stdout

    def test_up_waits_until_all_services_are_healthy(self, shim: LaneShim, tmp_path: Path) -> None:
        """up polls compose ps until the whole service set is healthy (AC2)."""
        sequence = tmp_path / "ps_sequence.json"
        sequence.write_text(json.dumps([UNHEALTHY_PS, HEALTHY_PS]), encoding="utf-8")
        result = run_lane(shim, "dev", "up", extra_env={"LANE_SHIM_PS_SEQUENCE": str(sequence)})
        assert result.returncode == 0, result.stderr
        polls = [inv for inv in shim.invocations() if "ps" in inv and "--format" in inv]
        assert len(polls) >= 2, "the health wait loop must poll until healthy"
        assert f"http://127.0.0.1:{LANE_PORTS['dev']}" in result.stdout
        for service in SERVICES:
            assert service in result.stdout, "service states must be reported"

    def test_up_fails_and_lists_unhealthy_services(self, shim: LaneShim, tmp_path: Path) -> None:
        """A service that never turns healthy fails up with the offender listed."""
        sequence = tmp_path / "ps_sequence.json"
        sequence.write_text(json.dumps([UNHEALTHY_PS]), encoding="utf-8")
        result = run_lane(
            shim,
            "dev",
            "up",
            extra_env={"LANE_SHIM_PS_SEQUENCE": str(sequence), "LANE_WAIT_SECONDS": "2"},
        )
        assert result.returncode != 0
        assert "not healthy" in result.stderr
        assert "coach-mcp" in result.stderr

    def test_up_fails_on_incomplete_service_set(self, shim: LaneShim, tmp_path: Path) -> None:
        """A crashed/absent sidecar fails the gate instead of being silently absent."""
        sequence = tmp_path / "ps_sequence.json"
        sequence.write_text(json.dumps([INCOMPLETE_PS]), encoding="utf-8")
        result = run_lane(
            shim,
            "dev",
            "up",
            extra_env={"LANE_SHIM_PS_SEQUENCE": str(sequence), "LANE_WAIT_SECONDS": "2"},
        )
        assert result.returncode != 0
        assert "service set mismatch" in result.stderr
        assert "github-mcp" in result.stderr

    def test_up_fails_fast_when_lane_env_file_is_missing(self, shim: LaneShim) -> None:
        """A missing lane env file fails before any docker invocation."""
        result = run_lane(shim, "dev", "up", with_env_file=False)
        assert result.returncode != 0
        assert "Missing" in result.stderr
        assert ".env.dev.example" in result.stderr
        assert shim.invocations() == [], "the fail-fast must precede any docker invocation"

    def test_status_scopes_to_lane_project(self, shim: LaneShim) -> None:
        """status runs ps -a for the lane's compose project only (AC2)."""
        result = run_lane(shim, "dev", "status")
        assert result.returncode == 0, result.stderr
        invocations = shim.invocations()
        assert len(invocations) == 1
        assert invocations[0] == [
            "compose",
            "-p",
            "coach-web-dev",
            "-f",
            str(shim.repo / "compose.yaml"),
            "-f",
            str(shim.repo / "compose.dev.yml"),
            "--env-file",
            str(shim.repo / ".env.dev"),
            "ps",
            "-a",
        ]

    def test_logs_passes_tail_limit_and_optional_service(self, shim: LaneShim) -> None:
        """logs tails 100 lines for the lane project, optionally one service (AC2)."""
        result = run_lane(shim, "dev", "logs", "coach-web")
        assert result.returncode == 0, result.stderr
        assert shim.invocations()[-1][-3:] == ["logs", "--tail=100", "coach-web"]

        result = run_lane(shim, "qa", "logs", lane="qa")
        assert result.returncode == 0, result.stderr
        assert shim.invocations()[-1][-2:] == ["logs", "--tail=100"]

    def test_down_is_scoped_to_the_lane_project(self, shim: LaneShim) -> None:
        """down tears down only the lane's own compose project (AC2 blast radius)."""
        result = run_lane(shim, "qa", "down", lane="qa")
        assert result.returncode == 0, result.stderr
        invocations = shim.invocations()
        assert len(invocations) == 1
        assert invocations[0][-2:] == ["down", "--remove-orphans"]
        assert "coach-web-qa" in invocations[0]
