#!/usr/bin/env bash
# scripts/check_sidecar_drift.sh — coach-mcp sidecar pin drift guard (#275).
#
# The coach-mcp sidecar (ghcr.io/fpittelo/coach) is built and published by a
# SEPARATE pipeline (fpittelo/coach). Each lane's compose override pins the
# sidecar by digest, so an upstream push cannot silently change the topology
# coach-web is tested against (the #248 incident class: a sidecar tool-schema
# change can break coach-web's transport assumptions with no coach-web commit).
#
# This check resolves the current digest of each lane's floating tag and
# compares it to the digest pinned in the lane's compose file. A mismatch exits
# non-zero with an actionable message, so a sidecar change is a deliberate act,
# not a silent one. It is invoked by the CI "Compose Lane Validation" job.
#
# Usage: scripts/check_sidecar_drift.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SIDECAR_REPO="ghcr.io/fpittelo/coach"
LANES=(dev qa prod)
declare -A LANE_COMPOSE=(
    [dev]="compose.dev.yml"
    [qa]="compose.qa.yml"
    [prod]="compose.prod.yml"
)

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH" >&2
    exit 1
fi

# Resolve the current registry digest of a floating tag (empty on failure).
resolve_digest() {
    local ref="$1" digest
    digest="$(docker buildx imagetools inspect "${ref}" 2>/dev/null \
        | sed -n 's/^Digest:[[:space:]]*//p' | head -n1)" || true
    if ! [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        return 1
    fi
    printf '%s' "${digest}"
}

# Extract the digest pinned in a lane compose file (empty when absent).
pinned_digest() {
    local file="$1"
    grep -oE "${SIDECAR_REPO}@sha256:[0-9a-f]{64}" "${file}" 2>/dev/null \
        | head -n1 | sed "s|${SIDECAR_REPO}@||" || true
}

status=0
for lane in "${LANES[@]}"; do
    compose_file="${REPO_ROOT}/${LANE_COMPOSE[${lane}]}"
    if [[ ! -f "${compose_file}" ]]; then
        echo "ERROR: ${LANE_COMPOSE[${lane}]} not found — run from the repository checkout" >&2
        status=1
        continue
    fi

    pinned="$(pinned_digest "${compose_file}")"
    if [[ -z "${pinned}" ]]; then
        echo "ERROR: ${LANE_COMPOSE[${lane}]} does not pin ${SIDECAR_REPO} by digest" >&2
        status=1
        continue
    fi

    if ! current="$(resolve_digest "${SIDECAR_REPO}:${lane}")"; then
        echo "ERROR: could not resolve ${SIDECAR_REPO}:${lane} from the registry" >&2
        echo "       (check network access and GHCR read permission)" >&2
        status=1
        continue
    fi

    if [[ "${current}" != "${pinned}" ]]; then
        echo "ERROR: coach-mcp sidecar drift on the '${lane}' lane" >&2
        echo "  pinned   (${LANE_COMPOSE[${lane}]}): ${pinned}" >&2
        echo "  upstream (${SIDECAR_REPO}:${lane}): ${current}" >&2
        echo "  The upstream '${lane}' image moved. Re-validate the lane, then update" >&2
        echo "  the pin deliberately:" >&2
        echo "    docker buildx imagetools inspect ${SIDECAR_REPO}:${lane}" >&2
        echo "    # set the new digest in ${LANE_COMPOSE[${lane}]} and .env.${lane}.example" >&2
        status=1
    else
        echo "OK: ${lane} lane pin matches ${SIDECAR_REPO}:${lane} (${pinned})"
    fi
done

exit "${status}"
