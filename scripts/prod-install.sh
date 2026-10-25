#!/usr/bin/env bash
# prod-install.sh — install / refresh / remove the boot-persistent supervision
# and the release-driven auto-update of the prod lane (systemd USER units;
# MADR-0024, issues #328 and #326).
#
# Usage:
#   scripts/prod-install.sh              # install / refresh (idempotent)
#   scripts/prod-install.sh --uninstall  # disable + remove the units
#
# install (default):
#   1. asserts `loginctl show-user "$USER" -p Linger` is "yes" — without linger
#      the lane only runs while someone is logged in; on failure it prints the
#      exact remediation and exits non-zero (C7);
#   2. renders deploy/systemd/coach-web.service.in and
#      deploy/systemd/coach-web-update.{service,timer}.in into
#      ~/.config/systemd/user/ (the committed templates are the source of
#      truth; the installed copies are never hand-edited);
#   3. `systemctl --user daemon-reload`, `enable --now coach-web.service` (the
#      lane) and `enable --now coach-web-update.timer` (the #326 auto-update);
#   4. prints the boot-persistent user-unit audit list (linger widens boot
#      scope to every enabled user unit — C7) and a verification block.
#
# The units never carry a secret: .env.prod is passed to compose via
# --env-file only (never EnvironmentFile=), and this script never reads,
# copies or prints .env.prod nor any `docker compose config` output (C4).
#
# --uninstall disables and removes the units (the service's ExecStop downs the
# project; the timer is stopped first so no update can race the teardown).
# The compose data volume coach-web-prod_coach-data is never touched:
# `docker compose down` keeps named volumes unless `-v` is given, and this
# script never passes it.

set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNIT_DIR="${HOME}/.config/systemd/user"
# #328 — boot supervision of the lane.
LANE_SERVICE_NAME="coach-web.service"
LANE_SERVICE_TEMPLATE="${REPO_ROOT}/deploy/systemd/${LANE_SERVICE_NAME}.in"
LANE_SERVICE_UNIT_PATH="${USER_UNIT_DIR}/${LANE_SERVICE_NAME}"
# #326 — release-driven auto-update (timer-driven oneshot).
UPDATE_SERVICE_NAME="coach-web-update.service"
UPDATE_SERVICE_TEMPLATE="${REPO_ROOT}/deploy/systemd/${UPDATE_SERVICE_NAME}.in"
UPDATE_SERVICE_UNIT_PATH="${USER_UNIT_DIR}/${UPDATE_SERVICE_NAME}"
UPDATE_TIMER_NAME="coach-web-update.timer"
UPDATE_TIMER_TEMPLATE="${REPO_ROOT}/deploy/systemd/${UPDATE_TIMER_NAME}.in"
UPDATE_TIMER_UNIT_PATH="${USER_UNIT_DIR}/${UPDATE_TIMER_NAME}"
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

  (no argument)  install / refresh the prod-lane units — the boot supervisor
                 (coach-web.service) and the release-driven auto-update
                 (coach-web-update.service + coach-web-update.timer) — then verify
  --uninstall    disable and remove the units (data volume untouched)
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
    local template="$1" content
    content="$(<"${template}")"
    printf '%s\n' "${content//@REPO_ROOT@/${REPO_ROOT}}"
}

# Verification block: unit state, linger, /healthz (bounded poll) and the
# compose project state. It never fails the install — a lane that is still
# starting is reported, not hidden.
verify_block() {
    local healthz=""
    log "verification:"
    log "  unit ${LANE_SERVICE_NAME}: $(systemctl --user is-active "${LANE_SERVICE_NAME}" 2>/dev/null || echo unknown) (enabled: $(systemctl --user is-enabled "${LANE_SERVICE_NAME}" 2>/dev/null || echo unknown))"
    log "  timer ${UPDATE_TIMER_NAME}: $(systemctl --user is-active "${UPDATE_TIMER_NAME}" 2>/dev/null || echo unknown) (enabled: $(systemctl --user is-enabled "${UPDATE_TIMER_NAME}" 2>/dev/null || echo unknown))"
    log "  next auto-update tick:"
    systemctl --user list-timers "${UPDATE_TIMER_NAME}" --no-pager 2>&1 || true
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
        error "  ${HEALTHZ_URL} did not answer 200 within 60s — inspect: journalctl --user -u ${LANE_SERVICE_NAME}"
    fi
    log "  compose project state (expect coach-web, coach-mcp, github-mcp healthy):"
    dc ps -a 2>&1 || true
    log "  data volume ${DATA_VOLUME} untouched."
}

# Render one committed template into the user unit directory (idempotent: an
# unchanged unit is reported, never rewritten).
install_unit_file() {
    local template="$1" unit_path="$2" desired existing
    if [[ ! -f "${template}" ]]; then
        error "unit template not found: ${template}"
        exit 1
    fi
    desired="$(render_template "${template}")"
    existing=""
    if [[ -f "${unit_path}" ]]; then
        existing="$(<"${unit_path}")"
    fi
    if [[ "${existing}" == "${desired}" ]]; then
        log "unit already up to date (idempotent no-op): ${unit_path}"
    else
        printf '%s\n' "${desired}" > "${unit_path}"
        log "rendered ${unit_path} from the committed template"
    fi
}

install_units() {
    assert_linger

    mkdir -p "${USER_UNIT_DIR}"
    install_unit_file "${LANE_SERVICE_TEMPLATE}" "${LANE_SERVICE_UNIT_PATH}"
    install_unit_file "${UPDATE_SERVICE_TEMPLATE}" "${UPDATE_SERVICE_UNIT_PATH}"
    install_unit_file "${UPDATE_TIMER_TEMPLATE}" "${UPDATE_TIMER_UNIT_PATH}"

    systemctl --user daemon-reload
    systemctl --user enable --now "${LANE_SERVICE_NAME}"
    log "enabled and started ${LANE_SERVICE_NAME}"
    # The timer owns the 5-minute release-driven update; its service is never
    # enabled on its own (it is timer-activated).
    systemctl --user enable --now "${UPDATE_TIMER_NAME}"
    log "enabled and started ${UPDATE_TIMER_NAME} (release-driven auto-update, #326)"

    audit_boot_units
    verify_block
}

uninstall_units() {
    # The timer goes first: an update must not race the teardown below.
    if [[ -f "${UPDATE_TIMER_UNIT_PATH}" ]]; then
        systemctl --user disable --now "${UPDATE_TIMER_NAME}" || true
        rm -f "${UPDATE_TIMER_UNIT_PATH}"
        log "disabled and removed ${UPDATE_TIMER_UNIT_PATH}"
    else
        log "timer not installed at ${UPDATE_TIMER_UNIT_PATH} — nothing to remove"
    fi
    if [[ -f "${UPDATE_SERVICE_UNIT_PATH}" ]]; then
        rm -f "${UPDATE_SERVICE_UNIT_PATH}"
        log "removed ${UPDATE_SERVICE_UNIT_PATH}"
    fi
    if [[ -f "${LANE_SERVICE_UNIT_PATH}" ]]; then
        # ExecStop downs the project (project-scoped, keeps named volumes).
        systemctl --user disable --now "${LANE_SERVICE_NAME}" || true
        rm -f "${LANE_SERVICE_UNIT_PATH}"
        log "disabled and removed ${LANE_SERVICE_UNIT_PATH}"
    else
        log "unit not installed at ${LANE_SERVICE_UNIT_PATH} — nothing to remove"
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
    install_units
else
    require_command systemctl
    uninstall_units
fi
