#!/usr/bin/env bash
# prod-install.sh — install / refresh / remove the boot-persistent supervision
# of the prod lane (systemd USER units; MADR-0024, issue #328).
#
# Usage:
#   scripts/prod-install.sh              # install / refresh (idempotent)
#   scripts/prod-install.sh --uninstall  # disable + remove the unit
#
# install (default):
#   1. asserts `loginctl show-user "$USER" -p Linger` is "yes" — without linger
#      the lane only runs while someone is logged in; on failure it prints the
#      exact remediation and exits non-zero (C7);
#   2. renders deploy/systemd/coach-web.service.in into
#      ~/.config/systemd/user/coach-web.service (the committed template is the
#      source of truth; the installed copy is never hand-edited);
#   3. `systemctl --user daemon-reload` + `enable --now coach-web.service`;
#   4. prints the boot-persistent user-unit audit list (linger widens boot
#      scope to every enabled user unit — C7) and a verification block.
#
# The unit never carries a secret: .env.prod is passed to compose via
# --env-file only (never EnvironmentFile=), and this script never reads,
# copies or prints .env.prod nor any `docker compose config` output (C4).
#
# --uninstall disables and removes the unit (its ExecStop downs the project).
# The compose data volume coach-web-prod_coach-data is never touched:
# `docker compose down` keeps named volumes unless `-v` is given, and this
# script never passes it.

set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="${REPO_ROOT}/deploy/systemd/coach-web.service.in"
UNIT_NAME="coach-web.service"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
UNIT_PATH="${USER_UNIT_DIR}/${UNIT_NAME}"
COMPOSE_PROJECT="coach-web-prod"
DATA_VOLUME="${COMPOSE_PROJECT}_coach-data"
HEALTHZ_URL="http://127.0.0.1:8000/healthz"
LOGIN_USER="${USER:-$(id -un)}"

log() {
    printf '[prod-install] %s\n' "$*"
}

error() {
    printf '[prod-install] ERROR: %s\n' "$*" >&2
}

usage() {
    cat >&2 <<'EOF'
Usage: scripts/prod-install.sh [--uninstall]

  (no argument)  install / refresh the boot-persistent prod-lane unit
                 (~/.config/systemd/user/coach-web.service), then verify
  --uninstall    disable and remove the unit (data volume untouched)
EOF
}

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "$1 is not installed or not on PATH"
        exit 1
    fi
}

# The lane compose invocation for read-only reporting. Never dumps the
# resolved environment (no `config` subcommand — C4).
dc() {
    docker compose -p "${COMPOSE_PROJECT}" \
        -f "${REPO_ROOT}/compose.yaml" \
        -f "${REPO_ROOT}/compose.prod.yml" \
        --env-file "${REPO_ROOT}/.env.prod" "$@"
}

linger_state() {
    loginctl show-user "${LOGIN_USER}" -p Linger --value 2>/dev/null || true
}

# C7: linger widens the boot scope to every enabled user unit — record the list.
audit_boot_units() {
    local wants_dir unit found="false"
    log "boot-persistent user units (linger widens boot scope to all of them — C7):"
    shopt -s nullglob
    for wants_dir in "${USER_UNIT_DIR}"/*.wants; do
        for unit in "${wants_dir}"/*; do
            printf '  %s -> %s\n' "$(basename "${unit}")" "$(basename "${wants_dir}")"
            found="true"
        done
    done
    shopt -u nullglob
    if [[ "${found}" != "true" ]]; then
        printf '  (none)\n'
    fi
}

assert_linger() {
    local linger
    linger="$(linger_state)"
    if [[ "${linger}" != "yes" ]]; then
        error "linger is '${linger:-unknown}' for user ${LOGIN_USER}: the prod lane would only run while you are logged in."
        error "Fix this by enabling boot persistence for your user (it widens the boot scope to every enabled user unit):"
        error "  loginctl enable-linger \"${LOGIN_USER}\""
        exit 1
    fi
    log "linger enabled for ${LOGIN_USER} — the prod lane survives logout and reboot (C7 audit list below)."
}

render_template() {
    local content
    content="$(<"${TEMPLATE}")"
    printf '%s\n' "${content//@REPO_ROOT@/${REPO_ROOT}}"
}

# Verification block: unit state, linger, /healthz (bounded poll) and the
# compose project state. It never fails the install — a lane that is still
# starting is reported, not hidden.
verify_block() {
    local healthz=""
    log "verification:"
    log "  unit ${UNIT_NAME}: $(systemctl --user is-active "${UNIT_NAME}" 2>/dev/null || echo unknown) (enabled: $(systemctl --user is-enabled "${UNIT_NAME}" 2>/dev/null || echo unknown))"
    log "  linger: $(linger_state)"
    for _ in $(seq 1 20); do
        if healthz="$(curl -fsS "${HEALTHZ_URL}" 2>/dev/null)"; then
            break
        fi
        healthz=""
        sleep 3
    done
    if [[ -n "${healthz}" ]]; then
        log "  ${HEALTHZ_URL}: ${healthz}"
    else
        error "  ${HEALTHZ_URL} did not answer 200 within 60s — inspect: journalctl --user -u ${UNIT_NAME}"
    fi
    log "  compose project state (expect coach-web, coach-mcp, github-mcp healthy):"
    dc ps -a 2>&1 || true
    log "  data volume ${DATA_VOLUME} untouched."
}

install_unit() {
    local desired existing
    if [[ ! -f "${TEMPLATE}" ]]; then
        error "unit template not found: ${TEMPLATE}"
        exit 1
    fi

    assert_linger

    mkdir -p "${USER_UNIT_DIR}"
    desired="$(render_template)"
    existing=""
    if [[ -f "${UNIT_PATH}" ]]; then
        existing="$(<"${UNIT_PATH}")"
    fi

    if [[ "${existing}" == "${desired}" ]]; then
        log "unit already up to date (idempotent no-op): ${UNIT_PATH}"
    else
        printf '%s\n' "${desired}" > "${UNIT_PATH}"
        log "rendered ${UNIT_PATH} from the committed template"
    fi

    systemctl --user daemon-reload
    systemctl --user enable --now "${UNIT_NAME}"
    log "enabled and started ${UNIT_NAME}"

    audit_boot_units
    verify_block
}

uninstall_unit() {
    if [[ -f "${UNIT_PATH}" ]]; then
        # ExecStop downs the project (project-scoped, keeps named volumes).
        systemctl --user disable --now "${UNIT_NAME}" || true
        rm -f "${UNIT_PATH}"
        log "disabled and removed ${UNIT_PATH}"
    else
        log "unit not installed at ${UNIT_PATH} — nothing to remove"
    fi
    systemctl --user daemon-reload
    log "data volume ${DATA_VOLUME} not touched (no -v, no volume removal)."
}

MODE="install"
case "${1:-}" in
    "" | install)
        MODE="install"
        ;;
    --uninstall | uninstall)
        MODE="uninstall"
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    *)
        error "unknown argument '$1'"
        usage
        exit 2
        ;;
esac

if [[ "${#}" -gt 1 ]]; then
    error "too many arguments"
    usage
    exit 2
fi

if [[ "${MODE}" == "install" ]]; then
    require_command systemctl
    require_command loginctl
    require_command docker
    require_command curl
    install_unit
else
    require_command systemctl
    uninstall_unit
fi
