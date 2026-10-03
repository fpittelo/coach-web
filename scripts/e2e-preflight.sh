#!/usr/bin/env bash
# E2E pre-flight validation for the local Docker lane topology (ADR-007, #109).
#
# Validates ONE lane (dev | qa | prod; default dev):
#   1. the lane's compose configuration parses (docker compose config)
#   2. every image the lane must PULL exists in its registry — fails fast with
#      a clear message when a lane image is absent from GHCR (qa/prod lanes
#      pull CI-built promotion artifacts; the dev lane builds coach-web locally
#      so only its sidecars are checked)
#   3. the expected service set (coach-web, coach-mcp, github-mcp) is PRESENT
#      and all three services turn healthy — `docker compose ps -a` is used so
#      a crashed/exited sidecar cannot be silently absent from the gate
#   4. coach-web /health responds with the expected payload
#   5. coach-web / serves the SPA
#   6. coach-web is published on 127.0.0.1 ONLY and the sidecars publish no
#      host ports at all (loopback binding is the primary boundary, ADR-007)
#   7. (prod only) /api/* returns 401 without a session (auth boundary)
#
# Usage:
#   ./scripts/e2e-preflight.sh [dev|qa|prod]
#
# Teardown is scoped to the lane's own compose project (-p coach-web-<lane>);
# other lanes running concurrently are never touched.

set -euo pipefail

LANE="${1:-dev}"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAX_WAIT_SECONDS=180

log() {
    printf '[e2e-preflight] %s\n' "$*"
}

error() {
    printf '[e2e-preflight] ERROR: %s\n' "$*" >&2
}

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
        LANE_PORT=8000
        LANE_COMPOSE="compose.prod.yml"
        ;;
    *)
        error "unknown lane '${LANE}' (expected dev|qa|prod)"
        exit 1
        ;;
esac

COMPOSE_BASE="${PROJECT_DIR}/compose.yaml"
COMPOSE_LANE="${PROJECT_DIR}/${LANE_COMPOSE}"
ENV_FILE="${PROJECT_DIR}/.env.${LANE}"
ENV_EXAMPLE="${PROJECT_DIR}/.env.${LANE}.example"
PROJECT="coach-web-${LANE}"
COACH_WEB_URL="http://127.0.0.1:${LANE_PORT}"

dc() {
    docker compose -p "${PROJECT}" -f "${COMPOSE_BASE}" -f "${COMPOSE_LANE}" --env-file "${ENV_FILE}" "$@"
}

cleanup() {
    log "Tearing down project ${PROJECT} (scoped to this lane only)..."
    dc down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

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

log "Linting the ${LANE} lane compose configuration..."
dc config --quiet
log "Compose configuration is valid."

log "Resolving lane images..."
config_json="$(dc config --format json)"
images_to_pull="$(printf '%s' "${config_json}" | python3 -c '
import json
import sys

config = json.load(sys.stdin)
for name in sorted(config.get("services", {})):
    service = config["services"][name]
    if "build" in service:
        continue  # built from source on this host — nothing to pull
    image = service.get("image", "")
    if image:
        print(image)
')"

if [[ -n "${images_to_pull}" ]]; then
    log "Verifying lane images exist in their registry (fail fast before up)..."
    while IFS= read -r image_ref; do
        [[ -z "${image_ref}" ]] && continue
        if ! docker manifest inspect "${image_ref}" >/dev/null 2>&1; then
            error "Lane image '${image_ref}' is not reachable."
            error "Either the image is not yet published to GHCR (qa/prod lanes require the promotion to have run — see docs/admin_guide.md, Promotion Runbook) or registry access is missing (docker login ghcr.io with a read:packages PAT)."
            exit 1
        fi
        log "Image OK: ${image_ref}"
    done <<< "${images_to_pull}"
fi

log "Starting the ${LANE} lane (compose project ${PROJECT})..."
if [[ "${LANE}" == "dev" ]]; then
    if ! dc up -d --build; then
        error "Failed to build/start the dev lane"
        dc ps -a || true
        dc logs --tail 50 || true
        exit 1
    fi
else
    if ! dc up -d; then
        error "Failed to start the ${LANE} lane"
        dc ps -a || true
        dc logs --tail 50 || true
        exit 1
    fi
fi

log "Waiting for the expected service set to be present and healthy (timeout ${MAX_WAIT_SECONDS}s)..."
seconds_waited=0
all_healthy="false"
while [[ "${seconds_waited}" -lt "${MAX_WAIT_SECONDS}" ]]; do
    if dc ps -a --format json 2>/dev/null | python3 -c '
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

sys.exit(0)
'; then
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

log "Asserting loopback-only host publishing..."
ps_json="$(dc ps -a --format json)"
printf '%s' "${ps_json}" | python3 -c '
import json
import sys

raw = sys.stdin.read().strip()
entries = []
if raw:
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        entries = [json.loads(line) for line in raw.splitlines() if line.strip()]
violations = []
for entry in entries:
    service = entry.get("Service", "<unknown>")
    publishers = entry.get("Publishers") or []
    if service == "coach-web":
        for publisher in publishers:
            published_port = publisher.get("PublishedPort") or 0
            if published_port == 0:
                continue
            host_ip = publisher.get("PublishedIP") or publisher.get("URL") or publisher.get("IP") or ""
            if host_ip != "127.0.0.1":
                violations.append(
                    "coach-web published on {0}:{1} (must be 127.0.0.1)".format(host_ip, published_port)
                )
    elif publishers:
        violations.append("{0} publishes host ports but must publish none".format(service))
for violation in violations:
    print(violation, file=sys.stderr)
sys.exit(1 if violations else 0)
'
log "Publishing is loopback-only (coach-web on 127.0.0.1:${LANE_PORT}, sidecars unpublished)."

log "Validating coach-web health endpoint..."
if ! health_response="$(curl -fsS "${COACH_WEB_URL}/health" 2>/dev/null)"; then
    error "coach-web /health did not respond on ${COACH_WEB_URL}"
    exit 1
fi
if ! printf '%s' "${health_response}" | python3 -c "import sys, json; d=json.load(sys.stdin); assert d.get('status') == 'healthy' and d.get('service') == 'coach-web'"; then
    error "Unexpected health response: ${health_response}"
    exit 1
fi
log "Health endpoint OK: ${health_response}"

log "Validating coach-web root endpoint..."
if ! curl -fsS "${COACH_WEB_URL}/" >/dev/null 2>&1; then
    error "Root endpoint did not respond"
    exit 1
fi
log "Root endpoint OK."

if [[ "${LANE}" == "prod" ]]; then
    log "Validating the prod auth boundary (/api/* must deny anonymous access)..."
    status_code="$(curl -sS -o /dev/null -w '%{http_code}' "${COACH_WEB_URL}/api/agent/stream" 2>/dev/null || true)"
    if [[ "${status_code}" != "401" ]]; then
        error "Expected 401 from /api/agent/stream without a session, got: ${status_code:-none}"
        exit 1
    fi
    log "Auth boundary OK: /api/agent/stream returned 401 without a session."
fi

log "E2E pre-flight passed for lane '${LANE}'."
