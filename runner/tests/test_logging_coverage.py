import json
import logging
import stat
import sys
import types

import pytest

from app.core import setup_logging as logging_module
from app.core.setup_logging import (
    JSONFormatter,
    LogContext,
    _add_file_handler,
    _create_formatter,
    _is_unix_socket,
    _resolve_syslog_address,
    get_logger,
    setup_default_logging,
    setup_logging,
    setup_uvicorn_logging,
)


@pytest.fixture(autouse=True)
def clear_test_loggers():
    for name in [
        "runner-non-test",
        "runner-context",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ]:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    yield
    for name in [
        "runner-non-test",
        "runner-context",
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
    ]:
        logging.getLogger(name).handlers.clear()


def test_json_formatter_includes_custom_fields_and_exception_stack():
    formatter = JSONFormatter()

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.makeLogRecord(
            {
                "name": "custom.logger",
                "level": logging.ERROR,
                "pathname": __file__,
                "lineno": 42,
                "msg": "failure",
                "func": "test_fn",
                "module": "test_module",
                "task_id": "task-1",
                "runner_id": "runner-1",
                "component": "worker",
                "operation": "send",
                "exc_info": sys.exc_info(),
                "stack_info": "stack",
            }
        )
        formatted = json.loads(formatter.format(record))

    assert formatted["task_id"] == "task-1"
    assert formatted["runner_id"] == "runner-1"
    assert formatted["component"] == "worker"
    assert formatted["operation"] == "send"
    assert "exception" in formatted
    assert formatted["stack_trace"] == "stack"


def test_setup_logging_adds_file_and_syslog_handlers_outside_pytest(monkeypatch, tmp_path):
    calls = {}
    fake_sys = types.SimpleNamespace(modules={}, argv=["python"])

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(logging_module, "sys", fake_sys)
    monkeypatch.setattr(logging_module.config, "LOG_DIRECTORY", f"{tmp_path}/")
    monkeypatch.setattr(
        logging_module.os,
        "makedirs",
        lambda path, exist_ok=True: calls.setdefault("mkdir", (path, exist_ok)),
    )
    monkeypatch.setattr(
        logging_module,
        "_add_file_handler",
        lambda logger, formatter, log_path, level, max_bytes, backup_count: calls.setdefault(
            "file",
            (log_path, level, max_bytes, backup_count, isinstance(formatter, JSONFormatter)),
        ),
    )
    monkeypatch.setattr(
        logging_module,
        "_add_syslog_handler",
        lambda logger, formatter: calls.setdefault("syslog", isinstance(formatter, JSONFormatter)),
    )

    preexisting = logging.getLogger("runner-non-test")
    preexisting.addHandler(logging.NullHandler())

    logger = setup_logging("Runner Non Test", json_format=True)

    assert calls["mkdir"] == (f"{tmp_path}/", True)
    assert calls["file"][0].endswith("runner_non_test.log")
    assert calls["file"][4] is True
    assert calls["syslog"] is True
    assert logger.propagate is False
    assert len(logger.handlers) == 1


def test_setup_logging_raises_when_log_directory_cannot_be_created(monkeypatch):
    monkeypatch.setattr(
        logging_module.os,
        "makedirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")),
    )

    with pytest.raises(OSError, match="Failed to create log directory"):
        setup_logging("broken")


def test_create_formatter_returns_json_and_text_variants():
    assert isinstance(_create_formatter(True), JSONFormatter)
    assert isinstance(_create_formatter(False), logging.Formatter)


def test_add_file_handler_success_and_permission_error(monkeypatch, tmp_path):
    logger = logging.getLogger("runner-file-handler")
    logger.handlers.clear()

    class FakeRotatingFileHandler(logging.Handler):
        def __init__(self, **kwargs):
            super().__init__()
            self.kwargs = kwargs

    monkeypatch.setattr(logging_module, "RotatingFileHandler", FakeRotatingFileHandler)
    _add_file_handler(logger, _create_formatter(False), str(tmp_path / "runner.log"))
    assert any(isinstance(handler, FakeRotatingFileHandler) for handler in logger.handlers)

    class FailingRotatingFileHandler:
        def __init__(self, **kwargs):
            raise PermissionError("readonly")

    monkeypatch.setattr(logging_module, "RotatingFileHandler", FailingRotatingFileHandler)
    with pytest.raises(PermissionError, match="Cannot write to log file"):
        _add_file_handler(logger, _create_formatter(False), str(tmp_path / "readonly.log"))


def test_resolve_syslog_address_returns_none_when_no_candidate(monkeypatch):
    monkeypatch.setattr(
        logging_module, "_DEFAULT_SYSLOG_ADDRESSES", ("/dev/log", "/var/run/syslog")
    )
    monkeypatch.setattr(logging_module, "_is_unix_socket", lambda _path: False)
    assert _resolve_syslog_address(None) is None


def test_is_unix_socket_handles_success_and_oserror(monkeypatch):
    fake_stat = types.SimpleNamespace(st_mode=stat.S_IFSOCK)
    monkeypatch.setattr(logging_module.os, "stat", lambda _path: fake_stat)
    assert _is_unix_socket("/dev/log") is True

    monkeypatch.setattr(
        logging_module.os,
        "stat",
        lambda _path: (_ for _ in ()).throw(OSError("missing")),
    )
    assert _is_unix_socket("/missing") is False


def test_get_logger_returns_existing_logger():
    logger = logging.getLogger("runner-existing")
    assert get_logger("runner-existing") is logger


def test_log_context_injects_fields_and_restores_log_record_factory():
    logger = logging.getLogger("runner-context")
    original_factory = logging.getLogRecordFactory()

    with LogContext(logger, task_id="task-ctx", runner_id="runner-ctx") as context:
        assert context.logger is logger
        record = logging.getLogRecordFactory()(
            "runner-context",
            logging.INFO,
            __file__,
            123,
            "message",
            (),
            None,
        )

    assert record.task_id == "task-ctx"
    assert record.runner_id == "runner-ctx"
    assert logging.getLogRecordFactory() is original_factory


def test_setup_default_logging_delegates_to_setup_logging(monkeypatch):
    sentinel = logging.getLogger("runner-default")
    monkeypatch.setattr(logging_module, "setup_logging", lambda **kwargs: sentinel)
    assert setup_default_logging(json_format=True) is sentinel


def test_setup_uvicorn_logging_with_file_handler(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_module.config, "LOG_DIRECTORY", f"{tmp_path}/")
    monkeypatch.setattr(logging_module, "get_runner_instance_id", lambda: 3)

    class FakeRotatingFileHandler(logging.Handler):
        def __init__(self, *args, **kwargs):
            super().__init__()
            self.filename = args[0]

    monkeypatch.setattr(logging_module, "RotatingFileHandler", FakeRotatingFileHandler)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(logging.NullHandler())

    setup_uvicorn_logging(json_format=False)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert logger.propagate is False
        assert len(logger.handlers) == 2


def test_setup_uvicorn_logging_without_file_handler(monkeypatch, tmp_path):
    monkeypatch.setattr(logging_module.config, "LOG_DIRECTORY", f"{tmp_path}/")
    monkeypatch.setattr(logging_module, "get_runner_instance_id", lambda: 7)

    class FailingRotatingFileHandler:
        def __init__(self, *args, **kwargs):
            raise PermissionError("readonly")

    monkeypatch.setattr(logging_module, "RotatingFileHandler", FailingRotatingFileHandler)

    setup_uvicorn_logging(json_format=True)

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JSONFormatter)
