#!/usr/bin/env bash

# Restart helper for Esup-Runner Manager.
# - Prefer systemd --user restart when the user unit exists
# - Fallback to legacy system unit restart when needed
# - Otherwise: stop best-effort then start in foreground

set -u

SERVICE_NAME="esup-runner-manager"

_log() {
  echo "$@"
}

_has_user_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl --user cat "${SERVICE_NAME}.service" >/dev/null 2>&1
}

_has_system_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1
}

_restart_via_systemd_user() {
  _log "==> systemd --user: restart ${SERVICE_NAME}.service"

  systemctl --user restart "${SERVICE_NAME}.service" >/dev/null 2>&1
}

_restart_via_systemd_system() {
  _log "==> systemd (system): restart ${SERVICE_NAME}.service"

  if systemctl restart "${SERVICE_NAME}.service" >/dev/null 2>&1; then
    return 0
  fi

  if command -v sudo >/dev/null 2>&1; then
    sudo systemctl restart "${SERVICE_NAME}.service"
    return 0
  fi

  return 1
}

main() {
  _log "==> restart manager"

  if _has_user_systemd_unit; then
    if _restart_via_systemd_user; then
      _log "==> restarted via systemd --user"
      return 0
    fi

    _log "==> systemd --user restart failed"
    return 1
  fi

  if _has_system_systemd_unit; then
    if _restart_via_systemd_system; then
      _log "==> restarted via systemd (system scope)"
      return 0
    fi

    _log "==> systemd (system scope) restart failed (permission?)"
    return 1
  fi

  _log "==> no systemd unit; restarting locally"

  # Stop best-effort then start in foreground (same behavior as `make run`).
  bash scripts/stop_manager.sh
  exec uv run esup-runner-manager
}

main "$@"
