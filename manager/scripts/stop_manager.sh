#!/usr/bin/env bash

# Best-effort stop for Esup-Runner Manager.
# - Prefer systemd unit stop (if installed)
# - Otherwise, stop by listening port and/or known command patterns

set -u

SERVICE_NAME="esup-runner-manager"
WORKDIR="/opt/esup-runner/manager"
ENV_FILE="${WORKDIR}/.env"

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

MANAGER_PORT="${MANAGER_PORT:-$(_read_env_var "${ENV_FILE}" "MANAGER_PORT" || true)}"
MANAGER_PORT="${MANAGER_PORT:-8000}"

_log() {
  echo "$@"
}

_stop_via_systemd() {
  command -v systemctl >/dev/null 2>&1 || return 1

  # If unit file does not exist, bail out quickly.
  systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1 || return 1

  # If it's not active, nothing to do.
  systemctl is-active --quiet "${SERVICE_NAME}.service" || return 1

  _log "==> systemd: stop ${SERVICE_NAME}.service"

  if systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl stop "${SERVICE_NAME}.service"
    return 0
  fi

  _log "==> systemd stop failed (no permission?)"
  return 1
}

_pids_listening_on_port() {
  local port="$1"

  if command -v ss >/dev/null 2>&1; then
    # Extract pid=1234 from: users:(("python",pid=1234,fd=...))
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
    # fuser prints pids on stdout, sometimes with extra spacing
    fuser -n tcp "${port}" 2>/dev/null | tr ' ' '\n' | awk '/^[0-9]+$/' | sort -u
    return 0
  fi

  return 1
}

_pids_by_patterns() {
  # Keep patterns specific to the manager to avoid killing unrelated processes.
  # - uv run esup-runner-manager (systemd / start.sh)
  # - gunicorn app.main:app (production launcher)
  # - launcher.py (dev/prod python entry)
  pgrep -f "uv run esup-runner-manager|gunicorn .*app\.main:app|python(3)? .*launcher\.py" 2>/dev/null || true
}

_kill_and_wait() {
  local signal_name="$1"; shift
  local -a pids=("$@")

  [[ ${#pids[@]} -gt 0 ]] || return 0

  _log "==> send ${signal_name} to: ${pids[*]}"
  kill "-${signal_name}" "${pids[@]}" 2>/dev/null || true

  # Wait up to 10s
  local -a still
  for _ in $(seq 1 20); do
    still=()
    for pid in "${pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        still+=("${pid}")
      fi
    done

    if [[ ${#still[@]} -eq 0 ]]; then
      return 0
    fi

    sleep 0.5
  done

  # Return remaining pids in a global var via echo
  echo "${still[*]}"
  return 2
}

main() {
  _log "==> stop manager"

  if _stop_via_systemd; then
    _log "==> stopped via systemd"
    return 0
  fi

  # Collect candidates
  local -a pids=()
  local port_pids
  port_pids="$(_pids_listening_on_port "${MANAGER_PORT}" || true)"
  if [[ -n "${port_pids}" ]]; then
    # shellcheck disable=SC2206
    pids+=( ${port_pids} )
  fi

  local pattern_pids
  pattern_pids="$(_pids_by_patterns || true)"
  if [[ -n "${pattern_pids}" ]]; then
    # shellcheck disable=SC2206
    pids+=( ${pattern_pids} )
  fi

  # Unique pids
  if [[ ${#pids[@]} -gt 0 ]]; then
    mapfile -t pids < <(printf "%s\n" "${pids[@]}" | awk '/^[0-9]+$/' | sort -u)
  fi

  if [[ ${#pids[@]} -eq 0 ]]; then
    _log "==> aucun process manager trouvé (port=${MANAGER_PORT})"
    return 0
  fi

  local remaining
  remaining="$(_kill_and_wait TERM "${pids[@]}" || true)"

  # If TERM didn't stop everything, hard kill remaining
  if [[ -n "${remaining}" ]]; then
    # shellcheck disable=SC2206
    local -a still=( ${remaining} )
    _log "==> encore actifs après SIGTERM: ${still[*]}"
    _kill_and_wait KILL "${still[@]}" >/dev/null 2>&1 || true
  fi

  _log "==> stop terminé"
}

main "$@"
