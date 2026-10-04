#!/usr/bin/env bash
# lane.sh — single agent entrypoint for the dev/qa lane lifecycle (#134, epic #133).
#
# A thin wrapper around docker compose: everything is delegated using the
# lane's fixed project name (coach-web-<lane>), compose file pair and env
# file — no new deployment logic (KIS).
#
# Usage:
#   ./scripts/lane.sh <dev|qa> <up|status|logs|down> [service]
#
# Actions:
#   up      build/start the lane, wait until the expected service set
#           (coach-web, coach-mcp, github-mcp) is healthy — same wait-loop
#           pattern as scripts/e2e-preflight.sh — then print the lane URL
#           and the per-service states
#   status  docker compose ps -a for the lane project only
#   logs    docker compose logs --tail=100 [service] for the lane project only
#   down    project-scoped teardown (down --remove-orphans): the blast radius
#           is strictly the lane's own compose project — concurrent lanes and
#           unrelated compose projects are never touched
#
# The prod lane is intentionally NOT automated here: it is a manual, human-owned
# procedure (digest-pinned release image + OIDC secrets) — see the usage header
# in compose.prod.yml and docs/admin_guide.md (Promotion Runbook). Lane
# validation remains the job of scripts/e2e-preflight.sh.
#
# The lane env file is never read or modified by this script; it is passed to
# compose via --env-file as the interpolation source only (ADR-007).
#
# LANE_WAIT_SECONDS overrides the health-wait timeout in seconds (default 180);
# it exists so the wait loop can be exercised quickly by the test suite.

set -euo pipefail

MAX_WAIT_SECONDS="${LANE_WAIT_SECONDS:-180}"

log() {
    printf '[lane] %s\n' "$*"
}

error() {
    printf '[lane] ERROR: %s\n' "$*" >&2
}

usage() {
    cat >&2 <<'EOF'
Usage: scripts/lane.sh <dev|qa> <up|status|logs|down> [service]

Actions:
  up      build/start the lane and wait until all services are healthy
  status  container state for the lane's compose project (ps -a)
  logs    lane logs (--tail=100), optionally filtered to one service
  down    tear down the lane's compose project (--remove-orphans)

The prod lane is manual-only (digest-pinned image + OIDC secrets): see the
usage header in compose.prod.yml and docs/admin_guide.md (Promotion Runbook).
Lane validation gate: ./scripts/e2e-preflight.sh <lane>
EOF
}

LANE="${1:-}"
ACTION="${2:-}"
SERVICE="${3:-}"

if [[ -z "${LANE}" || -z "${ACTION}" ]]; then
    error "lane and action arguments are required"
    usage
    exit 2
fi

case "${LANE}" in
    dev)
        LANE_PORT=8100
        LANE_COMPOSE="compose.dev.yml"
        ;;
    qa)
        LANE_PORT=8200
        LANE_COMPOSE="compose.qa.yml"
        ;;
    prod)
        error "lane 'prod' is not automated by lane.sh — the prod lane is a manual procedure:"
        error "  cp .env.prod.example .env.prod  # then fill in secrets + the release digest"
        error "  docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod pull"
        error "  docker compose -p coach-web-prod -f compose.yaml -f compose.prod.yml --env-file .env.prod up -d"
        error "  ./scripts/e2e-preflight.sh prod"
        error "See the usage header in compose.prod.yml and docs/admin_guide.md (Promotion Runbook)."
        exit 2
        ;;
    *)
        error "unknown lane '${LANE}' (expected dev|qa)"
        usage
        exit 2
        ;;
esac

case "${ACTION}" in
    up | status | down)
        if [[ "${#}" -gt 2 ]]; then
            error "action '${ACTION}' takes no extra arguments"
            usage
            exit 2
        fi
        ;;
    logs)
        if [[ "${#}" -gt 3 ]]; then
            error "action 'logs' takes at most one service argument"
            usage
            exit 2
        fi
        ;;
    *)
        error "unknown action '${ACTION}' (expected up|status|logs|down)"
        usage
        exit 2
        ;;
esac

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_BASE="${PROJECT_DIR}/compose.yaml"
COMPOSE_LANE="${PROJECT_DIR}/${LANE_COMPOSE}"
ENV_FILE="${PROJECT_DIR}/.env.${LANE}"
ENV_EXAMPLE="${PROJECT_DIR}/.env.${LANE}.example"
PROJECT="coach-web-${LANE}"
LANE_URL="http://127.0.0.1:${LANE_PORT}"

dc() {
    docker compose -p "${PROJECT}" -f "${COMPOSE_BASE}" -f "${COMPOSE_LANE}" --env-file "${ENV_FILE}" "$@"
}

if ! command -v docker >/dev/null 2>&1; then
    error "Docker is not installed or not on PATH"
    exit 1
fi

if [[ ! -f "${COMPOSE_BASE}" || ! -f "${COMPOSE_LANE}" ]]; then
    error "Missing compose files (${COMPOSE_BASE} / ${COMPOSE_LANE}) — run from the repository checkout"
    exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    error "Missing ${ENV_FILE}. Copy ${ENV_EXAMPLE} to ${ENV_FILE} and fill in your secrets."
    exit 1
fi

case "${ACTION}" in
    up)
        log "Starting the ${LANE} lane (compose project ${PROJECT})..."
        if ! dc up -d --build; then
            error "Failed to build/start the ${LANE} lane"
            dc ps -a || true
            dc logs --tail 50 || true
            exit 1
        fi

        log "Waiting for the expected service set to be present and healthy (timeout ${MAX_WAIT_SECONDS}s)..."
        seconds_waited=0
        states=""
        all_healthy="false"
        while [[ "${seconds_waited}" -lt "${MAX_WAIT_SECONDS}" ]]; do
            # The parser validates the exact service set (a crashed/exited
            # sidecar cannot be silently absent) and health; on success it
            # prints the per-service states for the final report.
            if states="$(dc ps -a --format json 2>/dev/null | python3 -c '
import json
import sys

EXPECTED_SERVICES = {"coach-web", "coach-mcp", "github-mcp"}

raw = sys.stdin.read().strip()
entries = []
if raw:
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]

present = {entry.get("Service") for entry in entries}
if present != EXPECTED_SERVICES:
    missing = sorted(EXPECTED_SERVICES - present)
    unexpected = sorted(present - EXPECTED_SERVICES)
    print(
        "service set mismatch (missing: {0}; unexpected: {1})".format(missing, unexpected),
        file=sys.stderr,
    )
    sys.exit(1)

not_healthy = sorted(
    entry.get("Service", "<unknown>")
    for entry in entries
    if entry.get("Health") != "healthy"
)
if not_healthy:
    print("services not healthy: {0}".format(not_healthy), file=sys.stderr)
    sys.exit(1)

for entry in sorted(entries, key=lambda item: str(item.get("Service"))):
    print("{0}: {1}".format(entry.get("Service", "<unknown>"), entry.get("Health", "<unknown>")))
sys.exit(0)
')"; then
                all_healthy="true"
                break
            fi
            sleep 2
            seconds_waited=$((seconds_waited + 2))
        done

        if [[ "${all_healthy}" != "true" ]]; then
            error "The expected service set (coach-web, coach-mcp, github-mcp) did not become healthy within ${MAX_WAIT_SECONDS}s"
            error "A crashed or absent sidecar fails this gate: 'docker compose ps -a' below lists every container, including exited ones."
            dc ps -a || true
            dc logs --tail 50 || true
            exit 1
        fi
        log "All expected services are present and healthy."
        log "Lane URL: ${LANE_URL}"
        log "Service states:"
        printf '%s\n' "${states}"
        ;;
    status)
        dc ps -a
        ;;
    logs)
        if [[ -n "${SERVICE}" ]]; then
            dc logs --tail=100 "${SERVICE}"
        else
            dc logs --tail=100
        fi
        ;;
    down)
        log "Tearing down project ${PROJECT} (scoped to this lane only)..."
        dc down --remove-orphans
        ;;
esac
