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
# 3) If GPU lock refresh is requested (make lock-upgrade-gpu-**), manage runner/uv.lock:
#    - restore tracked runner/uv.lock before git pull (avoid local lock conflicts),
#    - then regenerate it during runner update.
# 4) Update installed components:
#    - manager: make init, make sync, then restart service if available.
#    - runner:  make init, make sync variant based on runner mode, then restart service if available.
# 5) Optionally run a post-update smoke test with
#    manager/scripts/check_pipeline_tasks.py.
#    In transcription-cpu/transcription-gpu modes, the test adds
#    --with-transcription-translation.
# 6) Optionally send an update summary email to MANAGER_EMAIL
#    (using SMTP settings from runner/.env).
#
# Concrete usage examples:
# - Unless --manager-only/--runner-only is provided, commands below update
#   both manager and runner when they are detected as installed.
# 1) Dry-run (preview only, no command execution):
#    cd /opt/esup-runner && ./update-stack.sh --dry-run --skip-uv-update --skip-git-update
# 2) Runner transcription on CPU (includes --with-transcription-translation in smoke test):
#    cd /opt/esup-runner && ./update-stack.sh --runner-sync-mode transcription-cpu
# 3) Runner transcription on GPU, standard profile (current lock stack):
#    cd /opt/esup-runner && ./update-stack.sh --runner-sync-mode transcription-gpu
# 4) Runner transcription on GPU, CUDA12 legacy profile (lock refresh with make lock-upgrade-gpu-12):
#    cd /opt/esup-runner && ./update-stack.sh --runner-sync-mode transcription-gpu --gpu-lock-profile cuda12
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
SEND_TEST_EMAIL=0
USE_SUDO=1
DRY_RUN=0
RUN_INIT=0
RESTART_POLICY="if-changed"
# Internal delay before smoke test (kept fixed on purpose, no CLI flag).
POST_UPDATE_TEST_DELAY_SECONDS=20
RUNNER_SYNC_MODE="auto"
GPU_LOCK_PROFILE="none"
TARGET_SCOPE="auto"
# Add full transcription+translation checks in smoke test when transcription mode is targeted.
RUN_TEST_WITH_TRANSCRIPTION_TRANSLATION=0

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
STEP_COUNTER=0

COLOR_RESET=""
COLOR_BOLD=""
COLOR_DIM=""
COLOR_INFO=""
COLOR_ACCENT=""
COLOR_WARN=""
COLOR_ERROR=""
COLOR_SUCCESS=""

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
  --skip-uv-update                  Skip uv installer update
  --skip-git-update                 Skip git fetch/pull
  --with-init                       Run make init for updated components
  --restart-policy <policy>         if-changed|always|never (default: if-changed)
  --always-restart                  Shortcut for --restart-policy always
  --no-restart                      Shortcut for --restart-policy never
  --skip-test                       Skip post-update check_pipeline_tasks test
  --skip-email                      Skip email notification
  --send-test-email                 Send only a test update email, then exit
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
  - transcription-cpu/transcription-gpu enable full smoke test mode:
    check_pipeline_tasks.py --with-transcription-translation
USAGE
}

init_output_theme() {
  # Enable ANSI colors only for interactive terminals (or when explicitly forced).
  local enable_color=0

  if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-}" != "dumb" ]]; then
    enable_color=1
  fi

  if [[ "${FORCE_COLOR:-0}" == "1" || "${CLICOLOR_FORCE:-0}" == "1" ]]; then
    enable_color=1
  fi

  if [[ "${enable_color}" -eq 1 ]]; then
    COLOR_RESET=$'\033[0m'
    COLOR_BOLD=$'\033[1m'
    COLOR_DIM=$'\033[2m'
    COLOR_INFO=$'\033[36m'
    COLOR_ACCENT=$'\033[34m'
    COLOR_WARN=$'\033[33m'
    COLOR_ERROR=$'\033[31m'
    COLOR_SUCCESS=$'\033[32m'
  fi
}

timestamp() {
  # Build a consistent timestamp prefix for all log lines.
  date "+%Y-%m-%d %H:%M:%S"
}

log() {
  # Print an informational log entry.
  printf '%s[%s]%s %s%s%s %s\n' \
    "${COLOR_DIM}" "$(timestamp)" "${COLOR_RESET}" \
    "${COLOR_INFO}" "[INFO]" "${COLOR_RESET}" "$*"
}

log_action() {
  # Print action-oriented logs (individual command steps).
  printf '%s[%s]%s %s%s%s %s\n' \
    "${COLOR_DIM}" "$(timestamp)" "${COLOR_RESET}" \
    "${COLOR_ACCENT}" "[STEP]" "${COLOR_RESET}" "$*"
}

warn() {
  # Print a warning log entry on stderr.
  printf '%s[%s]%s %s%s%s %s\n' \
    "${COLOR_DIM}" "$(timestamp)" "${COLOR_RESET}" \
    "${COLOR_WARN}" "[WARN]" "${COLOR_RESET}" "$*" >&2
}

die() {
  # Print an error log entry and exit immediately.
  printf '%s[%s]%s %s%s%s %s\n' \
    "${COLOR_DIM}" "$(timestamp)" "${COLOR_RESET}" \
    "${COLOR_ERROR}" "[ERROR]" "${COLOR_RESET}" "$*" >&2
  exit 1
}

print_separator() {
  # Draw a visual separator between major workflow sections.
  printf '%s%s%s\n' "${COLOR_DIM}" "-------------------------------------------------------------------------------" "${COLOR_RESET}"
}

print_step_banner() {
  # Render a numbered banner for each high-level workflow phase.
  local title="$1"

  STEP_COUNTER=$((STEP_COUNTER + 1))
  printf '\n'
  print_separator
  printf '%s%s%s %s\n' "${COLOR_BOLD}${COLOR_ACCENT}" "[Step ${STEP_COUNTER}]" "${COLOR_RESET}" "${title}"
  print_separator
}

render_boolean_status() {
  # Convert internal 0/1 flags to human-readable yes/no values.
  local value="${1:-0}"
  if [[ "${value}" -eq 1 ]]; then
    printf 'yes\n'
  else
    printf 'no\n'
  fi
}

print_summary_line() {
  # Print one summary row with contextual color based on status value.
  local label="$1"
  local value="$2"
  local value_color="${COLOR_INFO}"

  case "${value}" in
    yes|ok|sent|dry-run|0)
      value_color="${COLOR_SUCCESS}"
      ;;
    no|skipped|disabled)
      value_color="${COLOR_WARN}"
      ;;
    failed)
      value_color="${COLOR_ERROR}"
      ;;
  esac

  printf '  %-20s %s%s%s\n' "${label}:" "${value_color}" "${value}" "${COLOR_RESET}"
}

print_update_summary() {
  # Print a compact end-of-run summary for the most important outcomes.
  local manager_status runner_status

  manager_status="$(render_boolean_status "${UPDATED_MANAGER}")"
  runner_status="$(render_boolean_status "${UPDATED_RUNNER}")"

  printf '\n'
  print_separator
  printf '%s%s%s\n' "${COLOR_BOLD}${COLOR_ACCENT}" "Update summary" "${COLOR_RESET}"
  print_separator
  print_summary_line "Manager updated" "${manager_status}"
  print_summary_line "Runner updated" "${runner_status}"
  print_summary_line "Post-update test" "${TEST_STATUS}"
  print_summary_line "Email notification" "${EMAIL_STATUS}"
  if [[ "${EXIT_CODE}" -eq 0 ]]; then
    print_summary_line "Exit code" "0"
  else
    print_summary_line "Exit code" "${EXIT_CODE}"
  fi
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
  log_action "${description}"
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
  log_action "${description}"
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
  log_action "${description}"
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
  log_action "${description}"
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
  log_action "${description}"
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
  log_action "${description}"
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

read_component_version() {
  # Read a component version from <component>/VERSION for notifications.
  local component_dir="$1"
  local version_file="${component_dir}/VERSION"
  local version_value=""

  if [[ ! -f "${version_file}" ]]; then
    printf '%s\n' "unknown"
    return 0
  fi

  version_value="$(awk '
    NR == 1 {
      gsub(/\r/, "", $0)
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0)
      print
      exit
    }
  ' "${version_file}" 2>/dev/null || true)"

  if [[ -z "${version_value}" ]]; then
    printf '%s\n' "unknown"
    return 0
  fi

  printf '%s\n' "${version_value}"
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
  # Resolve the non-root account used for user-level operations (uv/systemd --user).
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
  # Run systemctl --user for a target account with the correct runtime directory.
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
    log_action "Restart ${service_name}.service via systemd --user (${service_user})"
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
      warn "Falling back to system service scope for ${service_name}.service when available."
    fi
  fi

  if ! systemctl cat "${service_name}.service" >/dev/null 2>&1; then
    warn "Service ${service_name}.service not installed: restart skipped."
    return 0
  fi

  run_checked_as_root "Restart ${service_name}.service" systemctl restart "${service_name}.service"
}

update_uv() {
  # Update uv for the current/service user while avoiding root-owned installs.
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
    log_action "Update uv for service user (${target_user})"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
      print_dry_run_command "DRY-RUN:" runuser -u "${target_user}" -- bash -lc "${installer_cmd}"
      return 0
    fi
    runuser -u "${target_user}" -- bash -lc "${installer_cmd}" \
      || die "Command failed (Update uv for service user ${target_user})"
    return 0
  fi

  if [[ "${USE_SUDO}" -eq 1 ]] && command -v sudo >/dev/null 2>&1; then
    log_action "Update uv for service user (${target_user})"
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
  # Refresh git sources and detect which components changed (for restart decisions).
  local before_rev after_rev
  local restore_runner_uv_lock="${1:-0}"

  # When a GPU lock refresh is planned later, ensure we start git pull from a clean
  # tracked lock file to avoid pull failures caused by local runner/uv.lock edits.
  if [[ "${restore_runner_uv_lock}" -eq 1 ]]; then
    if git -C "${REPO_ROOT}" ls-files --error-unmatch runner/uv.lock >/dev/null 2>&1; then
      run_checked_in_dir "${REPO_ROOT}" "Reset runner/uv.lock before git pull" git restore -- runner/uv.lock
    else
      log "runner/uv.lock is not tracked in git: restore step skipped."
    fi
  fi

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
      local gpu_sync_target="sync-transcription-gpu"
      case "${GPU_LOCK_PROFILE}" in
        cuda12)
          # Explicit GPU lock refresh: this updates runner/uv.lock for CUDA12.
          log "GPU lock profile: cuda12 (make lock-upgrade-gpu-12)"
          run_checked_in_dir "${RUNNER_DIR}" "Runner lock refresh for CUDA12" make lock-upgrade-gpu-12
          gpu_sync_target="sync-transcription-gpu-cuda12"
          RUNNER_RESTART_REQUIRED=1
          ;;
        latest)
          # Explicit GPU lock refresh: this updates runner/uv.lock to latest GPU stack.
          log "GPU lock profile: latest (make lock-upgrade-gpu-latest)"
          run_checked_in_dir "${RUNNER_DIR}" "Runner lock refresh for latest GPU stack" make lock-upgrade-gpu-latest
          RUNNER_RESTART_REQUIRED=1
          ;;
        none)
          :
          ;;
      esac
      run_checked_in_dir "${RUNNER_DIR}" "Runner dependency sync (make ${gpu_sync_target})" make "${gpu_sync_target}"
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

normalize_manager_admin_url() {
  # Normalize MANAGER_URL into the admin URL used by update email actions.
  local manager_url="$1"

  while [[ "${manager_url}" == */ ]]; do
    manager_url="${manager_url%/}"
  done

  if [[ -z "${manager_url}" ]]; then
    return 0
  fi

  if [[ "${manager_url}" == */admin ]]; then
    printf '%s\n' "${manager_url}"
  else
    printf '%s/admin\n' "${manager_url}"
  fi
}

resolve_manager_admin_url() {
  # Resolve the best manager admin URL from runner or manager environment files.
  local manager_url=""

  if [[ -f "${RUNNER_ENV_FILE}" ]]; then
    manager_url="$(read_env_var "${RUNNER_ENV_FILE}" "MANAGER_URL" || true)"
  fi

  if [[ -z "${manager_url}" && -f "${MANAGER_ENV_FILE}" ]]; then
    manager_url="$(build_manager_url_from_manager_env)"
  fi

  normalize_manager_admin_url "${manager_url}"
}

run_post_update_test() {
  # Optional smoke test through manager/scripts/check_pipeline_tasks.py
  # with optional full transcription+translation chain.
  if [[ "${RUN_TEST}" -ne 1 ]]; then
    TEST_STATUS="disabled"
    log "Post-update test skipped (--skip-test)."
    return 0
  fi

  if [[ ! -f "${MANAGER_ENV_FILE}" ]]; then
    TEST_STATUS="skipped"
    warn "Manager .env not found: check_pipeline_tasks test skipped."
    return 0
  fi

  local token manager_url
  local test_args=()

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
    warn "Missing token or manager URL: check_pipeline_tasks test skipped."
    return 0
  fi

  if [[ "${RUN_TEST_WITH_TRANSCRIPTION_TRANSLATION}" -eq 1 ]]; then
    test_args+=("--with-transcription-translation")
    log "Post-update test includes transcription+translation chain."
  fi

  if [[ "${POST_UPDATE_TEST_DELAY_SECONDS}" -gt 0 ]]; then
    log "Waiting ${POST_UPDATE_TEST_DELAY_SECONDS}s before post-update test."
    if [[ "${DRY_RUN}" -eq 0 ]]; then
      sleep "${POST_UPDATE_TEST_DELAY_SECONDS}"
    fi
  fi

  log_action "Post-update encoding test via manager/scripts/check_pipeline_tasks.py"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    local masked_token
    masked_token="$(mask_secret "${token}")"
    printf 'DRY-RUN: (cd %q &&' "${MANAGER_DIR}"
    printf ' %q' env \
      "RUNNER_API_TOKEN=${masked_token}" \
      "RUNNER_MANAGER_URL=${manager_url}" \
      uv run scripts/check_pipeline_tasks.py "${test_args[@]}"
    printf ')\n'
    TEST_STATUS="dry-run"
    return 0
  fi

  if (
    cd "${MANAGER_DIR}" || exit 1
    env RUNNER_API_TOKEN="${token}" RUNNER_MANAGER_URL="${manager_url}" \
      uv run scripts/check_pipeline_tasks.py "${test_args[@]}"
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
    log "Email notification skipped (--skip-email)."
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
  if [[ "${SEND_TEST_EMAIL}" -eq 1 ]]; then
    updated_label="preview only"
  elif [[ "${#updated_parts[@]}" -eq 0 ]]; then
    updated_label="none"
  else
    updated_label="$(printf '%s, ' "${updated_parts[@]}")"
    updated_label="${updated_label%, }"
  fi

  local manager_version=""
  local runner_version=""
  local version_lines=""
  local subject_versions=()

  if [[ "${UPDATED_MANAGER}" -eq 1 || ( "${SEND_TEST_EMAIL}" -eq 1 && -d "${MANAGER_DIR}" ) ]]; then
    manager_version="$(read_component_version "${MANAGER_DIR}")"
    version_lines="${version_lines}
- Manager version      : ${manager_version}"
    subject_versions+=("manager ${manager_version}")
  fi

  if [[ "${UPDATED_RUNNER}" -eq 1 || ( "${SEND_TEST_EMAIL}" -eq 1 && -d "${RUNNER_DIR}" ) ]]; then
    runner_version="$(read_component_version "${RUNNER_DIR}")"
    version_lines="${version_lines}
- Runner version       : ${runner_version}"
    subject_versions+=("runner ${runner_version}")
  fi

  local versions_label
  if [[ "${#subject_versions[@]}" -eq 0 ]]; then
    versions_label="no-component-version"
  else
    versions_label="$(printf '%s, ' "${subject_versions[@]}")"
    versions_label="${versions_label%, }"
  fi

  local git_revision hostname_value date_value subject body overall_status test_status_label
  local manager_admin_url report_title report_summary report_eyebrow
  git_revision="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  hostname_value="$(hostname -f 2>/dev/null || hostname 2>/dev/null || echo "unknown-host")"
  date_value="$(date -Is 2>/dev/null || date)"
  manager_admin_url="$(resolve_manager_admin_url)"

  if [[ "${SEND_TEST_EMAIL}" -eq 1 ]]; then
    overall_status="TEST EMAIL"
  elif [[ "${EXIT_CODE}" -eq 0 ]]; then
    overall_status="SUCCESS"
  else
    overall_status="COMPLETED WITH WARNINGS"
  fi

  case "${TEST_STATUS}" in
    ok)
      test_status_label="OK"
      ;;
    failed)
      test_status_label="FAILED"
      ;;
    dry-run)
      test_status_label="DRY-RUN"
      ;;
    disabled)
      test_status_label="DISABLED"
      ;;
    skipped)
      test_status_label="SKIPPED"
      ;;
    *)
      test_status_label="${TEST_STATUS}"
      ;;
  esac

  if [[ "${SEND_TEST_EMAIL}" -eq 1 ]]; then
    subject="[esup-runner] Test email - update notification preview"
    report_eyebrow="Stack update email preview"
    report_title="ESUP-Runner test email"
    report_summary="This preview verifies SMTP configuration, HTML rendering and the manager admin link without running an update."
    body="Hello,

This is a test ESUP-Runner update notification.

========================================================================
ESUP-RUNNER UPDATE EMAIL PREVIEW
========================================================================
Overall status:
- Workflow result      : ${overall_status}
- Updated components   : ${updated_label}${version_lines}

Execution details:
- Git revision         : ${git_revision}
- Date                 : ${date_value}
- Host                 : ${hostname_value}
- Post-update test     : ${test_status_label}
- Manager admin        : ${manager_admin_url:-unavailable}

This message was generated automatically by the ESUP-Runner update-stack.sh."
  else
    subject="[esup-runner] ${overall_status} - ${versions_label}"
    report_eyebrow="Stack update report"
    report_title="ESUP-Runner update completed"
    report_summary="The automatic stack update finished and this report summarizes the result."
    body="Hello,

The automatic ESUP-Runner update has completed.

========================================================================
ESUP-RUNNER UPDATE REPORT
========================================================================
Overall status:
- Workflow result      : ${overall_status}
- Updated components   : ${updated_label}${version_lines}

Execution details:
- Git revision         : ${git_revision}
- Date                 : ${date_value}
- Host                 : ${hostname_value}
- Post-update test     : ${test_status_label}
- Manager admin        : ${manager_admin_url:-unavailable}

This message was generated automatically by the ESUP-Runner update-stack.sh."
  fi

  local logo_path=""
  if [[ -n "${MANAGER_DIR}" && -f "${MANAGER_DIR}/app/web/static/logo.png" ]]; then
    logo_path="${MANAGER_DIR}/app/web/static/logo.png"
  elif [[ -n "${RUNNER_DIR}" && -f "${RUNNER_DIR}/app/web/static/logo.png" ]]; then
    logo_path="${RUNNER_DIR}/app/web/static/logo.png"
  fi

  log_action "Sending update email to ${manager_email}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf 'DRY-RUN: send email to %s via %s:%s\n' "${manager_email}" "${smtp_server}" "${smtp_port}"
    if [[ -n "${manager_admin_url}" ]]; then
      printf 'DRY-RUN: manager admin URL %s\n' "${manager_admin_url}"
    fi
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
    EMAIL_OVERALL_STATUS="${overall_status}" \
    EMAIL_UPDATED_COMPONENTS="${updated_label}" \
    EMAIL_MANAGER_VERSION="${manager_version}" \
    EMAIL_RUNNER_VERSION="${runner_version}" \
    EMAIL_GIT_REVISION="${git_revision}" \
    EMAIL_DATE="${date_value}" \
    EMAIL_HOST="${hostname_value}" \
    EMAIL_TEST_STATUS="${test_status_label}" \
    EMAIL_LOGO_PATH="${logo_path}" \
    EMAIL_MANAGER_ADMIN_URL="${manager_admin_url}" \
    EMAIL_REPORT_EYEBROW="${report_eyebrow}" \
    EMAIL_REPORT_TITLE="${report_title}" \
    EMAIL_REPORT_SUMMARY="${report_summary}" \
    python3 - <<'PY'
from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from html import escape
from pathlib import Path


LOGO_CID = "esup-runner-logo"

TONE_STYLES = {
    "success": {
        "accent": "#198754",
        "badge_bg": "#e8f6ef",
        "badge_text": "#0f5132",
    },
    "warning": {
        "accent": "#fd7e14",
        "badge_bg": "#fff4e5",
        "badge_text": "#7a3f00",
    },
    "info": {
        "accent": "#0d6bf4",
        "badge_bg": "#eaf3ff",
        "badge_text": "#084298",
    },
}


def as_bool(raw: str) -> bool:
    """Parse shell-style truthy environment values."""
    return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def display(raw: str, fallback: str = "(none)") -> str:
    """Return a stripped display value, or the fallback when it is blank."""
    value = (raw or "").strip()
    return value or fallback


def is_http_url(raw: str) -> bool:
    """Return True when a value is an HTTP(S) URL safe for email links."""
    return raw.startswith(("http://", "https://"))


def render_logo(logo_cid: str | None) -> str:
    """Render either the inline ESUP-Runner logo or a text fallback."""
    if logo_cid:
        return (
            f'<img src="cid:{logo_cid}" width="190" alt="ESUP-Runner" '
            'style="display:block;width:190px;max-width:100%;height:auto;border:0;outline:none;">'
        )
    return (
        '<div style="font-size:22px;font-weight:700;color:#1f2937;line-height:1.2;">'
        "ESUP-Runner</div>"
    )


def render_rows(rows: list[tuple[str, str]]) -> str:
    """Render key/value rows for the update email details table."""
    rendered = []
    for label, value in rows:
        rendered.append(
            "<tr>"
            '<td style="padding:10px 12px;border-bottom:1px solid #e9ecef;'
            'font-size:13px;color:#6c747c;width:34%;vertical-align:top;">'
            f"{escape(label)}"
            "</td>"
            '<td style="padding:10px 12px;border-bottom:1px solid #e9ecef;'
            'font-size:14px;color:#212529;font-weight:600;vertical-align:top;'
            'word-break:break-word;">'
            f"{escape(display(value))}"
            "</td>"
            "</tr>"
        )
    return "".join(rendered)


def render_html_email(
    *,
    overall_status: str,
    updated_components: str,
    manager_version: str,
    runner_version: str,
    git_revision: str,
    date_value: str,
    host: str,
    test_status: str,
    manager_admin_url: str,
    eyebrow: str,
    title: str,
    summary: str,
    logo_cid: str | None,
) -> str:
    """Render the HTML body for update report and preview emails."""
    if overall_status == "SUCCESS":
        tone = "success"
    elif overall_status == "TEST EMAIL":
        tone = "info"
    else:
        tone = "warning"
    style = TONE_STYLES[tone]
    rows = [
        ("Workflow result", overall_status),
        ("Updated components", updated_components),
    ]
    if manager_version:
        rows.append(("Manager version", manager_version))
    if runner_version:
        rows.append(("Runner version", runner_version))
    rows.extend(
        [
            ("Git revision", git_revision),
            ("Date", date_value),
            ("Host", host),
            ("Post-update test", test_status),
            ("Manager admin", manager_admin_url),
        ]
    )

    action_html = ""
    if is_http_url(manager_admin_url):
        action_html = (
            '<tr><td style="padding:0 28px 28px 28px;background:#ffffff;">'
            f'<a href="{escape(manager_admin_url, quote=True)}" '
            f'style="display:inline-block;background:{style["accent"]};color:#ffffff;'
            "text-decoration:none;font-size:14px;font-weight:700;padding:11px 18px;"
            'border-radius:6px;">Open manager</a>'
            "</td></tr>"
        )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f"<title>{escape(title)}</title></head>"
        '<body style="margin:0;padding:0;background:#f4f6f8;'
        '-webkit-text-size-adjust:100%;font-family:Arial,Helvetica,sans-serif;">'
        '<span style="display:none!important;visibility:hidden;opacity:0;color:transparent;'
        'height:0;width:0;overflow:hidden;">'
        f"{escape(summary)}</span>"
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="background:#f4f6f8;margin:0;padding:0;">'
        '<tr><td align="center" style="padding:28px 12px;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="max-width:680px;background:#ffffff;border-collapse:separate;'
        'border-spacing:0;border-radius:8px;overflow:hidden;'
        'box-shadow:0 12px 32px rgba(33,37,41,0.12);">'
        f'<tr><td style="height:5px;background:{style["accent"]};font-size:0;line-height:0;">'
        "&nbsp;</td></tr>"
        '<tr><td style="padding:26px 28px 18px 28px;background:#ffffff;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">'
        "<tr>"
        f'<td align="left" style="vertical-align:middle;">{render_logo(logo_cid)}</td>'
        '<td align="right" style="vertical-align:middle;">'
        f'<span style="display:inline-block;background:{style["badge_bg"]};'
        f'color:{style["badge_text"]};border:1px solid {style["accent"]};'
        'border-radius:999px;padding:7px 12px;font-size:12px;font-weight:700;'
        'text-transform:uppercase;letter-spacing:0;">'
        f"{escape(overall_status)}</span>"
        "</td></tr></table>"
        '<div style="font-size:13px;color:#6c747c;font-weight:700;text-transform:uppercase;'
        f'letter-spacing:0;margin-top:24px;">{escape(eyebrow)}</div>'
        f'<h1 style="margin:8px 0 10px 0;color:#212529;font-size:26px;line-height:1.25;'
        f'font-weight:700;">{escape(title)}</h1>'
        f'<p style="margin:0;color:#495057;font-size:15px;line-height:1.6;">{escape(summary)}</p>'
        "</td></tr>"
        '<tr><td style="padding:0 28px 24px 28px;background:#ffffff;">'
        '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
        'style="border:1px solid #e9ecef;border-radius:6px;overflow:hidden;">'
        f"{render_rows(rows)}"
        "</table></td></tr>"
        f"{action_html}"
        '<tr><td style="padding:18px 28px 24px 28px;background:#f8f9fa;'
        'border-top:1px solid #e9ecef;color:#6c747c;font-size:12px;line-height:1.5;">'
        "This message was generated automatically by the ESUP-Runner update-stack.sh."
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def attach_logo(message: EmailMessage, logo_path: str) -> None:
    """Attach the ESUP-Runner logo as an inline image when available."""
    path = Path(logo_path)
    if not path.is_file():
        return

    html_part = message.get_body(("html",))
    if html_part is None:
        return

    html_part.add_related(
        path.read_bytes(),
        maintype="image",
        subtype="png",
        cid=f"<{LOGO_CID}>",
    )


smtp_server = os.environ.get("SMTP_SERVER", "").strip()
smtp_port_raw = os.environ.get("SMTP_PORT", "25").strip()
smtp_use_tls = as_bool(os.environ.get("SMTP_USE_TLS", "false"))
smtp_username = os.environ.get("SMTP_USERNAME", "").strip()
smtp_password = os.environ.get("SMTP_PASSWORD", "")
smtp_sender = os.environ.get("SMTP_SENDER", "").strip()
manager_email = os.environ.get("MANAGER_EMAIL", "").strip()
subject = os.environ.get("EMAIL_SUBJECT", "").strip()
body = os.environ.get("EMAIL_BODY", "")
overall_status = os.environ.get("EMAIL_OVERALL_STATUS", "").strip()
updated_components = os.environ.get("EMAIL_UPDATED_COMPONENTS", "").strip()
manager_version = os.environ.get("EMAIL_MANAGER_VERSION", "").strip()
runner_version = os.environ.get("EMAIL_RUNNER_VERSION", "").strip()
git_revision = os.environ.get("EMAIL_GIT_REVISION", "").strip()
date_value = os.environ.get("EMAIL_DATE", "").strip()
host = os.environ.get("EMAIL_HOST", "").strip()
test_status = os.environ.get("EMAIL_TEST_STATUS", "").strip()
logo_path = os.environ.get("EMAIL_LOGO_PATH", "").strip()
manager_admin_url = os.environ.get("EMAIL_MANAGER_ADMIN_URL", "").strip()
report_eyebrow = os.environ.get("EMAIL_REPORT_EYEBROW", "").strip()
report_title = os.environ.get("EMAIL_REPORT_TITLE", "").strip()
report_summary = os.environ.get("EMAIL_REPORT_SUMMARY", "").strip()

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

logo_cid = LOGO_CID if logo_path and Path(logo_path).is_file() else None
html_body = render_html_email(
    overall_status=display(overall_status, "UNKNOWN"),
    updated_components=display(updated_components),
    manager_version=manager_version,
    runner_version=runner_version,
    git_revision=display(git_revision, "unknown"),
    date_value=display(date_value, "unknown"),
    host=display(host, "unknown-host"),
    test_status=display(test_status, "SKIPPED"),
    manager_admin_url=display(manager_admin_url, "unavailable"),
    eyebrow=display(report_eyebrow, "Stack update report"),
    title=display(report_title, "ESUP-Runner update completed"),
    summary=display(
        report_summary,
        "The automatic stack update finished and this report summarizes the result.",
    ),
    logo_cid=logo_cid,
)

msg = EmailMessage()
msg["Subject"] = subject or "[esup-runner] Update completed"
msg["From"] = smtp_sender
msg["To"] = manager_email
msg.set_content(body)
msg.add_alternative(html_body, subtype="html")
attach_logo(msg, logo_path)

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
      --send-test-email)
        SEND_TEST_EMAIL=1
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

  if [[ "${SEND_TEST_EMAIL}" -eq 1 && "${RUN_EMAIL}" -ne 1 ]]; then
    die "--send-test-email cannot be combined with --skip-email"
  fi
}

main() {
  # Orchestrate full update lifecycle for detected/selected components.
  init_output_theme
  parse_args "$@"
  validate_inputs

  print_step_banner "Preparation and installation detection"

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
  local effective_runner_mode=""
  local restore_runner_uv_lock_before_git_update=0

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

  if [[ "${do_runner}" -eq 1 ]]; then
    effective_runner_mode="$(infer_runner_sync_mode)"
    log "Runner sync mode: ${effective_runner_mode}"

    case "${effective_runner_mode}" in
      transcription-cpu|transcription-gpu)
        RUN_TEST_WITH_TRANSCRIPTION_TRANSLATION=1
        ;;
    esac

    # If we are going to run make lock-upgrade-gpu-**, pre-clean tracked runner/uv.lock
    # before git pull to reduce chances of lock-related merge conflicts.
    if [[ "${effective_runner_mode}" == "transcription-gpu" ]]; then
      case "${GPU_LOCK_PROFILE}" in
        cuda12|latest)
          restore_runner_uv_lock_before_git_update=1
          ;;
      esac
    fi
  fi

  if [[ "${RUNNER_SYNC_MODE}" == "transcription-cpu" || "${RUNNER_SYNC_MODE}" == "transcription-gpu" ]]; then
    RUN_TEST_WITH_TRANSCRIPTION_TRANSLATION=1
  fi

  if [[ "${RUN_TEST_WITH_TRANSCRIPTION_TRANSLATION}" -eq 1 ]]; then
    log "Post-update smoke test mode: with --with-transcription-translation."
  fi

  if [[ "${SEND_TEST_EMAIL}" -eq 1 ]]; then
    TEST_STATUS="disabled"
    log "Test email mode: update, restart and smoke-test steps are skipped."
    print_step_banner "Send test update notification email"
    send_update_email_if_configured
    print_update_summary
    exit "${EXIT_CODE}"
  fi

  if [[ "${RUN_UV_UPDATE}" -eq 1 ]]; then
    print_step_banner "Update uv installer"
    update_uv
  else
    print_step_banner "Update uv installer"
    log "uv update skipped (--skip-uv-update)"
  fi

  if [[ "${RUN_GIT_UPDATE}" -eq 1 ]]; then
    print_step_banner "Update git sources"
    update_git_sources "${restore_runner_uv_lock_before_git_update}"
  else
    print_step_banner "Update git sources"
    log "git update skipped (--skip-git-update)"
  fi

  if [[ "${do_manager}" -eq 1 ]]; then
    print_step_banner "Update manager component"
    update_manager
  fi

  if [[ "${do_runner}" -eq 1 ]]; then
    print_step_banner "Update runner component (${effective_runner_mode})"
    update_runner "${effective_runner_mode}"
  fi

  print_step_banner "Run post-update smoke test"
  run_post_update_test
  print_step_banner "Send update notification email"
  send_update_email_if_configured

  print_update_summary
  exit "${EXIT_CODE}"
}

main "$@"
