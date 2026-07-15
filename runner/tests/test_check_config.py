"""Validates the runner configuration preflight script."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from scripts import check_config


def _valid_config():
    """Return a minimal valid-looking configuration summary fixture."""
    return SimpleNamespace(
        RUNNER_BASE_PORT=8082,
        RUNNER_INSTANCES=2,
        RUNNER_TASK_TYPES={"studio", "encoding"},
        ENCODING_TYPE="CPU",
        MANAGER_URL="http://manager:8081",
        RUNNER_TOKEN="never-print-this-secret",
    )


def test_direct_script_import_adds_runner_root(monkeypatch):
    """Validate Direct execution makes the application package importable."""
    runner_root = Path(check_config.__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != str(runner_root)])
    spec = importlib.util.spec_from_file_location("check_config_direct", check_config.__file__)
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert str(runner_root) in sys.path


def test_main_reports_valid_non_sensitive_summary(monkeypatch, capsys):
    """Validate A successful check prints useful values without the runner token."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(check_config, "_load_and_validate_config", lambda: (_valid_config(), ()))

    assert check_config.main() == 0

    output = capsys.readouterr().out
    assert "✓ INFO: Runner configuration loaded and validated." in output
    assert "Instances: 2" in output
    assert "Ports: 8082-8083" in output
    assert "Task types: encoding, studio" in output
    assert "Manager URL: http://manager:8081" in output
    assert "never-print-this-secret" not in output


def test_main_reports_each_validation_error(monkeypatch, capsys):
    """Validate Invalid configuration produces actionable output and exit code 2."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        check_config,
        "_load_and_validate_config",
        lambda: (None, ("DEBUG must be a boolean", "MANAGER_URL must be absolute")),
    )

    assert check_config.main() == 2

    output = capsys.readouterr().out
    assert "✗ ERROR: DEBUG must be a boolean" in output
    assert "✗ ERROR: MANAGER_URL must be absolute" in output
    assert "✗ ERROR: Runner configuration is invalid." in output


def test_load_and_validate_config_uses_central_validator(monkeypatch):
    """Validate The script delegates loading and validation to app.core.config."""
    calls = {"validate": 0}
    config = _valid_config()

    def validate_configuration():
        calls["validate"] += 1

    config.validate_configuration = validate_configuration
    config_module = SimpleNamespace(get_config=lambda: config)
    monkeypatch.setattr(check_config.importlib, "import_module", lambda _name: config_module)

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is config
    assert errors == ()
    assert calls["validate"] == 1


def test_load_and_validate_config_preserves_structured_errors(monkeypatch):
    """Validate Aggregated configuration errors remain separate output entries."""

    class FakeValidationError(ValueError):
        errors = ("first error", "second error")

    def fail_import(_name):
        raise FakeValidationError("aggregated message")

    monkeypatch.setattr(check_config.importlib, "import_module", fail_import)

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is None
    assert errors == ("first error", "second error")


def test_load_and_validate_config_formats_unexpected_errors(monkeypatch):
    """Validate Unexpected loading failures receive a concise contextual message."""
    monkeypatch.setattr(
        check_config.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(RuntimeError("import failed")),
    )

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is None
    assert errors == ("Unable to load runner configuration: import failed",)


def test_port_range_formats_single_instance():
    """Validate A single runner instance is displayed as one port."""
    config = _valid_config()
    config.RUNNER_INSTANCES = 1

    assert check_config._port_range(config) == "8082"
