"""Exercise native MediaMTX updates without network, root or real service changes."""

import fcntl
import hashlib
import importlib.util
import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_mediamtx.py"
spec = importlib.util.spec_from_file_location("update_mediamtx_installer", SCRIPT)
assert spec is not None and spec.loader is not None
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
ROLE = SCRIPT.parents[1].name
OPTIONS = ["update", "--version", "1.21.3"]


def release_archive(version):
    """Build one valid archive for the requested fake executable version."""
    content = f"#!/bin/sh\necho v{version}\n".encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("mediamtx")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


class NativeInstallation:
    """Model releases and services while delegating version checks to real subprocesses."""

    def __init__(self, tmp_path, monkeypatch):
        self.home = tmp_path / "home"
        self.binary = self.home / ".local/bin/mediamtx"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("#!/bin/sh\necho v1.21.1\n")
        self.binary.chmod(0o755)
        self.rows = []
        self.calls = []
        self.downloads = []
        self.processes = {}
        self.failure = None
        self.release = release_archive("1.21.3")
        self.real_run = subprocess.run
        self.real_samefile = os.path.samefile
        self.monkeypatch = monkeypatch
        self.system_units = tmp_path / "system-units"
        account = pwd.getpwuid(os.geteuid())
        self.account = pwd.struct_passwd((*account[:5], str(self.home), account.pw_shell))

    def add_service(self, instance, scope="user", active=True):
        """Prepare a standard instance and record its simulated service state."""
        directory = self.home / ".config/esup-runner" / instance
        directory.mkdir(parents=True)
        (directory / "mediamtx.yml").write_text(
            "rtmp: true\n# retained credentials: private-control-secret\n"
        )
        (directory / "mediamtx.env").write_text("MTX_APIADDRESS=127.0.0.1:19997\n")
        name = f"esup-runner-mediamtx@{instance}.service"
        if scope == "user":
            unit = self.home / ".config/systemd/user/esup-runner-mediamtx@.service"
            unit.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(installer.ROOT / "production/esup-runner-mediamtx@.service", unit)
        else:
            self.system_units.mkdir(exist_ok=True)
            self.monkeypatch.setattr(
                installer, "SYSTEMD_SYSTEM_DIR", self.system_units, raising=False
            )
            self.monkeypatch.setattr(
                installer, "require_system_owned", lambda _: None, raising=False
            )
            args = installer.argparse.Namespace(instance=instance, user=self.account.pw_name)
            _, _, _, unit, content = installer.system_service_plan(args)
            unit.write_text(content)
        pid = str(10000 + len(self.rows))
        row = {
            "name": name,
            "scope": scope,
            "directory": directory,
            "unit": unit,
            "properties": {
                "LoadState": "loaded",
                "ActiveState": "active" if active else "inactive",
                "FragmentPath": str(unit),
                "DropInPaths": "",
                "NeedDaemonReload": "no",
                "User": self.account.pw_name if scope == "system" else "",
                "MainPID": pid if active else "0",
            },
            "pid": pid,
        }
        self.rows.append(row)
        self.processes[pid] = self.binary.read_bytes() if active else b""
        return row

    def run(self, command, **kwargs):
        """Execute version queries, intercepting service operations and configuration checks."""
        self.calls.append((command, kwargs))
        if self.failure is not None:
            self.failure(command, kwargs)
        if command[0] == "sudo" and command[1] == "-v":
            return subprocess.CompletedProcess(command, 0)
        if "systemctl" in command:
            return self.systemctl(command)
        if any(str(part).startswith("--validate-conf=") for part in command):
            return subprocess.CompletedProcess(command, 0)
        return self.real_run(command, **kwargs)

    def systemctl(self, command):
        """Track start/stop effects and expose their effective properties."""
        scope = "user" if "--user" in command else "system"
        if "list-units" in command:
            rows = [
                {"unit": row["name"]}
                for row in self.rows
                if row["scope"] == scope and row.get("listed", True)
            ]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(rows))
        row = next(
            (row for row in self.rows if row["scope"] == scope and row["name"] in command),
            None,
        )
        if row is None:
            return subprocess.CompletedProcess(command, 1, stdout="LoadState=not-found\n")
        if "show" in command:
            properties = row["properties"]
            return subprocess.CompletedProcess(
                command, 0, stdout="\n".join(f"{key}={value}" for key, value in properties.items())
            )
        if "stop" in command:
            row["properties"].update(ActiveState="inactive", MainPID="0")
        elif "start" in command:
            row["properties"].update(ActiveState="active", MainPID=row["pid"])
            self.processes[row["pid"]] = self.binary.read_bytes()
        elif "reset-failed" not in command:
            pytest.fail(f"Unexpected service action: {command}")
        return subprocess.CompletedProcess(command, 0)

    def samefile(self, process, target):
        """Distinguish simulated processes still running the old executable."""
        path = Path(process)
        if str(path).startswith("/proc/"):
            return self.processes[path.parent.name] == Path(target).read_bytes()
        return self.real_samefile(process, target)

    def download(self, url, destination, limit):
        """Provide matching archives and checksums without using the network."""
        self.downloads.append(url)
        if url.endswith("checksums.sha256"):
            digest = hashlib.sha256(self.release).hexdigest()
            destination.write_text(f"{digest}  mediamtx_v1.21.3_linux_amd64.tar.gz\n")
        else:
            destination.write_bytes(self.release)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Keep all configuration, binaries, services and downloads isolated."""
    state = NativeInstallation(tmp_path, monkeypatch)
    monkeypatch.setattr(installer.Path, "home", classmethod(lambda cls: state.home))
    monkeypatch.setattr(installer.pwd, "getpwnam", lambda _: state.account)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer.time, "sleep", lambda _: None)
    monkeypatch.setattr(installer.subprocess, "run", state.run)
    monkeypatch.setattr(installer.os.path, "samefile", state.samefile)
    monkeypatch.setattr(installer, "download", state.download)
    state.add_service(ROLE)
    return state


def snapshot(directory):
    """Capture original private configuration bytes and permissions."""
    return {
        path: (path.read_bytes(), path.stat().st_mode)
        for path in directory.rglob("*")
        if path.is_file()
    }


def actions(state):
    """Return mutations to systemd, excluding read-only discovery and version checks."""
    return [command for command, _ in state.calls if "stop" in command or "start" in command]


def test_update_preserves_existing_configuration_and_stopped_services(installation, monkeypatch):
    state = installation
    state.add_service("other", active=False)
    before = snapshot(state.home / ".config")
    original = state.binary.read_bytes()
    monkeypatch.setenv("MTX_AUTHMETHOD", "http")
    assert installer.main(OPTIONS) == 0
    assert state.binary.read_bytes() != original
    assert before == snapshot(state.home / ".config")
    assert len(state.downloads) == 2
    assert all("/releases/download/v1.21.3/" in url for url in state.downloads)
    assert [command[-2:] for command in actions(state)] == [
        ["stop", f"esup-runner-mediamtx@{ROLE}.service"],
        ["start", f"esup-runner-mediamtx@{ROLE}.service"],
    ]
    validation = [
        (command, kwargs) for command, kwargs in state.calls if "--validate-conf=" in command[-1]
    ]
    assert len(validation) == 2
    assert all(kwargs["env"]["MTX_APIADDRESS"] == "127.0.0.1:19997" for _, kwargs in validation)
    assert all("MTX_AUTHMETHOD" not in kwargs["env"] for _, kwargs in validation)
    backups = list(state.binary.parent.glob("mediamtx-backup-*"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o700
    assert (backups[0] / "mediamtx").read_bytes() == original
    assert not list(state.binary.parent.glob(".mediamtx-update-*"))
    calls = len(state.calls)
    assert installer.main(OPTIONS) == 0
    assert len(state.downloads) == 2
    assert not actions(SimpleNamespace(calls=state.calls[calls:]))


def test_shared_binary_stops_both_roles_before_replacement(installation):
    state = installation
    other = "runner" if ROLE == "manager" else "manager"
    state.add_service(other)
    assert installer.main(OPTIONS) == 0
    assert [command[-2] for command in actions(state)] == ["stop", "stop", "start", "start"]
    assert all(row["properties"]["ActiveState"] == "active" for row in state.rows)
    assert all(content == state.binary.read_bytes() for content in state.processes.values())


@pytest.mark.parametrize("colors", [False, True])
def test_preview_does_not_write_download_or_control_services(
    installation, capsys, monkeypatch, colors
):
    state = installation
    if colors:
        monkeypatch.delenv("NO_COLOR", raising=False)
    else:
        monkeypatch.setenv("NO_COLOR", "1")
    before = snapshot(state.home)
    assert installer.main(OPTIONS + ["--dry-run"]) == 0
    assert before == snapshot(state.home)
    assert not actions(state) and not state.downloads
    assert not any(command[0] == "sudo" for command, _ in state.calls)
    captured = capsys.readouterr()
    assert "=== MediaMTX update ===" in captured.out
    assert "Installed version : 1.21.1" in captured.out
    assert "Target version    : 1.21.3" in captured.out
    assert "Mode              : dry run" in captured.out
    assert "MediaMTX services:" in captured.out
    assert "Conclusion:" in captured.out
    assert "✓ INFO: Dry run complete: no files changed or services restarted." in captured.out
    assert "⚠ WARNING: Restarting MediaMTX" in captured.out
    assert not captured.err
    if colors:
        assert "\033[32m✓ INFO:" in captured.out and "\033[33m⚠ WARNING:" in captured.out
    else:
        assert "\033[" not in captured.out


def assert_preflight_unchanged(state, capsys):
    """Require failed discovery to leave the installation and service states intact."""
    before = snapshot(state.home)
    assert installer.main(OPTIONS) == 1
    assert before == snapshot(state.home)
    assert not actions(state) and not state.downloads
    assert "private-control-secret" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "problem",
    [
        "missing_binary",
        "broken_symlink",
        "parent_symlink",
        "root",
        "foreign_owner",
        "downgrade",
        "concurrent_update",
    ],
)
def test_unsafe_binary_keeps_original_installation(installation, monkeypatch, capsys, problem):
    state = installation
    if problem == "missing_binary":
        state.binary.unlink()
    elif problem == "broken_symlink":
        state.binary.unlink()
        state.binary.symlink_to(state.home / "missing")
    elif problem == "parent_symlink":
        parent = state.binary.parent
        destination = parent.with_name("actual-bin")
        parent.rename(destination)
        parent.symlink_to(destination, target_is_directory=True)
    elif problem == "root":
        monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    elif problem == "foreign_owner":
        account = pwd.getpwuid(os.getuid())
        monkeypatch.setattr(installer.os, "geteuid", lambda: os.getuid() + 1)
        monkeypatch.setattr(installer.pwd, "getpwuid", lambda _: account)
    elif problem == "downgrade":
        state.binary.write_text("#!/bin/sh\necho v1.22.0\n")
    else:

        def busy(*_):
            raise BlockingIOError()

        monkeypatch.setattr(installer.fcntl, "flock", busy)
    assert_preflight_unchanged(state, capsys)


@pytest.mark.parametrize(
    "problem",
    [
        "no_service",
        "custom_unit",
        "custom_path",
        "drop_in",
        "needs_reload",
        "transition",
        "unloaded",
        "bad_instance",
        "unit_symlink",
    ],
)
def test_unsafe_service_keeps_original_installation(installation, capsys, problem):
    state = installation
    row = state.rows[0]
    if problem == "no_service":
        state.rows.clear()
    elif problem == "custom_unit":
        row["unit"].write_text("custom unit must be retained")
    elif problem == "bad_instance":
        row["name"] = "esup-runner-mediamtx@bad\\x20name.service"
    elif problem == "unit_symlink":
        unit = row["unit"]
        unit.rename(unit.with_name("original.service"))
        unit.symlink_to("original.service")
    else:
        key, value = {
            "custom_path": ("FragmentPath", "/etc/systemd/user/custom.service"),
            "drop_in": ("DropInPaths", "/tmp/custom.conf"),
            "needs_reload": ("NeedDaemonReload", "yes"),
            "transition": ("ActiveState", "activating"),
            "unloaded": ("LoadState", "error"),
        }[problem]
        row["properties"][key] = value
    assert_preflight_unchanged(state, capsys)


@pytest.mark.parametrize(
    "problem", ["missing_directory", "incomplete_config", "config_symlink", "custom_environment"]
)
def test_unsafe_configuration_keeps_original_installation(installation, capsys, problem):
    state = installation
    directory = state.rows[0]["directory"]
    if problem == "missing_directory":
        shutil.rmtree(directory)
    elif problem == "incomplete_config":
        (directory / "mediamtx.env").unlink()
    elif problem == "config_symlink":
        path = directory / "mediamtx.yml"
        path.rename(directory / "original.yml")
        path.symlink_to("original.yml")
    else:
        (directory / "mediamtx.env").write_text("MTX_AUTHMETHOD=private-control-secret")
    assert_preflight_unchanged(state, capsys)


@pytest.mark.parametrize("scope", ["user", "system"])
def test_unavailable_systemd_scope_keeps_original_installation(installation, capsys, scope):
    def fail(command, _):
        if "list-units" in command and ("--user" in command) == (scope == "user"):
            raise subprocess.CalledProcessError(1, command, stderr=b"private-control-secret")

    installation.failure = fail
    assert_preflight_unchanged(installation, capsys)


def assert_preparation_failed(state, capsys, original):
    """Require a failed preparation to preserve configurations and avoid service control."""
    before = snapshot(state.home / ".config")
    assert installer.main(OPTIONS) == 1
    assert state.binary.read_bytes() == original
    assert before == snapshot(state.home / ".config")
    assert not actions(state)
    assert not list(state.binary.parent.glob(".mediamtx-update-*"))
    assert "private-control-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["download", "checksum", "version"])
def test_bad_release_never_stops_services(installation, monkeypatch, capsys, failure):
    state = installation
    if failure == "download":

        def download(*_):
            raise OSError("Simulated download failure")

        monkeypatch.setattr(installer, "download", download)
    elif failure == "version":
        state.release = release_archive("1.21.2")
    else:
        download = installer.download

        def corrupt(url, destination, limit):
            download(url, destination, limit)
            if url.endswith("checksums.sha256"):
                destination.write_text("invalid checksum")

        monkeypatch.setattr(installer, "download", corrupt)
    assert_preparation_failed(state, capsys, state.binary.read_bytes())


@pytest.mark.parametrize("failure", ["validation", "backup", "binary_changed", "service_changed"])
def test_preparation_failure_never_stops_services(installation, monkeypatch, capsys, failure):
    state = installation
    original = state.binary.read_bytes()
    if failure == "validation":

        def fail(command, _):
            if command[-1].startswith("--validate-conf="):
                raise subprocess.CalledProcessError(1, command, stderr=b"private-control-secret")

        state.failure = fail
    elif failure == "backup":

        def fail(*_):
            raise OSError("Simulated backup failure")

        monkeypatch.setattr(installer, "update_backup", fail)
    else:
        validate = installer.update_validate

        def changed(*args):
            validate(*args)
            if failure == "binary_changed":
                state.binary.write_bytes(original + b"# concurrent edit\n")
            else:
                state.rows[0]["properties"]["ActiveState"] = "inactive"

        monkeypatch.setattr(installer, "update_validate", changed)
    expected = original + (b"# concurrent edit\n" if failure == "binary_changed" else b"")
    assert_preparation_failed(state, capsys, expected)


@pytest.mark.parametrize("failure", ["stop", "replace", "start", "running", "interrupt"])
def test_failed_activation_restores_old_binary_and_all_active_services(
    installation, monkeypatch, failure
):
    state = installation
    state.add_service("second")
    original = state.binary.read_bytes()
    before = snapshot(state.home / ".config")
    failed = False

    def fail(command, _):
        nonlocal failed
        marker = "stop" if failure == "stop" else "start"
        if failure in ("stop", "start", "interrupt") and marker in command and not failed:
            failed = True
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise subprocess.CalledProcessError(1, command, stderr=b"private-control-secret")

    state.failure = fail
    if failure == "replace":
        replace = installer.os.replace

        def fail_replace(source, destination):
            if Path(source).name == "mediamtx":
                raise OSError("Simulated replacement failure")
            return replace(source, destination)

        monkeypatch.setattr(installer.os, "replace", fail_replace)
    if failure == "running":
        check = installer.update_check_running

        def fail_check(*args):
            nonlocal failed
            if not failed:
                failed = True
                raise ValueError("New service did not start")
            return check(*args)

        monkeypatch.setattr(installer, "update_check_running", fail_check)
    assert installer.main(OPTIONS) == 1
    assert state.binary.read_bytes() == original
    assert before == snapshot(state.home / ".config")
    assert all(row["properties"]["ActiveState"] == "active" for row in state.rows)
    assert all(content == original for content in state.processes.values())


@pytest.mark.parametrize("failure", ["stop", "restore", "start"])
def test_failed_rollback_reports_backup_for_manual_recovery(
    installation, monkeypatch, capsys, failure
):
    state = installation
    original = state.binary.read_bytes()

    def fail(command, _):
        if "start" in command:
            raise subprocess.CalledProcessError(1, command, stderr=b"private-control-secret")
        if failure == "stop" and "stop" in command and state.binary.read_bytes() != original:
            raise OSError("Cannot stop new service")

    state.failure = fail
    if failure == "restore":
        replace = installer.os.replace

        def fail_restore(source, destination):
            if Path(source).name == "rollback":
                raise OSError("Cannot restore binary")
            return replace(source, destination)

        monkeypatch.setattr(installer.os, "replace", fail_restore)
    assert installer.main(OPTIONS) == 1
    captured = capsys.readouterr()
    error = captured.err
    assert "mediamtx-backup-" in error
    assert "✗ ERROR:" in error
    assert "⚠ WARNING: MediaMTX activation failed" in captured.out
    assert "private-control-secret" not in error


@pytest.mark.parametrize(
    "pid,state", [("0", "inactive"), ("bad", "active"), ("123", "active"), ("124", "active")]
)
def test_running_check_rejects_inactive_missing_and_old_processes(
    installation, monkeypatch, pid, state
):
    service = installer.update_services()[0]
    monkeypatch.setattr(
        installer, "update_properties", lambda *_: {"MainPID": pid, "ActiveState": state}
    )

    def missing(*_):
        raise FileNotFoundError()

    monkeypatch.setattr(
        installer.os.path, "samefile", missing if pid == "123" else lambda *_: False
    )
    with pytest.raises(ValueError, match="did not start"):
        installer.update_check_running(service, installation.binary)


def test_foreign_system_service_is_untouched(installation):
    state = installation
    foreign = state.add_service("foreign")
    foreign["scope"] = "system"
    foreign["properties"]["User"] = "another-service-account"
    assert installer.main(OPTIONS) == 0
    assert not any("sudo" in command for command, _ in state.calls)
    assert all(foreign["name"] not in command for command in actions(state))
    assert foreign["properties"]["ActiveState"] == "active"


def test_xdg_user_unit_is_reused(installation, tmp_path, monkeypatch):
    row = installation.rows[0]
    config_home = tmp_path / "xdg"
    unit = config_home / "systemd/user" / row["unit"].name
    unit.parent.mkdir(parents=True)
    row["unit"].rename(unit)
    row["unit"] = unit
    row["properties"]["FragmentPath"] = str(unit)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    assert installer.main(OPTIONS) == 0


def test_backup_preserves_certificate_symlinks(installation):
    state = installation
    certificate = state.home / "certificate.pem"
    certificate.write_text("certificate")
    link = state.rows[0]["directory"] / "fullchain.pem"
    link.symlink_to(certificate)
    assert installer.main(OPTIONS) == 0
    backup = next(state.binary.parent.glob("mediamtx-backup-*"))
    saved = next(backup.glob("*/configuration/fullchain.pem"))
    assert saved.is_symlink() and saved.readlink() == certificate


def test_runner_requires_manager_installer_for_a_shared_system_service(installation, capsys):
    state = installation
    row = state.rows[0]
    row["scope"] = "system"
    row["properties"]["User"] = pwd.getpwuid(os.geteuid()).pw_name
    before = snapshot(state.home)
    assert installer.main(OPTIONS) == 1
    assert before == snapshot(state.home)
    assert "run update from manager/" in capsys.readouterr().err
    assert not actions(state) and not state.downloads


@pytest.mark.parametrize("scope,accepted", [("system", True), ("user", False)])
def test_running_check_respects_capability_restrictions(installation, monkeypatch, scope, accepted):
    service = installer.update_services()[0]._replace(
        command=["systemctl"] if scope == "system" else ["systemctl", "--user"]
    )
    monkeypatch.setattr(
        installer, "update_properties", lambda *_: {"MainPID": "123", "ActiveState": "active"}
    )

    def denied(*_):
        raise PermissionError()

    monkeypatch.setattr(installer.os.path, "samefile", denied)
    if accepted:
        installer.update_check_running(service, installation.binary)
    else:
        with pytest.raises(ValueError, match="did not start"):
            installer.update_check_running(service, installation.binary)


def test_running_check_waits_for_a_stable_process(installation, monkeypatch):
    service = installer.update_services()[0]
    pids = iter(["123", "124", "124"])
    monkeypatch.setattr(
        installer, "update_properties", lambda *_: {"MainPID": next(pids), "ActiveState": "active"}
    )
    monkeypatch.setattr(installer.os.path, "samefile", lambda *_: True)
    installer.update_check_running(service, installation.binary)
    assert next(pids, None) is None


def test_configured_but_unloaded_service_is_validated_and_kept_stopped(installation):
    state = installation
    row = state.rows[0]
    row["listed"] = False
    row["properties"].update(ActiveState="inactive", MainPID="0")
    assert installer.main(OPTIONS) == 0
    assert not actions(state)
    assert row["properties"]["ActiveState"] == "inactive"
    assert any(command[-1].startswith("--validate-conf=") for command, _ in state.calls)


def test_failed_property_query_cannot_hide_a_service(installation, monkeypatch, capsys):
    run = installer.subprocess.run

    def fail(command, **kwargs):
        if "show" in command:
            return subprocess.CompletedProcess(command, 1, stdout="")
        return run(command, **kwargs)

    monkeypatch.setattr(installer.subprocess, "run", fail)
    assert_preflight_unchanged(installation, capsys)


def link_executable(state, relative=False):
    """Expose an existing shared executable through the account-local path."""
    target = state.home / "shared/mediamtx"
    target.parent.mkdir()
    state.binary.rename(target)
    target.chmod(0o555)
    destination = os.path.relpath(target, state.binary.parent) if relative else target
    state.binary.symlink_to(destination)
    return target


@pytest.mark.parametrize("relative", [False, True])
def test_symlink_update_replaces_only_the_local_link(installation, relative):
    state = installation
    state.add_service("second")
    target = link_executable(state, relative)
    link = state.binary.readlink()
    original = (target.read_bytes(), target.stat().st_ino, target.stat().st_mode)
    configuration = snapshot(state.home / ".config")

    assert installer.main(OPTIONS) == 0

    assert not state.binary.is_symlink()
    assert state.binary.read_bytes() != original[0]
    assert (target.read_bytes(), target.stat().st_ino, target.stat().st_mode) == original
    assert snapshot(state.home / ".config") == configuration
    assert [command[-2] for command in actions(state)] == ["stop", "stop", "start", "start"]
    backup = next(state.binary.parent.glob("mediamtx-backup-*")) / "mediamtx"
    assert backup.is_symlink() and backup.readlink() == link


@pytest.mark.parametrize("relative", [False, True])
def test_symlink_preview_preserves_link_and_target(installation, capsys, relative):
    state = installation
    target = link_executable(state, relative)
    link = (state.binary.readlink(), state.binary.lstat().st_ino)
    original = target.read_bytes()
    before = snapshot(state.home)

    assert installer.main(OPTIONS + ["--dry-run"]) == 0

    assert (state.binary.readlink(), state.binary.lstat().st_ino) == link
    assert target.read_bytes() == original
    assert snapshot(state.home) == before
    assert not actions(state) and not state.downloads
    assert "its target stays unchanged" in capsys.readouterr().out


def test_symlink_with_target_already_at_requested_version_is_unchanged(installation):
    state = installation
    state.binary.write_text("#!/bin/sh\necho v1.21.3\n")
    target = link_executable(state)
    before = (state.binary.readlink(), target.read_bytes())
    assert installer.main(OPTIONS) == 0
    assert (state.binary.readlink(), target.read_bytes()) == before
    assert not actions(state) and not state.downloads


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("failure", ["validation", "start"])
def test_failed_symlink_update_preserves_target_and_recovers_original_link(
    installation,
    relative,
    failure,
):
    state = installation
    target = link_executable(state, relative)
    original = (target.read_bytes(), target.stat().st_ino)
    link = state.binary.readlink()
    failed = False

    def fail(command, _):
        nonlocal failed
        selected = (
            command[-1].startswith("--validate-conf=")
            if failure == "validation"
            else "start" in command
        )
        if selected and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, command)

    state.failure = fail
    assert installer.main(OPTIONS) == 1
    assert state.binary.is_symlink() and state.binary.readlink() == link
    assert (target.read_bytes(), target.stat().st_ino) == original
    assert all(row["properties"]["ActiveState"] == "active" for row in state.rows)
    assert all(content == original[0] for content in state.processes.values())


@pytest.mark.parametrize("changed", ["link", "target"])
def test_concurrent_change_to_link_or_target_aborts_before_stopping_services(
    installation,
    monkeypatch,
    changed,
):
    state = installation
    target = link_executable(state)
    validate = installer.update_validate

    def change(*args):
        validate(*args)
        if changed == "link":
            state.binary.unlink()
            state.binary.symlink_to(os.path.relpath(target, state.binary.parent))
        else:
            target.chmod(0o755)
            target.write_text("#!/bin/sh\necho v1.21.2\n")

    monkeypatch.setattr(installer, "update_validate", change)
    assert installer.main(OPTIONS) == 1
    assert state.binary.is_symlink()
    assert not actions(state)


def test_link_target_can_belong_to_another_account(installation, monkeypatch):
    state = installation
    target = link_executable(state)
    original_stat = installer.Path.stat

    def foreign_target(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if path == state.binary and kwargs.get("follow_symlinks", True):
            return SimpleNamespace(
                st_uid=0,
                st_mode=metadata.st_mode,
                st_dev=metadata.st_dev,
                st_ino=metadata.st_ino,
                st_mtime_ns=metadata.st_mtime_ns,
                st_ctime_ns=metadata.st_ctime_ns,
            )
        return metadata

    monkeypatch.setattr(installer.Path, "stat", foreign_target)
    original = target.read_bytes()
    assert installer.main(OPTIONS) == 0
    assert not state.binary.is_symlink()
    assert target.read_bytes() == original


def test_local_link_must_belong_to_the_service_account(installation, monkeypatch, capsys):
    state = installation
    link_executable(state)
    original_lstat = installer.Path.lstat

    def foreign_link(path, *args, **kwargs):
        if path == state.binary:
            return SimpleNamespace(st_uid=os.getuid() + 1)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(installer.Path, "lstat", foreign_link)
    assert_preflight_unchanged(state, capsys)


def test_installer_help_uses_only_the_component_and_standard_library(installation):
    result = installation.real_run(
        [sys.executable, "-S", str(SCRIPT), "update", "--help"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "--version" in result.stdout and "--dry-run" in result.stdout


def test_directory_lock_excludes_a_second_update(installation):
    descriptor = os.open(installation.binary.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert installer.main(OPTIONS) == 1
        assert not installation.downloads and not actions(installation)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    "options", [[], ["--version", "latest"], ["--version", "1.21.0"], ["--version", "1.21.3-rc1"]]
)
def test_update_requires_an_explicit_supported_stable_release(options):
    with pytest.raises(SystemExit):
        installer.parse_args(["update", *options])
