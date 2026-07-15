"""Regression tests for Uvicorn process failure handling."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import app.managers.process_manager as process_manager_module


def test_run_uvicorn_instance_logs_and_propagates_failure(monkeypatch):
    """A Uvicorn startup failure must make the child process fail."""
    failure = RuntimeError("Uvicorn startup failed")
    mock_logger = Mock()
    fake_uvicorn = Mock()
    fake_uvicorn.run.side_effect = failure
    fake_config = SimpleNamespace(
        ENCODING_TYPE="CPU",
        RUNNER_HOST="localhost",
        RUNNER_PROTOCOL="http",
    )

    monkeypatch.setattr(process_manager_module, "logger", mock_logger)
    monkeypatch.setattr(process_manager_module, "reload_config_from_env", lambda: fake_config)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

    with pytest.raises(RuntimeError) as raised:
        process_manager_module.run_uvicorn_instance(4, 9104)

    assert raised.value is failure
    fake_uvicorn.run.assert_called_once()
    mock_logger.exception.assert_called_once_with("Uvicorn instance %s failed", 4)
