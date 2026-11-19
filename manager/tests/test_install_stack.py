"""Exercise the native installer without packages, root actions, or network access."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPOSITORY_ROOT / "install-stack.sh"


def run_shell(tmp_path, body, *args, extra_env=None):
    """Source installer functions and restrict every fixture path to a temporary tree."""
    environment = os.environ.copy()
    environment.update(
        FIXTURE_ROOT=str(tmp_path / "checkout"),
        FIXTURE_HOME=str(tmp_path / "home"),
        TRACE_FILE=str(tmp_path / "trace"),
    )
    environment.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", 'source "$1"\nshift\n' + body, "installer-test", str(INSTALLER), *args],
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )


@pytest.fixture
def checkout(tmp_path):
    """Create a complete checkout fixture without real application configuration."""
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts/install_stack.py").write_text("# helper fixture\n")
    for role in ("manager", "runner"):
        (root / role).mkdir()
        (root / role / "Makefile").write_text("# Makefile fixture\n")
        (root / role / ".env.example").write_text("# example fixture\n")
    return root


MOCK_HOST = r"""
ROOT_DIR="$FIXTURE_ROOT"
trace() { printf '%s\n' "$*" >> "$TRACE_FILE"; }
preflight() { trace 'host preflight'; }
provision_host() {
  trace 'host provision'
  SERVICE_HOME="$FIXTURE_HOME"
  SERVICE_UID=12345
  SERVICE_GROUP=fixture
  UV=/fixture/bin/uv
  mkdir -p "$SERVICE_HOME/.config/systemd/user"
}
as_user() {
  trace "user $*"
  case "$1" in
    git) return 0 ;;
    systemctl)
      case "$3" in
        is-active) test -f "$SERVICE_HOME/$5.active" ;;
        show)
          if [[ -f "$SERVICE_HOME/.config/systemd/user/$4" ]]; then
            printf '%s\n' "$SERVICE_HOME/.config/systemd/user/$4"
          fi ;;
        start) touch "$SERVICE_HOME/$4.active" ;;
        *) return 0 ;;
      esac ;;
    test|mkdir|tee) "$@" ;;
    *) printf 'Unexpected command\n' >&2; return 99 ;;
  esac
}
helper() {
  trace "helper $*"
  if [[ "$1" == "${FIXTURE_FAIL_HELPER:-}" ]]; then return 91; fi
  case "$1" in
    configure)
      local role
      for role in manager runner; do
        if [[ "$COMPONENT" == both || "$COMPONENT" == "$role" ]]; then
          if [[ ! -f "$ROOT_DIR/$role/.env" ]]; then
            printf 'FIXTURE_SECRET=private-%s\n' "$role" > "$ROOT_DIR/$role/.env"
          fi
        fi
      done ;;
    sync-mode) printf '%s\n' "${FIXTURE_SYNC_MODE:-base}" ;;
    manager-host) printf 'manager.internal\n' ;;
    local-manager)
      if [[ -f "$ROOT_DIR/manager/.env" ]]; then printf true; else printf false; fi ;;
    live-enabled) printf false ;;
    live-configured) printf '%s' "${FIXTURE_LIVE_CONFIGURED:-false}" ;;
    check-ports) [[ "${FIXTURE_FAIL_PORTS:-0}" != 1 ]] ;;
    live-password)
      printf 'Relay transfer diagnostic\n'
      printf 'fixture-private-relay\n' >&3 ;;
    check) [[ "${FIXTURE_FAIL_CHECK:-0}" != 1 ]] ;;
    summary) return 0 ;;
    *) return 99 ;;
  esac
}
component_command() {
  local role="$1"
  shift
  trace "component $role $*"
  if [[ "$1 $2" == 'make create-service' ]]; then
    printf 'fixture-unit-%s\n' "$role" > "$SERVICE_HOME/.config/systemd/user/esup-runner-$role.service"
    touch "$SERVICE_HOME/esup-runner-$role.service.active"
  fi
}
component_python() {
  local role="$1"
  shift
  trace "python $role $*"
  if [[ "$1 ${2:-}" == 'scripts/install_mediamtx.py setup' ]]; then
    mkdir -p "$SERVICE_HOME/.config/esup-runner/$role"
    printf 'fixture-media\n' > "$SERVICE_HOME/.config/esup-runner/$role/mediamtx.yml"
  fi
  if [[ " $* " == *' --relay-password-stdin '* ]]; then
    local relay
    IFS= read -r relay
    [[ "$relay" == fixture-private-relay ]]
  fi
}
main "$@"
"""


def trace_lines(tmp_path):
    """Read mocked effects for ordering and unintended-command assertions."""
    trace = tmp_path / "trace"
    return trace.read_text().splitlines() if trace.exists() else []


def test_source_does_not_run_installer(tmp_path):
    result = run_shell(tmp_path, "printf 'loaded\\n'")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "loaded\n"
    assert not (tmp_path / "checkout").exists()


def test_dry_run_needs_no_privileges_or_secrets_and_has_no_effects(tmp_path):
    result = run_shell(
        tmp_path,
        'ROOT_DIR="$FIXTURE_ROOT"\npreflight() { exit 99; }\nmain "$@"',
        "--dry-run",
        "--with-live",
        "--with-transcription",
        "--secrets-file",
        str(tmp_path / "unreadable-and-missing-secrets"),
    )
    assert result.returncode == 0, result.stderr
    assert "both" in result.stdout
    assert "Dry-run:" in result.stdout
    assert not (tmp_path / "checkout").exists()
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "trace").exists()


@pytest.mark.parametrize(
    "args,message",
    [
        (["--component"], "Missing value"),
        (["--manager-host", "--dry-run"], "Missing value"),
        (["--secrets-file", ""], "Missing value"),
        (["--unknown"], "Unknown option"),
        (["--component", "database", "--dry-run"], "--component"),
        (["--component", "manager", "--with-transcription", "--dry-run"], "requires a Runner"),
        (["--service-user", "root", "--dry-run"], "Invalid service"),
        (["--service-user", "name;id", "--dry-run"], "Invalid service"),
        (["--manager-host", "0.0.0.0", "--dry-run"], "wildcard"),
        (["--runner-host", "::", "--dry-run"], "wildcard"),
        (["--manager-host", "::1", "--dry-run"], "IPv4"),
        (["--runner-host", "::1", "--dry-run"], "IPv4"),
        (["--runner-host", "host/path", "--dry-run"], "Invalid host"),
        (["--manager-url", "http://manager", "--dry-run"], "Runner-only"),
    ],
)
def test_invalid_options_fail_before_host_actions(tmp_path, args, message):
    result = run_shell(tmp_path, MOCK_HOST, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert trace_lines(tmp_path) == []


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:secret@manager.internal:8081",
        "http://manager.internal:8081/private",
        "http://manager.internal:8081?token=secret",
        "http://manager.internal:8081#fragment",
        "http://manager.internal:8081/\nother",
    ],
)
def test_unsafe_manager_urls_fail_before_host_actions(tmp_path, url):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--manager-url",
        url,
        "--dry-run",
    )
    assert result.returncode != 0
    assert "secret" not in result.stdout + result.stderr
    assert trace_lines(tmp_path) == []


def test_remote_manager_ipv6_url_is_allowed_in_preview(tmp_path):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--manager-url",
        "http://[2001:db8::1]:8081",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert trace_lines(tmp_path) == []


@pytest.mark.parametrize(
    "component,roles", [("both", ["manager", "runner"]), ("manager", ["manager"])]
)
def test_fresh_install_runs_configuration_validation_and_service_steps(
    tmp_path, checkout, component, roles
):
    result = run_shell(tmp_path, MOCK_HOST, "--component", component, "--non-interactive")
    assert result.returncode == 0, result.stderr
    lines = trace_lines(tmp_path)
    assert lines[:2] == ["host preflight", "host provision"]
    for role in roles:
        assert (checkout / role / ".env").is_file()
        sync = next(
            i for i, line in enumerate(lines) if line.startswith(f"component {role} make sync ")
        )
        validate = lines.index(f"python {role} scripts/check_config.py")
        create = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(f"component {role} make create-service ")
        )
        assert sync < validate < create
    if component == "manager":
        assert not (checkout / "runner/.env").exists()
    assert any(line.startswith("helper check ") for line in lines)
    assert "private-manager" not in result.stdout + result.stderr
    assert not any("restart" in line for line in lines)


def test_remote_runner_receives_endpoint_and_selected_cpu_profile(tmp_path, checkout):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--manager-url",
        "http://manager.internal:8081",
        "--runner-host",
        "runner.internal",
        "--with-transcription",
        "--non-interactive",
        extra_env={"FIXTURE_SYNC_MODE": "transcription-cpu"},
    )
    assert result.returncode == 0, result.stderr
    lines = trace_lines(tmp_path)
    configure = next(line for line in lines if line.startswith("helper configure "))
    assert "--manager-url http://manager.internal:8081" in configure
    assert "--runner-host runner.internal" in configure
    assert "--with-transcription" in configure
    assert any(line.startswith("component runner make sync-transcription-cpu ") for line in lines)
    assert not (checkout / "manager/.env").exists()


def test_failure_then_rerun_keeps_credentials_units_and_active_services(tmp_path, checkout):
    first = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "both",
        "--non-interactive",
        extra_env={"FIXTURE_FAIL_CHECK": "1"},
    )
    assert first.returncode != 0
    assert "Failed during health checks" in first.stderr
    original = {
        role: (
            (checkout / role / ".env").read_bytes(),
            (tmp_path / f"home/.config/systemd/user/esup-runner-{role}.service").read_bytes(),
        )
        for role in ("manager", "runner")
    }
    (tmp_path / "trace").unlink()
    second = run_shell(tmp_path, MOCK_HOST, "--component", "both", "--non-interactive")
    assert second.returncode == 0, second.stderr
    lines = trace_lines(tmp_path)
    assert not any(line.startswith("component ") or "restart" in line for line in lines)
    for role, (environment, unit) in original.items():
        assert (checkout / role / ".env").read_bytes() == environment
        assert (
            tmp_path / f"home/.config/systemd/user/esup-runner-{role}.service"
        ).read_bytes() == unit
        assert f"user systemctl --user enable esup-runner-{role}.service" in lines


def test_stopped_existing_unit_is_started_without_overwriting(tmp_path, checkout):
    unit = tmp_path / "home/.config/systemd/user/esup-runner-manager.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("custom-unit\n")
    result = run_shell(tmp_path, MOCK_HOST, "--component", "manager", "--non-interactive")
    assert result.returncode == 0, result.stderr
    assert unit.read_text() == "custom-unit\n"
    lines = trace_lines(tmp_path)
    assert "user systemctl --user start esup-runner-manager.service" in lines
    assert not any("make create-service" in line for line in lines)


def test_local_live_uses_local_credentials_and_creates_dropin_before_service(tmp_path, checkout):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "both",
        "--manager-host",
        "manager.internal",
        "--with-live",
        "--non-interactive",
    )
    assert result.returncode == 0, result.stderr
    lines = trace_lines(tmp_path)
    assert (
        "python manager scripts/install_mediamtx.py setup --instance manager "
        "--rtmp-address 0.0.0.0:1935"
    ) in lines
    assert "python runner scripts/configure_live.py --instance runner" in lines
    assert not any("--manager-rtmp-host" in line for line in lines)
    dropin = tmp_path / "home/.config/systemd/user/esup-runner-runner.service.d/20-live.conf"
    assert "KillMode=control-group" in dropin.read_text()
    write = next(i for i, line in enumerate(lines) if line.startswith("user tee "))
    create = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("component runner make create-service ")
    )
    assert write < create


def test_remote_live_forwards_relay_only_on_stdin(tmp_path, checkout):
    secrets = tmp_path / "install-secrets"
    secrets.write_text("INSTALL_LIVE_RELAY_PASSWORD=fixture-private-relay\n")
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--manager-url",
        "http://manager.internal:8081",
        "--runner-host",
        "runner.internal",
        "--with-live",
        "--non-interactive",
        "--secrets-file",
        str(secrets),
    )
    assert result.returncode == 0, result.stderr
    trace = "\n".join(trace_lines(tmp_path))
    assert "helper live-password --secrets-stdin --output-fd 3" in trace
    assert "--manager-rtmp-host manager.internal --relay-password-stdin" in trace
    assert "Relay transfer diagnostic" in result.stderr
    assert "Relay transfer diagnostic" not in result.stdout
    assert "fixture-private-relay" not in trace + result.stdout + result.stderr


def test_complete_live_configuration_and_existing_unit_are_preserved(tmp_path, checkout):
    environment = checkout / "runner/.env"
    environment.write_text("FIXTURE_SECRET=preserve-custom-live-settings\n")
    unit = tmp_path / "home/.config/systemd/user/esup-runner-runner.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("custom-runner-unit\n")
    media = tmp_path / "home/.config/esup-runner/runner/mediamtx.yml"
    media.parent.mkdir(parents=True)
    media.write_text("custom-existing-media\n")
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--with-live",
        "--non-interactive",
        extra_env={"FIXTURE_LIVE_CONFIGURED": "true"},
    )
    assert result.returncode == 0, result.stderr
    lines = trace_lines(tmp_path)
    assert environment.read_text() == "FIXTURE_SECRET=preserve-custom-live-settings\n"
    assert unit.read_text() == "custom-runner-unit\n"
    assert media.read_text() == "custom-existing-media\n"
    assert not any(
        "scripts/configure_live.py" in line or " setup --instance " in line for line in lines
    )
    assert not (unit.parent / "esup-runner-runner.service.d/20-live.conf").exists()
    assert not any("restart" in line for line in lines)


def test_resumed_live_install_creates_required_dropin_for_missing_unit(tmp_path, checkout):
    environment = checkout / "runner/.env"
    environment.write_text("FIXTURE_SECRET=preserve-completed-live-settings\n")
    media = tmp_path / "home/.config/esup-runner/runner/mediamtx.yml"
    media.parent.mkdir(parents=True)
    media.write_text("completed-media\n")
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--with-live",
        "--non-interactive",
        extra_env={"FIXTURE_LIVE_CONFIGURED": "true"},
    )
    assert result.returncode == 0, result.stderr
    dropin = tmp_path / "home/.config/systemd/user/esup-runner-runner.service.d/20-live.conf"
    assert "KillMode=control-group" in dropin.read_text()
    assert environment.read_text() == "FIXTURE_SECRET=preserve-completed-live-settings\n"
    lines = trace_lines(tmp_path)
    assert not any("scripts/configure_live.py" in line for line in lines)
    write = next(i for i, line in enumerate(lines) if line.startswith("user tee "))
    create = next(
        i
        for i, line in enumerate(lines)
        if line.startswith("component runner make create-service ")
    )
    assert write < create


def test_busy_ports_fail_before_starting_applications(tmp_path, checkout):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "both",
        "--non-interactive",
        extra_env={"FIXTURE_FAIL_PORTS": "1"},
    )
    assert result.returncode != 0
    lines = trace_lines(tmp_path)
    assert not any(
        "make create-service" in line or "--user start esup-runner-" in line for line in lines
    )
    assert not any("restart" in line for line in lines)


@pytest.mark.parametrize(
    "command,flags",
    [
        ("live-configured", ["--with-live"]),
        ("live-enabled", []),
        ("local-manager", ["--with-live"]),
    ],
)
def test_live_inspection_failure_cannot_be_treated_as_disabled(tmp_path, checkout, command, flags):
    result = run_shell(
        tmp_path,
        MOCK_HOST,
        "--component",
        "runner",
        "--non-interactive",
        *flags,
        extra_env={"FIXTURE_FAIL_HELPER": command},
    )
    assert result.returncode != 0
    lines = trace_lines(tmp_path)
    assert not any(
        "scripts/configure_live.py" in line or "make create-service" in line for line in lines
    )
    assert not any("--user start esup-runner-" in line or "restart" in line for line in lines)


def test_sparse_checkout_reports_missing_sources_without_modifying_git(tmp_path, checkout):
    (checkout / "runner/Makefile").unlink()
    result = run_shell(tmp_path, MOCK_HOST, "--component", "both", "--non-interactive")
    assert result.returncode != 0
    assert "git sparse-checkout add runner scripts" in result.stderr
    lines = trace_lines(tmp_path)
    assert not any(
        "git clone" in line or "git pull" in line or "git reset" in line for line in lines
    )
    assert not (checkout / "manager/.env").exists()


def test_sparse_checkout_missing_helper_gives_recovery_command(tmp_path, checkout):
    (checkout / "scripts/install_stack.py").unlink()
    result = run_shell(tmp_path, MOCK_HOST, "--component", "manager", "--non-interactive")
    assert result.returncode != 0
    assert "git sparse-checkout add" in result.stderr
    assert "scripts" in result.stderr


def test_service_commands_drop_operator_environment(tmp_path):
    body = r"""
SERVICE_HOME="$FIXTURE_HOME"
SERVICE_UID=12345
runuser() { printf '%s\n' "$@"; }
as_user make sync
"""
    result = run_shell(
        tmp_path,
        body,
        extra_env={"SUDO_USER": "operator", "RUNNER_TOKEN": "operator-private-token"},
    )
    assert result.returncode == 0, result.stderr
    args = result.stdout.splitlines()
    assert args[:5] == ["-u", "esup-runner", "--", "env", "-i"]
    assert f"UV_CACHE_DIR={tmp_path}/home/.cache/esup-runner/uv" in args
    assert "XDG_RUNTIME_DIR=/run/user/12345" in args
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/12345/bus" in args
    assert not any("SUDO_USER" in arg or "operator-private-token" in arg for arg in args)
    assert args[-2:] == ["make", "sync"]


def test_component_commands_run_from_component_as_service_account(tmp_path, checkout):
    body = r"""
ROOT_DIR="$FIXTURE_ROOT"
as_user() { printf 'as-user %s %s\n' "$PWD" "$*"; }
component_command runner make sync
"""
    result = run_shell(tmp_path, body)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"as-user {checkout}/runner make sync"


@pytest.mark.parametrize(
    "component,needs_ffmpeg,cache_mode,cache_uid,success",
    [
        ("manager", False, "", "", True),
        ("runner", True, "750", "12345", True),
        ("runner", True, "777", "12345", False),
        ("runner", True, "750", "99999", False),
    ],
)
def test_provisioning_keeps_downloads_and_python_under_service_account(
    tmp_path, component, needs_ffmpeg, cache_mode, cache_uid, success
):
    body = r"""
ROOT_DIR="$FIXTURE_ROOT"
COMPONENT="$1"
trace() { printf '%s\n' "$*" >> "$TRACE_FILE"; }
apt-get() { trace "root apt-get $*"; }
id() {
  case "$1" in
    -u) printf '12345\n' ;;
    -gn) printf 'fixture\n' ;;
    *) return 0 ;;
  esac
}
getent() { printf 'esup-runner:x:12345:12345:fixture:%s:/bin/bash\n' "$FIXTURE_HOME"; }
stat() {
  if [[ "$2" == %a ]]; then
    printf '%s\n' "$FIXTURE_CACHE_MODE"
  elif [[ "$3" == "$FIXTURE_HOME/.cache/esup-runner/uv" ]]; then
    printf '%s\n' "$FIXTURE_CACHE_UID"
  else
    printf '12345\n'
  fi
}
install() { trace "root install $*"; }
loginctl() { trace "root loginctl $*"; }
systemctl() { trace "root systemctl $*"; }
as_user() {
  trace "user $*"
  if [[ "$1 ${2:-}" == 'test -x' ]]; then return 1; fi
  return 0
}
provision_host
"""
    cache_dir = tmp_path / "home/.cache/esup-runner/uv"
    if cache_mode:
        cache_dir.mkdir(parents=True)
    result = run_shell(
        tmp_path,
        body,
        component,
        extra_env={"FIXTURE_CACHE_MODE": cache_mode, "FIXTURE_CACHE_UID": cache_uid},
    )
    lines = trace_lines(tmp_path)
    packages = next(line for line in lines if line.startswith("root apt-get install "))
    assert ("ffmpeg" in packages.split()) is needs_ffmpeg
    if not success:
        assert result.returncode != 0
        assert "uv cache must belong" in result.stderr
        assert not any(line.startswith("user bash ") or "python install " in line for line in lines)
        assert not (tmp_path / "checkout").exists()
        assert list(cache_dir.iterdir()) == []
        return
    assert result.returncode == 0, result.stderr
    bootstrap = next(line for line in lines if line.startswith("user bash -o pipefail -c "))
    assert 'cd "$HOME"' in bootstrap
    assert "curl --proto" in bootstrap
    assert any(
        line.startswith(f"user {tmp_path}/home/.local/bin/uv ")
        and f"--directory {tmp_path}/home" in line
        and "python install 3.12" in line
        for line in lines
    )
    assert "root loginctl enable-linger esup-runner" in lines
    assert "root systemctl start user@12345.service" in lines
    assert not any(line.startswith(("root make ", "root bash ", "root uv ")) for line in lines)
    assert not (tmp_path / "checkout").exists()
    assert cache_dir.exists() == bool(cache_mode)
    assert not any(
        line.startswith("root install ")
        and line.split()[-1].startswith(("/var/lib/", "/tmp/"))
        and line.split()[-1] != str(tmp_path / "checkout")
        for line in lines
    )


def test_help_lists_supported_modes(tmp_path):
    result = run_shell(tmp_path, 'main "$@"', "--help")
    assert result.returncode == 0
    for option in (
        "--component",
        "--with-live",
        "--with-transcription",
        "--dry-run",
        "--secrets-file",
    ):
        assert option in result.stdout


def test_reject_symlinked_installation_destination(tmp_path):
    destination = tmp_path / "target"
    destination.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(destination, target_is_directory=True)
    result = run_shell(tmp_path, f"reject_symlinks {shlex.quote(str(link / 'child'))}")
    assert result.returncode != 0
    assert "symbolic link" in result.stderr
