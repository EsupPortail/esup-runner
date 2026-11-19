"""Regression tests for private configuration created by the native installer."""

import argparse
import importlib.util
import io
import json
import os
import shutil
import stat
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import bcrypt
import pytest
from dotenv import dotenv_values

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture
def installer(monkeypatch):
    """Load the repository helper without importing either application package."""
    spec = importlib.util.spec_from_file_location(
        "install_stack_config", REPOSITORY / "scripts/install_stack.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original_gensalt = bcrypt.gensalt
    monkeypatch.setattr(module.bcrypt, "gensalt", lambda: original_gensalt(rounds=4))
    return module


@pytest.fixture
def stack(tmp_path):
    """Copy only public examples into an isolated, initially unconfigured stack."""
    for name in ("manager", "runner"):
        (tmp_path / name).mkdir()
        shutil.copy(REPOSITORY / name / ".env.example", tmp_path / name / ".env.example")
    return tmp_path


def options(stack, **overrides):
    """Build the same defaults passed by the shell entrypoint."""
    values = {
        "root": stack,
        "component": "both",
        "service_user": "esup-runner",
        "service_home": "/home/esup-runner",
        "manager_host": "127.0.0.1",
        "runner_host": "127.0.0.1",
        "manager_url": "",
        "with_transcription": False,
        "with_live": False,
        "non_interactive": True,
        "timeout": 0.01,
    }
    return argparse.Namespace(**(values | overrides))


def env(stack, component):
    """Read generated dotenv data without expanding bcrypt dollar signs."""
    return dotenv_values(stack / component / ".env", interpolate=False)


@pytest.mark.parametrize("component", ["manager", "runner", "both"])
def test_fresh_scopes_use_private_files_and_matching_credentials(
    installer, stack, component, capsys
):
    args = options(stack, component=component)
    if component == "runner":
        args.manager_url = "http://manager.internal:8081"
    credentials = {
        "INSTALL_ADMIN_PASSWORD": "private-admin-password",
        "INSTALL_RUNNER_TOKEN": "private-existing-manager-token",
    }

    installer.configure(args, credentials)

    for name in installer.selected_components(component):
        assert stat.S_IMODE((stack / name / ".env").stat().st_mode) == 0o600
        values = env(stack, name)
        assert values["SERVICE_USER"] == "esup-runner"
        assert values["UV_CACHE_DIR"] == "/home/esup-runner/.cache/esup-runner/uv"
        assert "CHANGE_ME" not in " ".join(value or "" for value in values.values())
    if component != "runner":
        manager = env(stack, "manager")
        assert bcrypt.checkpw(
            b"private-admin-password", manager["ADMIN_USERS__admin"].encode("ascii")
        )
        assert "MANAGER_DATA_DIR" not in manager  # Let the Manager use its default data directory.
        assert manager["RUNNERS_STORAGE_DIR"] == "/tmp/esup-runner"
        assert manager["MANAGER_BIND_HOST"] == "127.0.0.1"
        assert manager["AUTHORIZED_TOKENS__runners"] != manager["AUTHORIZED_TOKENS__app"]
    if component != "manager":
        runner = env(stack, "runner")
        assert runner["RUNNER_TASK_TYPES"] == "[1x(encoding,studio)]"
        assert runner["STORAGE_DIR"] == "/tmp/esup-runner"
        assert runner["RUNNER_BASE_NAME"] == "my-runner"
        assert runner["RUNNER_MONITORING"] == "false"
        expected = (
            env(stack, "manager")["AUTHORIZED_TOKENS__runners"]
            if component == "both"
            else credentials["INSTALL_RUNNER_TOKEN"]
        )
        assert runner["RUNNER_TOKEN"] == expected
    output = capsys.readouterr().out
    assert all(value not in output for value in credentials.values())


def test_adding_runner_reuses_existing_manager_without_rotating_secrets(installer, stack):
    installer.configure(
        options(stack, component="manager", manager_host="manager.internal"),
        {"INSTALL_ADMIN_PASSWORD": "keep-this-password"},
    )
    manager_path = stack / "manager/.env"
    before = manager_path.read_bytes()

    installer.configure(options(stack, component="runner", runner_host="runner.internal"), {})

    assert manager_path.read_bytes() == before
    runner = env(stack, "runner")
    assert runner["MANAGER_URL"] == "http://manager.internal:8081"
    assert runner["RUNNER_TOKEN"] == env(stack, "manager")["AUTHORIZED_TOKENS__runners"]


def test_retry_preserves_existing_configuration_byte_for_byte(installer, stack, monkeypatch):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "keep-this-password"})
    initial = {name: (stack / name / ".env").read_bytes() for name in ("manager", "runner")}
    monkeypatch.setattr(
        installer.getpass, "getpass", lambda prompt: pytest.fail("Unexpected prompt")
    )

    installer.configure(options(stack, non_interactive=False), {})

    for name, content in initial.items():
        assert (stack / name / ".env").read_bytes() == content


def test_input_error_does_not_leave_half_generated_stack(installer, stack):
    with pytest.raises(ValueError, match="Administrator password"):
        installer.configure(options(stack), {})
    assert not (stack / "manager/.env").exists()
    assert not (stack / "runner/.env").exists()


def test_failed_second_write_can_be_resumed_without_secret_rotation(installer, stack, monkeypatch):
    original = installer.create_env

    def fail_runner(path, content):
        if path.parent.name == "runner":
            raise OSError("simulated disk failure")
        original(path, content)

    monkeypatch.setattr(installer, "create_env", fail_runner)
    with pytest.raises(OSError):
        installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    initial = (stack / "manager/.env").read_bytes()
    monkeypatch.setattr(installer, "create_env", original)

    installer.configure(options(stack), {})

    assert (stack / "manager/.env").read_bytes() == initial
    assert (
        env(stack, "runner")["RUNNER_TOKEN"] == env(stack, "manager")["AUTHORIZED_TOKENS__runners"]
    )


def test_atomic_writer_never_clobbers_configuration(installer, tmp_path):
    path = tmp_path / ".env"
    path.write_text("PRIVATE=existing\n")
    with pytest.raises(FileExistsError):
        installer.create_env(path, "PRIVATE=replacement\n")
    assert path.read_text() == "PRIVATE=existing\n"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("component", ["manager", "runner"])
def test_configuration_symlinks_are_rejected(installer, stack, tmp_path, component):
    target = tmp_path / "private-target"
    target.write_text("PRIVATE=existing\n")
    (stack / component / ".env").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    assert target.read_text() == "PRIVATE=existing\n"


@pytest.mark.parametrize("flag", ["with_live", "with_transcription"])
def test_optional_features_do_not_rewrite_existing_configuration(installer, stack, flag):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    before = (stack / "runner/.env").read_bytes()
    with pytest.raises(ValueError, match="existing"):
        installer.configure(options(stack, **{flag: True}), {})
    assert (stack / "runner/.env").read_bytes() == before


def test_existing_service_account_mismatch_is_rejected(installer, stack):
    (stack / "manager/.env").write_text("SERVICE_USER=another-user\n")
    with pytest.raises(ValueError, match="SERVICE_USER"):
        installer.configure(options(stack, component="manager"), {})


def test_cpu_transcription_and_live_have_separate_capacity(installer, stack):
    installer.configure(
        options(stack, with_live=True, with_transcription=True),
        {"INSTALL_ADMIN_PASSWORD": "private-password"},
    )
    runner = env(stack, "runner")
    assert runner["RUNNER_TASK_TYPES"] == "[1x(encoding,studio,transcription),1x(live)]"
    assert runner["RUNNER_INSTANCES"] == "2"
    info = installer.inspect_configuration(stack, "both")
    assert info["runner"]["sync_mode"] == "transcription-cpu"
    assert info["runner"]["urls"] == ["http://127.0.0.1:8082", "http://127.0.0.1:8083"]


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "bad host",
        "host\nINJECT=true",
        "https://host",
        "$(id)",
        "999.999.999.999",
    ],
)
def test_unsafe_advertised_hosts_are_rejected(installer, value):
    with pytest.raises(ValueError):
        installer.validate_host(value)


@pytest.mark.parametrize(
    "value", ["manager.internal", "127.0.0.1", "10.0.0.1", "::1", "2001:db8::1"]
)
def test_dns_and_ip_hosts_are_supported(installer, value):
    assert installer.validate_host(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "ftp://manager.internal",
        "http://username:secret@manager.internal",
        "http://manager.internal/api",
        "http://manager.internal?token=secret",
        "http://manager.internal#secret",
        "http://manager.internal:0",
        "http://manager.internal:65536",
        "http://manager.internal\nINJECT=true",
        "http://0.0.0.0:8081",
    ],
)
def test_unsafe_manager_urls_are_rejected(installer, value):
    with pytest.raises(ValueError):
        installer.validate_manager_url(value)


@pytest.mark.parametrize("component", ["manager", "runner"])
def test_native_ipv6_is_rejected_without_changing_remote_url_support(installer, stack, component):
    with pytest.raises(ValueError, match="DNS name or IPv4"):
        installer.configure(
            options(stack, **{component + "_host": "::1"}),
            {"INSTALL_ADMIN_PASSWORD": "private-password"},
        )
    assert installer.validate_manager_url("http://[::1]:8081") == "http://[::1]:8081"
    assert not (stack / "manager/.env").exists()


@pytest.mark.parametrize("token", ["CHANGE_ME_RUNNER_TOKEN", "unsafe\nTOKEN=yes", "$(unsafe)", ""])
def test_remote_runner_requires_a_usable_token(installer, stack, token):
    with pytest.raises(ValueError, match="token"):
        installer.configure(
            options(stack, component="runner", manager_url="http://manager.internal:8081"),
            {"INSTALL_RUNNER_TOKEN": token},
        )
    assert not (stack / "runner/.env").exists()


def test_hidden_password_prompt_requires_matching_confirmation(installer, stack, monkeypatch):
    replies = iter(["private-password", "different-password"])
    monkeypatch.setattr(installer.getpass, "getpass", lambda prompt: next(replies))
    with pytest.raises(ValueError, match="do not match"):
        installer.configure(options(stack, non_interactive=False), {})
    assert not (stack / "manager/.env").exists()


def test_inspection_and_summary_do_not_expose_credentials(installer, stack, capsys):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    capsys.readouterr()
    assert installer.main(["inspect", "--root", str(stack), "--component", "both"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert "runner" in info and "manager" in info
    assert installer.main(["summary", "--root", str(stack), "--component", "both"]) == 0
    output = capsys.readouterr().out
    for key, value in env(stack, "manager").items():
        if key.startswith(("AUTHORIZED_TOKENS__", "ADMIN_USERS__")):
            assert value not in json.dumps(info) + output


def test_secrets_stdin_supports_unattended_setup(installer, stack, monkeypatch):
    monkeypatch.setattr(
        installer.sys, "stdin", io.StringIO("INSTALL_ADMIN_PASSWORD=private-password\n")
    )
    assert (
        installer.main(
            [
                "configure",
                "--root",
                str(stack),
                "--component",
                "both",
                "--secrets-stdin",
                "--non-interactive",
            ]
        )
        == 0
    )
    assert (stack / "runner/.env").exists()


def test_health_checks_require_actual_online_runner_registration(installer, stack, monkeypatch):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    requested = []

    def request(url, token="", timeout=5.0):
        requested.append((url, token))
        if url.endswith("/api/health"):
            return {"status": "healthy"}
        if url.endswith("/api/runners"):
            return {
                "runners": [
                    {
                        "id": "runner-0",
                        "url": "http://127.0.0.1:8082",
                        "status": "online",
                    }
                ]
            }
        return {"status": "healthy", "registered": True, "runner_id": "runner-0"}

    monkeypatch.setattr(installer, "request_json", request)
    installer.check_runtime(options(stack, timeout=1))
    assert requested[0][1] == env(stack, "runner")["RUNNER_TOKEN"]
    assert requested[-1][1] == ""


@pytest.mark.parametrize("failure", ["offline", "wrong-id", "unregistered", "network"])
def test_unready_services_fail_without_printing_credentials(installer, stack, monkeypatch, failure):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})

    def request(url, token="", timeout=5.0):
        if failure == "network":
            raise URLError("network unavailable")
        if url.endswith("/api/health"):
            return {"status": "healthy"}
        if url.endswith("/api/runners"):
            return {
                "runners": [
                    {
                        "id": "runner-0",
                        "url": "http://127.0.0.1:8082",
                        "status": "offline" if failure == "offline" else "online",
                    }
                ]
            }
        return {
            "status": "healthy",
            "registered": failure != "unregistered",
            "runner_id": "another-runner" if failure == "wrong-id" else "runner-0",
        }

    monkeypatch.setattr(installer, "request_json", request)
    with pytest.raises(ValueError, match="registration"):
        installer.check_runtime(options(stack))


def test_redirects_never_forward_credentials(installer):
    assert (
        installer.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere")
        is None
    )


def test_adding_manager_reuses_matching_unselected_runner_token(installer, stack):
    installer.configure(
        options(stack, component="runner", manager_url="http://127.0.0.1:8081"),
        {"INSTALL_RUNNER_TOKEN": "keep-this-runner-token"},
    )
    initial = (stack / "runner/.env").read_bytes()
    installer.configure(
        options(stack, component="manager"), {"INSTALL_ADMIN_PASSWORD": "private-password"}
    )
    assert env(stack, "manager")["AUTHORIZED_TOKENS__runners"] == "keep-this-runner-token"
    assert (stack / "runner/.env").read_bytes() == initial


def test_live_manager_retry_works_before_connection_helper_runs(installer, stack):
    args = options(stack, component="manager", with_live=True)
    installer.configure(args, {"INSTALL_ADMIN_PASSWORD": "private-password"})
    initial = (stack / "manager/.env").read_bytes()
    assert env(stack, "manager")["LIVE_ENABLED"] == "true"
    installer.configure(args, {})
    assert (stack / "manager/.env").read_bytes() == initial


def test_remote_live_requires_relay_secret_before_creating_files(installer, stack):
    args = options(
        stack, component="runner", manager_url="http://remote.internal:8081", with_live=True
    )
    with pytest.raises(ValueError, match="Live relay password"):
        installer.configure(args, {"INSTALL_RUNNER_TOKEN": "private-api-token"})
    assert not (stack / "runner/.env").exists()


def test_adding_runner_to_local_live_manager_reuses_local_relay(installer, stack):
    service_home = stack / "service-home"
    args = options(stack, component="manager", with_live=True, service_home=str(service_home))
    installer.configure(args, {"INSTALL_ADMIN_PASSWORD": "private-password"})
    media = service_home / ".config/esup-runner/manager/mediamtx.yml"
    media.parent.mkdir(parents=True)
    media.write_text("# Existing local relay credentials\n")

    installer.configure(
        options(stack, component="runner", with_live=True, service_home=str(service_home)), {}
    )

    assert "live" in env(stack, "runner")["RUNNER_TASK_TYPES"]
    assert (
        env(stack, "runner")["RUNNER_TOKEN"] == env(stack, "manager")["AUTHORIZED_TOKENS__runners"]
    )


def test_local_manager_without_live_fails_before_creating_live_runner(installer, stack):
    installer.configure(
        options(stack, component="manager"), {"INSTALL_ADMIN_PASSWORD": "private-password"}
    )
    with pytest.raises(ValueError, match="local Manager"):
        installer.configure(options(stack, component="runner", with_live=True), {})
    assert not (stack / "runner/.env").exists()


@pytest.mark.parametrize(
    "command,expected",
    [("sync-mode", "base"), ("manager-host", "127.0.0.1"), ("local-manager", "true")],
)
def test_shell_inspection_contracts(installer, stack, capsys, command, expected):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    capsys.readouterr()
    assert installer.main([command, "--root", str(stack)]) == 0
    assert capsys.readouterr().out.strip() == expected


def test_manager_host_command_uses_effective_manager_configuration(installer, stack, capsys):
    installer.configure(
        options(stack, component="manager", manager_host="manager.internal"),
        {"INSTALL_ADMIN_PASSWORD": "private-password"},
    )
    capsys.readouterr()
    assert installer.main(["manager-host", "--root", str(stack), "--component", "manager"]) == 0
    assert capsys.readouterr().out.strip() == "manager.internal"


def test_local_manager_never_matches_a_different_remote_target(installer, stack):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})
    runner = stack / "runner/.env"
    runner.write_text(
        runner.read_text().replace("http://127.0.0.1:8081", "http://remote.internal:8081")
    )
    assert installer.is_local_manager(stack) is False


@pytest.mark.parametrize("component", ["manager", "runner"])
def test_live_detection_distinguishes_planned_from_complete_connections(
    installer, stack, component, capsys
):
    installer.configure(
        options(stack, with_live=True), {"INSTALL_ADMIN_PASSWORD": "private-password"}
    )
    capsys.readouterr()
    assert installer.main(["live-enabled", "--root", str(stack), "--component", component]) == 0
    assert capsys.readouterr().out.strip() == "true"
    assert installer.live_configured(stack, component) is False
    path = stack / component / ".env"
    with path.open("a") as stream:
        stream.write("LIVE_MEDIAMTX_API_PASSWORD=private-control-password\n")
        if component == "runner":
            stream.write("LIVE_UPSTREAM_RTMP_URL=rtmp://relay:private@manager.internal:1935\n")
            stream.write("LIVE_LOCAL_RTMP_URL=rtmp://transcoder:private@127.0.0.1:2935\n")
    assert installer.main(["live-configured", "--root", str(stack), "--component", component]) == 0
    assert capsys.readouterr().out.strip() == "true"


@pytest.fixture
def relay_pipe():
    """Provide a dedicated transfer pipe, separate from captured diagnostics."""
    reader, writer = os.pipe()
    try:
        yield reader, writer
    finally:
        os.close(reader)
        os.close(writer)


def test_live_password_command_only_emits_supplied_relay_secret(
    installer, relay_pipe, monkeypatch, capsys
):
    reader, writer = relay_pipe
    monkeypatch.setattr(
        installer.sys,
        "stdin",
        io.StringIO(
            "INSTALL_LIVE_RELAY_PASSWORD='private-relay-$ecret-é'\n"
            "INSTALL_ADMIN_PASSWORD=private-admin-secret\n"
        ),
    )
    assert installer.main(["live-password", "--secrets-stdin", "--output-fd", str(writer)]) == 0
    assert os.read(reader, 4096) == "private-relay-$ecret-é\n".encode("utf-8")
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_live_password_command_requires_explicit_output(installer, monkeypatch, capsys):
    monkeypatch.setattr(
        installer.sys, "stdin", io.StringIO("INSTALL_LIVE_RELAY_PASSWORD=private-relay-secret\n")
    )
    with pytest.raises(SystemExit) as error:
        installer.main(["live-password", "--secrets-stdin"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--output-fd" in captured.err
    assert "private-relay-secret" not in captured.err


@pytest.mark.parametrize("output_fd", [-1, 0, 1, 2])
def test_live_password_command_rejects_standard_descriptors(
    installer, monkeypatch, capsys, output_fd
):
    monkeypatch.setattr(
        installer.sys, "stdin", io.StringIO("INSTALL_LIVE_RELAY_PASSWORD=private-relay-secret\n")
    )
    assert installer.main(["live-password", "--secrets-stdin", "--output-fd", str(output_fd)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dedicated pipe" in captured.err
    assert "private-relay-secret" not in captured.err


def test_live_password_command_rejects_regular_files(installer, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        installer.sys, "stdin", io.StringIO("INSTALL_LIVE_RELAY_PASSWORD=private-relay-secret\n")
    )
    destination = tmp_path / "diagnostics.log"
    with destination.open("w") as stream:
        assert (
            installer.main(
                ["live-password", "--secrets-stdin", "--output-fd", str(stream.fileno())]
            )
            == 1
        )
    assert destination.read_bytes() == b""
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "dedicated pipe" in captured.err
    assert "private-relay-secret" not in captured.err


@pytest.mark.parametrize("password", ["", "CHANGE_ME_RELAY_PASSWORD", "private\nrelay-secret"])
def test_live_password_command_rejects_invalid_secrets_without_writing(
    installer, relay_pipe, monkeypatch, capsys, password
):
    reader, writer = relay_pipe
    monkeypatch.setattr(
        installer, "input_secrets", lambda: {"INSTALL_LIVE_RELAY_PASSWORD": password}
    )
    assert installer.main(["live-password", "--secrets-stdin", "--output-fd", str(writer)]) == 1
    os.set_blocking(reader, False)
    with pytest.raises(BlockingIOError):
        os.read(reader, 4096)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "missing, unsafe, or still an example" in captured.err
    assert "CHANGE_ME" not in captured.err
    assert "relay-secret" not in captured.err


def test_live_password_command_handles_pipe_errors_privately(installer, monkeypatch, capsys):
    reader, writer = os.pipe()
    os.close(reader)
    monkeypatch.setattr(
        installer.sys, "stdin", io.StringIO("INSTALL_LIVE_RELAY_PASSWORD=private-relay-secret\n")
    )
    try:
        assert installer.main(["live-password", "--secrets-stdin", "--output-fd", str(writer)]) == 1
    finally:
        os.close(writer)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "private-relay-secret" not in captured.err


def test_port_probe_checks_every_configured_listening_port(installer, stack, monkeypatch):
    installer.configure(
        options(stack, with_live=True), {"INSTALL_ADMIN_PASSWORD": "private-password"}
    )
    addresses = []
    monkeypatch.setattr(
        installer.socket, "getaddrinfo", lambda *args, **kwargs: [(installer.socket.AF_INET,)]
    )

    def create(address, *, family):
        addresses.append(address)
        return nullcontext()

    monkeypatch.setattr(installer.socket, "create_server", create)
    assert installer.main(["check-ports", "--root", str(stack), "--component", "both"]) == 0
    assert addresses == [("127.0.0.1", 8081), ("127.0.0.1", 8082), ("127.0.0.1", 8083)]


@pytest.mark.parametrize("failure", ["occupied", "unresolvable"])
def test_port_probe_reports_unusable_local_addresses(installer, stack, monkeypatch, failure):
    installer.configure(options(stack), {"INSTALL_ADMIN_PASSWORD": "private-password"})

    def fail(*args, **kwargs):
        raise OSError("simulated failure")

    monkeypatch.setattr(
        installer.socket, "getaddrinfo", lambda *args, **kwargs: [(installer.socket.AF_INET,)]
    )
    monkeypatch.setattr(installer.socket, "create_server", fail)
    if failure == "unresolvable":
        monkeypatch.setattr(installer.socket, "getaddrinfo", fail)
    with pytest.raises(ValueError, match="cannot bind"):
        installer.check_ports(stack, "runner")


def test_json_transport_sets_auth_without_following_redirects(installer, monkeypatch):
    requests = []

    def open_request(request, *, timeout):
        requests.append((request, timeout))
        return io.BytesIO(b'{"status":"healthy"}')

    monkeypatch.setattr(
        installer, "build_opener", lambda handler: SimpleNamespace(open=open_request)
    )
    assert installer.request_json("http://manager.internal/api/health", "private-token") == {
        "status": "healthy"
    }
    assert requests[0][0].get_header("X-api-token") == "private-token"
    assert requests[0][1] == 5.0


def test_request_deadline_is_recomputed_per_call(installer, monkeypatch):
    monkeypatch.setattr(installer.time, "monotonic", lambda: 9.5)
    requested = []
    monkeypatch.setattr(
        installer, "request_json", lambda url, token, timeout: requested.append(timeout) or {}
    )
    installer.request_before(10.0, "http://manager.internal/api/health")
    assert requested == [0.5]
    with pytest.raises(ValueError, match="deadline"):
        installer.request_before(9.0, "http://manager.internal/api/health")
