"""Regression tests for the lane env-file permission assertion (MADR-0008 C3, #170).

``scripts/e2e-preflight.sh`` asserts that every existing lane env file is mode
600 (owner read/write only). A group- or world-readable lane env file exposes
the lane's secrets (``OPENROUTER_API_KEY``, ``GITHUB_TOKEN``,
``INTERVALS_API_KEY``, ``AUTH_SESSION_SECRET``, ...) to every local user — the
#112 posture claims 600, and MADR-0008 C3 requires the enforcement.

The validation logic lives in ``scripts/preflight_env_permissions_check.py``
(extracted from the bash script so it can be fixture-tested, mirroring
``preflight_loopback_check.py``). These tests pin its contract: existing paths
are checked, non-existent paths are skipped, violations are printed to stderr,
exit 1 on any violation, 0 otherwise, 2 on usage error.
"""

import importlib.util
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECKER_PATH = PROJECT_ROOT / "scripts" / "preflight_env_permissions_check.py"


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """Load scripts/preflight_env_permissions_check.py as a module."""
    assert CHECKER_PATH.is_file(), "scripts/preflight_env_permissions_check.py must exist"
    spec = importlib.util.spec_from_file_location("preflight_env_permissions_check", CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_env_file(tmp_path: Path, mode: int, name: str = ".env.dev") -> Path:
    """Create a fixture env file with the given permission bits."""
    path = tmp_path / name
    path.write_text("OPENROUTER_API_KEY=placeholder\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def run_checker(*paths: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker CLI exactly as e2e-preflight.sh does."""
    return subprocess.run(
        [sys.executable, str(CHECKER_PATH), *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        check=False,
        cwd=PROJECT_ROOT,
    )


class TestCollectViolations:
    """collect_violations flags every existing env file not at mode 600."""

    def test_mode_600_passes(self, checker: ModuleType, tmp_path: Path) -> None:
        """A 600 env file raises no violation (the required posture)."""
        assert checker.collect_violations([make_env_file(tmp_path, 0o600)]) == []

    def test_mode_644_fails(self, checker: ModuleType, tmp_path: Path) -> None:
        """A world-readable 644 env file is a violation naming the file and mode."""
        path = make_env_file(tmp_path, 0o644)
        violations = checker.collect_violations([path])
        assert len(violations) == 1
        assert str(path) in violations[0]
        assert "644" in violations[0]
        assert "600" in violations[0]

    @pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o666, 0o777, 0o400])
    def test_any_non_600_mode_fails(self, checker: ModuleType, tmp_path: Path, mode: int) -> None:
        """Every mode other than 600 is a violation (no loosening)."""
        violations = checker.collect_violations([make_env_file(tmp_path, mode)])
        assert len(violations) == 1
        assert f"{mode:03o}" in violations[0]

    def test_missing_path_is_skipped(self, checker: ModuleType, tmp_path: Path) -> None:
        """A non-existent path is skipped (the script owns the missing-file policy)."""
        assert checker.collect_violations([tmp_path / ".env.qa"]) == []

    def test_only_offending_paths_are_reported(self, checker: ModuleType, tmp_path: Path) -> None:
        """With several paths, only the non-600 ones are reported."""
        good = make_env_file(tmp_path, 0o600, name=".env.dev")
        bad = make_env_file(tmp_path, 0o644, name=".env.qa")
        violations = checker.collect_violations([good, bad])
        assert len(violations) == 1
        assert str(bad) in violations[0]


class TestCliContract:
    """CLI behavior: paths in, stderr violations out, exit codes 0/1/2."""

    def test_cli_passes_on_600(self, tmp_path: Path) -> None:
        """The CLI exits 0 with no output when every env file is 600."""
        result = run_checker(make_env_file(tmp_path, 0o600))
        assert result.returncode == 0
        assert result.stderr == ""

    def test_cli_fails_on_644(self, tmp_path: Path) -> None:
        """The CLI exits 1 and names the offending file on stderr."""
        path = make_env_file(tmp_path, 0o644)
        result = run_checker(path)
        assert result.returncode == 1
        assert str(path) in result.stderr
        assert "600" in result.stderr

    def test_cli_without_arguments_fails_closed(self) -> None:
        """The CLI without arguments returns 2 (usage error)."""
        result = run_checker()
        assert result.returncode == 2
        assert "usage" in result.stderr.lower()

    def test_cli_reports_every_offending_file(self, tmp_path: Path) -> None:
        """The CLI reports each offending file, not just the first."""
        first = make_env_file(tmp_path, 0o644, name=".env.dev")
        second = make_env_file(tmp_path, 0o640, name=".env.qa")
        result = run_checker(first, second)
        assert result.returncode == 1
        assert str(first) in result.stderr
        assert str(second) in result.stderr


class TestMainInProcess:
    """main() is exercised in-process so the module's own coverage is complete."""

    def test_main_returns_zero_when_clean(self, checker: ModuleType, tmp_path: Path) -> None:
        """main() returns 0 when every env file is mode 600."""
        assert checker.main([str(make_env_file(tmp_path, 0o600))]) == 0

    def test_main_returns_one_on_violation(self, checker: ModuleType, tmp_path: Path) -> None:
        """main() returns 1 when an env file is not mode 600."""
        assert checker.main([str(make_env_file(tmp_path, 0o644))]) == 1

    def test_main_without_arguments_fails_closed(self, checker: ModuleType) -> None:
        """main() without arguments returns 2 (usage error)."""
        assert checker.main([]) == 2


class TestModuleEntrypoint:
    """The __main__ guard is exercised so the module reaches full coverage."""

    def test_module_entrypoint_exits_with_usage_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running the module as __main__ with no args exits 2 (usage error)."""
        monkeypatch.setattr(sys, "argv", [str(CHECKER_PATH)])
        with pytest.raises(SystemExit) as excinfo:
            runpy.run_path(str(CHECKER_PATH), run_name="__main__")
        assert excinfo.value.code == 2
