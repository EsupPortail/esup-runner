"""Validates base handler process registration, termination, and timeout error handling."""

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
    """Validate Base handler register script process covers empty task and import error."""
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
    """Validate Base handler terminate external process covers fallback and wait error."""
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
    """Validate Base handler wait external process timeout calls terminate."""
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
    """Validate Base handler run external script for task reraises unrelated type error."""
    handler = _ConcreteBaseHandler()
    script_path = tmp_path / "script.py"
    script_path.write_text("print('ok')", encoding="utf-8")

    def _raise_type_error(*_args, **_kwargs):
        raise TypeError("another signature mismatch")

    monkeypatch.setattr(handler, "run_external_script", _raise_type_error)

    with pytest.raises(TypeError, match="another signature mismatch"):
        handler.run_external_script_for_task(script_path, args=[], timeout=3, task_id="task-1")


def test_base_handler_run_external_script_task_id_paths(monkeypatch, tmp_path):
    """Validate Base handler run external script task id paths."""
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
    """Validate Base handler read log tail missing and truncated."""
    handler = _ConcreteBaseHandler()
    assert handler._read_log_tail(tmp_path / "missing.log") == ""

    long_log = tmp_path / "long.log"
    long_log.write_text("abcdef", encoding="utf-8")
    assert handler._read_log_tail(long_log, max_chars=3) == "def"


def test_base_handler_reclassify_success_stderr_moves_non_error_lines(tmp_path):
    """Validate Base handler reclassify success stderr moves non error lines."""
    handler = _ConcreteBaseHandler()
    stdout_log = tmp_path / "info_script.log"
    stderr_log = tmp_path / "error_script.log"

    stdout_log.write_text("existing stdout", encoding="utf-8")
    stderr_log.write_text(
        "frame=   10 fps=20.0\n" "Output #0, mp4, to '/tmp/out.mp4':\n" "real warning line\n",
        encoding="utf-8",
    )

    handler._reclassify_success_stderr(stdout_log, stderr_log, returncode=0)

    stdout_text = stdout_log.read_text(encoding="utf-8")
    stderr_text = stderr_log.read_text(encoding="utf-8")

    assert "existing stdout" in stdout_text
    assert "frame=   10 fps=20.0" in stdout_text
    assert "Output #0, mp4, to '/tmp/out.mp4':" in stdout_text
    assert "real warning line" not in stdout_text
    assert stderr_text.strip() == "real warning line"


def test_base_handler_reclassify_success_stderr_keeps_stderr_on_failure(tmp_path):
    """Validate Base handler reclassify success stderr keeps stderr on failure."""
    handler = _ConcreteBaseHandler()
    stdout_log = tmp_path / "info_script.log"
    stderr_log = tmp_path / "error_script.log"

    stdout_log.write_text("existing stdout", encoding="utf-8")
    stderr_log.write_text("frame=   10 fps=20.0\n", encoding="utf-8")

    handler._reclassify_success_stderr(stdout_log, stderr_log, returncode=1)

    assert stdout_log.read_text(encoding="utf-8") == "existing stdout"
    assert stderr_log.read_text(encoding="utf-8") == "frame=   10 fps=20.0\n"


def test_base_handler_probable_error_stderr_line_empty_and_traceback():
    """Validate Base handler probable error stderr line empty and traceback."""
    handler = _ConcreteBaseHandler()
    assert handler._is_probable_error_stderr_line("") is False
    assert handler._is_probable_error_stderr_line(" Traceback (most recent call last):") is True


def test_base_handler_reclassify_success_stderr_returns_on_empty_or_all_error_lines(tmp_path):
    """Validate Base handler reclassify success stderr returns on empty or all error lines."""
    handler = _ConcreteBaseHandler()
    stdout_log = tmp_path / "info_script.log"
    missing_stderr_log = tmp_path / "missing_error_script.log"
    stderr_log = tmp_path / "error_script.log"

    stdout_log.write_text("existing stdout", encoding="utf-8")

    # Missing file forces _read_log_lines exception path and [] fallback.
    assert handler._read_log_lines(missing_stderr_log) == []
    handler._reclassify_success_stderr(stdout_log, missing_stderr_log, returncode=0)
    assert stdout_log.read_text(encoding="utf-8") == "existing stdout"

    stderr_log.write_text("warning: keep in stderr\ntraceback line\n", encoding="utf-8")
    handler._reclassify_success_stderr(stdout_log, stderr_log, returncode=0)

    assert stdout_log.read_text(encoding="utf-8") == "existing stdout"
    assert stderr_log.read_text(encoding="utf-8") == "warning: keep in stderr\ntraceback line\n"


def test_base_handler_reclassify_success_stderr_swallows_rewrite_errors(monkeypatch, tmp_path):
    """Validate Base handler reclassify success stderr swallows rewrite errors."""
    handler = _ConcreteBaseHandler()
    stdout_log = tmp_path / "info_script.log"
    stderr_log = tmp_path / "error_script.log"

    stdout_log.write_text("existing stdout\n", encoding="utf-8")
    stderr_log.write_text("frame=   10 fps=20.0\n", encoding="utf-8")
    monkeypatch.setattr(
        handler,
        "_append_lines_to_stdout_log",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("append failed")),
    )

    handler._reclassify_success_stderr(stdout_log, stderr_log, returncode=0)

    # Reclassification errors are intentionally swallowed.
    assert stderr_log.read_text(encoding="utf-8") == "frame=   10 fps=20.0\n"


def test_encoding_handler_extract_script_error_prefers_explicit_error():
    """Validate Encoding handler extract script error prefers explicit error."""
    handler = VideoEncodingHandler()
    assert handler._extract_script_error({"error": " explicit failure "}) == "explicit failure"
