#!/usr/bin/env bash
# coach-web local quality-gate harness entrypoint (issue #131).
#
# The HOME-wide gate implementation lives in opencode-home-config/harness/run.sh
# (SSOT). This project entrypoint pins the run to the *project* toolchain so the
# native fast path can never drift to host-global tools:
#
#   deps : `uv sync --all-extras` - a plain `uv sync` strips the `dev` optional
#          dependency group, removing the gate toolchain from .venv (#131 AC1).
#   gate : `.venv/bin` is prepended to PATH before the shared native gate runs,
#          so ruff/black/isort/mypy/pytest resolve from the lock-pinned project
#          .venv, never from `~/.local/bin`. An absent or incomplete .venv fails
#          fast (exit 3) instead of silently falling back to host site-packages
#          (#131 AC2). The shared container fallback is untouched.
#
# Usage: run.sh <python|rust> [deps|gate|all] [repo_root]
set -euo pipefail

STACK="${1:?usage: run.sh <python|rust> [deps|gate|all] [repo_root]}"
PHASE="${2:-all}"
REPO_ROOT="${3:-$(git rev-parse --show-toplevel)}"
HARNESS_RUNNER="${HARNESS_RUNNER:-${HOME}/projects/opencode-home-config/harness/run.sh}"
VENV_BIN="${REPO_ROOT}/.venv/bin"

sync_python_deps() {
  echo "==> [deps] native path: uv sync --all-extras (retain the dev gate toolchain)"
  (cd "${REPO_ROOT}" && uv sync --all-extras)
}

# Refuse to run the native gate against anything but the project .venv (#131 AC2).
require_python_venv() {
  local tool
  if [[ ! -d "${VENV_BIN}" ]]; then
    echo "error: project .venv not found at ${VENV_BIN}" >&2
    echo "       run 'bash harness/run.sh python deps' first; refusing to use host PATH tools." >&2
    exit 3
  fi
  for tool in ruff black isort mypy pytest; do
    if [[ ! -x "${VENV_BIN}/${tool}" ]]; then
      echo "error: project .venv is missing the gate toolchain: ${VENV_BIN}/${tool}" >&2
      echo "       run 'bash harness/run.sh python deps' first; refusing to use host PATH tools." >&2
      exit 3
    fi
  done
  if ! "${VENV_BIN}/python" -c 'import pytest_cov' >/dev/null 2>&1; then
    echo "error: pytest-cov is not importable from ${VENV_BIN}/python" >&2
    echo "       run 'bash harness/run.sh python deps' first; refusing to use host PATH tools." >&2
    exit 3
  fi
}

require_harness_runner() {
  if [[ ! -f "${HARNESS_RUNNER}" ]]; then
    echo "error: HOME gate runner not found at ${HARNESS_RUNNER}" >&2
    echo "       set HARNESS_RUNNER to the opencode-home-config harness/run.sh path." >&2
    exit 4
  fi
}

if [[ "${STACK}" == "python" ]]; then
  case "${PHASE}" in
    deps)
      sync_python_deps
      ;;
    gate | all)
      [[ "${PHASE}" == "all" ]] && sync_python_deps
      require_python_venv
      require_harness_runner
      export PATH="${VENV_BIN}:${PATH}"
      echo "==> [gate] native path: project .venv toolchain (venv-first PATH, fail-fast, coverage >= 80%)"
      exec bash "${HARNESS_RUNNER}" python gate "${REPO_ROOT}"
      ;;
    *)
      echo "error: unknown phase '${PHASE}' (expected: deps|gate|all)" >&2
      exit 2
      ;;
  esac
else
  # rust (and any future stack): defer entirely to the shared runner unchanged.
  require_harness_runner
  exec bash "${HARNESS_RUNNER}" "${STACK}" "${PHASE}" "${REPO_ROOT}"
fi
