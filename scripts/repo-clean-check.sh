#!/usr/bin/env bash
# repo-clean-check.sh — refuse to execute production tooling from a dirty or
# foreign checkout (#344).
#
# The prod lane's supervisor (coach-web.service, boot) and auto-updater
# (coach-web-update.service, every 5 min) run docker-group-privileged code
# straight from the developer working tree. During the v0.14.0 rollout the tree
# was checked out on a still-in-review branch, so unreviewed host-side code
# performed the production swap. This guard is the boundary: an update (or a
# boot) is only allowed when the checkout is provably the reviewed trunk.
#
# Three conditions, each fail-safe (an unknown git failure aborts, never passes):
#   1. the path is a git work tree;
#   2. the working tree has no *tracked* modifications (untracked files are
#      allowed: they cannot change a tracked script);
#   3. the checkout is on `main` and HEAD equals the last-known
#      `refs/remotes/origin/main` (no fetch here — the ref reflects the last
#      `git fetch`/`git pull`; the residual window is documented in
#      docs/admin_guide.md).
#
# Usage:
#   scripts/repo-clean-check.sh [REPO_ROOT]   # exit 0 = clean, 1 = refuse
#
# On refusal it prints ONE actionable line (failed condition + fix) and exits 1.

set -euo pipefail
umask 077

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# One-line refusal: the failed condition, the three conditions, and the fix.
refuse() {
    printf "[repo-clean-check] ERROR: %s — prod tooling requires a clean checkout of 'main' at origin/main in %s; fix: git -C %s checkout main && git -C %s pull --ff-only\n" \
        "$1" "${REPO_ROOT}" "${REPO_ROOT}" "${REPO_ROOT}" >&2
    exit 1
}

# 1. Is this a git work tree? A git failure is unknown state, not a pass.
inside="$(git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree 2>/dev/null)" \
    || refuse "cannot run git in the checkout"
[[ "${inside}" == "true" ]] || refuse "not a git work tree"

# 2. No tracked modifications (untracked files are allowed).
status="$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no 2>/dev/null)" \
    || refuse "cannot read the working-tree status"
if [[ -n "${status}" ]]; then
    refuse "working tree has tracked changes: $(head -n1 <<<"${status}")"
fi

# 3a. On branch main.
branch="$(git -C "${REPO_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null)" \
    || refuse "cannot determine the checked-out branch"
[[ "${branch}" == "main" ]] || refuse "checkout is on '${branch}', not 'main'"

# 3b. HEAD equals the last-known origin/main (no fetch — see the header).
head_sha="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null)" \
    || refuse "cannot resolve HEAD"
origin_sha="$(git -C "${REPO_ROOT}" rev-parse --verify --quiet refs/remotes/origin/main 2>/dev/null)" \
    || refuse "no refs/remotes/origin/main (the checkout never fetched main)"
[[ "${head_sha}" == "${origin_sha}" ]] \
    || refuse "HEAD ${head_sha} != origin/main ${origin_sha}"

printf '[repo-clean-check] ok: clean checkout of main at %s\n' "${head_sha}"
