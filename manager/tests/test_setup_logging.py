"""Unit coverage for app.core.setup_logging."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler

import pytest

from app.core import setup_logging as logging_module
from app.core.setup_logging import (
    JSONFormatter,
    LogContext,
    _add_file_handler,
    _add_syslog_handler,
    _coerce_log_level,
    _create_formatter,
    get_uvicorn_log_config,
    setup_default_logging,
    setup_logging,
    setup_uvicorn_logging,
)


def test_json_formatter_includes_custom_fields():
    formatter = JSONFormatter()
    record = logging.makeLogRecord(
        {
            "name": "logger",
            "level": logging.INFO,
            "pathname": __file__,
            "lineno": 10,
            "msg": "hello",
            "func": "func",
            "module": "mod",
            "task_id": "t1",
        }
    )
    formatted = json.loads(formatter.format(record))
    assert formatted["task_id"] == "t1"
    assert formatted["message"] == "hello"


def test_json_formatter_handles_exception():
    formatter = JSONFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.makeLogRecord(
            {
                "name": "logger",
                "level": logging.ERROR,
                "pathname": __file__,
                "lineno": 20,
                "msg": "err",
                "func": "func",
                "module": "mod",
                "exc_info": sys.exc_info(),
            }
        )
    formatted = json.loads(formatter.format(record))
    assert "exception" in formatted


def test_coerce_log_level_accepts_str_and_int():
    assert _coerce_log_level("debug") == logging.DEBUG
    assert _coerce_log_level(logging.WARNING) == logging.WARNING


def test_setup_logging_uses_tmp_dir_during_tests(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    monkeypatch.setenv("PYTEST_LOG_DIR", str(tmp_path))
    logger = setup_logging("Sample", json_format=True, log_level=logging.DEBUG)
    assert logger.handlers
    assert all(not isinstance(h, RotatingFileHandler) for h in logger.handlers)


def test_setup_logging_raises_on_makedirs_failure(monkeypatch):
    def fail_makedirs(*_args, **_kwargs):
        raise OSError("nope")

    monkeypatch.setattr(logging_module.os, "makedirs", fail_makedirs)
    with pytest.raises(OSError):
        setup_logging("BadDir")


def test_add_file_handler_permission_error(monkeypatch, tmp_path):
    logger = logging.getLogger("file-handler-test")
    logger.handlers.clear()
    formatter = _create_formatter(False)
    log_path = str(tmp_path / "x.log")

    class FailingHandler(RotatingFileHandler):
        def __init__(self, *_args, **_kwargs):
            raise PermissionError("nope")

    monkeypatch.setattr(logging_module, "RotatingFileHandler", FailingHandler)
    with pytest.raises(PermissionError):
        _add_file_handler(logger, formatter, log_path)


def test_syslog_handler_exception(monkeypatch):
    logger = logging.getLogger("syslog-test")
    logger.handlers.clear()

    class FailingSyslog:
        def __init__(self, *_args, **_kwargs):
            raise OSError("fail")

    monkeypatch.setattr(logging_module, "SysLogHandler", FailingSyslog)
    _add_syslog_handler(logger, _create_formatter(False))
    assert logger.handlers == []


def test_log_context_injects_fields_and_restores_factory():
    logger = logging.getLogger("context-test")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    captured = {}

    class Capture(logging.Handler):
        def emit(self, record):
            captured["task_id"] = getattr(record, "task_id", None)

    handler = Capture()
    logger.addHandler(handler)

    with LogContext(logger, task_id="task-123"):
        logger.info("inside")

    assert captured["task_id"] == "task-123"


def test_setup_default_logging_with_string_level(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    logger = setup_default_logging(log_level="INFO")
    assert logger.level == logging.INFO


def test_get_logger_returns_named_logger():
    logger = logging_module.get_logger("custom")
    assert isinstance(logger, logging.Logger)


def test_setup_uvicorn_logging_handles_permission_error(monkeypatch):
    class FailingHandler(RotatingFileHandler):
        def __init__(self, *_args, **_kwargs):
            raise PermissionError("fail")

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "1")
    monkeypatch.setattr(logging_module, "RotatingFileHandler", FailingHandler)
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logging.getLogger(name).handlers.clear()

    setup_uvicorn_logging(json_format=True)
    uvicorn_logger = logging.getLogger("uvicorn")
    assert uvicorn_logger.handlers


def test_setup_uvicorn_logging_adds_file_handler(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(logging_module.config, "LOG_DIRECTORY", f"{tmp_path}/")
    for name in ["uvicorn", "uvicorn.error", "uvicorn.access"]:
        logging.getLogger(name).handlers.clear()

    setup_uvicorn_logging(json_format=False)
    uvicorn_logger = logging.getLogger("uvicorn")
    assert any(isinstance(h, RotatingFileHandler) for h in uvicorn_logger.handlers)


def test_setup_uvicorn_logging_clears_existing_handlers(monkeypatch, tmp_path):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(logging_module.config, "LOG_DIRECTORY", f"{tmp_path}/")

    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.handlers.clear()
    dummy = logging.StreamHandler()
    uvicorn_logger.addHandler(dummy)

    setup_uvicorn_logging(json_format=False)
    assert dummy not in logging.getLogger("uvicorn").handlers


def test_get_uvicorn_log_config_json_format():
    cfg = get_uvicorn_log_config(json_format=True)
    assert cfg["formatters"]["json"]["()"] is JSONFormatter
