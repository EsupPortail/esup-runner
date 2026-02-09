#!/usr/bin/env bash

# Restart helper for Esup-Runner Manager.
# - Prefer systemd restart when the unit exists
# - Otherwise: stop best-effort then start in foreground

set -u

SERVICE_NAME="esup-runner-manager"

_log() {
  echo "$@"
}

_has_systemd_unit() {
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1
}

_restart_via_systemd() {
  _log "==> systemd: restart ${SERVICE_NAME}.service"

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

  if _has_systemd_unit; then
    if _restart_via_systemd; then
      _log "==> restarted via systemd"
      return 0
    fi

    _log "==> systemd restart failed (permission?)"
    return 1
  fi

  _log "==> no systemd unit; restarting locally"

  # Stop best-effort then start in foreground (same behavior as `make run`).
  bash scripts/stop_manager.sh
  exec uv run esup-runner-manager
}

main "$@"
