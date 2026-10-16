"""Exercise diagnostic config loading without weakening Runner startup validation."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.core.config as config_module
from app.core import _check_config


@pytest.fixture
def diagnostic_environment(tmp_path):
    """Build an isolated installation with Live task groups and no relay URL."""
    env_file = tmp_path / ".env"
    env_file.write_text("\n", encoding="utf-8")
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "CONFIG_ENV_PATH": str(env_file),
        "RUNNER_TOKEN": "test-runner-token",
        "RUNNER_TASK_TYPES": "[1x(encoding,studio),2x(live)]",
        "ENCODING_TYPE": "CPU",
        "UV_CACHE_DIR": "/tmp/esup-runner-uv-cache",
        "NO_COLOR": "1",
    }
    for name in ("LOG_DIR", "STORAGE_DIR", "CACHE_DIR"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        environment[name] = str(directory)
    for name in ("huggingface", "whisper-models"):
        (Path(environment["CACHE_DIR"]) / name).mkdir()
    return environment


@pytest.mark.parametrize(
    "settings,expected_error",
    [
        ({}, "LIVE_UPSTREAM_RTMP_URL is invalid"),
        ({"LIVE_UPSTREAM_RTMP_URL": "invalid"}, "LIVE_UPSTREAM_RTMP_URL is invalid"),
        ({"LIVE_TRANSCODER": "invalid"}, "Invalid LIVE configuration"),
        ({"LIVE_UPSTREAM_RTMP_URL": "rtmp://manager:1935"}, None),
    ],
)
def test_diagnostics_warn_without_changing_explicit_validation(
    monkeypatch, diagnostic_environment, settings, expected_error, capsys
):
    monkeypatch.setattr(os, "environ", diagnostic_environment)
    diagnostic_environment.update(settings)
    cfg = config_module.Config()
    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", cfg)

    assert _check_config.load_config_for_checks() is config_module
    assert _check_config._DEFER_CONFIG_VALIDATION.get() is False
    output = capsys.readouterr().out
    if expected_error is None:
        assert output == ""
        cfg.validate_configuration()
    else:
        assert "⚠ WARNING: Live configuration is incomplete or invalid" in output
        assert expected_error in output
        with pytest.raises(config_module.ConfigValidationError, match=expected_error):
            cfg.validate_configuration()


def test_diagnostics_keep_non_live_errors_blocking(monkeypatch, diagnostic_environment, capsys):
    monkeypatch.setattr(os, "environ", diagnostic_environment)
    monkeypatch.setenv("RUNNER_TOKEN", "")
    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", config_module.Config())

    with pytest.raises(config_module.ConfigValidationError) as error:
        _check_config.load_config_for_checks()

    assert error.value.errors == ("RUNNER_TOKEN must be set to a secure value",)
    assert "⚠ WARNING: Live configuration" in capsys.readouterr().out
    assert _check_config._DEFER_CONFIG_VALIDATION.get() is False


def test_diagnostic_import_failure_restores_validation(monkeypatch):
    def fail_import(_name):
        assert _check_config._DEFER_CONFIG_VALIDATION.get() is True
        raise ImportError("dependency unavailable")

    monkeypatch.setattr(_check_config.importlib, "import_module", fail_import)
    with pytest.raises(ImportError, match="dependency unavailable"):
        _check_config.load_config_for_checks()
    assert _check_config._DEFER_CONFIG_VALIDATION.get() is False


@pytest.mark.parametrize("script", ["check_runner_resources", "check_runner_storage"])
@pytest.mark.parametrize(
    "settings,expected_code,warning",
    [
        ({}, 0, True),
        ({"LIVE_UPSTREAM_RTMP_URL": "rtmp://relay:relay-secret@manager:1935"}, 0, False),
        ({"LIVE_UPSTREAM_RTMP_URL": "invalid://relay:relay-secret@manager"}, 0, True),
        ({"LIVE_TRANSCODER": "invalid"}, 0, True),
        (
            {
                "LIVE_UPSTREAM_RTMP_URL": "rtmp://manager:1935",
                "LIVE_POD_URL": "https://pod.example",
            },
            0,
            True,
        ),
        ({"RUNNER_TOKEN": ""}, 2, True),
        ({"RUNNER_TASK_TYPES": "unsupported"}, 2, False),
        ({"RUNNER_TASK_TYPES": "encoding,studio"}, 0, False),
        ({"DIAGNOSTIC_TEST_LOW_RESOURCES": "1"}, 2, True),
    ],
)
def test_script_main_outside_pytest(
    script, settings, expected_code, warning, diagnostic_environment
):
    """Run real config imports and mains with deterministic host resources."""
    code = f"""
import os
from scripts import {script} as script
low = os.getenv("DIAGNOSTIC_TEST_LOW_RESOURCES") == "1"
if hasattr(script, "ResourceInfo"):
    script._get_resources = lambda: script.ResourceInfo(1 if low else 64, 1 if low else 128, [])
else:
    script._disk_usage_for_path = lambda _path: (1000.0, 0.0, 0.0 if low else 900.0)
    script._directory_size_bytes = lambda _path: 0
raise SystemExit(script.main())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env={**diagnostic_environment, **settings},
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == expected_code, result.stdout + result.stderr
    assert ("⚠ WARNING: Live configuration" in result.stdout) is warning
    assert "Traceback" not in result.stdout + result.stderr
    assert "relay-secret" not in result.stdout + result.stderr
    if expected_code == 0:
        assert "✗ ERROR" not in result.stdout
    if not any(name in settings for name in ("RUNNER_TOKEN", "RUNNER_TASK_TYPES")):
        assert ("=== Storage check" if script.endswith("storage") else "=== Resource check") in (
            result.stdout
        )


@pytest.mark.parametrize("target", ["import", "preflight"])
def test_normal_config_validation_remains_strict(target, diagnostic_environment):
    command = (
        ["-c", "import app.core.config"] if target == "import" else ["scripts/check_config.py"]
    )
    result = subprocess.run(
        [sys.executable, *command],
        cwd=Path(__file__).resolve().parents[1],
        env=diagnostic_environment,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == (1 if target == "import" else 2)
    assert "LIVE_UPSTREAM_RTMP_URL is invalid" in result.stdout + result.stderr
    assert "Live configuration is incomplete or invalid" not in result.stdout + result.stderr
