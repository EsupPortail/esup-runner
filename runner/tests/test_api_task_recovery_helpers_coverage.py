import builtins
import json

import pytest
from fastapi import HTTPException

from app.api.routes import task as task_module


@pytest.fixture(autouse=True)
def clear_recovery_monitors(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUNNER_TASK_STATUS_FILE",
        str(tmp_path / "runner_task_statuses.test.json"),
    )
    task_module._RECOVERY_MONITORS.clear()
    yield
    task_module._RECOVERY_MONITORS.clear()


def test_task_helpers_parse_pid_and_is_process_alive_branches(monkeypatch):
    assert task_module._parse_process_pid({}) is None
    assert task_module._parse_process_pid({"process_pid": "oops"}) is None
    assert task_module._parse_process_pid({"process_pid": 0}) is None

    def _raise_lookup(_pid, _sig):
        raise ProcessLookupError()

    monkeypatch.setattr(task_module.os, "kill", _raise_lookup)
    assert task_module._is_process_alive(1234) is False

    def _raise_permission(_pid, _sig):
        raise PermissionError()

    monkeypatch.setattr(task_module.os, "kill", _raise_permission)
    assert task_module._is_process_alive(1234) is True

    def _raise_os_error(_pid, _sig):
        raise OSError("bad pid")

    monkeypatch.setattr(task_module.os, "kill", _raise_os_error)
    assert task_module._is_process_alive(1234) is False

    monkeypatch.setattr(task_module.os, "kill", lambda *_args, **_kwargs: None)
    assert task_module._is_process_alive(1234) is True


def test_task_collect_recovery_script_output_truncates_and_ignores_blank_paths(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(task_module, "_resolve_task_root_if_exists", lambda _task_id: None)

    short_log = tmp_path / "stderr-short.log"
    short_log.write_text("short", encoding="utf-8")
    short_output = task_module._collect_recovery_script_output(
        "task-recovery-short",
        {
            "script_stdout_path": "   ",
            "script_stderr_path": str(short_log),
        },
    )
    assert short_output is not None
    assert short_output.startswith("[stderr-short.log]")

    long_log = tmp_path / "stderr.log"
    long_log.write_text("x" * 120500, encoding="utf-8")

    output = task_module._collect_recovery_script_output(
        "task-recovery",
        {
            "script_stdout_path": "   ",
            "script_stderr_path": str(long_log),
        },
    )

    assert output is not None
    assert len(output) <= task_module._MAX_RECOVERY_LOG_CHARS


def test_task_resolve_output_dir_and_has_useful_output_branches(monkeypatch, tmp_path):
    task_root = tmp_path / "task"
    task_root.mkdir(parents=True)
    output_dir = task_root / "output"
    output_dir.mkdir(parents=True)

    monkeypatch.setattr(task_module, "_resolve_task_root_if_exists", lambda _task_id: task_root)
    monkeypatch.setattr(
        task_module, "_find_direct_child_entry", lambda *_args, **_kwargs: output_dir
    )
    monkeypatch.setattr(
        task_module,
        "_resolve_within_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    assert task_module._resolve_task_output_dir_if_exists("task-a") is None

    not_a_directory = task_root / "output_file"
    not_a_directory.write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        task_module, "_resolve_within_base", lambda *_args, **_kwargs: not_a_directory
    )
    assert task_module._resolve_task_output_dir_if_exists("task-b") is None

    nested_dir = output_dir / "subdir"
    nested_dir.mkdir(parents=True)
    (output_dir / "task_metadata.json").write_text("{}", encoding="utf-8")
    (output_dir / "encoding.log").write_text("log", encoding="utf-8")
    assert task_module._has_useful_output_files(output_dir) is False


def test_task_read_recovery_task_results_error_branches(monkeypatch, tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True)
    metadata_path = output_dir / "task_metadata.json"

    monkeypatch.setattr(
        task_module, "_resolve_task_output_dir_if_exists", lambda _task_id: output_dir
    )
    monkeypatch.setattr(
        task_module,
        "_find_direct_child_entry",
        lambda *_args, **_kwargs: metadata_path,
    )
    monkeypatch.setattr(
        task_module,
        "_resolve_within_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(HTTPException(status_code=404)),
    )
    assert task_module._read_recovery_task_results("task-a") is None

    metadata_path.mkdir(parents=True)
    monkeypatch.setattr(
        task_module, "_resolve_within_base", lambda *_args, **_kwargs: metadata_path
    )
    assert task_module._read_recovery_task_results("task-b") is None

    metadata_path.rmdir()
    metadata_path.write_text("{", encoding="utf-8")
    assert task_module._read_recovery_task_results("task-c") is None

    metadata_path.write_text(json.dumps(["not-a-dict"]), encoding="utf-8")
    assert task_module._read_recovery_task_results("task-d") is None

    metadata_path.write_text(json.dumps({"results": "not-a-dict"}), encoding="utf-8")
    assert task_module._read_recovery_task_results("task-e") is None


def test_task_ensure_recovery_manifest_error_branches(monkeypatch, tmp_path):
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=500, detail="boom")),
    )
    assert task_module._ensure_recovery_manifest("task-a") is False

    output_dir = tmp_path / "task" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "task_metadata.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=404, detail="missing")),
    )
    monkeypatch.setattr(
        task_module, "_resolve_task_output_dir_if_exists", lambda _task_id: output_dir
    )
    monkeypatch.setattr(task_module, "_has_useful_output_files", lambda _out: False)
    assert task_module._ensure_recovery_manifest("task-b") is False

    monkeypatch.setattr(task_module, "_has_useful_output_files", lambda _out: True)
    original_open = builtins.open

    def _failing_open(*_args, **_kwargs):
        raise OSError("cannot write")

    monkeypatch.setattr(builtins, "open", _failing_open)
    assert task_module._ensure_recovery_manifest("task-c") is False
    monkeypatch.setattr(builtins, "open", original_open)


def test_task_infer_workspace_terminal_status_returns_none_when_manifest_creation_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        task_module,
        "_read_recovery_task_results",
        lambda _task_id: {"success": True, "script_output": {"ok": True}},
    )
    monkeypatch.setattr(
        task_module, "_collect_recovery_script_output", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(task_module, "_ensure_recovery_manifest", lambda _task_id: False)

    assert task_module._infer_workspace_terminal_status("task-inf", {}) is None


def test_task_get_owned_statuses_and_restart_attempts_branches(monkeypatch):
    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")
    monkeypatch.setattr(task_module, "get_runner_state", lambda: {"task_statuses": []})
    assert task_module._get_owned_task_statuses({"running"}) == {}

    monkeypatch.setattr(
        task_module,
        "get_runner_state",
        lambda: {
            "task_statuses": {
                "task-non-dict": "oops",
                "   ": {"status": "running", "runner_id": "runner-a"},
                "task-ok": {"status": "running", "runner_id": "runner-a"},
            }
        },
    )
    assert task_module._get_owned_task_statuses({"running"}) == {
        "task-ok": {"status": "running", "runner_id": "runner-a"}
    }
    assert task_module._get_recovery_restart_attempts({"recovery_restart_attempts": "bad"}) == 0


def test_task_load_recovery_task_request_branches(monkeypatch):
    assert task_module._load_recovery_task_request("task-a", {"task_request": "{"}) is None

    request = task_module._load_recovery_task_request(
        "task-b",
        {
            "completion_callback": "http://manager/callback",
            "task_request": {
                "task_id": "task-b",
                "etab_name": "UM",
                "app_name": "Pod",
                "task_type": "encoding",
                "source_url": "https://example.org/video.mp4",
                "parameters": {},
                "notify_url": "http://notify",
            },
        },
    )
    assert request is not None
    assert request.completion_callback == "http://manager/callback"

    monkeypatch.setattr(
        task_module.TaskRequest,
        "model_validate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("invalid")),
    )
    assert (
        task_module._load_recovery_task_request(
            "task-c",
            {
                "task_request": {
                    "task_id": "task-c",
                    "etab_name": "UM",
                    "app_name": "Pod",
                    "task_type": "encoding",
                    "source_url": "https://example.org/video.mp4",
                    "parameters": {},
                    "notify_url": "http://notify",
                }
            },
        )
        is None
    )


def test_task_schedule_failed_task_restart_when_attempt_limit_reached():
    payload = {"recovery_restart_attempts": task_module._RECOVERY_AUTO_RESTART_MAX_ATTEMPTS}
    assert task_module._schedule_failed_task_restart("task-max", payload) is False


@pytest.mark.asyncio
async def test_task_reconcile_recovered_task_non_404_is_raised(monkeypatch):
    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=500, detail="boom")),
    )
    with pytest.raises(HTTPException) as exc:
        await task_module._reconcile_recovered_task("task-err", {})
    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_task_reconcile_recovered_task_fallback_failure_path(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        task_module,
        "_resolve_task_manifest_path",
        lambda _task_id: (_ for _ in ()).throw(HTTPException(status_code=404, detail="missing")),
    )
    monkeypatch.setattr(task_module, "_parse_process_pid", lambda _payload: None)
    monkeypatch.setattr(
        task_module, "_infer_workspace_terminal_status", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        task_module, "_collect_recovery_script_output", lambda *_args, **_kwargs: "logs"
    )

    async def _fake_finalize(task_id, payload, *, status, error_message=None, script_output=None):
        captured["task_id"] = task_id
        captured["status"] = status
        captured["error_message"] = error_message
        captured["script_output"] = script_output

    monkeypatch.setattr(task_module, "_finalize_recovered_task", _fake_finalize)

    status = await task_module._reconcile_recovered_task("task-fallback", {"error_message": "boom"})

    assert status == "failed"
    assert captured["task_id"] == "task-fallback"
    assert captured["status"] == "failed"
    assert captured["error_message"] == "boom"
    assert captured["script_output"] == "logs"


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_exception_and_finally(monkeypatch):
    errors: list[str] = []

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "running"})

    async def _raise_reconcile(*_args, **_kwargs):
        raise RuntimeError("reconcile boom")

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _raise_reconcile)
    monkeypatch.setattr(
        task_module.logger, "error", lambda *args, **_kwargs: errors.append("logged")
    )

    task_module._RECOVERY_MONITORS["task-mon"] = object()
    await task_module._monitor_recovered_task("task-mon")

    assert "task-mon" not in task_module._RECOVERY_MONITORS
    assert errors == ["logged"]


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_returns_when_payload_missing(monkeypatch):
    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: None)
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called when payload is missing")
        ),
    )

    task_module._RECOVERY_MONITORS["task-missing"] = object()
    await task_module._monitor_recovered_task("task-missing")
    assert "task-missing" not in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_returns_when_status_not_running(monkeypatch):
    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "failed"})
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called when task is not running")
        ),
    )

    task_module._RECOVERY_MONITORS["task-failed"] = object()
    await task_module._monitor_recovered_task("task-failed")
    assert "task-failed" not in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_monitor_recovered_task_refreshes_on_terminal_state(monkeypatch):
    refreshed = {"called": 0}

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(task_module, "get_task_status", lambda _task_id: {"status": "running"})
    monkeypatch.setattr(
        task_module,
        "_refresh_availability_from_recovered_state",
        lambda: refreshed.__setitem__("called", refreshed["called"] + 1),
    )

    async def _terminal_status(*_args, **_kwargs):
        return "completed"

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _terminal_status)

    task_module._RECOVERY_MONITORS["task-done"] = object()
    await task_module._monitor_recovered_task("task-done")

    assert refreshed["called"] == 1
    assert "task-done" not in task_module._RECOVERY_MONITORS


def test_task_schedule_recovery_monitor_skips_existing_and_creates_new(monkeypatch):
    class _ExistingTask:
        def done(self):
            return False

    task_module._RECOVERY_MONITORS["task-existing"] = _ExistingTask()

    monkeypatch.setattr(
        task_module.asyncio,
        "create_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not create task")),
    )
    task_module._schedule_recovery_monitor("task-existing")

    created = {}

    class _CreatedTask:
        def done(self):
            return False

    def _fake_create_task(coro):
        created["created"] = True
        coro.close()
        return _CreatedTask()

    monkeypatch.setattr(task_module.asyncio, "create_task", _fake_create_task)
    task_module._schedule_recovery_monitor("task-new")

    assert created["created"] is True
    assert "task-new" in task_module._RECOVERY_MONITORS


@pytest.mark.asyncio
async def test_task_recover_owned_running_and_failed_tasks_handle_exceptions(monkeypatch):
    async def _raise_running(*_args, **_kwargs):
        raise RuntimeError("running recovery failed")

    monkeypatch.setattr(task_module, "_reconcile_recovered_task", _raise_running)
    await task_module._recover_owned_running_tasks({"task-r": {}})

    async def _raise_failed(*_args, **_kwargs):
        raise RuntimeError("failed recovery failed")

    monkeypatch.setattr(task_module, "_recover_failed_task", _raise_failed)
    restarted = await task_module._recover_owned_failed_tasks({"task-f": {}})
    assert restarted == 0


@pytest.mark.asyncio
async def test_task_recover_owned_running_tasks_returns_early_when_empty(monkeypatch):
    monkeypatch.setattr(
        task_module.logger,
        "info",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("logger.info must not be called for empty running_tasks")
        ),
    )
    monkeypatch.setattr(
        task_module,
        "_reconcile_recovered_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reconcile must not be called for empty running_tasks")
        ),
    )
    monkeypatch.setattr(
        task_module,
        "_schedule_recovery_monitor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("monitor must not be scheduled for empty running_tasks")
        ),
    )

    await task_module._recover_owned_running_tasks({})
