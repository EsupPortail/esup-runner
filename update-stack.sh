#!/usr/bin/env bash

# update-stack.sh - automated "stack update" helper for Esup-Runner.
#
# Purpose:
# - Provide a single entry point for manual and CRON upgrades in monorepo
#   deployments.
# - Keep the update path predictable for both manager and runner components.
#
# Installation detection:
# - manager is considered installed if manager/.env exists.
# - runner is considered installed if runner/.env exists.
#
# High-level workflow:
# 1) Optionally update uv (service/current user only, never root).
# 2) Optionally update sources (git fetch --tags + git pull --ff-only).
# 3) Update installed components:
#    - manager: make init, make sync, then restart service if available.
#    - runner:  make init, make sync variant based on runner mode, then restart service if available.
# 4) Optionally run a post-update smoke test with
#    manager/scripts/example_async_client.py.
# 5) Optionally send an update summary email to MANAGER_EMAIL
#    (using SMTP settings from runner/.env).
#
# Typical weekly CRON example (Monday at 03:00):
#   0 3 * * 1 cd /opt/esup-runner && ./update-stack.sh >> /var/log/esup-runner/update-stack.log 2>&1

set -u
set -o pipefail

DEFAULT_REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DEFAULT_REPO_ROOT}"

RUN_UV_UPDATE=1
RUN_GIT_UPDATE=1
RUN_TEST=1
RUN_EMAIL=1
USE_SUDO=1
DRY_RUN=0
RUN_INIT=0
RESTART_POLICY="if-changed"
SLEEP_BEFORE_TEST=20
RUNNER_SYNC_MODE="auto"
GPU_LOCK_PROFILE="none"
TARGET_SCOPE="auto"

UPDATED_MANAGER=0
UPDATED_RUNNER=0
TEST_STATUS="skipped"
EMAIL_STATUS="skipped"
EXIT_CODE=0
MANAGER_RESTART_REQUIRED=0
RUNNER_RESTART_REQUIRED=0

MANAGER_DIR=""
RUNNER_DIR=""
MANAGER_ENV_FILE=""
RUNNER_ENV_FILE=""
SERVICE_USER_HINT="${ESUP_RUNNER_USER:-esup-runner}"

usage() {
  cat <<'USAGE'
Usage:
  ./update-stack.sh [options]

Options:
  --root-dir <path>                 Repository root (default: script directory)
  --manager-only                    Update only manager
  --runner-only                     Update only runner
  --runner-sync-mode <mode>         auto|base|transcription-cpu|transcription-gpu
  --gpu-lock-profile <profile>      none|cuda12|latest (only for transcription-gpu)
  --sleep-before-test <seconds>     Delay before example_async_client test (default: 20)
  --skip-uv-update                  Skip uv installer update
  --skip-git-update                 Skip git fetch/pull
  --with-init                       Run make init for updated components
  --restart-policy <policy>         if-changed|always|never (default: if-changed)
  --always-restart                  Shortcut for --restart-policy always
  --no-restart                      Shortcut for --restart-policy never
  --skip-test                       Skip post-update example_async_client test
  --skip-email                      Skip email notification
  --no-sudo                         Do not use sudo even when needed
  --dry-run                         Print commands without executing them
  -h, --help                        Show this help

Detection:
  - manager is considered installed if <root>/manager/.env exists
  - runner is considered installed if <root>/runner/.env exists

Runner sync mode:
  - auto: if RUNNER_TASK_TYPES does not contain "transcription" -> base
          otherwise transcription-cpu or transcription-gpu is inferred
          (GPU inferred from ENCODING_TYPE=GPU or nvidia-smi presence)
USAGE
}

timestamp() {
  date "+%Y-%m-%d %H:%M:%S"
}

log() {
  printf '[%s] %s\n' "$(timestamp)" "$*"
}

warn() {
  printf '[%s] WARNING: %s\n' "$(timestamp)" "$*" >&2
}

die() {
  printf '[%s] ERROR: %s\n' "$(timestamp)" "$*" >&2
  exit 1
}

mark_non_fatal_failure() {
  # Preserve a non-zero exit code while continuing the remaining workflow.
  EXIT_CODE=1
}

print_dry_run_command() {
  # Render a command in shell-escaped form for dry-run logs.
  local prefix="$1"
  shift
  printf '%s' "${prefix}"
  printf ' %q' "$@"
  printf '\n'
}

run_checked() {
  # Run a command and fail fast on error.
  local description="$1"
  shift
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    print_dry_run_command "DRY-RUN:" "$@"
    return 0
  fi
  "$@" || die "Command failed (${description})"
}

run_checked_in_dir() {
  # Run a command in a specific directory and fail fast on error.
  local directory="$1"
  shift
  local description="$1"
  shift
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN: (cd %q &&' "${directory}"
    printf ' %q' "$@"
    printf ')\n'
    return 0
  fi
  (
    cd "${directory}" || exit 1
    "$@"
  ) || die "Command failed (${description})"
}

run_shell_checked() {
  # Run a shell pipeline/compound command and fail fast on error.
  local description="$1"
  local shell_cmd="$2"
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN: %s\n' "${shell_cmd}"
    return 0
  fi
  bash -lc "${shell_cmd}" || die "Command failed (${description})"
}

run_shell_checked_in_dir() {
  # Run a shell command in a specific directory and fail fast on error.
  local directory="$1"
  local description="$2"
  local shell_cmd="$3"
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN: (cd %q && %s)\n' "${directory}" "${shell_cmd}"
    return 0
  fi
  (
    cd "${directory}" || exit 1
    bash -lc "${shell_cmd}"
  ) || die "Command failed (${description})"
}

path_changed_between_revs() {
  # Return success when a path changed between two revisions.
  local old_rev="$1"
  local new_rev="$2"
  local target_path="$3"

  git -C "${REPO_ROOT}" diff --quiet "${old_rev}" "${new_rev}" -- "${target_path}"
  [[ $? -ne 0 ]]
}

run_checked_as_root() {
  # Run a privileged command (root or sudo) and fail fast on error.
  local description="$1"
  shift
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    print_dry_run_command "DRY-RUN (root):" "$@"
    return 0
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    "$@" || die "Command failed (${description})"
    return 0
  fi
  if [[ "${USE_SUDO}" -eq 1 ]] && command -v sudo >/dev/null 2>&1; then
    sudo "$@" || die "Command failed (${description})"
    return 0
  fi
  die "Root privileges required for: ${description}"
}

run_checked_as_root_in_dir() {
  # Run a privileged command inside a directory (root or sudo).
  local directory="$1"
  shift
  local description="$1"
  shift
  log "==> ${description}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN (root): (cd %q &&' "${directory}"
    printf ' %q' "$@"
    printf ')\n'
    return 0
  fi
  if [[ "${EUID}" -eq 0 ]]; then
    (
      cd "${directory}" || exit 1
      "$@"
    ) || die "Command failed (${description})"
    return 0
  fi
  if [[ "${USE_SUDO}" -eq 1 ]] && command -v sudo >/dev/null 2>&1; then
    (
      cd "${directory}" || exit 1
      sudo "$@"
    ) || die "Command failed (${description})"
    return 0
  fi
  die "Root privileges required for: ${description}"
}

read_env_var() {
  # Read one variable from a .env-like file without sourcing it.
  local env_file="$1"
  local var_name="$2"

  [[ -f "${env_file}" ]] || return 1

  awk -F= -v key="${var_name}" '
    $0 ~ /^[[:space:]]*#/ { next }
    $0 ~ /^[[:space:]]*$/ { next }
    {
      k = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k == key) {
        val = substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
        if (val ~ /^".*"$/) {
          sub(/^"/, "", val)
          sub(/"$/, "", val)
        } else if (val ~ /^\x27.*\x27$/) {
          sub(/^\x27/, "", val)
          sub(/\x27$/, "", val)
        }
        print val
        exit
      }
    }
  ' "${env_file}"
}

read_first_authorized_token() {
  # Pick the first AUTHORIZED_TOKENS__* entry from manager/.env.
  local env_file="$1"

  [[ -f "${env_file}" ]] || return 1

  awk -F= '
    $0 ~ /^[[:space:]]*#/ { next }
    $0 ~ /^[[:space:]]*$/ { next }
    {
      k = $1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", k)
      if (k ~ /^AUTHORIZED_TOKENS__/) {
        val = substr($0, index($0, "=") + 1)
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", val)
        if (val ~ /^".*"$/) {
          sub(/^"/, "", val)
          sub(/"$/, "", val)
        } else if (val ~ /^\x27.*\x27$/) {
          sub(/^\x27/, "", val)
          sub(/\x27$/, "", val)
        }
        print val
        exit
      }
    }
  ' "${env_file}"
}

validate_integer() {
  local value="$1"
  [[ "${value}" =~ ^[0-9]+$ ]]
}

mask_secret() {
  # Keep only the first/last 4 chars when logging secrets in dry-run mode.
  local value="${1:-}"
  local value_len="${#value}"

  if [[ "${value_len}" -le 8 ]]; then
    printf '%s\n' "***"
    return 0
  fi

  local prefix="${value:0:4}"
  local suffix="${value:value_len-4:4}"
  printf '%s***%s\n' "${prefix}" "${suffix}"
}

acquire_lock() {
  # Prevent concurrent executions (useful with cron).
  local lock_file="/tmp/esup-runner-update.lock"
  if ! command -v flock >/dev/null 2>&1; then
    warn "flock unavailable: lock protection disabled."
    return 0
  fi
  exec 9>"${lock_file}" || die "Cannot open lock file: ${lock_file}"
  flock -n 9 || die "Another update is already running (lock: ${lock_file})"
}

resolve_service_user() {
  local configured_user repo_owner current_user

  configured_user="$(read_env_var "${MANAGER_ENV_FILE}" "SERVICE_USER" || true)"
  if [[ -n "${configured_user}" ]] && id -u "${configured_user}" >/dev/null 2>&1; then
    printf '%s\n' "${configured_user}"
    return 0
  fi

  if id -u "${SERVICE_USER_HINT}" >/dev/null 2>&1; then
    printf '%s\n' "${SERVICE_USER_HINT}"
    return 0
  fi

  repo_owner="$(stat -c '%U' "${REPO_ROOT}" 2>/dev/null || true)"
  if [[ -n "${repo_owner}" ]] && id -u "${repo_owner}" >/dev/null 2>&1; then
    printf '%s\n' "${repo_owner}"
    return 0
  fi

  current_user="$(id -un 2>/dev/null || true)"
  if [[ -n "${current_user}" ]]; then
    printf '%s\n' "${current_user}"
    return 0
  fi

  return 1
}

run_systemctl_user() {
  local target_user="$1"
  shift
  local target_uid runtime_dir current_user

  target_uid="$(id -u "${target_user}" 2>/dev/null || true)"
  [[ -n "${target_uid}" ]] || return 1

  runtime_dir="/run/user/${target_uid}"
  [[ -d "${runtime_dir}" ]] || return 1

  current_user="$(id -un 2>/dev/null || true)"
  if [[ "${current_user}" == "${target_user}" ]]; then
    env XDG_RUNTIME_DIR="${runtime_dir}" systemctl --user "$@"
    return $?
  fi

  if [[ "${EUID}" -eq 0 ]] && command -v runuser >/dev/null 2>&1; then
    runuser -u "${target_user}" -- env XDG_RUNTIME_DIR="${runtime_dir}" systemctl --user "$@"
    return $?
  fi

  if [[ "${USE_SUDO}" -eq 1 ]] && command -v sudo >/dev/null 2>&1; then
    sudo -u "${target_user}" env XDG_RUNTIME_DIR="${runtime_dir}" systemctl --user "$@"
    return $?
  fi

  return 1
}

restart_service_if_present() {
  # Restart user units first (systemd --user), then fallback to legacy system units.
  local service_name="$1"
  local service_user service_uid service_home user_unit_path

  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not available: skip restart for ${service_name}."
    return 0
  fi

  service_user="$(resolve_service_user || true)"
  if [[ -n "${service_user}" ]] && run_systemctl_user "${service_user}" cat "${service_name}.service" >/dev/null 2>&1; then
    log "==> Restart ${service_name}.service via systemd --user (${service_user})"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      service_uid="$(id -u "${service_user}" 2>/dev/null || true)"
      print_dry_run_command "DRY-RUN:" env "XDG_RUNTIME_DIR=/run/user/${service_uid}" systemctl --user restart "${service_name}.service"
      return 0
    fi
    run_systemctl_user "${service_user}" restart "${service_name}.service" \
      || die "Command failed (Restart ${service_name}.service via systemd --user)"
    return 0
  fi

  if [[ -n "${service_user}" ]]; then
    service_home="$(getent passwd "${service_user}" | awk -F: 'NR==1 {print $6}')"
    user_unit_path="${service_home}/.config/systemd/user/${service_name}.service"
    if [[ -n "${service_home}" && -f "${user_unit_path}" ]]; then
      warn "Found ${user_unit_path} but could not reach systemd --user manager for ${service_user}."
      warn "Hint: enable lingering once with: sudo loginctl enable-linger ${service_user}"
      return 0
    fi
  fi

  if ! systemctl cat "${service_name}.service" >/dev/null 2>&1; then
    warn "Service ${service_name}.service not installed: restart skipped."
    return 0
  fi

  run_checked_as_root "Restart ${service_name}.service" systemctl restart "${service_name}.service"
}

update_uv() {
  local current_user target_user installer_cmd

  installer_cmd="curl -LsSf https://astral.sh/uv/install.sh | sh"
  current_user="$(id -un 2>/dev/null || true)"

  if [[ "${EUID}" -ne 0 ]]; then
    run_shell_checked "Update uv for current user (${current_user})" "${installer_cmd}"
    return 0
  fi

  target_user="$(resolve_service_user || true)"
  if [[ -z "${target_user}" || "${target_user}" == "root" ]]; then
    warn "Skipping uv update: no non-root service user could be resolved."
    warn "Hint: set SERVICE_USER in manager/.env or ESUP_RUNNER_USER (for example: esup-runner)."
    return 0
  fi

  if [[ "${target_user}" == "${current_user}" ]]; then
    run_shell_checked "Update uv for service user (${target_user})" "${installer_cmd}"
    return 0
  fi

  if command -v runuser >/dev/null 2>&1; then
    log "==> Update uv for service user (${target_user})"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      print_dry_run_command "DRY-RUN:" runuser -u "${target_user}" -- bash -lc "${installer_cmd}"
      return 0
    fi
    runuser -u "${target_user}" -- bash -lc "${installer_cmd}" \
      || die "Command failed (Update uv for service user ${target_user})"
    return 0
  fi

  if [[ "${USE_SUDO}" -eq 1 ]] && command -v sudo >/dev/null 2>&1; then
    log "==> Update uv for service user (${target_user})"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      print_dry_run_command "DRY-RUN:" sudo -u "${target_user}" bash -lc "${installer_cmd}"
      return 0
    fi
    sudo -u "${target_user}" bash -lc "${installer_cmd}" \
      || die "Command failed (Update uv for service user ${target_user})"
    return 0
  fi

  warn "Skipping uv update for ${target_user}: cannot switch user (runuser/sudo unavailable)."
}

update_git_sources() {
  local before_rev after_rev

  before_rev="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"
  run_checked_in_dir "${REPO_ROOT}" "git fetch --tags" git fetch --tags
  run_checked_in_dir "${REPO_ROOT}" "git pull --ff-only" git pull --ff-only
  after_rev="$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || true)"

  if [[ -z "${before_rev}" || -z "${after_rev}" ]]; then
    warn "Unable to read git revisions, restart decision will stay conservative."
    return 0
  fi

  if [[ "${before_rev}" == "${after_rev}" ]]; then
    log "Git revision unchanged (${after_rev})."
    return 0
  fi

  log "Git revision changed: ${before_rev} -> ${after_rev}"

  if path_changed_between_revs "${before_rev}" "${after_rev}" "manager"; then
    MANAGER_RESTART_REQUIRED=1
    log "Detected changes under manager/."
  fi

  if path_changed_between_revs "${before_rev}" "${after_rev}" "runner"; then
    RUNNER_RESTART_REQUIRED=1
    log "Detected changes under runner/."
  fi
}

infer_runner_sync_mode() {
  # Resolution order:
  # 1) CLI (--runner-sync-mode)
  # 2) Optional RUNNER_SYNC_MODE in runner/.env
  # 3) Heuristic from RUNNER_TASK_TYPES + GPU hints
  if [[ "${RUNNER_SYNC_MODE}" != "auto" ]]; then
    printf '%s\n' "${RUNNER_SYNC_MODE}"
    return 0
  fi

  local configured_mode
  configured_mode="$(read_env_var "${RUNNER_ENV_FILE}" "RUNNER_SYNC_MODE" || true)"
  case "${configured_mode}" in
    base|transcription-cpu|transcription-gpu)
      log "Runner sync mode forced by RUNNER_SYNC_MODE=${configured_mode} in .env"
      printf '%s\n' "${configured_mode}"
      return 0
      ;;
  esac

  local runner_task_types
  runner_task_types="$(read_env_var "${RUNNER_ENV_FILE}" "RUNNER_TASK_TYPES" || true)"
  if ! printf '%s\n' "${runner_task_types}" | grep -qi "transcription"; then
    printf '%s\n' "base"
    return 0
  fi

  local encoding_type
  encoding_type="$(read_env_var "${RUNNER_ENV_FILE}" "ENCODING_TYPE" || true)"
  encoding_type="$(printf '%s' "${encoding_type}" | tr '[:lower:]' '[:upper:]')"
  if [[ "${encoding_type}" == "GPU" ]]; then
    printf '%s\n' "transcription-gpu"
    return 0
  fi

  if command -v nvidia-smi >/dev/null 2>&1; then
    printf '%s\n' "transcription-gpu"
    return 0
  fi

  printf '%s\n' "transcription-cpu"
}

update_manager() {
  # Manager update flow: optional init, sync deps, conditional service restart.
  if [[ "${RUN_INIT}" -eq 1 ]]; then
    run_checked_as_root_in_dir "${MANAGER_DIR}" "Manager init (make init)" make init
  else
    log "Manager init skipped (enable with --with-init)."
  fi

  run_checked_in_dir "${MANAGER_DIR}" "Manager dependency sync (make sync)" make sync

  case "${RESTART_POLICY}" in
    always)
      restart_service_if_present "esup-runner-manager"
      ;;
    if-changed)
      if [[ "${MANAGER_RESTART_REQUIRED}" -eq 1 ]]; then
        restart_service_if_present "esup-runner-manager"
      else
        log "Manager restart skipped (no change detected under manager/)."
      fi
      ;;
    never)
      log "Manager restart skipped (--restart-policy never)."
      ;;
  esac

  UPDATED_MANAGER=1
}

update_runner() {
  # Runner update flow with mode-specific dependency sync and conditional restart.
  local mode="$1"

  if [[ "${RUN_INIT}" -eq 1 ]]; then
    run_checked_as_root_in_dir "${RUNNER_DIR}" "Runner init (make init)" make init
  else
    log "Runner init skipped (enable with --with-init)."
  fi

  case "${mode}" in
    base)
      run_checked_in_dir "${RUNNER_DIR}" "Runner dependency sync (make sync)" make sync
      ;;
    transcription-cpu)
      run_checked_in_dir "${RUNNER_DIR}" "Runner dependency sync (make sync-transcription-cpu)" make sync-transcription-cpu
      ;;
    transcription-gpu)
      case "${GPU_LOCK_PROFILE}" in
        cuda12)
          log "GPU lock profile: cuda12 (make lock-upgrade-gpu-12)"
          run_checked_in_dir "${RUNNER_DIR}" "Runner lock refresh for CUDA12" make lock-upgrade-gpu-12
          RUNNER_RESTART_REQUIRED=1
          ;;
        latest)
          log "GPU lock profile: latest (make lock-upgrade-gpu-latest)"
          run_checked_in_dir "${RUNNER_DIR}" "Runner lock refresh for latest GPU stack" make lock-upgrade-gpu-latest
          RUNNER_RESTART_REQUIRED=1
          ;;
        none)
          :
          ;;
      esac
      run_checked_in_dir "${RUNNER_DIR}" "Runner dependency sync (make sync-transcription-gpu)" make sync-transcription-gpu
      ;;
    *)
      die "Unsupported runner sync mode: ${mode}"
      ;;
  esac

  case "${RESTART_POLICY}" in
    always)
      restart_service_if_present "esup-runner-runner"
      ;;
    if-changed)
      if [[ "${RUNNER_RESTART_REQUIRED}" -eq 1 ]]; then
        restart_service_if_present "esup-runner-runner"
      else
        log "Runner restart skipped (no change detected under runner/)."
      fi
      ;;
    never)
      log "Runner restart skipped (--restart-policy never)."
      ;;
  esac

  UPDATED_RUNNER=1
}

build_manager_url_from_manager_env() {
  # Build a local manager URL fallback when runner/.env MANAGER_URL is missing.
  local manager_protocol manager_host manager_port
  manager_protocol="$(read_env_var "${MANAGER_ENV_FILE}" "MANAGER_PROTOCOL" || true)"
  manager_host="$(read_env_var "${MANAGER_ENV_FILE}" "MANAGER_HOST" || true)"
  manager_port="$(read_env_var "${MANAGER_ENV_FILE}" "MANAGER_PORT" || true)"

  manager_protocol="${manager_protocol:-http}"
  manager_host="${manager_host:-127.0.0.1}"
  manager_port="${manager_port:-8081}"

  if [[ "${manager_host}" == "0.0.0.0" ]]; then
    manager_host="127.0.0.1"
  fi

  printf '%s://%s:%s\n' "${manager_protocol}" "${manager_host}" "${manager_port}"
}

run_post_update_test() {
  # Optional smoke test through manager/scripts/example_async_client.py.
  if [[ "${RUN_TEST}" -ne 1 ]]; then
    TEST_STATUS="disabled"
    return 0
  fi

  if [[ ! -f "${MANAGER_ENV_FILE}" ]]; then
    TEST_STATUS="skipped"
    warn "Manager .env not found: example_async_client test skipped."
    return 0
  fi

  local token manager_url

  token="$(read_env_var "${RUNNER_ENV_FILE}" "RUNNER_TOKEN" || true)"
  if [[ -z "${token}" ]]; then
    token="$(read_first_authorized_token "${MANAGER_ENV_FILE}" || true)"
  fi

  manager_url="$(read_env_var "${RUNNER_ENV_FILE}" "MANAGER_URL" || true)"
  if [[ -z "${manager_url}" ]]; then
    manager_url="$(build_manager_url_from_manager_env)"
  fi

  if [[ -z "${token}" || -z "${manager_url}" ]]; then
    TEST_STATUS="skipped"
    warn "Missing token or manager URL: example_async_client test skipped."
    return 0
  fi

  if [[ "${SLEEP_BEFORE_TEST}" -gt 0 ]]; then
    log "Waiting ${SLEEP_BEFORE_TEST}s before post-update test."
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      sleep "${SLEEP_BEFORE_TEST}"
    fi
  fi

  log "==> Post-update encoding test via manager/scripts/example_async_client.py"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    local masked_token
    masked_token="$(mask_secret "${token}")"
    printf 'DRY-RUN: (cd %q && RUNNER_API_TOKEN=%s RUNNER_MANAGER_URL=%q uv run scripts/example_async_client.py)\n' \
      "${MANAGER_DIR}" "${masked_token}" "${manager_url}"
    TEST_STATUS="dry-run"
    return 0
  fi

  if (
    cd "${MANAGER_DIR}" || exit 1
    env RUNNER_API_TOKEN="${token}" RUNNER_MANAGER_URL="${manager_url}" uv run scripts/example_async_client.py
  ); then
    TEST_STATUS="ok"
  else
    TEST_STATUS="failed"
    warn "Post-update test failed."
    mark_non_fatal_failure
  fi
}

send_update_email_if_configured() {
  # Optional summary email sent to MANAGER_EMAIL using runner SMTP settings.
  if [[ "${RUN_EMAIL}" -ne 1 ]]; then
    EMAIL_STATUS="disabled"
    return 0
  fi

  if [[ ! -f "${RUNNER_ENV_FILE}" ]]; then
    EMAIL_STATUS="skipped"
    warn "Runner .env not found: update email skipped."
    return 0
  fi

  local manager_email smtp_server smtp_port smtp_use_tls smtp_username smtp_password smtp_sender
  manager_email="$(read_env_var "${RUNNER_ENV_FILE}" "MANAGER_EMAIL" || true)"
  smtp_server="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_SERVER" || true)"
  smtp_port="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_PORT" || true)"
  smtp_use_tls="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_USE_TLS" || true)"
  smtp_username="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_USERNAME" || true)"
  smtp_password="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_PASSWORD" || true)"
  smtp_sender="$(read_env_var "${RUNNER_ENV_FILE}" "SMTP_SENDER" || true)"

  if [[ -z "${manager_email}" ]]; then
    EMAIL_STATUS="skipped"
    warn "MANAGER_EMAIL is not configured in runner/.env: email skipped."
    return 0
  fi

  if [[ -z "${smtp_server}" ]]; then
    EMAIL_STATUS="skipped"
    warn "SMTP_SERVER is not configured in runner/.env: email skipped."
    return 0
  fi

  smtp_port="${smtp_port:-25}"

  local updated_parts=()
  if [[ "${UPDATED_MANAGER}" -eq 1 ]]; then
    updated_parts+=("manager")
  fi
  if [[ "${UPDATED_RUNNER}" -eq 1 ]]; then
    updated_parts+=("runner")
  fi

  local updated_label
  if [[ "${#updated_parts[@]}" -eq 0 ]]; then
    updated_label="none"
  else
    updated_label="$(IFS=','; printf '%s' "${updated_parts[*]}")"
  fi

  local git_revision hostname_value date_value subject body
  git_revision="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  hostname_value="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo "unknown-host")"
  date_value="$(date -Is 2>/dev/null || date)"
  subject="[esup-runner] Mise a jour terminee (${updated_label})"
  body="Bonjour,

La mise a jour automatique ESUP Runner est terminee.

Composants mis a jour: ${updated_label}
Revision git: ${git_revision}
Date: ${date_value}
Machine: ${hostname_value}
Test post-maj: ${TEST_STATUS}

Cordialement,"

  log "==> Sending update email to ${manager_email}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN: send email to %s via %s:%s\n' "${manager_email}" "${smtp_server}" "${smtp_port}"
    EMAIL_STATUS="dry-run"
    return 0
  fi

  if env \
    SMTP_SERVER="${smtp_server}" \
    SMTP_PORT="${smtp_port}" \
    SMTP_USE_TLS="${smtp_use_tls}" \
    SMTP_USERNAME="${smtp_username}" \
    SMTP_PASSWORD="${smtp_password}" \
    SMTP_SENDER="${smtp_sender}" \
    MANAGER_EMAIL="${manager_email}" \
    EMAIL_SUBJECT="${subject}" \
    EMAIL_BODY="${body}" \
    python3 - <<'PY'
import os
import smtplib
import sys
from email.message import EmailMessage


def as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


smtp_server = os.environ.get("SMTP_SERVER", "").strip()
smtp_port_raw = os.environ.get("SMTP_PORT", "25").strip()
smtp_use_tls = as_bool(os.environ.get("SMTP_USE_TLS", "false"))
smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
smtp_password = os.environ.get("SMTP_PASSWORD", "")
smtp_sender = os.environ.get("SMTP_SENDER", "").strip()
manager_email = os.environ.get("MANAGER_EMAIL", "").strip()
subject = os.environ.get("EMAIL_SUBJECT", "").strip()
body = os.environ.get("EMAIL_BODY", "")

if not smtp_server or not manager_email:
    print("SMTP_SERVER and MANAGER_EMAIL are required.", file=sys.stderr)
    sys.exit(1)

if not smtp_sender:
    if "@" in manager_email:
        smtp_sender = "esup-runner@" + manager_email.split("@", 1)[1]
    else:
        smtp_sender = "esup-runner@localhost"

try:
    smtp_port = int(smtp_port_raw)
except ValueError:
    smtp_port = 25

msg = EmailMessage()
msg["Subject"] = subject or "[esup-runner] Mise a jour terminee"
msg["From"] = smtp_sender
msg["To"] = manager_email
msg.set_content(body)

with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as smtp:
    if smtp_use_tls:
        smtp.starttls()
    if smtp_username and smtp_password:
        smtp.login(smtp_username, smtp_password)
    smtp.send_message(msg)
PY
  then
    EMAIL_STATUS="sent"
  else
    EMAIL_STATUS="failed"
    warn "Unable to send update email."
    mark_non_fatal_failure
  fi
}

parse_args() {
  # Parse CLI flags and update global runtime options.
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --root-dir)
        [[ $# -ge 2 ]] || die "Missing value for --root-dir"
        REPO_ROOT="$2"
        shift 2
        ;;
      --manager-only)
        TARGET_SCOPE="manager"
        shift
        ;;
      --runner-only)
        TARGET_SCOPE="runner"
        shift
        ;;
      --runner-sync-mode)
        [[ $# -ge 2 ]] || die "Missing value for --runner-sync-mode"
        RUNNER_SYNC_MODE="$2"
        shift 2
        ;;
      --gpu-lock-profile)
        [[ $# -ge 2 ]] || die "Missing value for --gpu-lock-profile"
        GPU_LOCK_PROFILE="$2"
        shift 2
        ;;
      --sleep-before-test)
        [[ $# -ge 2 ]] || die "Missing value for --sleep-before-test"
        SLEEP_BEFORE_TEST="$2"
        shift 2
        ;;
      --skip-uv-update)
        RUN_UV_UPDATE=0
        shift
        ;;
      --skip-git-update)
        RUN_GIT_UPDATE=0
        shift
        ;;
      --with-init)
        RUN_INIT=1
        shift
        ;;
      --restart-policy)
        [[ $# -ge 2 ]] || die "Missing value for --restart-policy"
        RESTART_POLICY="$2"
        shift 2
        ;;
      --always-restart)
        RESTART_POLICY="always"
        shift
        ;;
      --no-restart)
        RESTART_POLICY="never"
        shift
        ;;
      --skip-test)
        RUN_TEST=0
        shift
        ;;
      --skip-email)
        RUN_EMAIL=0
        shift
        ;;
      --no-sudo)
        USE_SUDO=0
        shift
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
  done
}

validate_inputs() {
  # Validate parsed options early before running any update step.
  case "${RUNNER_SYNC_MODE}" in
    auto|base|transcription-cpu|transcription-gpu) ;;
    *)
      die "Invalid --runner-sync-mode: ${RUNNER_SYNC_MODE}"
      ;;
  esac

  case "${GPU_LOCK_PROFILE}" in
    none|cuda12|latest) ;;
    *)
      die "Invalid --gpu-lock-profile: ${GPU_LOCK_PROFILE}"
      ;;
  esac

  case "${TARGET_SCOPE}" in
    auto|manager|runner) ;;
    *)
      die "Invalid scope"
      ;;
  esac

  case "${RESTART_POLICY}" in
    if-changed|always|never) ;;
    *)
      die "Invalid --restart-policy: ${RESTART_POLICY}"
      ;;
  esac

  validate_integer "${SLEEP_BEFORE_TEST}" || die "--sleep-before-test must be an integer >= 0"
}

main() {
  # Orchestrate full update lifecycle for detected/selected components.
  parse_args "$@"
  validate_inputs

  REPO_ROOT="$(cd "${REPO_ROOT}" 2>/dev/null && pwd)" || die "Invalid --root-dir: ${REPO_ROOT}"
  MANAGER_DIR="${REPO_ROOT}/manager"
  RUNNER_DIR="${REPO_ROOT}/runner"
  MANAGER_ENV_FILE="${MANAGER_DIR}/.env"
  RUNNER_ENV_FILE="${RUNNER_DIR}/.env"

  git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "Not a git repository root: ${REPO_ROOT}"

  acquire_lock

  local manager_installed=0
  local runner_installed=0
  local do_manager=0
  local do_runner=0

  [[ -f "${MANAGER_ENV_FILE}" ]] && manager_installed=1
  [[ -f "${RUNNER_ENV_FILE}" ]] && runner_installed=1

  case "${TARGET_SCOPE}" in
    manager)
      do_manager=1
      do_runner=0
      ;;
    runner)
      do_manager=0
      do_runner=1
      ;;
    auto)
      do_manager="${manager_installed}"
      do_runner="${runner_installed}"
      ;;
  esac

  if [[ "${do_manager}" -eq 1 && "${manager_installed}" -ne 1 ]]; then
    die "manager/.env not found: manager is not detected as installed."
  fi

  if [[ "${do_runner}" -eq 1 && "${runner_installed}" -ne 1 ]]; then
    die "runner/.env not found: runner is not detected as installed."
  fi

  if [[ "${do_manager}" -ne 1 && "${do_runner}" -ne 1 ]]; then
    die "No installed component detected (.env missing in manager/ and runner/)."
  fi

  log "Repository root: ${REPO_ROOT}"
  log "Detected installation: manager=${manager_installed}, runner=${runner_installed}"
  log "Update selection: manager=${do_manager}, runner=${do_runner}"

  if [[ "${RUN_UV_UPDATE}" -eq 1 ]]; then
    update_uv
  else
    log "uv update skipped (--skip-uv-update)"
  fi

  if [[ "${RUN_GIT_UPDATE}" -eq 1 ]]; then
    update_git_sources
  else
    log "git update skipped (--skip-git-update)"
  fi

  if [[ "${do_manager}" -eq 1 ]]; then
    update_manager
  fi

  if [[ "${do_runner}" -eq 1 ]]; then
    local effective_runner_mode
    effective_runner_mode="$(infer_runner_sync_mode)"
    log "Runner sync mode: ${effective_runner_mode}"
    update_runner "${effective_runner_mode}"
  fi

  run_post_update_test
  send_update_email_if_configured

  log "Update summary: manager=${UPDATED_MANAGER}, runner=${UPDATED_RUNNER}, test=${TEST_STATUS}, email=${EMAIL_STATUS}"
  exit "${EXIT_CODE}"
}

main "$@"
