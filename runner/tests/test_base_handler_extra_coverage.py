import builtins
import subprocess
from typing import Any

import pytest

from app.models.models import TaskRequest
from app.task_handlers.base_handler import BaseTaskHandler
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler


class _ConcreteBaseHandler(BaseTaskHandler):
    task_type = "dummy-extra"

    def validate_parameters(self, parameters: dict[str, Any]) -> bool:
        return BaseTaskHandler.validate_parameters(self, parameters)  # type: ignore[return-value]

    def execute_task(self, task_id: str, task_request: TaskRequest) -> dict[str, Any]:
        return BaseTaskHandler.execute_task(self, task_id, task_request)  # type: ignore[return-value]


def test_base_handler_register_script_process_covers_empty_task_and_import_error(monkeypatch):
    handler = _ConcreteBaseHandler()

    handler._register_script_process(None, 1111)

    original_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "app.core.state":
            raise ImportError("blocked")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    handler._register_script_process("task-1", 2222)


def test_base_handler_terminate_external_process_covers_fallback_and_wait_error(monkeypatch):
    handler = _ConcreteBaseHandler()

    class _Process:
        pid = 1234
        killed = False
        waited = False

        def kill(self):
            self.killed = True

        def wait(self, timeout=0):
            self.waited = True
            raise RuntimeError("wait failed")

    proc = _Process()

    monkeypatch.setattr(
        "os.killpg", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no pg"))
    )
    handler._terminate_external_process(proc)

    assert proc.killed is True
    assert proc.waited is True


def test_base_handler_wait_external_process_timeout_calls_terminate(monkeypatch):
    handler = _ConcreteBaseHandler()
    terminated = {"called": False}

    class _Process:
        def wait(self, timeout=0):
            raise subprocess.TimeoutExpired(cmd="python", timeout=timeout)

    monkeypatch.setattr(
        handler, "_terminate_external_process", lambda _proc: terminated.__setitem__("called", True)
    )
    returncode, error = handler._wait_external_process(_Process(), timeout=9)

    assert returncode is None
    assert error == "Script timeout after 9 seconds"
    assert terminated["called"] is True


def test_base_handler_run_external_script_for_task_reraises_unrelated_type_error(
    monkeypatch, tmp_path
):
    handler = _ConcreteBaseHandler()
    script_path = tmp_path / "script.py"
    script_path.write_text("print('ok')", encoding="utf-8")

    def _raise_type_error(*_args, **_kwargs):
        raise TypeError("another signature mismatch")

    monkeypatch.setattr(handler, "run_external_script", _raise_type_error)

    with pytest.raises(TypeError, match="another signature mismatch"):
        handler.run_external_script_for_task(script_path, args=[], timeout=3, task_id="task-1")


def test_base_handler_run_external_script_task_id_paths(monkeypatch, tmp_path):
    handler = _ConcreteBaseHandler()
    script_path = tmp_path / "script.py"
    script_path.write_text("print('ok')", encoding="utf-8")

    class _Proc:
        pid = 3333

    monkeypatch.setattr(handler, "_register_script_process", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: _Proc())
    monkeypatch.setattr(
        handler, "_wait_external_process", lambda *_args, **_kwargs: (None, "timeout")
    )
    timeout_result = handler.run_external_script(
        script_path, args=[], timeout=1, task_id="task-timeout"
    )
    assert timeout_result == {"success": False, "error": "timeout"}

    monkeypatch.setattr(handler, "_wait_external_process", lambda *_args, **_kwargs: (None, None))
    no_code_result = handler.run_external_script(
        script_path, args=[], timeout=1, task_id="task-no-code"
    )
    assert no_code_result == {"success": False, "error": "Script terminated without return code"}

    monkeypatch.setattr(
        subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    error_result = handler.run_external_script(
        script_path, args=[], timeout=1, task_id="task-error"
    )
    assert error_result["success"] is False
    assert "Script execution failed: boom" in error_result["error"]


def test_base_handler_read_log_tail_missing_and_truncated(tmp_path):
    handler = _ConcreteBaseHandler()
    assert handler._read_log_tail(tmp_path / "missing.log") == ""

    long_log = tmp_path / "long.log"
    long_log.write_text("abcdef", encoding="utf-8")
    assert handler._read_log_tail(long_log, max_chars=3) == "def"


def test_encoding_handler_extract_script_error_prefers_explicit_error():
    handler = VideoEncodingHandler()
    assert handler._extract_script_error({"error": " explicit failure "}) == "explicit failure"
