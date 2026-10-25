#!/usr/bin/env bash
# prod-update.sh — release-driven, provenance-gated auto-update of the prod lane
# (MADR-0024 Option 4, issue #326). Run by coach-web-update.timer every 5 minutes.
#
# Flow (MADR-0024 §Option 4 steps 1-8; security conditions C1-C8):
#   1. shared `flock` on the prod-lane lock — held => skip, exit 0 (C3);
#   2. C8 pause check: 12 consecutive failures stop the hammering early;
#   3. resolve ghcr.io/fpittelo/coach-web:prod and validate the digest (C2);
#   4. no-op when it equals the digest already pinned in .env.prod (step 3);
#   5. C1 provenance: digest(:prod) must equal the digest of the latest release
#      tag, read from the PUBLIC Releases API — never the app's GITHUB_TOKEN;
#   6. C6 encrypted pre-swap snapshot of the database, off-volume, mode 600,
#      last 4 kept;
#   7. C4 atomic .env.prod rewrite (mode 600 before and after, previous digest
#      retained in .env.prod.prev);
#   8. pull, recreate, then gate on 3/3 healthy <= 120 s AND /healthz reporting
#      the release tag; on any failure after the swap the previous digest is
#      restored and the previous image is brought back, exit non-zero (step 8).
#
# Usage:
#   scripts/prod-update.sh            # timer entrypoint (no-op when current)
#   scripts/prod-update.sh --force    # re-apply even when unchanged; clears a C8 pause
#   scripts/prod-update.sh --rollback # restore the digest retained in .env.prod.prev
#
# Every decision (digest comparison, provenance, env rewrite, health gate, state
# streaks) lives in scripts/prod_update.py — pure and unit-tested. This script is
# only the docker/HTTP shell around it. It never runs `set -x`, never prints
# .env.prod (only the public image digest) and never runs `docker compose config`
# (C4/C8).

set -euo pipefail
umask 077

# The provenance gate is unauthenticated by construction (C1): this process needs
# no GitHub credential at all, and must never borrow the application's token.
unset GITHUB_TOKEN GH_TOKEN

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPDATER="${REPO_ROOT}/scripts/prod_update.py"
ENV_FILE="${REPO_ROOT}/.env.prod"
IMAGE_REPO="ghcr.io/fpittelo/coach-web"
RELEASES_API="https://api.github.com/repos/fpittelo/coach-web/releases/latest"
HEALTHZ_URL="http://127.0.0.1:8000/healthz"
COMPOSE_PROJECT="coach-web-prod"
STATE_FILE="${HOME}/.local/state/coach-web/update-state.json"
SNAPSHOT_DIR="${HOME}/.local/state/coach-web/snapshots"
LOCK_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
LOCK_FILE="${LOCK_DIR}/coach-web-prod.lock"
KEEP_SNAPSHOTS=4
FAILURE_LIMIT=12
GATE_TIMEOUT_SECONDS=120
WORK_DIR=""

log() {
    printf '[prod-update] %s\n' "$*"
}

error() {
    printf '[prod-update] ERROR: %s\n' "$*" >&2
}

usage() {
    cat <<'EOF'
Usage: scripts/prod-update.sh [--force | --rollback]

  (no argument)  converge the prod lane onto ghcr.io/fpittelo/coach-web:prod when
                 that digest is the latest release and differs from .env.prod
  --force        skip the no-op check and the C8 pause, re-pull and re-apply
  --rollback     restore the digest retained in .env.prod.prev and re-apply it
EOF
}

# The lane compose invocation. Never dumps the resolved environment (no `config`
# subcommand — C4); .env.prod is handed over as `--env-file` only.
dc() {
    docker compose -p "${COMPOSE_PROJECT}" \
        -f "${REPO_ROOT}/compose.yaml" \
        -f "${REPO_ROOT}/compose.prod.yml" \
        --env-file "${ENV_FILE}" "$@"
}

cleanup() {
    if [[ -n "${WORK_DIR}" ]]; then
        rm -rf "${WORK_DIR}"
    fi
    return 0
}

# Resolve the manifest digest of an image reference; empty + non-zero on failure
# (same resolution as scripts/check_sidecar_drift.sh).
resolve_digest() {
    local ref="$1" digest
    digest="$(docker buildx imagetools inspect "${ref}" 2>/dev/null \
        | sed -n 's/^Digest:[[:space:]]*//p' | head -n1)" || true
    if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        return 1
    fi
    printf '%s' "${digest}"
}

# C8: record this run's outcome in the state file. Bookkeeping only — a failure
# to write it is reported and never masks the update outcome.
record() {
    local outcome="$1" out=""
    if ! out="$(python3 "${UPDATER}" state-record --state-file "${STATE_FILE}" \
            --outcome "${outcome}" 2>&1)"; then
        error "cannot record the ${outcome} outcome in ${STATE_FILE}: ${out}"
        return 0
    fi
    log "state: ${out}"
    return 0
}

# C8: exit non-zero when the failure streak reached the limit, so the caller can
# surface the status and skip instead of hammering the registry.
check_pause() {
    local out=""
    if out="$(python3 "${UPDATER}" state-check --state-file "${STATE_FILE}" \
            --limit "${FAILURE_LIMIT}" 2>&1)"; then
        return 0
    fi
    log "auto-update paused (C8): ${out}"
    log "inspect:  cat ${STATE_FILE}"
    log "resume:   scripts/prod-update.sh --force"
    return 1
}

# C6: encrypted pre-swap snapshot. The maintenance CLI runs INSIDE the running
# coach-web container, so DATA_ENCRYPTION_KEY stays in the container environment
# and never reaches this script. The artifact is staged on the container's tmpfs
# (encrypted, off the data volume) and copied out to an owner-only host
# directory; the last KEEP_SNAPSHOTS artifacts are kept.
write_snapshot() {
    local tag="$1" stamp name staged
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    name="coach-${stamp}-${tag}.backup"
    mkdir -p "${SNAPSHOT_DIR}"
    chmod 700 "${SNAPSHOT_DIR}"
    staged="/tmp/${name}"
    if ! dc exec -T coach-web python -m coach_web.maintenance backup --output "${staged}" >/dev/null; then
        error "the in-container backup failed — check the lane's data-encryption key (docs/admin_guide.md) and the WAL checkpoint"
        return 1
    fi
    if ! dc cp "coach-web:${staged}" "${SNAPSHOT_DIR}/${name}" >/dev/null; then
        dc exec -T coach-web rm -f "${staged}" >/dev/null 2>&1 || true
        error "cannot copy the snapshot out of the container to ${SNAPSHOT_DIR}"
        return 1
    fi
    dc exec -T coach-web rm -f "${staged}" >/dev/null 2>&1 || true
    chmod 600 "${SNAPSHOT_DIR}/${name}"
    log "pre-swap snapshot: ${SNAPSHOT_DIR}/${name} (mode 600, keeping the last ${KEEP_SNAPSHOTS})"
    prune_snapshots
}

# C6: keep only the newest KEEP_SNAPSHOTS artifacts.
prune_snapshots() {
    local victims victim
    victims="$(find "${SNAPSHOT_DIR}" -maxdepth 1 -type f -name 'coach-*.backup' \
        -printf '%T@ %p\n' 2>/dev/null | sort -rn | tail -n "+$((KEEP_SNAPSHOTS + 1))" \
        | cut -d' ' -f2-)"
    if [[ -z "${victims}" ]]; then
        return 0
    fi
    while IFS= read -r victim; do
        rm -f -- "${victim}"
        log "pruned old snapshot: ${victim}"
    done <<< "${victims}"
}

# Step 8 gate: 3/3 healthy AND /healthz on the release version, within the window.
wait_for_gate() {
    local tag="$1"
    local ps_json="${WORK_DIR}/ps.json"
    local healthz_json="${WORK_DIR}/healthz.json"
    local deadline=$((SECONDS + GATE_TIMEOUT_SECONDS))
    local verdict=""
    while [[ "${SECONDS}" -lt "${deadline}" ]]; do
        if dc ps -a --format json >"${ps_json}" 2>/dev/null \
            && curl -fsS --max-time 5 "${HEALTHZ_URL}" >"${healthz_json}" 2>/dev/null; then
            if verdict="$(python3 "${UPDATER}" health-gate --tag "${tag}" \
                    --ps-json "${ps_json}" --healthz-json "${healthz_json}" 2>&1)"; then
                log "gate ${verdict}"
                return 0
            fi
        fi
        sleep 2
    done
    error "gate FAIL after ${GATE_TIMEOUT_SECONDS}s: ${verdict:-no verdict (no healthy service set)}"
    return 1
}

# Step 8: restore the previous digest, bring the previous image back, record the
# failure. The lane keeps serving the previous release.
handle_post_swap_failure() {
    local reason="$1" out=""
    error "${reason} — rolling back to the previous release"
    if out="$(python3 "${UPDATER}" rewrite --env-file "${ENV_FILE}" --from-prev 2>&1)"; then
        log "${out}"
        if ! dc up -d --remove-orphans; then
            error "rollback 'up' failed — the lane needs manual attention (docs/admin_guide.md)"
        fi
    else
        error "rollback rewrite failed: ${out} — the lane needs manual attention"
    fi
    record failure
}

# The normal path: converge the lane onto the latest approved release.
do_update() {
    local force="$1"
    local prod_digest="" decision="" tag="" tag_digest="" release_json="" verdict="" out=""

    if [[ "${force}" != "true" ]] && ! check_pause; then
        return 0
    fi

    if ! prod_digest="$(resolve_digest "${IMAGE_REPO}:prod")"; then
        error "cannot resolve ${IMAGE_REPO}:prod (registry unreachable or digest malformed) — no pull, no recreate (C2)"
        record failure
        return 1
    fi
    log "resolved ${IMAGE_REPO}:prod -> ${prod_digest}"

    if ! decision="$(python3 "${UPDATER}" compare --env-file "${ENV_FILE}" \
            --prod-digest "${prod_digest}" 2>&1)"; then
        error "digest comparison failed: ${decision} — no pull, no recreate (C2)"
        record failure
        return 1
    fi
    if [[ "${decision}" == "no-change" && "${force}" != "true" ]]; then
        log "no change: ${ENV_FILE} already pins ${prod_digest} — no pull, no recreate"
        record success
        return 0
    fi
    log "update available: ${decision} (${prod_digest})"

    # C1 provenance gate — public, unauthenticated Releases API.
    release_json="${WORK_DIR}/release.json"
    if ! curl -fsS --connect-timeout 10 --max-time 30 \
            -H 'Accept: application/vnd.github+json' \
            "${RELEASES_API}" >"${release_json}" 2>/dev/null; then
        error "cannot read ${RELEASES_API} (unreachable, rate-limited or no release) — no pull, no recreate (C2)"
        record failure
        return 1
    fi
    if ! tag="$(python3 "${UPDATER}" release-tag --release-json "${release_json}" 2>&1)"; then
        error "latest release tag absent or malformed: ${tag} — no pull, no recreate (C2)"
        record failure
        return 1
    fi
    if ! tag_digest="$(resolve_digest "${IMAGE_REPO}:${tag}")"; then
        error "cannot resolve ${IMAGE_REPO}:${tag} — no pull, no recreate (C2)"
        record failure
        return 1
    fi
    if ! verdict="$(python3 "${UPDATER}" provenance --prod-digest "${prod_digest}" \
            --tag "${tag}" --tag-digest "${tag_digest}" 2>&1)"; then
        error "provenance gate refused the update (C1): ${verdict} — lane untouched"
        record failure
        return 1
    fi
    log "provenance ${verdict}"

    if ! write_snapshot "${tag}"; then
        error "no pre-swap snapshot — refusing to swap the image (C6)"
        record failure
        return 1
    fi

    # C4 swap: atomic rewrite, previous digest retained for the rollback.
    if ! out="$(python3 "${UPDATER}" rewrite --env-file "${ENV_FILE}" \
            --digest "${prod_digest}" 2>&1)"; then
        error "cannot rewrite ${ENV_FILE}: ${out} — lane untouched (C4)"
        record failure
        return 1
    fi
    log "${out}"

    if ! dc pull coach-web; then
        handle_post_swap_failure "pulling ${IMAGE_REPO}@${prod_digest} failed"
        return 1
    fi
    if ! dc up -d --remove-orphans; then
        handle_post_swap_failure "recreating the lane failed"
        return 1
    fi
    if ! wait_for_gate "${tag}"; then
        handle_post_swap_failure "the post-swap health gate failed"
        return 1
    fi

    log "update complete: the prod lane is serving ${tag} (${prod_digest})"
    record success
    return 0
}

# Manual rollback (C3: takes the same shared lock as the timer and the preflight;
# documented in docs/admin_guide.md). Converges onto the digest retained in
# .env.prod.prev — no pull, the previous image is already local.
do_rollback() {
    local out=""
    if ! out="$(python3 "${UPDATER}" rewrite --env-file "${ENV_FILE}" --from-prev 2>&1)"; then
        error "rollback failed: ${out}"
        return 1
    fi
    log "${out}"
    if ! dc up -d --remove-orphans; then
        error "rollback 'up' failed — the lane needs manual attention (docs/admin_guide.md)"
        return 1
    fi
    log "rollback complete: check http://127.0.0.1:8000/healthz"
    return 0
}

main() {
    local force="false" rollback="false"
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --force)
                force="true"
                ;;
            --rollback)
                rollback="true"
                ;;
            -h | --help)
                usage
                return 0
                ;;
            *)
                error "unknown argument '$1'"
                usage >&2
                return 2
                ;;
        esac
        shift
    done

    if [[ ! -d "${LOCK_DIR}" ]]; then
        error "runtime directory ${LOCK_DIR} not found — cannot take the shared prod-lane lock (C3)"
        return 1
    fi

    # C3: one lock for the timer, the preflight and the manual rollback. A held
    # lock means another prod-lane operation is running — this tick is a no-op.
    exec 9>"${LOCK_FILE}"
    if ! flock -n 9; then
        log "another prod-lane operation holds ${LOCK_FILE} — skipping this tick (C3)"
        return 0
    fi

    WORK_DIR="$(mktemp -d)"
    trap cleanup EXIT

    if [[ "${rollback}" == "true" ]]; then
        do_rollback
        return $?
    fi
    do_update "${force}"
}

main "$@"
