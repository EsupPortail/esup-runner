"""Check native Live setup with isolated files; no services or sockets are used."""

import importlib.util
import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_live.py"
spec = importlib.util.spec_from_file_location("configure_live", SCRIPT)
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture
def installation(tmp_path):
    directory = tmp_path / "runner"
    directory.mkdir()
    content = (helper.ROOT / "production/mediamtx-live.yml").read_text()
    for placeholder in ("PUBLISH", "RELAY", "READ", "CONTROL"):
        content = content.replace(
            f"CHANGE_ME_{placeholder}_PASSWORD", f"runner-{placeholder}-secret"
        )
    (directory / "mediamtx.yml").write_text(content)
    (directory / "mediamtx.env").write_text("")
    manager = tmp_path / "manager"
    manager.mkdir()
    (manager / "mediamtx.yml").write_text(
        helper.yaml.safe_dump(
            {
                "rtmp": True,
                "api": True,
                "rtmpAddress": "127.0.0.1:1935",
                "authInternalUsers": [
                    {
                        "user": "relay",
                        "pass": "manager-RELAY-secret",
                        "permissions": [{"action": "read"}],
                    }
                ],
            }
        )
    )
    (manager / "mediamtx.env").write_text("")
    env = tmp_path / ".env"
    env.write_text(
        '# Preserved settings\nRUNNER_TASK_TYPES="[1x(encoding,studio),2x(live)]"\nLIVE_TRANSCODER=cpu\n'
    )
    return tmp_path, env


def options(installation):
    root, env = installation
    return ["--config-root", str(root), "--env-file", str(env)]


def test_connections_are_private_minimal_and_idempotent(installation, capsys):
    role = "runner"
    root, env = installation
    original = env.read_text()
    assert helper.main(options(installation)) == 0
    values = dotenv_values(env, interpolate=False)
    assert values["LIVE_MEDIAMTX_API_PASSWORD"] == f"{role}-CONTROL-secret"
    assert "LIVE_MEDIAMTX_API_URL" not in values
    assert "LIVE_MEDIAMTX_API_USER" not in values
    assert values["LIVE_TRANSCODER"] == "cpu"
    assert values["RUNNER_TASK_TYPES"] == "[1x(encoding,studio),2x(live)]"
    assert values["LIVE_UPSTREAM_RTMP_URL"] == "rtmp://relay:manager-RELAY-secret@127.0.0.1:1935"
    assert values["LIVE_LOCAL_RTMP_URL"] == "rtmp://transcoder:runner-READ-secret@127.0.0.1:2935"
    backups = list(root.glob(".env.live-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == env.stat().st_mode & 0o777 == 0o600
    updated = env.read_bytes()
    assert helper.main(options(installation)) == 0
    assert env.read_bytes() == updated
    assert len(list(root.glob(".env.live-backup-*"))) == 1
    assert "secret" not in capsys.readouterr().out


def test_address_overrides_and_ipv6(installation):
    root, env = installation
    (root / "runner/mediamtx.env").write_text(
        "MTX_APIADDRESS=[::]:11997\nMTX_RTMPADDRESS=0.0.0.0:3935\n"
    )
    (root / "manager/mediamtx.env").write_text("MTX_RTMPADDRESS=192.0.2.10:1936\n")
    assert helper.main(options(installation)) == 0
    values = dotenv_values(env)
    assert values["LIVE_MEDIAMTX_API_URL"] == "http://[::1]:11997"
    assert values["LIVE_LOCAL_RTMP_URL"].endswith("@127.0.0.1:3935")
    assert values["LIVE_UPSTREAM_RTMP_URL"].endswith("@192.0.2.10:1936")


def test_remote_runner_needs_only_relay_password(installation, monkeypatch, capsys):
    root, env = installation
    (root / "manager/mediamtx.yml").unlink()
    monkeypatch.setattr(helper.sys, "stdin", io.StringIO("relay@secret:/+#\n"))
    assert (
        helper.main(
            options(installation)
            + ["--manager-rtmp-host", "manager.example.org", "--relay-password-stdin"]
        )
        == 0
    )
    assert (
        dotenv_values(env)["LIVE_UPSTREAM_RTMP_URL"]
        == "rtmp://relay:relay%40secret%3A%2F%2B%23@manager.example.org:1935"
    )
    assert "relay@secret" not in capsys.readouterr().out


def test_remote_prompt_and_dry_run(installation, monkeypatch):
    root, env = installation
    original = env.read_bytes()
    monkeypatch.setattr(helper.getpass, "getpass", lambda _: pytest.fail("Dry run prompted"))
    args = options(installation) + ["--manager-rtmp-host", "2001:db8::1"]
    assert helper.main(args + ["--dry-run"]) == 0
    assert env.read_bytes() == original
    assert not list(root.glob(".env.live-backup-*"))
    monkeypatch.setattr(helper.getpass, "getpass", lambda _: "prompt-secret")
    assert helper.main(args) == 0
    assert dotenv_values(env)["LIVE_UPSTREAM_RTMP_URL"].endswith("@[2001:db8::1]:1935")


@pytest.mark.parametrize(
    "broken",
    [
        "authInternalUsers: [secret:\n",
        "[]",
        "api: false",
        "authInternalUsers: not-a-list",
        "authInternalUsers: [null]",
        "authInternalUsers: []",
        "apiEncryption: true",
    ],
)
def test_invalid_yaml_never_changes_environment_or_leaks_source(installation, broken, capsys):
    root, env = installation
    path = root / "runner/mediamtx.yml"
    path.write_text(path.read_text() + "\n" + broken if broken not in ("[]",) else broken)
    original = env.read_bytes()
    assert helper.main(options(installation)) == 1
    assert env.read_bytes() == original
    assert "secret:" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "password", [None, "", "CHANGE_ME_SECRET", "sha256:secret", "argon2:secret", "a\nb"]
)
def test_unusable_passwords_rejected(password):
    with pytest.raises(ValueError):
        helper.validate_password(password)


@pytest.mark.parametrize(
    "change", ["permission", "duplicate", "environment", "listener", "symlink"]
)
def test_unsupported_installation_rejected(installation, change):
    root, env = installation
    path = root / "runner/mediamtx.yml"
    config = helper.yaml.safe_load(path.read_text())
    if change == "permission":
        config["authInternalUsers"][1]["permissions"] = []
    elif change == "duplicate":
        config["authInternalUsers"].append(config["authInternalUsers"][1])
    elif change == "listener":
        config["apiAddress"] = "127.0.0.1:99999"
    elif change == "environment":
        (root / "runner/mediamtx.env").write_text("MTX_AUTHMETHOD=http\n")
    path.write_text(helper.yaml.safe_dump(config))
    if change == "symlink":
        path.rename(root / "media.yml")
        path.symlink_to(root / "media.yml")
    original = env.read_bytes()
    assert helper.main(options(installation)) == 1
    assert env.read_bytes() == original


def test_dotenv_preservation_escaping_duplicates_and_multiline():
    original = "# Header\nOTHER='two\nlines'\nexport LIVE_MEDIAMTX_API_PASSWORD='old\nsecret'\nLIVE_MEDIAMTX_API_PASSWORD=duplicate\nLIVE_MEDIAMTX_API_USER=control\n"
    values = {
        "LIVE_MEDIAMTX_API_PASSWORD": "a'b\\c#$",
        "LIVE_MEDIAMTX_API_USER": "control",
    }
    updated = helper.render_environment(original, values, "manager")
    assert updated.startswith("# Header\nOTHER='two\nlines'\n")
    assert updated.count("LIVE_MEDIAMTX_API_PASSWORD=") == 1
    assert "LIVE_MEDIAMTX_API_USER" not in updated
    assert (
        dotenv_values(stream=io.StringIO(updated))["LIVE_MEDIAMTX_API_PASSWORD"]
        == values["LIVE_MEDIAMTX_API_PASSWORD"]
    )
    assert helper.render_environment(updated, values, "manager") == updated
    for content, settings in [
        ("", {"LIVE_MEDIAMTX_API_PASSWORD": "${secret}"}),
        ("BAD='\n", values),
    ]:
        with pytest.raises(ValueError):
            helper.render_environment(content, settings, "manager")


def test_concurrent_change_leaves_original(installation, monkeypatch):
    root, env = installation
    original = env.read_text()
    monkeypatch.setattr(helper, "read_private_input", lambda _: "concurrent edit")
    with pytest.raises(ValueError, match="changed"):
        helper.replace_environment(env, original, "updated")
    assert env.read_text() == original
    assert not list(root.glob(".env.live-????????"))


@pytest.mark.parametrize(
    "extra",
    [
        ["--instance", "../bad"],
        ["--manager-rtmp-port", "0"],
        ["--relay-password-stdin"],
    ],
)
def test_invalid_cli_options(installation, extra):
    with pytest.raises(SystemExit):
        helper.parse_args(options(installation) + extra)


def test_failed_replace_keeps_original_and_private_backup(installation, monkeypatch):
    root, env = installation
    original = env.read_bytes()

    def fail_replace(*_):
        raise OSError("simulated full disk")

    monkeypatch.setattr(helper.os, "replace", fail_replace)
    assert helper.main(options(installation)) == 1
    assert env.read_bytes() == original
    assert next(root.glob(".env.live-backup-*")).read_bytes() == original
    assert not list(root.glob(".env.live-????????"))


def test_environment_owner_and_missing_input(installation, monkeypatch):
    _, env = installation
    original = env.read_bytes()
    monkeypatch.setattr(helper.os, "geteuid", lambda: env.stat().st_uid + 1)
    assert helper.main(options(installation)) == 1
    assert env.read_bytes() == original
    env.unlink()
    assert helper.main(options(installation)) == 1


@pytest.mark.parametrize(
    "address", [None, "127.0.0.1", "127.0.0.1:9997/path", "user@127.0.0.1:9997"]
)
def test_invalid_listener_addresses(address):
    with pytest.raises(ValueError):
        helper.endpoint(address)


def test_remote_host_is_validated_before_password_prompt(installation, monkeypatch):
    monkeypatch.setattr(helper.getpass, "getpass", lambda _: pytest.fail("Invalid host prompted"))
    assert helper.main(options(installation) + ["--manager-rtmp-host", "user@manager.example"]) == 1


def test_new_assignment_after_unterminated_last_line():
    updated = helper.render_environment("OTHER=value", {"LIVE_ENABLED": "true"}, "manager")
    assert updated == "OTHER=value\nLIVE_ENABLED='true'\n"


def test_component_defaults_and_other_role_rejected():
    """Defaults resolve inside this component; callers cannot select another role."""
    args = helper.parse_args([])
    assert args.role == "runner"
    assert args.instance == "runner"
    assert args.env_file == SCRIPT.parents[1] / ".env"
    with pytest.raises(SystemExit):
        helper.parse_args(["manager"])


def test_install_and_connect_in_component_only_checkout(tmp_path):
    """A detached component can prepare MediaMTX and configure its default .env."""
    component = tmp_path / "runner"
    scripts = component / "scripts"
    production = component / "production"
    scripts.mkdir(parents=True)
    production.mkdir()
    for name in ("install_mediamtx.py", "configure_live.py"):
        shutil.copyfile(SCRIPT.parent / name, scripts / name)
    shutil.copyfile(
        SCRIPT.parents[1] / "production/mediamtx-live.yml",
        production / "mediamtx-live.yml",
    )
    env = component / ".env"
    env.write_text("OTHER=preserved\n")
    config_root = tmp_path / "installed"
    prepared = subprocess.run(
        [
            sys.executable,
            str(scripts / "install_mediamtx.py"),
            "configure",
            "--instance",
            "runner",
            "--config-root",
            str(config_root),
        ],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "uv run scripts/configure_live.py --instance runner" in prepared.stdout
    command = [
        sys.executable,
        str(scripts / "configure_live.py"),
        "--config-root",
        str(config_root),
    ]
    command += ["--manager-rtmp-host", "manager.example.org", "--relay-password-stdin"]
    configured = subprocess.run(
        command,
        cwd=tmp_path,
        input="remote-relay-secret\n",
        text=True,
        capture_output=True,
        check=True,
    )
    values = dotenv_values(env)
    assert values["OTHER"] == "preserved"
    assert values["LIVE_MEDIAMTX_API_PASSWORD"]
    assert not (tmp_path / "manager").exists()
    assert not (tmp_path / "scripts").exists()
    assert "remote-relay-secret" not in configured.stdout + configured.stderr
    assert len(list(component.glob(".env.live-backup-*"))) == 1
    assert (
        values["LIVE_UPSTREAM_RTMP_URL"]
        == "rtmp://relay:remote-relay-secret@manager.example.org:1935"
    )
