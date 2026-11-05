"""Exercise privileged service preparation without root, network or real systemd calls."""

import importlib.util
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_mediamtx.py"
spec = importlib.util.spec_from_file_location("system_mediamtx_installer", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def system_installation(tmp_path, monkeypatch, request):
    home = tmp_path / getattr(request, "param", "media-home")
    directory = home / ".config/esup-runner/manager"
    directory.mkdir(parents=True)
    (directory / "mediamtx.yml").write_text(
        "rtmp: true\nrtmpEncryption: optional\nrtmpsAddress: :443\n"
    )
    (directory / "mediamtx.env").write_text("MTX_RTMPADDRESS=192.0.2.10:1935\n")
    binary = home / ".local/bin/mediamtx"
    binary.parent.mkdir(parents=True)
    binary.write_text("prepared executable")
    binary.chmod(0o755)
    account = pwd.struct_passwd(("media", "x", 61000, 61001, "", str(home), "/bin/sh"))
    units = tmp_path / "system-units"
    units.mkdir()
    monkeypatch.setattr(installer, "SYSTEMD_SYSTEM_DIR", units)
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda _: account)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.os, "getgrouplist", lambda *_: [61001, 61002])
    monkeypatch.setattr(installer, "require_system_owned", lambda _: None)
    monkeypatch.setattr(installer, "download", lambda *_: pytest.fail("Root must not download"))
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "show" in command:
            unit = units / "esup-runner-mediamtx@manager.service"
            properties = (
                f"LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\nFragmentPath={unit}\nDropInPaths=\n"
                if unit.exists()
                else "LoadState=not-found\n"
            )
            return subprocess.CompletedProcess(command, 0, stdout=properties)
        return subprocess.CompletedProcess(
            command, 0, stdout="v1.21.1\n" if command[-1] == "--version" else ""
        )

    real_run = subprocess.run
    monkeypatch.setattr(installer.subprocess, "run", run)
    return SimpleNamespace(
        home=home,
        directory=directory,
        binary=binary,
        units=units,
        calls=calls,
        real_run=real_run,
        options=["start", "--instance", "manager", "--service-mode", "system", "--user", "media"],
    )


def test_system_mode_validates_as_service_user_and_installs_an_instance_unit(
    system_installation, monkeypatch
):
    state = system_installation
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.setenv("LD_PRELOAD", "untrusted-library")
    monkeypatch.setenv("MTX_AUTHMETHOD", "unrelated-secret")
    before = {p: p.read_bytes() for p in state.directory.iterdir()}
    assert installer.main(state.options) == 0
    unit = state.units / "esup-runner-mediamtx@manager.service"
    content = unit.read_text()
    for setting in (
        "User=media",
        "Group=61001",
        "AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "NoNewPrivileges=true",
        "WantedBy=multi-user.target",
        str(state.directory),
        str(state.binary),
    ):
        assert setting in content
    assert "%h" not in content and "/root" not in content
    assert unit.stat().st_mode & 0o777 == 0o644
    version, validate = [
        (command, options) for command, options in state.calls if command[0] == str(state.binary)
    ]
    assert version[0] == [str(state.binary), "--version"]
    assert validate[0][-1] == f"--validate-conf={state.directory / 'mediamtx.yml'}"
    for _, options in (version, validate):
        assert options["user"] == 61000 and options["group"] == 61001
        assert options["extra_groups"] == [61001, 61002]
        assert options["capture_output"] and options["check"]
        assert options["env"]["HOME"] == str(state.home)
        assert options["env"]["MTX_RTMPADDRESS"] == "192.0.2.10:1935"
        assert "LD_PRELOAD" not in options["env"] and "MTX_AUTHMETHOD" not in options["env"]
    assert [command for command, _ in state.calls[3:]] == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "esup-runner-mediamtx@manager.service"],
        ["systemctl", "is-active", "--quiet", "esup-runner-mediamtx@manager.service"],
    ]
    inode = unit.stat().st_ino
    assert installer.main(state.options) == 0
    assert unit.stat().st_ino == inode
    assert all("restart" not in command for command, _ in state.calls)
    assert before == {p: p.read_bytes() for p in state.directory.iterdir()}


def test_system_dry_run_does_not_require_root_or_start_processes(system_installation, monkeypatch):
    state = system_installation
    monkeypatch.setattr(installer.os, "geteuid", os.getuid)
    assert installer.main(state.options) == 1
    assert installer.main(state.options + ["--dry-run"]) == 0
    assert not list(state.units.iterdir()) and not state.calls


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd parser unavailable")
@pytest.mark.parametrize(
    "system_installation", ["media-home", "media % spaced home"], indirect=True
)
def test_generated_unit_is_accepted_by_the_systemd_parser(system_installation):
    state = system_installation
    assert installer.main(state.options) == 0
    unit = state.units / "esup-runner-mediamtx@manager.service"
    result = state.real_run(
        ["systemd-analyze", "verify", "--man=no", str(unit)], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert "EnvironmentFile= path is not absolute" not in result.stderr
    assert "WorkingDirectory= path is not absolute" not in result.stderr


@pytest.mark.parametrize(
    "system_installation", ['bad"quote', "bad\\backslash", "bad$dollar"], indirect=True
)
def test_system_start_refuses_unsupported_executable_paths(system_installation):
    state = system_installation
    assert installer.main(state.options) == 1
    assert not state.calls and not list(state.units.iterdir())


@pytest.mark.parametrize(
    "problem",
    [
        "unprepared",
        "missing_config",
        "missing_binary",
        "custom_unit",
        "symlink",
        "drop_in",
        "enabled_user",
    ],
)
def test_system_preflight_refuses_unsafe_or_conflicting_installations(
    system_installation, monkeypatch, problem
):
    state = system_installation
    unit = state.units / "esup-runner-mediamtx@manager.service"
    if problem == "unprepared":
        shutil.rmtree(state.directory)
    elif problem == "missing_config":
        (state.directory / "mediamtx.env").unlink()
    elif problem == "missing_binary":
        state.binary.unlink()
    elif problem == "custom_unit":
        unit.write_text("retain customized service")
    elif problem == "symlink":
        unit.symlink_to(state.binary)
    elif problem == "drop_in":
        (state.units / (unit.name + ".d")).mkdir()
    else:
        enabled = state.home / ".config/systemd/user/default.target.wants" / unit.name
        enabled.parent.mkdir(parents=True)
        enabled.symlink_to("../esup-runner-mediamtx@.service")
    assert installer.main(state.options) == 1
    assert not state.calls
    if problem == "custom_unit":
        assert unit.read_text() == "retain customized service"


@pytest.mark.parametrize("name, uid", [("root", 0), ("bad\nUser=root", 61000)])
def test_system_account_must_be_non_root_and_safe(system_installation, monkeypatch, name, uid):
    state = system_installation
    monkeypatch.setattr(
        installer.pwd,
        "getpwnam",
        lambda _: pwd.struct_passwd((name, "x", uid, 61001, "", str(state.home), "/bin/sh")),
    )
    assert installer.main(state.options) == 1
    assert not state.calls


def test_system_account_must_exist(system_installation, monkeypatch):
    def missing(_):
        raise KeyError("media")

    monkeypatch.setattr(installer.pwd, "getpwnam", missing)
    assert installer.main(system_installation.options) == 1
    assert not system_installation.calls


@pytest.mark.parametrize("version", ["v1.20.0", "devel", "v1.21.2-rc1"])
def test_system_mode_rejects_unsupported_binary_before_installing(
    system_installation, monkeypatch, version
):
    state = system_installation
    original = installer.subprocess.run

    def run(command, **kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, stdout=version)
        return original(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.main(state.options) == 1
    assert not list(state.units.iterdir())


def test_system_validation_failure_does_not_install_or_leak_secrets(
    system_installation, monkeypatch, capsys
):
    state = system_installation
    original = installer.subprocess.run

    def run(command, **kwargs):
        if "--validate-conf=" in command[-1]:
            raise subprocess.CalledProcessError(1, command, stderr=b"private-certificate-secret")
        return original(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.main(state.options) == 1
    assert not list(state.units.iterdir())
    assert "private-certificate-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("active, refused", [("active", True), ("inactive", False)])
def test_system_start_checks_the_user_bus_as_the_target_account(
    system_installation, monkeypatch, active, refused
):
    state = system_installation
    original_exists = Path.exists
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: True if str(path) == "/run/user/61000/bus" else original_exists(path),
    )
    original_run = installer.subprocess.run

    def run(command, **kwargs):
        if command[:2] == ["systemctl", "--user"]:
            assert command[:2] == ["systemctl", "--user"]
            assert kwargs["user"] == 61000
            assert kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/61000/bus"
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=f"LoadState=loaded\nActiveState={active}\nUnitFileState=disabled\n",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.main(state.options) == (1 if refused else 0)
    if refused:
        assert not state.calls and not list(state.units.iterdir())


@pytest.mark.parametrize("failure", ["--version", "enable", "is-active"])
def test_system_start_stops_after_command_failure_without_leaking_output(
    system_installation, monkeypatch, capsys, failure
):
    state = system_installation
    original_run = installer.subprocess.run

    def run(command, **kwargs):
        if failure in command:
            state.calls.append((command, kwargs))
            raise subprocess.CalledProcessError(1, command, output=b"private-startup-secret")
        return original_run(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", run)
    assert installer.main(state.options) == 1
    assert failure in state.calls[-1][0]
    assert "private-startup-secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "properties",
    [
        "LoadState=loaded\nActiveState=active\nFragmentPath=/run/systemd/system/custom.service\n",
        "LoadState=loaded\nActiveState=inactive\nFragmentPath=/usr/lib/systemd/system/esup-runner-mediamtx@.service\n",
        "LoadState=loaded\nActiveState=inactive\nDropInPaths=/etc/systemd/system/service.d/custom.conf\n",
    ],
)
def test_system_start_preserves_loaded_units_from_other_sources(
    system_installation, monkeypatch, properties
):
    state = system_installation
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 0, stdout=properties),
    )
    assert installer.main(state.options) == 1
    assert not list(state.units.iterdir())


@pytest.mark.parametrize(
    "properties, code, refused",
    [
        ("LoadState=not-found\n", 1, False),
        ("LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n", 0, False),
        ("ActiveState=active\nUnitFileState=disabled\n", 0, True),
        ("ActiveState=inactive\nUnitFileState=enabled\n", 0, True),
        ("", 1, True),
    ],
)
def test_conflict_checks_fail_closed(monkeypatch, properties, code, refused):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, code, stdout=properties),
    )
    if refused:
        with pytest.raises(ValueError):
            installer.refuse_service_conflict(["systemctl"], "manager")
    else:
        installer.refuse_service_conflict(["systemctl"], "manager")


def test_system_unit_paths_preserve_literals_and_reject_injection():
    path = Path('/home/media % $x/"quoted"')
    assert installer.system_unit_path(path) == '"/home/media %% $x/\\"quoted\\""'
    assert "$$x" in installer.system_unit_path(path, executable=True)
    assert installer.system_unit_path(path, quoted=False) == '/home/media %% $x/"quoted"'
    for path in (Path("relative"), Path("/home/../root"), Path("/home/bad\nUser=root")):
        with pytest.raises(ValueError):
            installer.system_unit_path(path)


def test_system_destination_requires_root_ownership_and_private_write_permissions(
    tmp_path, monkeypatch
):
    path = tmp_path / "service"
    path.write_text("unit")
    original_stat = Path.stat

    def metadata(candidate, **kwargs):
        if candidate == path:
            return SimpleNamespace(st_uid=0, st_mode=0o100644)
        return original_stat(candidate, **kwargs)

    monkeypatch.setattr(Path, "stat", metadata)
    installer.require_system_owned(path)
    monkeypatch.setattr(Path, "stat", lambda *_: SimpleNamespace(st_uid=1000, st_mode=0o100644))
    with pytest.raises(ValueError):
        installer.require_system_owned(path)
    monkeypatch.setattr(Path, "stat", lambda *_: SimpleNamespace(st_uid=0, st_mode=0o100666))
    with pytest.raises(ValueError):
        installer.require_system_owned(path)


@pytest.mark.parametrize(
    "options",
    [
        ["start", "--instance", "manager", "--service-mode", "system"],
        ["start", "--instance", "manager", "--user", "media"],
        ["start", "--instance", "manager", "--service-mode", "invalid"],
    ],
)
def test_service_mode_cli_requires_an_explicit_system_account(options):
    with pytest.raises(SystemExit):
        installer.parse_args(options)
