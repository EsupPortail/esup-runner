"""Verify the shared Live smoke test keeps usable credentials out of config artifacts."""

import base64
import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from dotenv import dotenv_values

from app.core.config import Config

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/live_smoke.py"
spec = importlib.util.spec_from_file_location("live_smoke", SCRIPT)
assert spec is not None and spec.loader is not None
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


@pytest.mark.parametrize("component", ["runner", "manager"])
def test_mediamtx_config_contains_verifiers_only(component, tmp_path):
    secret = "test-random-media-secret"
    content = smoke.media_template(component, secret)
    configuration = tmp_path / "media.yml"
    configuration.write_text(content)

    assert secret not in configuration.read_text()
    users = yaml.safe_load(content)["authInternalUsers"]
    assert users
    for user in users:
        scheme, digest = user["pass"].split(":", 1)
        assert scheme == "sha256"
        assert base64.b64decode(digest) == hashlib.sha256(secret.encode()).digest()
        assert user["permissions"]
    assert "CHANGE_ME_" not in content


@pytest.fixture
def runner_environment(tmp_path):
    return {
        "RUNNER_HOST": "127.0.0.1",
        "RUNNER_PORT": "8083",
        "RUNNER_INSTANCE_ID": "1",
        "RUNNER_INSTANCE_URL": "http://127.0.0.1:8083",
        "RUNNER_BASE_PORT": "8082",
        "RUNNER_BASE_NAME": "smoke",
        "RUNNER_INSTANCES": "3",
        "RUNNER_MONITORING": "true",
        "RUNNER_TASK_TYPES": "[1x(encoding,studio),2x(live)]",
        "RUNNER_TOKEN": "test-runner-token",
        "MANAGER_URL": "http://127.0.0.1:8081",
        "ENCODING_TYPE": "CPU",
        "LIVE_MEDIAMTX_API_URL": "http://127.0.0.1:10997",
        "LIVE_MEDIAMTX_API_PASSWORD": "test-media-secret",
        "LIVE_UPSTREAM_RTMP_URL": "rtmp://relay:test-media-secret@127.0.0.1:1935",
        "LIVE_LOCAL_RTMP_URL": "rtmp://transcoder:test-media-secret@127.0.0.1:2935",
        "LIVE_HLS_DIRECTORY": str(tmp_path / "hls" / "instance-{instance_id}"),
        "LIVE_POD_PASSWORD": "test-inherited-pod-password",
        "RUNNER_CUSTOM_TOKEN": "test-inherited-token",
        "PATH": "/usr/bin",
        "DEBUG": "false",
    }


def test_grouped_launcher_keeps_credentials_in_environment(
    runner_environment, tmp_path, monkeypatch
):
    spawn = Mock(return_value=object())
    media = Mock()
    process = smoke.start_runner_process(
        "runner-group",
        runner_environment,
        tmp_path,
        spawn,
        media,
        (2935, 10997),
        grouped=True,
    )
    spawn.assert_called_once()
    name, command, environment, cwd = spawn.call_args.args
    assert name == "runner-group"
    assert command == ["uv", "run", "--no-sync", "esup-runner-runner"]
    assert cwd == smoke.ROOT / "runner"
    media.assert_not_called()
    env_file = Path(environment["CONFIG_ENV_PATH"])
    settings = dotenv_values(env_file, interpolate=False)
    content = env_file.read_text()
    assert env_file.stat().st_mode & 0o777 == 0o600
    for key in (
        "RUNNER_TOKEN",
        "LIVE_MEDIAMTX_API_PASSWORD",
        "LIVE_UPSTREAM_RTMP_URL",
        "LIVE_LOCAL_RTMP_URL",
        "LIVE_POD_PASSWORD",
        "RUNNER_CUSTOM_TOKEN",
    ):
        assert key not in settings
        assert runner_environment[key] not in content
        assert environment[key] == runner_environment[key]
    assert settings["RUNNER_TASK_TYPES"] == "[1x(encoding,studio),2x(live)]"
    assert "RUNNER_TASK_TYPES" not in environment
    assert not {"RUNNER_INSTANCE_ID", "RUNNER_INSTANCE_URL", "RUNNER_PORT"} & environment.keys()

    # The launcher's dotenv loading leaves environment-only credentials intact.
    for key in ("RUNNER_INSTANCE_ID", "RUNNER_INSTANCE_URL", "RUNNER_PORT"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**settings, **environment}.items():
        monkeypatch.setenv(key, value)
    config = Config()
    assert config.RUNNER_TOKEN == runner_environment["RUNNER_TOKEN"]
    assert config.RUNNER_INSTANCES == 3
    assert config.LIVE.mediamtx_api_password == runner_environment["LIVE_MEDIAMTX_API_PASSWORD"]
    assert config.LIVE.upstream_rtmp_url == runner_environment["LIVE_UPSTREAM_RTMP_URL"]
    assert config.LIVE.local_rtmp_url == runner_environment["LIVE_LOCAL_RTMP_URL"]

    assert (
        smoke.start_runner_process(
            "runner-group",
            runner_environment,
            tmp_path,
            spawn,
            media,
            (2935, 10997),
            grouped=True,
            previous=process,
        )
        is process
    )
    spawn.assert_called_once()


def test_single_runner_keeps_process_environment(runner_environment, tmp_path):
    spawn = Mock(return_value=object())
    media = Mock()
    process = smoke.start_runner_process(
        "runner-0", runner_environment, tmp_path, spawn, media, (2935, 10997)
    )
    assert process is spawn.return_value
    media.assert_called_once_with("runner-0-media", 2935, 10997)
    assert spawn.call_args.args[2] is runner_environment
    assert not (tmp_path / "runner.env").exists()
