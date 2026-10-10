"""Behavioral tests for harness/run.sh — the project gate entrypoint (#131).

Two defects motivated the entrypoint, both pinned here without a daemon:

- ``deps`` must run ``uv sync --all-extras``: a plain ``uv sync`` strips the
  ``dev`` optional-dependency group and deletes the gate toolchain from
  ``.venv`` (AC1).
- ``gate`` must resolve ruff/black/isort/mypy/pytest from the project
  ``.venv`` (venv-first ``PATH``) and fail loudly when the venv is absent or
  incomplete — never silently fall back to host ``~/.local/bin`` tools (AC2).

The script is run from a throwaway repo with recording ``uv`` / ``.venv/bin``
shims and a recording stand-in for the shared HOME runner, so the argument
contract, the ``PATH`` handed to the runner, and the fail-fast paths are all
covered end to end. A separate "host tool" shim log proves host-global tools
are never invoked.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

PYTHON_GATE_TOOLS = ("ruff", "black", "isort", "mypy", "pytest")

_RECORD_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
exit {code}
"""

_RUNNER_SHIM = """#!/usr/bin/env bash
{{
  printf 'ARGV:%s\\n' "$*"
  printf 'PATH:%s\\n' "$PATH"
}} >> "{log}"
exit 0
"""

_PYTHON_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
if [[ "${{1:-}}" == "-c" && "${{2:-}}" == "import pytest_cov" ]]; then
  if [[ "${{COV_IMPORT_FAIL:-}}" == "1" ]]; then
    exit 1
  fi
fi
exit 0
"""


class HarnessShim:
    """Sandbox holding a copy of harness/run.sh plus recording shims."""

    def __init__(self, tmp_path: Path) -> None:
        self.repo = tmp_path / "repo"
        self.script = self.repo / "harness" / "run.sh"
        self.host_bin = tmp_path / "host-bin"
        self.venv_bin = self.repo / ".venv" / "bin"
        self.runner = tmp_path / "runner" / "run.sh"
        self.uv_log = tmp_path / "uv.log"
        self.host_log = tmp_path / "host.log"
        self.runner_log = tmp_path / "runner.log"
        for directory in (self.script.parent, self.host_bin, self.venv_bin, self.runner.parent):
            directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / "harness" / "run.sh", self.script)
        self.script.chmod(self.script.stat().st_mode | stat.S_IEXEC)
        (self.repo / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        self._write_shim(self.host_bin / "uv", self.uv_log)
        for tool in PYTHON_GATE_TOOLS:
            # Host-global stand-ins: if the script ever reached these the log is non-empty.
            self._write_shim(self.host_bin / tool, self.host_log)
        self.install_venv_tools()
        self.runner.write_text(_RUNNER_SHIM.format(log=self.runner_log), encoding="utf-8")
        self.runner.chmod(0o755)
        self.uv_log.touch()
        self.host_log.touch()
        self.runner_log.touch()

    @staticmethod
    def _write_shim(path: Path, log: Path, code: int = 0) -> None:
        path.write_text(_RECORD_SHIM.format(log=log, code=code), encoding="utf-8")
        path.chmod(0o755)

    def install_venv_tools(self) -> None:
        """(Re)create the project .venv/bin gate toolchain shims."""
        self.venv_bin.mkdir(parents=True, exist_ok=True)
        for tool in PYTHON_GATE_TOOLS:
            self._write_shim(self.venv_bin / tool, self.venv_bin / "tools.log")
        python = self.venv_bin / "python"
        python.write_text(_PYTHON_SHIM.format(log=self.venv_bin / "python.log"), encoding="utf-8")
        python.chmod(0o755)

    def remove_venv(self) -> None:
        shutil.rmtree(self.repo / ".venv")

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.host_bin}{os.pathsep}{env['PATH']}"
        env["HARNESS_RUNNER"] = str(self.runner)
        if extra:
            env.update(extra)
        return env

    def run(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run the sandboxed harness entrypoint with repo_root pinned as arg 3."""
        return subprocess.run(
            [str(self.script), *args, str(self.repo)],
            cwd=self.repo,
            env=self.env(extra_env),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def uv_invocations(self) -> list[str]:
        return self._lines(self.uv_log)

    def host_invocations(self) -> list[str]:
        return self._lines(self.host_log)

    def runner_invocations(self) -> list[str]:
        return self._lines(self.runner_log)

    @staticmethod
    def _lines(path: Path) -> list[str]:
        return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def shim(tmp_path: Path) -> HarnessShim:
    return HarnessShim(tmp_path)


class TestDepsSync:
    """deps keeps the dev optional-dependency group in the project .venv (AC1)."""

    def test_deps_runs_uv_sync_all_extras(self, shim: HarnessShim) -> None:
        result = shim.run("python", "deps")
        assert result.returncode == 0, result.stderr
        assert shim.uv_invocations() == ["sync --all-extras"]
        assert shim.runner_invocations() == []
        assert shim.host_invocations() == []

    def test_all_syncs_extras_before_delegating_gate(self, shim: HarnessShim) -> None:
        result = shim.run("python", "all")
        assert result.returncode == 0, result.stderr
        assert shim.uv_invocations() == ["sync --all-extras"]
        assert shim.runner_invocations()[0] == f"ARGV:python gate {shim.repo}"


class TestGateResolution:
    """gate runs against the project .venv only — never host PATH tools (AC2)."""

    def test_gate_delegates_with_venv_first_path(self, shim: HarnessShim) -> None:
        result = shim.run("python", "gate")
        assert result.returncode == 0, result.stderr
        invocations = shim.runner_invocations()
        assert invocations[0] == f"ARGV:python gate {shim.repo}"
        path = invocations[1].removeprefix("PATH:").split(os.pathsep)
        assert path[0] == str(shim.venv_bin), "the project .venv must lead PATH"
        assert shim.host_invocations() == [], "host-global gate tools must never run"

    def test_gate_does_not_sync_deps(self, shim: HarnessShim) -> None:
        result = shim.run("python", "gate")
        assert result.returncode == 0, result.stderr
        assert shim.uv_invocations() == [], "gate must not silently re-sync the venv"

    def test_gate_fails_fast_without_venv(self, shim: HarnessShim) -> None:
        shim.remove_venv()
        result = shim.run("python", "gate")
        assert result.returncode == 3
        assert "project .venv not found" in result.stderr
        assert "refusing to use host PATH tools" in result.stderr
        assert shim.runner_invocations() == []
        assert shim.host_invocations() == []

    def test_gate_fails_fast_when_tool_missing(self, shim: HarnessShim) -> None:
        (shim.venv_bin / "mypy").unlink()
        result = shim.run("python", "gate")
        assert result.returncode == 3
        assert "missing the gate toolchain" in result.stderr
        assert str(shim.venv_bin / "mypy") in result.stderr
        assert shim.runner_invocations() == []
        assert shim.host_invocations() == []

    def test_gate_fails_fast_when_pytest_cov_unimportable(self, shim: HarnessShim) -> None:
        result = shim.run("python", "gate", extra_env={"COV_IMPORT_FAIL": "1"})
        assert result.returncode == 3
        assert "pytest-cov is not importable" in result.stderr
        assert shim.runner_invocations() == []
        assert shim.host_invocations() == []


class TestContract:
    """Argument and runner-availability failures are loud (exit 2/4)."""

    def test_unknown_phase_is_rejected(self, shim: HarnessShim) -> None:
        result = shim.run("python", "redeploy")
        assert result.returncode == 2
        assert "unknown phase" in result.stderr
        assert shim.uv_invocations() == []
        assert shim.runner_invocations() == []

    def test_missing_runner_fails_loud(self, shim: HarnessShim) -> None:
        result = shim.run("python", "gate", extra_env={"HARNESS_RUNNER": "/nonexistent/run.sh"})
        assert result.returncode == 4
        assert "HOME gate runner not found" in result.stderr

    def test_rust_defers_to_shared_runner(self, shim: HarnessShim) -> None:
        result = shim.run("rust", "deps")
        assert result.returncode == 0, result.stderr
        assert shim.runner_invocations()[0] == f"ARGV:rust deps {shim.repo}"
        assert shim.uv_invocations() == []
