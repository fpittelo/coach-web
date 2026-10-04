"""Regression tests for the loopback publisher assertion (#138).

``scripts/e2e-preflight.sh`` validates host-port publishing from the
``docker compose ps -a --format json`` stream. Docker Compose v5.1.3 with
Docker Desktop ``ports.scheme=v2`` emits phantom publisher entries with
``PublishedPort == 0`` and an empty ``URL`` for exposed-but-unpublished
ports; those are NOT host publications and must never fail the gate.

The validation logic lives in ``scripts/preflight_loopback_check.py``
(extracted from the bash heredoc so it can be fixture-tested). These tests
pin its contract: stdin accepts jsonlines or a single JSON array, violations
are printed to stderr, exit 1 on any violation, 0 otherwise, 2 on usage
error. Nothing is loosened: genuine publications still hard-fail — sidecars
may publish no host port at all, and the web service may publish on
127.0.0.1 only (ADR-007 loopback boundary).
"""

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = PROJECT_ROOT / "scripts" / "preflight_loopback_check.py"
WEB_SERVICE = "coach-web"


def phantom_publisher(target_port: int) -> dict[str, Any]:
    """Return a compose v5.1.3 phantom publisher (exposed-but-unpublished)."""
    return {"URL": "", "TargetPort": target_port, "PublishedPort": 0, "Protocol": "tcp"}


def real_publisher(host_ip: str, published_port: int, target_port: int = 8000) -> dict[str, Any]:
    """Return a genuine host-port publisher entry."""
    return {
        "URL": host_ip,
        "TargetPort": target_port,
        "PublishedPort": published_port,
        "Protocol": "tcp",
    }


def compose_stream(
    coach_web: list[dict[str, Any]] | None = None,
    coach_mcp: list[dict[str, Any]] | None = None,
    github_mcp: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a compose ps stream for the three lane services (real names)."""
    return [
        {"Service": "coach-web", "Publishers": coach_web or []},
        {"Service": "coach-mcp", "Publishers": coach_mcp or []},
        {"Service": "github-mcp", "Publishers": github_mcp or []},
    ]


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """Load scripts/preflight_loopback_check.py as a module."""
    assert CHECKER_PATH.is_file(), "scripts/preflight_loopback_check.py must exist"
    spec = importlib.util.spec_from_file_location("preflight_loopback_check", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_checker(payload: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run the checker CLI exactly as e2e-preflight.sh does."""
    return subprocess.run(
        [sys.executable, str(CHECKER_PATH), *arguments],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
    )


class TestSidecarPublishers:
    """Sidecars must publish no host port; phantoms are not publications."""

    def test_phantom_only_publishers_pass(self, checker: ModuleType) -> None:
        """A sidecar carrying only PublishedPort-0 phantoms raises no violation (#138)."""
        entries = compose_stream(coach_mcp=[phantom_publisher(8000)])
        assert checker.collect_violations(entries, WEB_SERVICE) == []

    def test_real_published_port_fails(self, checker: ModuleType) -> None:
        """A genuine sidecar publication still hard-fails and names the service."""
        entries = compose_stream(coach_mcp=[real_publisher("0.0.0.0", 8000)])
        assert checker.collect_violations(entries, WEB_SERVICE) == [
            "coach-mcp publishes host ports but must publish none"
        ]

    def test_real_published_port_fails_via_cli(self) -> None:
        """The CLI exits 1 and names the offending sidecar on stderr."""
        payload = json.dumps(compose_stream(coach_mcp=[real_publisher("0.0.0.0", 8000)]))
        result = run_checker(payload, WEB_SERVICE)
        assert result.returncode == 1
        assert "coach-mcp publishes host ports but must publish none" in result.stderr

    def test_entry_without_publishers_key_passes(self, checker: ModuleType) -> None:
        """Entries without a Publishers key (older compose) raise no violation."""
        entries: list[dict[str, Any]] = [{"Service": "coach-mcp"}, {"Service": "coach-web"}]
        assert checker.collect_violations(entries, WEB_SERVICE) == []


class TestCoachWebPublishers:
    """coach-web may publish on 127.0.0.1 only; phantoms are ignored."""

    @pytest.mark.parametrize(
        "publisher",
        [
            real_publisher("127.0.0.1", 8100),
            {
                "URL": "",
                "PublishedIP": "127.0.0.1",
                "TargetPort": 8000,
                "PublishedPort": 8100,
                "Protocol": "tcp",
            },
        ],
        ids=["url-field", "published-ip-field"],
    )
    def test_loopback_publication_passes(
        self, checker: ModuleType, publisher: dict[str, Any]
    ) -> None:
        """A coach-web publication bound to 127.0.0.1 raises no violation."""
        entries = compose_stream(coach_web=[publisher])
        assert checker.collect_violations(entries, WEB_SERVICE) == []

    @pytest.mark.parametrize("host_ip", ["0.0.0.0", "::"])
    def test_non_loopback_publication_fails(self, checker: ModuleType, host_ip: str) -> None:
        """coach-web published on a non-loopback address hard-fails."""
        entries = compose_stream(coach_web=[real_publisher(host_ip, 8100)])
        assert checker.collect_violations(entries, WEB_SERVICE) == [
            f"coach-web published on {host_ip}:8100 (must be 127.0.0.1)"
        ]

    def test_phantom_only_publishers_pass(self, checker: ModuleType) -> None:
        """coach-web phantom-only publishers (exposed-only markers) raise no violation."""
        entries = compose_stream(coach_web=[phantom_publisher(8000)])
        assert checker.collect_violations(entries, WEB_SERVICE) == []


class TestIssue138Scenario:
    """The exact false-positive repro from #138 must pass on a clean host."""

    def clean_host_stream(self) -> list[dict[str, Any]]:
        """Return the dev-lane stream as compose v5.1.3 (ports.scheme=v2) emits it."""
        return compose_stream(
            coach_web=[real_publisher("127.0.0.1", 8100)],
            coach_mcp=[phantom_publisher(8000)],
            github_mcp=[phantom_publisher(8001)],
        )

    def test_clean_host_with_phantom_sidecars_passes(self, checker: ModuleType) -> None:
        """coach-web loopback + sidecar phantoms (v2 scheme) raise no violation."""
        assert checker.collect_violations(self.clean_host_stream(), WEB_SERVICE) == []

    def test_clean_host_scenario_passes_via_cli_jsonlines(self) -> None:
        """The CLI accepts the jsonlines stream and exits 0 on the clean scenario."""
        payload = "\n".join(json.dumps(entry) for entry in self.clean_host_stream())
        result = run_checker(payload, WEB_SERVICE)
        assert result.returncode == 0
        assert result.stderr == ""

    def test_real_sidecar_publication_still_fails_via_cli(self) -> None:
        """Nothing is loosened: a real sidecar publication fails the full scenario."""
        entries = compose_stream(
            coach_web=[real_publisher("127.0.0.1", 8100)],
            coach_mcp=[real_publisher("0.0.0.0", 8000)],
            github_mcp=[phantom_publisher(8001)],
        )
        payload = "\n".join(json.dumps(entry) for entry in entries)
        result = run_checker(payload, WEB_SERVICE)
        assert result.returncode == 1
        assert "coach-mcp publishes host ports but must publish none" in result.stderr


class TestInputStreamShapes:
    """Both compose ps output shapes parse: jsonlines and single JSON array."""

    def payload_for(self, entries: list[dict[str, Any]], shape: str) -> str:
        """Encode the stream in the requested shape."""
        if shape == "jsonlines":
            return "\n".join(json.dumps(entry) for entry in entries)
        return json.dumps(entries)

    @pytest.mark.parametrize("shape", ["jsonlines", "array"])
    def test_both_shapes_report_violations_identically(self, shape: str) -> None:
        """jsonlines and array encodings of the same stream give the same verdict."""
        entries = compose_stream(coach_mcp=[real_publisher("0.0.0.0", 8000)])
        result = run_checker(self.payload_for(entries, shape), WEB_SERVICE)
        assert result.returncode == 1
        assert "coach-mcp publishes host ports but must publish none" in result.stderr

    @pytest.mark.parametrize("shape", ["jsonlines", "array"])
    def test_both_shapes_accept_clean_stream(self, shape: str) -> None:
        """jsonlines and array encodings of a clean stream both exit 0."""
        entries = compose_stream(coach_web=[real_publisher("127.0.0.1", 8100)])
        result = run_checker(self.payload_for(entries, shape), WEB_SERVICE)
        assert result.returncode == 0
        assert result.stderr == ""


class TestParseEntries:
    """parse_entries accepts both compose ps stream shapes."""

    def test_parses_jsonlines(self, checker: ModuleType) -> None:
        """One JSON object per line parses into one entry per line."""
        raw = "\n".join(json.dumps(entry) for entry in compose_stream(coach_mcp=[]))
        entries = checker.parse_entries(raw)
        assert [entry["Service"] for entry in entries] == [
            "coach-web",
            "coach-mcp",
            "github-mcp",
        ]

    def test_parses_single_array(self, checker: ModuleType) -> None:
        """A single JSON array parses into one entry per element."""
        raw = json.dumps(compose_stream(coach_mcp=[]))
        entries = checker.parse_entries(raw)
        assert [entry["Service"] for entry in entries] == [
            "coach-web",
            "coach-mcp",
            "github-mcp",
        ]

    def test_parses_single_object(self, checker: ModuleType) -> None:
        """A single JSON object (one container) parses into a one-entry list."""
        raw = json.dumps({"Service": "coach-web", "Publishers": []})
        assert checker.parse_entries(raw) == [{"Service": "coach-web", "Publishers": []}]

    def test_empty_stream_yields_no_entries(self, checker: ModuleType) -> None:
        """An empty stream yields no entries."""
        assert checker.parse_entries("") == []
        assert checker.parse_entries("   \n") == []


class TestCliContract:
    """CLI behavior: stdin in, stderr violations out, exit codes 0/1/2."""

    def test_main_returns_one_on_violation(
        self, checker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() reads stdin and returns 1 when a violation exists."""
        payload = json.dumps(compose_stream(coach_mcp=[real_publisher("0.0.0.0", 8000)]))
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        assert checker.main([WEB_SERVICE]) == 1

    def test_main_returns_zero_when_clean(
        self, checker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() returns 0 when the stream violates nothing."""
        payload = json.dumps(compose_stream(coach_web=[real_publisher("127.0.0.1", 8100)]))
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        assert checker.main([WEB_SERVICE]) == 0

    def test_main_without_arguments_fails_closed(
        self, checker: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() without the web-service argument returns 2 (usage error)."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert checker.main([]) == 2
