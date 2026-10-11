"""Integration-level validation of the build-time version derivation (#198).

These tests inspect the Dockerfile, the deploy workflow and the packaging
metadata without requiring a Docker daemon, pinning the wiring that makes
every published image report a git-traceable version: release builds report
the pushed tag, dev/qa lane builds report ``<lane>-<short-sha>`` — and no
surface hardcodes a release-looking version that can go stale (the #198 bug).
"""

import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DOCKERFILE = "Dockerfile"
DEPLOY_WORKFLOW = ".github/workflows/deploy.yaml"
PYPROJECT = "pyproject.toml"
PACKAGE_INIT = "src/coach_web/__init__.py"


def _read(name: str) -> str:
    """Return a project-root file's contents (fails fast when absent)."""
    path = PROJECT_ROOT / name
    assert path.is_file(), f"{name} must exist"
    return path.read_text(encoding="utf-8")


def _load_workflow(name: str) -> dict[str, Any]:
    """Load a parsed GitHub Actions workflow from the project root."""
    data = yaml.safe_load(_read(name))
    assert isinstance(data, dict), f"{name} must contain a top-level mapping"
    return data


class TestDockerfileVersionOverride:
    """The runtime image carries the build-time version override (#198)."""

    @pytest.fixture
    def dockerfile(self) -> str:
        """Return the Dockerfile contents."""
        return _read(DOCKERFILE)

    def test_arg_declared_with_empty_default(self, dockerfile: str) -> None:
        """COACH_WEB_VERSION is a declared build-arg defaulting to empty."""
        assert "ARG COACH_WEB_VERSION=" in dockerfile

    def test_env_in_runtime_stage_passes_the_override_through(self, dockerfile: str) -> None:
        """The override becomes a runtime env var so resolve_version() reads it."""
        runtime = dockerfile[dockerfile.index("AS runtime") :]

        assert "COACH_WEB_VERSION=${COACH_WEB_VERSION}" in runtime


class TestDeployWorkflowVersionDerivation:
    """deploy.yaml derives the version from the git ref and passes it (#198)."""

    @pytest.fixture(scope="class")
    @classmethod
    def deploy(cls) -> dict[str, Any]:
        """Load the parsed deploy workflow."""
        return _load_workflow(DEPLOY_WORKFLOW)

    def _env_step(self, deploy: dict[str, Any]) -> dict[str, Any]:
        """Return the 'Determine environment tag' step of the build job."""
        steps = deploy["jobs"]["build-and-push"]["steps"]
        return next(step for step in steps if step.get("id") == "env")

    def _build_step(self, deploy: dict[str, Any]) -> dict[str, Any]:
        """Return the docker/build-push-action step of the build job."""
        steps = deploy["jobs"]["build-and-push"]["steps"]
        return next(
            step
            for step in steps
            if str(step.get("uses", "")).startswith("docker/build-push-action@")
        )

    def test_release_builds_report_the_git_tag(self, deploy: dict[str, Any]) -> None:
        """AC1 (#198): a v* tag push strips the leading v (v0.9.4 -> 0.9.4)."""
        run = self._env_step(deploy)["run"]

        assert "${GITHUB_REF_NAME#v}" in run

    def test_lane_builds_report_a_traceable_non_release_version(
        self, deploy: dict[str, Any]
    ) -> None:
        """Non-tag builds report <lane>-<short-sha> — never a misleading 0.1.0."""
        run = self._env_step(deploy)["run"]

        assert "${ENV_TAG}-${GITHUB_SHA::7}" in run

    def test_derived_version_is_exported_as_step_output(self, deploy: dict[str, Any]) -> None:
        """The derivation lands in the env step's ``version`` output."""
        run = self._env_step(deploy)["run"]

        assert 'echo "version=${VERSION}" >> "$GITHUB_OUTPUT"' in run

    def test_build_passes_the_version_build_arg(self, deploy: dict[str, Any]) -> None:
        """The image build receives COACH_WEB_VERSION from the env step."""
        build_args = self._build_step(deploy)["with"]["build-args"]

        assert "COACH_WEB_VERSION=${{ steps.env.outputs.version }}" in build_args


class TestPackagingVersionSentinel:
    """The static package version stays the honest sentinel (#198)."""

    def test_pyproject_version_is_the_honest_sentinel(self) -> None:
        """pyproject pins 0.0.0 — release versions come from the git tag only."""
        data = tomllib.loads(_read(PYPROJECT))

        assert data["project"]["version"] == "0.0.0"

    def test_package_init_declares_no_hardcoded_version(self) -> None:
        """coach_web.__init__ carries no __version__ (single source of truth)."""
        assert "__version__" not in _read(PACKAGE_INIT)
