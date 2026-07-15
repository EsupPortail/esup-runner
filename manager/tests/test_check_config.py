"""Validates the manager configuration preflight script."""

import importlib.util
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_config


def _valid_config():
    """Return a minimal valid-looking configuration summary fixture."""
    return SimpleNamespace(
        ENVIRONMENT="production",
        MANAGER_URL="https://manager.example.org:8081",
        MANAGER_BIND_HOST="0.0.0.0",
        MANAGER_PORT=8081,
        UVICORN_WORKERS=2,
        API_DOCS_VISIBILITY="private",
        AUTHORIZED_TOKENS={"runners": "never-print-this-token"},
        ADMIN_USERS={"admin": "never-print-this-hash"},
        RUNNERS_STORAGE_ENABLED=True,
    )


def test_direct_script_import_adds_manager_root(monkeypatch):
    """Validate direct execution makes the application package importable."""
    manager_root = Path(check_config.__file__).resolve().parents[1]
    monkeypatch.setattr(sys, "path", [path for path in sys.path if path != str(manager_root)])
    spec = importlib.util.spec_from_file_location(
        "manager_check_config_direct", check_config.__file__
    )
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert str(manager_root) in sys.path


def test_main_reports_valid_non_sensitive_summary(monkeypatch, capsys):
    """Validate a successful check prints useful values without secrets."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(check_config, "_load_and_validate_config", lambda: (_valid_config(), ()))

    assert check_config.main() == 0

    output = capsys.readouterr().out
    assert "✓ INFO: Manager configuration loaded and validated." in output
    assert "Environment: production" in output
    assert "Manager URL: https://manager.example.org:8081" in output
    assert "Bind address: 0.0.0.0:8081" in output
    assert "Uvicorn workers: 2" in output
    assert "API docs visibility: private" in output
    assert "Authorized tokens: 1" in output
    assert "Admin users: 1" in output
    assert "Shared runner storage: enabled" in output
    assert "never-print-this-token" not in output
    assert "never-print-this-hash" not in output


def test_print_summary_reports_disabled_storage(capsys):
    """Validate the summary renders disabled shared storage explicitly."""
    config = _valid_config()
    config.RUNNERS_STORAGE_ENABLED = False

    check_config._print_summary(config)

    assert "Shared runner storage: disabled" in capsys.readouterr().out


def test_main_reports_each_validation_error(monkeypatch, capsys):
    """Validate invalid configuration produces actionable output and exit code 2."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(
        check_config,
        "_load_and_validate_config",
        lambda: (None, ("MANAGER_PORT must be an integer", "LOG_LEVEL is invalid")),
    )

    assert check_config.main() == 2

    output = capsys.readouterr().out
    assert "✗ ERROR: MANAGER_PORT must be an integer" in output
    assert "✗ ERROR: LOG_LEVEL is invalid" in output
    assert "✗ ERROR: Manager configuration is invalid." in output


def test_load_and_validate_config_uses_central_validator(monkeypatch):
    """Validate the script delegates loading and validation to app.core.config."""
    calls = {"validate": 0}
    config = _valid_config()

    def validate_configuration():
        """Record the central validator invocation."""
        calls["validate"] += 1

    config.validate_configuration = validate_configuration
    config_module = SimpleNamespace(get_config=lambda: config)
    monkeypatch.setattr(check_config.importlib, "import_module", lambda _name: config_module)

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is config
    assert errors == ()
    assert calls["validate"] == 1


def test_load_and_validate_config_avoids_duplicate_startup_validation(monkeypatch):
    """Validate an import-time validated config is not checked and warned twice."""
    config = _valid_config()
    config._configuration_validated = True

    def unexpected_validation():
        """Fail if the script repeats validation already done during import."""
        raise AssertionError("validation must not run twice")

    config.validate_configuration = unexpected_validation
    config_module = SimpleNamespace(get_config=lambda: config)
    monkeypatch.setattr(check_config.importlib, "import_module", lambda _name: config_module)

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is config
    assert errors == ()


def test_load_and_validate_config_preserves_structured_errors(monkeypatch):
    """Validate aggregated configuration errors remain separate output entries."""

    class FakeValidationError(ValueError):
        """Expose structured errors like the production validation exception."""

        errors = ("first error", "second error")

    def fail_import(_name):
        """Simulate import-time validation failure."""
        raise FakeValidationError("aggregated message")

    monkeypatch.setattr(check_config.importlib, "import_module", fail_import)

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is None
    assert errors == ("first error", "second error")


def test_load_and_validate_config_formats_unexpected_errors(monkeypatch):
    """Validate unexpected loading failures receive a concise contextual message."""
    monkeypatch.setattr(
        check_config.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(RuntimeError("import failed")),
    )

    loaded, errors = check_config._load_and_validate_config()

    assert loaded is None
    assert errors == ("Unable to load manager configuration: import failed",)


def test_script_main_guard_returns_main_exit_code(monkeypatch):
    """Validate direct script execution forwards the preflight exit code."""
    from app.core import config as config_module

    config = _valid_config()
    config.validate_configuration = lambda: None
    monkeypatch.setattr(config_module, "get_config", lambda: config)
    monkeypatch.setenv("NO_COLOR", "1")

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(check_config.__file__), run_name="__main__")

    assert raised.value.code == 0
