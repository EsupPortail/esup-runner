#!/usr/bin/env bash

# install-stack.sh - automated native stack installation helper for Esup-Runner.
#
# Purpose:
# - Provide a single entry point to install manager, runner, or both.
# - Support guided and unattended installation on Debian/Ubuntu with systemd.
# - Allow downloading this script before cloning the repository.
#
# Existing installation:
# - Reuse the checkout under /opt/esup-runner and preserve existing .env files.
# - Preserve service units and keep active application services running.
# - New installations use CPU mode, with optional transcription and Live support.
#
# High-level workflow:
# 1) Select components and validate installation options.
# 2) Install system packages and prepare the dedicated service account.
# 3) Install uv and Python as the service user, then enable systemd user startup.
# 4) Reuse existing sources or clone the official repository.
# 5) Create missing private configuration files and share local API credentials.
# 6) Install component dependencies, initialize directories and check local ports.
#    If Live is enabled, prepare MediaMTX and application connections.
# 7) Validate configuration and install or start inactive application services.
# 8) Check service health and Runner registration, then print useful addresses.
#
# Concrete usage examples:
# 1) Guided installation:
#    sudo bash install-stack.sh
# 2) Dry-run (preview only, no installation commands executed):
#    bash install-stack.sh --component both --dry-run
# 3) Manager for remote Runners:
#    sudo bash install-stack.sh --component manager --manager-host manager.internal
# 4) Runner connected to an existing Manager:
#    sudo bash install-stack.sh --component runner \
#      --manager-url http://manager.internal:8081 --runner-host runner.internal
# 5) Local stack with CPU transcription and Live:
#    sudo bash install-stack.sh --component both --with-transcription --with-live
# 6) Unattended installation (private input file described in INSTALLATION.md):
#    sudo bash install-stack.sh --component both --non-interactive \
#      --secrets-file /root/esup-runner-install.env

set -Eeuo pipefail

ROOT_DIR=/opt/esup-runner
SERVICE_USER=esup-runner
COMPONENT=""
MANAGER_HOST=""
RUNNER_HOST=""
MANAGER_URL=""
WITH_TRANSCRIPTION=0
WITH_LIVE=0
NON_INTERACTIVE=0
DRY_RUN=0
SECRETS_FILE=""
PHASE=preparation
SERVICE_HOME=""
SERVICE_UID=""
SERVICE_GROUP=""
UV=""
HELPER=""

usage() {
  # Print available options and installation defaults.
  cat <<'USAGE'
Usage: sudo bash install-stack.sh [options]

Without options, answer a few questions to install Manager, Runner, or both.
Supported hosts: Debian/Ubuntu with systemd. Default: CPU, /opt/esup-runner.

  --component <role>       manager|runner|both
  --manager-host <host>    Manager private DNS name/IP (default: 127.0.0.1)
  --runner-host <host>     Runner local DNS name/IP reachable by the Manager
  --manager-url <url>      Existing Manager private API URL (Runner-only)
  --with-transcription    Enable CPU transcription on the Runner
  --with-live             Install MediaMTX and configure Live streaming
  --service-user <name>   Service account (default: esup-runner)
  --non-interactive       Require all necessary inputs without prompting
  --secrets-file <path>   Private dotenv file; see INSTALLATION.md
  --dry-run               Describe actions without downloads or changes
  -h, --help              Show this help

Passwords and API tokens are prompted for privately, never passed as arguments.
Existing .env files and service units are preserved. Use update-stack.sh to update.
USAGE
}

# Print an informational log entry.
log() { printf '[install] %s\n' "$*"; }
# Print an error log entry and exit immediately.
die() { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

parse_args() {
  # Parse CLI flags and update global installation options.
  while (( $# )); do
    case "$1" in
      --component|--manager-host|--runner-host|--manager-url|--service-user|--secrets-file)
        (( $# >= 2 )) && [[ -n "$2" && "$2" != --* ]] || die "Missing value for $1"
        case "$1" in
          --component) COMPONENT="$2" ;;
          --manager-host) MANAGER_HOST="$2" ;;
          --runner-host) RUNNER_HOST="$2" ;;
          --manager-url) MANAGER_URL="$2" ;;
          --service-user) SERVICE_USER="$2" ;;
          --secrets-file) SECRETS_FILE="$2" ;;
        esac
        shift 2 ;;
      --with-transcription) WITH_TRANSCRIPTION=1; shift ;;
      --with-live) WITH_LIVE=1; shift ;;
      --non-interactive) NON_INTERACTIVE=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "Unknown option: $1 (see --help)" ;;
    esac
  done
}

prompt_value() {
  # Read one guided input and use its default when the answer is empty.
  local variable="$1" question="$2" fallback="$3" answer
  read -r -p "$question [$fallback]: " answer
  printf -v "$variable" '%s' "${answer:-$fallback}"
}

choose_options() {
  # Complete missing choices interactively, then apply local address defaults.
  local answer live_role=manager
  if (( DRY_RUN )); then
    COMPONENT="${COMPONENT:-both}"
  elif (( ! NON_INTERACTIVE )); then
    [[ -t 0 ]] || die "Use --non-interactive and --secrets-file without a terminal."
    if [[ -z "$COMPONENT" ]]; then
      prompt_value COMPONENT 'Install manager, runner, or both' both
    fi
    if [[ "$COMPONENT" != runner && ! -f "$ROOT_DIR/manager/.env" && -z "$MANAGER_HOST" ]]; then
      prompt_value MANAGER_HOST 'Manager private DNS name/IP' 127.0.0.1
    fi
    if [[ "$COMPONENT" != manager && ! -f "$ROOT_DIR/runner/.env" ]]; then
      if [[ "$COMPONENT" == runner && ! -f "$ROOT_DIR/manager/.env" && -z "$MANAGER_URL" ]]; then
        prompt_value MANAGER_URL 'Existing Manager private API URL' http://127.0.0.1:8081
      fi
      if [[ -z "$RUNNER_HOST" ]]; then
        prompt_value RUNNER_HOST 'Runner local DNS name/IP reachable by the Manager' 127.0.0.1
      fi
      if (( ! WITH_TRANSCRIPTION )); then
        prompt_value answer 'Enable CPU transcription? (y/n)' n
        [[ "$answer" != y ]] || WITH_TRANSCRIPTION=1
      fi
    fi
    [[ "$COMPONENT" != runner ]] || live_role=runner
    if (( ! WITH_LIVE )) && [[ ! -f "$ROOT_DIR/$live_role/.env" ]]; then
      prompt_value answer 'Enable Live streaming? (y/n)' n
      [[ "$answer" != y ]] || WITH_LIVE=1
    fi
  fi
  MANAGER_HOST="${MANAGER_HOST:-127.0.0.1}"
  RUNNER_HOST="${RUNNER_HOST:-127.0.0.1}"
}

validate_options() {
  # Validate selected components, account names and addresses before host changes.
  [[ "$COMPONENT" == manager || "$COMPONENT" == runner || "$COMPONENT" == both ]] \
    || die "--component must be manager, runner, or both."
  [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*$ && "$SERVICE_USER" != root ]] \
    || die "Invalid service account name."
  [[ "$COMPONENT" != manager || "$WITH_TRANSCRIPTION" == 0 ]] \
    || die "--with-transcription requires a Runner."
  [[ "$COMPONENT" == runner || -z "$MANAGER_URL" ]] \
    || die "--manager-url is for Runner-only installations; use --manager-host otherwise."
  local host
  for host in "$MANAGER_HOST" "$RUNNER_HOST"; do
    [[ "$host" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "Invalid host name or IP address."
    [[ "$host" != 0.0.0.0 && "$host" != :: && "$host" != '[::]' ]] \
      || die "Advertise a reachable address instead of a wildcard."
    [[ "$host" != *:* ]] || die "Native installation requires an IPv4 address or DNS name for local services."
  done
  if [[ -n "$MANAGER_URL" ]]; then
    local url_pattern='^https?://([A-Za-z0-9.-]+|\[[A-Fa-f0-9:]+\])(:[0-9]+)?/?$'
    [[ "$MANAGER_URL" =~ $url_pattern ]] \
      || die "--manager-url must be an HTTP(S) base URL without credentials, path, query or fragment."
  fi
}

print_plan() {
  # Describe the selected installation without reading secrets or changing files.
  log "Plan: $COMPONENT in $ROOT_DIR, account $SERVICE_USER (CPU)."
  log 'Install system packages, uv and Python 3.12; reuse an existing checkout or clone the official repository.'
  log 'Create missing .env files with private credentials; preserve existing configuration.'
  (( ! WITH_TRANSCRIPTION )) || log 'Install CPU transcription dependencies.'
  (( ! WITH_LIVE )) || log 'Install and connect MediaMTX; reserve separate Runner capacity for Live.'
  log 'Initialize default directories; install missing user services and enable startup at boot.'
  log 'Keep active services running; check health and Runner registration.'
  log 'Dry-run: no commands executed, no credentials read, no files changed.'
}

reject_symlinks() {
  # Reject symbolic links in the destination and its parent directories.
  local path="$1"
  while [[ "$path" != / && "$path" != . ]]; do
    [[ ! -L "$path" ]] || die "Refusing symbolic link in installation path."
    path="$(dirname -- "$path")"
  done
}

preflight() {
  # Check host requirements, prevent concurrent installs and validate the secrets file.
  [[ "$EUID" == 0 ]] || die "Run with sudo: system packages, account and boot startup require root."
  [[ -d /run/systemd/system ]] || die "A running systemd host is required."
  command -v apt-get >/dev/null || die "This installer supports Debian/Ubuntu (apt-get)."
  command -v runuser >/dev/null || die "runuser (util-linux) is required."
  command -v flock >/dev/null || die "flock (util-linux) is required."
  reject_symlinks "$ROOT_DIR"
  umask 077
  exec 9>/run/esup-runner-install.lock
  flock -n 9 || die "Another stack installation is running."
  if [[ -n "$SECRETS_FILE" ]]; then
    reject_symlinks "$SECRETS_FILE"
    [[ -f "$SECRETS_FILE" && -r "$SECRETS_FILE" ]] || die "Cannot read --secrets-file."
    local mode
    mode="$(stat -c '%a' -- "$SECRETS_FILE")"
    (( (8#$mode & 077) == 0 )) || die "--secrets-file must be private (chmod 600)."
  fi
}

as_user() {
  # Run commands as the service account with a clean environment and user bus.
  runuser -u "$SERVICE_USER" -- env -i \
    HOME="$SERVICE_HOME" USER="$SERVICE_USER" LOGNAME="$SERVICE_USER" \
    PATH="$SERVICE_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
    XDG_RUNTIME_DIR="/run/user/$SERVICE_UID" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$SERVICE_UID/bus" \
    UV_CACHE_DIR="$SERVICE_HOME/.cache/esup-runner/uv" UV_PYTHON=3.12 UV_FROZEN=1 \
    "$@"
}

provision_host() {
  # Prepare packages, account, directories, Python and systemd user startup.
  PHASE='system prerequisites'
  local packages=(curl ca-certificates git make python3-venv)
  [[ "$COMPONENT" == manager ]] || packages+=(ffmpeg)
  log 'Installing system prerequisites.'
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --create-home --user-group --shell /bin/bash "$SERVICE_USER"
  fi
  SERVICE_UID="$(id -u "$SERVICE_USER")"
  [[ "$SERVICE_UID" != 0 ]] || die "The service account must be unprivileged."
  SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
  SERVICE_HOME="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  [[ "$SERVICE_HOME" == /* && "$SERVICE_HOME" != / && "$SERVICE_HOME" != /root ]] \
    || die "The service account needs its own absolute home directory."
  reject_symlinks "$SERVICE_HOME"
  if [[ ! -d "$ROOT_DIR" ]]; then
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 755 "$ROOT_DIR"
  fi
  [[ "$(stat -c '%u' "$ROOT_DIR")" == "$SERVICE_UID" ]] \
    || die "The existing installation directory must belong to $SERVICE_USER; ownership was left unchanged."
  # make init and uv create the remaining directories as the service account.
  reject_symlinks /var/log/esup-runner
  if [[ ! -d /var/log/esup-runner ]]; then
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 750 /var/log/esup-runner
  fi
  local cache_dir="$SERVICE_HOME/.cache/esup-runner/uv" cache_mode
  reject_symlinks "$cache_dir"
  if [[ -e "$cache_dir" ]]; then
    [[ -d "$cache_dir" ]] && as_user test -w "$cache_dir" \
      || die "The existing uv cache is not a writable directory for $SERVICE_USER."
    cache_mode="$(stat -c '%a' "$cache_dir")"
    [[ "$(stat -c '%u' "$cache_dir")" == "$SERVICE_UID" ]] \
      && (( (8#$cache_mode & 022) == 0 )) \
      || die "The uv cache must belong to $SERVICE_USER and must not be writable by other accounts."
  fi
  PHASE='service account and Python'
  # Install and run uv under the service account, including when launched from /root.
  UV="$SERVICE_HOME/.local/bin/uv"
  if ! as_user test -x "$UV"; then
    log 'Installing uv under the service account.'
    as_user bash -o pipefail -c 'cd "$HOME"; curl --proto "=https" --tlsv1.2 -fsSL https://astral.sh/uv/install.sh | sh'
  fi
  as_user "$UV" --directory "$SERVICE_HOME" python install 3.12
  loginctl enable-linger "$SERVICE_USER"
  systemctl start "user@$SERVICE_UID.service"
  as_user systemctl --user show-environment >/dev/null \
    || die "The systemd user bus is unavailable for $SERVICE_USER."
}

prepare_sources() {
  # Reuse an existing checkout or clone sources, then check required component files.
  PHASE='source checkout'
  if [[ -e "$ROOT_DIR/.git" ]]; then
    as_user git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null \
      || die "The existing installation is not a usable Git checkout."
    log 'Reusing the checkout without fetching or modifying tracked files.'
  else
    [[ -z "$(find "$ROOT_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] \
      || die "The installation directory is not empty and has no Git checkout."
    as_user git clone https://github.com/EsupPortail/esup-runner.git "$ROOT_DIR"
  fi
  HELPER="$ROOT_DIR/scripts/install_stack.py"
  [[ -f "$HELPER" ]] || die "Installer helper missing. For sparse checkouts, run git sparse-checkout add scripts; otherwise use a version containing install-stack.sh."
  local role
  for role in manager runner; do
    if [[ "$COMPONENT" == both || "$COMPONENT" == "$role" ]]; then
      [[ -f "$ROOT_DIR/$role/Makefile" && -f "$ROOT_DIR/$role/.env.example" ]] \
        || die "Missing $role sources. For sparse checkouts, run git sparse-checkout add $role scripts."
    fi
  done
}

helper() {
  # Run configuration and verification helpers outside the component environments.
  as_user env -u UV_FROZEN "$UV" --directory "$ROOT_DIR" --no-config run \
    --no-project --with python-dotenv --with 'bcrypt>=5,<6' --python 3.12 \
    python "$HELPER" "$@"
}

configure_stack() {
  # Create missing configuration and pass supplied secrets through standard input.
  PHASE='configuration'
  local args=(configure --root "$ROOT_DIR" --component "$COMPONENT"
    --service-user "$SERVICE_USER" --service-home "$SERVICE_HOME"
    --manager-host "$MANAGER_HOST" --runner-host "$RUNNER_HOST")
  [[ -z "$MANAGER_URL" ]] || args+=(--manager-url "$MANAGER_URL")
  (( ! WITH_TRANSCRIPTION )) || args+=(--with-transcription)
  (( ! WITH_LIVE )) || args+=(--with-live)
  (( ! NON_INTERACTIVE )) || args+=(--non-interactive)
  if [[ -n "$SECRETS_FILE" ]]; then
    helper "${args[@]}" --secrets-stdin < "$SECRETS_FILE"
  else
    helper "${args[@]}"
  fi
}

component_command() {
  # Run a service-user command from the selected component's project directory.
  local role="$1"
  shift
  (cd "$ROOT_DIR/$role" && as_user "$@")
}

component_python() {
  # Use the component's installed Python environment without resynchronizing dependencies.
  local role="$1"
  shift
  component_command "$role" "$UV" run --no-sync python "$@"
}

prepare_live() {
  # Prepare Live services and missing connections while preserving existing settings.
  local role="$1" existing_unit="$2"
  # Install the Runner stop policy before startup, including after an interrupted setup.
  if [[ "$role" == runner && -z "$existing_unit" ]]; then
    local dropin="$SERVICE_HOME/.config/systemd/user/esup-runner-runner.service.d/20-live.conf"
    as_user mkdir -p "$(dirname "$dropin")"
    if ! as_user test -e "$dropin"; then
      as_user tee "$dropin" >/dev/null <<'UNIT'
[Unit]
After=esup-runner-mediamtx@runner.service
Wants=esup-runner-mediamtx@runner.service

[Service]
KillMode=control-group
TimeoutStopSec=45
UNIT
    fi
  fi
  local configured
  configured="$(helper live-configured --root "$ROOT_DIR" --component "$role")"
  if [[ "$configured" == true ]]; then
    log "$role Live connections are already configured; preserving them."
    if [[ -f "$SERVICE_HOME/.config/esup-runner/$role/mediamtx.yml" ]]; then
      component_python "$role" scripts/install_mediamtx.py start --instance "$role"
    fi
    return
  fi
  local setup=(scripts/install_mediamtx.py setup --instance "$role")
  if [[ "$role" == manager ]]; then
    local effective_host
    effective_host="$(helper manager-host --root "$ROOT_DIR" --component manager)"
    if [[ "$effective_host" == localhost || "$effective_host" =~ ^127\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
      setup+=(--rtmp-address 127.0.0.1:1935)
    else
      setup+=(--rtmp-address 0.0.0.0:1935)
    fi
  fi
  component_python "$role" "${setup[@]}"
  # Fill missing application connections while the application service is stopped.
  if [[ "$role" == manager ]]; then
    component_python manager scripts/configure_live.py --instance manager
  else
    local local_manager
    local_manager="$(helper local-manager --root "$ROOT_DIR")"
    if [[ "$local_manager" == true && -f "$SERVICE_HOME/.config/esup-runner/manager/mediamtx.yml" ]]; then
      component_python runner scripts/configure_live.py --instance runner
    else
      local remote_host
      remote_host="$(helper manager-host --root "$ROOT_DIR")"
      if [[ -n "$SECRETS_FILE" ]]; then
        # Reserve descriptor 3 for the relay pipe and keep diagnostics on stderr.
        helper live-password --secrets-stdin --output-fd 3 < "$SECRETS_FILE" 3>&1 1>&2 \
          | component_python runner scripts/configure_live.py --instance runner \
            --manager-rtmp-host "$remote_host" --relay-password-stdin
      else
        (( ! NON_INTERACTIVE )) || die "Remote Live requires INSTALL_LIVE_RELAY_PASSWORD in --secrets-file."
        component_python runner scripts/configure_live.py --instance runner --manager-rtmp-host "$remote_host"
      fi
    fi
  fi
  component_python "$role" scripts/install_mediamtx.py start --instance "$role"
}

install_component() {
  # Install and start an inactive component, preserving active services and custom units.
  local role="$1" unit="esup-runner-$1.service"
  PHASE="$role installation"
  if as_user systemctl --user is-active --quiet "$unit"; then
    log "$role is active; preserving its environment and service without restarting."
    as_user systemctl --user enable "$unit"
    return
  fi
  local target=sync
  if [[ "$role" == runner ]]; then
    local sync_mode
    sync_mode="$(helper sync-mode --root "$ROOT_DIR")"
    [[ "$sync_mode" == base ]] || target="sync-$sync_mode"
  fi
  component_command "$role" make "$target" "UV=$UV"
  component_command "$role" make init "UV=$UV" "PYTHON=$UV run --no-sync python" \
    "SERVICE_USER=$SERVICE_USER"
  helper check-ports --root "$ROOT_DIR" --component "$role"
  local unit_file live_enabled
  unit_file="$(as_user systemctl --user show "$unit" --property=FragmentPath --value)"
  live_enabled="$(helper live-enabled --root "$ROOT_DIR" --component "$role")"
  if (( WITH_LIVE )) || [[ "$live_enabled" == true ]]; then
    prepare_live "$role" "$unit_file"
  fi
  component_python "$role" scripts/check_config.py
  if [[ -z "$unit_file" ]]; then
    component_command "$role" make create-service "UV=$UV" "SERVICE_USER=$SERVICE_USER" \
      "SERVICE_HOME=$SERVICE_HOME"
  else
    as_user systemctl --user daemon-reload
    as_user systemctl --user enable "$unit"
    as_user systemctl --user start "$unit"
  fi
  as_user systemctl --user is-active --quiet "$unit" || die "$role did not start. Check its user journal."
}

main() {
  # Orchestrate the selected installation and its final health checks.
  parse_args "$@"
  choose_options
  validate_options
  if (( DRY_RUN )); then
    print_plan
    return
  fi
  preflight
  trap 'printf "[install] Failed during %s. Fix the error and rerun the same command; configuration is preserved.\n" "$PHASE" >&2' ERR
  provision_host
  prepare_sources
  configure_stack
  [[ "$COMPONENT" == runner ]] || install_component manager
  [[ "$COMPONENT" == manager ]] || install_component runner
  PHASE='health checks'
  helper check --root "$ROOT_DIR" --component "$COMPONENT" --timeout 60
  helper summary --root "$ROOT_DIR" --component "$COMPONENT"
  log "Installation complete. For updates: cd $ROOT_DIR && ./update-stack.sh"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
