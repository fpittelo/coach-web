#!/usr/bin/env bash
# E2E pre-flight validation for the unified Docker Compose sidecar topology.
#
# Validates:
#   1. docker compose config parses without errors
#   2. All three services report healthy
#   3. coach-web /health endpoint responds with the expected payload
#   4. coach-web / endpoint serves the SPA
#
# Usage:
#   ./scripts/e2e-preflight.sh
#
# The script tears down any previously running project containers on exit.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.yml"
ENV_FILE="${PROJECT_DIR}/.env"
COACH_WEB_URL="http://127.0.0.1:8000"
MAX_WAIT_SECONDS=120

log() {
    printf '[e2e-preflight] %s\n' "$*"
}

error() {
    printf '[e2e-preflight] ERROR: %s\n' "$*" >&2
}

cleanup() {
    log "Cleaning up project containers..."
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" down --remove-orphans >/dev/null 2>&1 || true
}

trap cleanup EXIT

if ! command -v docker >/dev/null 2>&1; then
    error "Docker is not installed or not on PATH"
    exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
    error "Missing ${ENV_FILE}. Copy .env.example to .env and fill in your secrets."
    exit 1
fi

log "Linting compose configuration..."
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" config >/dev/null
log "Compose configuration is valid."

log "Building and starting sidecar topology..."
docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" up -d --build --wait

log "Waiting for all services to become healthy (timeout ${MAX_WAIT_SECONDS}s)..."
seconds_waited=0
while [[ ${seconds_waited} -lt ${MAX_WAIT_SECONDS} ]]; do
    unhealthy=$(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" ps --format json 2>/dev/null | \
        python -c "import sys, json; data=json.load(sys.stdin); print(any(c.get('Health') != 'healthy' for c in data))" 2>/dev/null || echo "true")
    if [[ "${unhealthy}" == "False" ]]; then
        log "All services are healthy."
        break
    fi
    sleep 2
    seconds_waited=$((seconds_waited + 2))
done

if [[ "${unhealthy}" != "False" ]]; then
    error "Services did not become healthy within ${MAX_WAIT_SECONDS}s"
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" ps
    docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" logs --tail 50
    exit 1
fi

log "Validating coach-web health endpoint..."
health_response=$(curl -fsS "${COACH_WEB_URL}/health" 2>/dev/null)
if ! printf '%s' "${health_response}" | python -c "import sys, json; d=json.load(sys.stdin); assert d.get('status') == 'healthy' and d.get('service') == 'coach-web'"; then
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

log "E2E pre-flight passed successfully."
