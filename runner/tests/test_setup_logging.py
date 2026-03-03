"""Unit tests for syslog handling in app.core.setup_logging."""

import json
import logging

from app.core import setup_logging as logging_module
from app.core.setup_logging import (
    JSONFormatter,
    LoggerDisplayNameFilter,
    _add_syslog_handler,
    _create_formatter,
    _resolve_syslog_address,
    get_uvicorn_log_config,
)


def test_json_formatter_aliases_uvicorn_error_logger_name():
    formatter = JSONFormatter()
    record = logging.makeLogRecord(
        {
            "name": "uvicorn.error",
            "level": logging.INFO,
            "pathname": __file__,
            "lineno": 10,
            "msg": "startup",
            "func": "func",
            "module": "mod",
        }
    )
    formatted = json.loads(formatter.format(record))
    assert formatted["logger"] == "uvicorn.server"


def test_logger_display_name_filter_aliases_uvicorn_error():
    filt = LoggerDisplayNameFilter()
    record = logging.makeLogRecord({"name": "uvicorn.error", "msg": "x", "level": logging.INFO})
    assert filt.filter(record) is True
    assert record.display_name == "uvicorn.server"


def test_resolve_syslog_address_returns_explicit_socket(monkeypatch):
    monkeypatch.setattr(logging_module, "_is_unix_socket", lambda path: path == "/custom/syslog")
    assert _resolve_syslog_address("/custom/syslog") == "/custom/syslog"


def test_resolve_syslog_address_returns_none_for_invalid_explicit(monkeypatch):
    monkeypatch.setattr(logging_module, "_is_unix_socket", lambda _path: False)
    assert _resolve_syslog_address("/custom/syslog") is None


def test_resolve_syslog_address_uses_fallback_candidates(monkeypatch):
    monkeypatch.setattr(
        logging_module, "_DEFAULT_SYSLOG_ADDRESSES", ("/dev/log", "/var/run/syslog")
    )
    monkeypatch.setattr(logging_module, "_is_unix_socket", lambda path: path == "/var/run/syslog")
    assert _resolve_syslog_address(None) == "/var/run/syslog"


def test_add_syslog_handler_skips_when_no_socket(monkeypatch):
    logger = logging.getLogger("runner-syslog-skip")
    logger.handlers.clear()

    class FailingIfCalled:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SysLogHandler should not be instantiated")

    monkeypatch.setattr(logging_module, "SysLogHandler", FailingIfCalled)
    monkeypatch.setattr(logging_module, "_resolve_syslog_address", lambda *_args, **_kwargs: None)

    _add_syslog_handler(logger, _create_formatter(False))
    assert logger.handlers == []


def test_add_syslog_handler_adds_handler_when_available(monkeypatch):
    logger = logging.getLogger("runner-syslog-ok")
    logger.handlers.clear()

    class FakeSyslog:
        def __init__(self, *args, **kwargs):
            pass

        def setFormatter(self, fmt):
            pass

        def setLevel(self, level):
            pass

        def addFilter(self, filt):
            pass

    monkeypatch.setattr(logging_module, "SysLogHandler", FakeSyslog)
    monkeypatch.setattr(
        logging_module, "_resolve_syslog_address", lambda *_args, **_kwargs: "/dev/log"
    )

    _add_syslog_handler(logger, _create_formatter(False))
    assert any(isinstance(h, FakeSyslog) for h in logger.handlers)


def test_add_syslog_handler_ignores_oserror(monkeypatch):
    logger = logging.getLogger("runner-syslog-error")
    logger.handlers.clear()

    class FailingSyslog:
        def __init__(self, *args, **kwargs):
            raise OSError("fail")

    monkeypatch.setattr(logging_module, "SysLogHandler", FailingSyslog)
    monkeypatch.setattr(
        logging_module, "_resolve_syslog_address", lambda *_args, **_kwargs: "/dev/log"
    )

    _add_syslog_handler(logger, _create_formatter(False))
    assert logger.handlers == []


def test_get_uvicorn_log_config_uses_display_name_filter():
    cfg = get_uvicorn_log_config(runner_instance_id=1, json_format=True)
    assert "display_name" in cfg["filters"]
    assert cfg["handlers"]["default"]["filters"] == ["display_name"]
    assert cfg["handlers"]["access"]["filters"] == ["display_name"]
