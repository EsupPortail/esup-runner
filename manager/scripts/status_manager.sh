#!/usr/bin/env bash

# Status helper for Esup-Runner Manager.
# Shows systemd user/system unit status (if present) + process discovery by port/patterns.

set -u

SERVICE_NAME="esup-runner-manager"
WORKDIR="/opt/esup-runner/manager"
ENV_FILE="${WORKDIR}/.env"

_log() {
  echo "$@"
}

_read_env_var() {
  local env_file="$1"
  local var_name="$2"

  [[ -f "${env_file}" ]] || return 1

  # Read KEY=VALUE lines without executing them (a .env file isn't necessarily shell-safe).
  # - Ignores comments and empty lines
  # - Strips surrounding quotes
  awk -F= -v key="${var_name}" '
    $0 ~ /^[[:space:]]*#/ { next }
    $0 ~ /^[[:space:]]*$/ { next }
    $1 == key {
      val = substr($0, index($0, "=") + 1)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
      if (val ~ /^".*"$/) { sub(/^"/, "", val); sub(/"$/, "", val) }
      if (val ~ /^\x27.*\x27$/) { sub(/^\x27/, "", val); sub(/\x27$/, "", val) }
      print val
      exit
    }
  ' "${env_file}"
}

_pids_listening_on_port() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    ss -lptn "sport = :${port}" 2>/dev/null \
      | awk -F'pid=' 'NR>1 {split($2,a,","); print a[1]}' \
      | awk '/^[0-9]+$/' \
      | sort -u
    return 0
  fi

  if command -v lsof >/dev/null 2>&1; then
    lsof -t -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | awk '/^[0-9]+$/' | sort -u
    return 0
  fi

  if command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "${port}" 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+$/' | sort -u
    return 0
  fi

  return 1
}

_pids_by_patterns() {
  pgrep -f "uv run esup-runner-manager|gunicorn .*app\.main:app|python(3)? .*launcher\.py" 2>/dev/null || true
}

_show_process_details() {
  local title="$1"; shift
  local pids_str="$1"

  if [[ -z "${pids_str}" ]]; then
    _log "- ${title}: (none)"
    return 0
  fi

  _log "- ${title}: ${pids_str}"

  if command -v ps >/dev/null 2>&1; then
    _log "  cmdline:"
    # shellcheck disable=SC2001
    for pid in ${pids_str}; do
      ps -p "${pid}" -o pid=,ppid=,etime=,cmd= 2>/dev/null | sed 's/^/    /' || true
    done
  fi
}

_show_systemd_status_user() {
  if ! systemctl --user cat "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    _log "- user unit (${SERVICE_NAME}.service): not installed"
    return 0
  fi

  systemctl --user is-active --quiet "${SERVICE_NAME}.service" \
    && _log "- user unit (${SERVICE_NAME}.service): active" \
    || _log "- user unit (${SERVICE_NAME}.service): inactive"
  systemctl --user is-enabled --quiet "${SERVICE_NAME}.service" \
    && _log "- user enabled: yes" \
    || _log "- user enabled: no"
  _log "- user main pid: $(systemctl --user show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
}

_show_systemd_status_system() {
  if ! systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    _log "- system unit (${SERVICE_NAME}.service): not installed"
    return 0
  fi

  systemctl is-active --quiet "${SERVICE_NAME}.service" \
    && _log "- system unit (${SERVICE_NAME}.service): active" \
    || _log "- system unit (${SERVICE_NAME}.service): inactive"
  systemctl is-enabled --quiet "${SERVICE_NAME}.service" \
    && _log "- system enabled: yes" \
    || _log "- system enabled: no"
  _log "- system main pid: $(systemctl show -p MainPID --value "${SERVICE_NAME}.service" 2>/dev/null || true)"
}

main() {
  _log "==> manager status"

  local manager_port
  manager_port="${MANAGER_PORT:-$(_read_env_var "${ENV_FILE}" "MANAGER_PORT" || true)}"
  manager_port="${manager_port:-8081}"

  _log "==> config"
  _log "- ENV_FILE: ${ENV_FILE} $( [[ -f "${ENV_FILE}" ]] && echo '(present)' || echo '(missing)' )"
  _log "- MANAGER_PORT: ${manager_port}"

  _log "==> systemd"
  if command -v systemctl >/dev/null 2>&1; then
    _show_systemd_status_user
    _show_systemd_status_system
  else
    _log "- systemctl: not available"
  fi

  _log "==> process discovery"
  local port_pids pattern_pids
  port_pids="$(_pids_listening_on_port "${manager_port}" || true)"
  pattern_pids="$(_pids_by_patterns || true)"

  _show_process_details "listening on port ${manager_port}" "${port_pids}"
  _show_process_details "matching known patterns" "${pattern_pids}"
}

main "$@"
