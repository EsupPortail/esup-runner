"""Validate periodic reconciliation without failing active work or accepting partial outputs."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import httpx
import pytest

from app.api.routes import task as runtime
from app.core import state
from app.core.config import Config
from app.models.models import TaskRequest
from app.services import manager_service, task_monitor
from app.services.task_dispatcher import TaskDispatcher
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler

_NOTIFY_COMPLETION = runtime.notify_completion


@pytest.fixture(autouse=True)
def isolate_monitor(monkeypatch, tmp_path):
    """Isolate persisted state, registries, process discovery and completion delivery."""
    initial_state = dict(state._RUNNER_STATE)
    initial_executions = dict(task_monitor._EXECUTIONS)
    initial_workers = set(task_monitor._WORKERS)
    state._RUNNER_STATE["task_statuses"] = {}
    state.set_available(False)
    task_monitor._EXECUTIONS.clear()
    task_monitor._WORKERS.clear()
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "statuses.json"))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.setattr(runtime.storage_manager, "base_path", str(tmp_path))
    monkeypatch.setattr(runtime, "_iter_proc_pids", lambda: [])
    monkeypatch.setattr(runtime, "_is_process_alive", lambda _pid: False)
    monkeypatch.setattr(task_monitor, "RECHECK_DELAY_SECONDS", 0)
    notifications = []

    async def notify(callback, task_id, status, error=None, script_output=None):
        notifications.append((task_id, status, error))
        return True

    monkeypatch.setattr(runtime, "notify_completion", notify)
    yield notifications
    state._RUNNER_STATE.clear()
    state._RUNNER_STATE.update(initial_state)
    task_monitor._EXECUTIONS.clear()
    task_monitor._EXECUTIONS.update(initial_executions)
    task_monitor._WORKERS.clear()
    task_monitor._WORKERS.update(initial_workers)


def running_task(task_id="task-1", **metadata):
    """Persist one running task with a current attempt and completion callback."""
    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        runner_id=state.get_runner_id(),
        completion_callback="http://manager.example/task/completion",
        execution_id="current-attempt",
        **metadata,
    )
    return state.get_task_status(task_id)


def outputs(tmp_path, *, success=True, execution_id="current-attempt", files=None):
    """Create explicit current-run result evidence and recorded deliverables."""
    output_dir = tmp_path / "task-1" / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "result.mp4").write_bytes(b"recorded media output")
    document = {
        "task_id": "task-1",
        "execution_id": execution_id,
        "results": {"success": success, "error": "Script timeout after 20 seconds"},
        "output_files": ["result.mp4"] if files is None else files,
    }
    (output_dir / "task_metadata.json").write_text(json.dumps(document))
    return output_dir


@pytest.mark.asyncio
async def test_successful_outputs_rebuild_manifest_and_notify(tmp_path, isolate_monitor):
    running_task()
    outputs(tmp_path)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "completed"
    assert state.is_available() is True
    manifest = json.loads((tmp_path / "task-1" / "manifest.json").read_text())
    assert manifest == {
        "task_id": "task-1",
        "execution_id": "current-attempt",
        "files": ["result.mp4"],
    }
    assert isolate_monitor == [("task-1", "completed", None)]
    await task_monitor.check_running_tasks_once()
    assert len(isolate_monitor) == 1


@pytest.mark.asyncio
async def test_current_manifest_confirms_success_without_metadata(tmp_path):
    running_task()
    output_dir = outputs(tmp_path)
    (output_dir / "task_metadata.json").unlink()
    (output_dir.parent / "manifest.json").write_text(
        json.dumps(
            {"task_id": "task-1", "execution_id": "current-attempt", "files": ["result.mp4"]}
        )
    )
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evidence", ["partial", "stale", "empty", "missing", "corrupt", "wrong-task"]
)
async def test_incomplete_or_unrelated_outputs_are_not_completed(
    tmp_path, evidence, isolate_monitor
):
    running_task()
    output_dir = outputs(tmp_path)
    metadata = output_dir / "task_metadata.json"
    if evidence == "partial":
        metadata.unlink()
    elif evidence == "stale":
        outputs(tmp_path, execution_id="previous-attempt")
        (output_dir.parent / "manifest.json").write_text(
            json.dumps(
                {"task_id": "task-1", "execution_id": "previous-attempt", "files": ["result.mp4"]}
            )
        )
    elif evidence == "empty":
        (output_dir / "result.mp4").write_bytes(b"")
    elif evidence == "missing":
        outputs(tmp_path, files=["result.mp4", "missing.m3u8"])
    elif evidence == "corrupt":
        metadata.write_text("{incomplete-json")
    else:
        document = json.loads(metadata.read_text())
        document["task_id"] = "another-task"
        metadata.write_text(json.dumps(document))
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"
    assert state.is_available() is True
    assert isolate_monitor[0][1] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../outside.mp4", "/tmp/outside.mp4", "linked.mp4"])
async def test_output_paths_cannot_escape_workspace(tmp_path, filename):
    running_task()
    output_dir = outputs(tmp_path, files=[filename])
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside the task")
    (output_dir / "linked.mp4").symlink_to(outside)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"
    assert outside.read_bytes() == b"outside the task"


@pytest.mark.asyncio
async def test_explicit_failure_takes_precedence_over_manifest(tmp_path):
    running_task()
    output_dir = outputs(tmp_path, success=False)
    (output_dir.parent / "manifest.json").write_text(
        json.dumps(
            {"task_id": "task-1", "execution_id": "current-attempt", "files": ["result.mp4"]}
        )
    )
    await task_monitor.check_running_tasks_once()
    payload = state.get_task_status("task-1")
    assert payload["status"] == "timeout"
    assert "20 seconds" in payload["error_message"]


@pytest.mark.asyncio
async def test_user_cancellation_is_preserved_even_with_successful_outputs(tmp_path):
    running_task(stop_requested="true")
    outputs(tmp_path)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"
    assert state.get_task_status("task-1")["error_message"] == "Cancelled by user."


@pytest.mark.asyncio
async def test_active_coroutine_without_pid_is_preserved():
    running_task()
    execution_id = task_monitor.register_execution("task-1")
    assert task_monitor.register_execution("task-1") == execution_id
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "running"
    task_monitor.unregister_execution("task-1", "old-execution")
    assert task_monitor.has_active_execution("task-1") is True
    task_monitor.unregister_execution("task-1", execution_id)
    assert task_monitor.has_active_execution("task-1") is False


@pytest.mark.asyncio
async def test_handler_thread_without_pid_is_preserved():
    running_task()
    started = threading.Event()
    release = threading.Event()

    class DownloadingHandler:
        def execute_task(self, *_args):
            started.set()
            release.wait(timeout=5)
            return {"success": True}

    worker = threading.Thread(
        target=task_monitor.execute_handler, args=(DownloadingHandler(), "task-1", None)
    )
    worker.start()
    try:
        assert started.wait(timeout=1)
        await task_monitor.check_running_tasks_once()
        assert state.get_task_status("task-1")["status"] == "running"
    finally:
        release.set()
        worker.join(timeout=2)
    assert task_monitor.has_active_execution("task-1") is False


@pytest.mark.asyncio
async def test_completed_coroutine_registration_does_not_hide_abandoned_status():
    running_task()

    async def abandoned():
        task_monitor.register_execution("task-1")

    execution = asyncio.create_task(abandoned())
    await execution
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_real_process_death_is_detected(monkeypatch):
    from app.services.task_recovery import is_process_alive

    monkeypatch.setattr(
        runtime, "_is_process_alive", lambda pid: is_process_alive(pid, os_module=os)
    )
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        birth = task_monitor.read_process_identity(process.pid)[1]
        running_task(process_pid=process.pid, process_start_ticks=birth)
        await task_monitor.check_running_tasks_once()
        assert state.get_task_status("task-1")["status"] == "running"
        process.terminate()
        process.wait(timeout=3)
        await task_monitor.check_running_tasks_once()
        assert state.get_task_status("task-1")["status"] == "failed"
        assert "process_start_ticks" not in state.get_task_status("task-1")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("process_state,birth", [("Z", 100), ("X", 100), ("S", 200)])
async def test_zombie_or_reused_pid_is_not_active(monkeypatch, process_state, birth):
    running_task(process_pid=123, process_start_ticks=100)
    monkeypatch.setattr(runtime, "_is_process_alive", lambda _pid: True)
    monkeypatch.setattr(task_monitor, "read_process_identity", lambda _pid: (process_state, birth))
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_unreadable_process_state_keeps_task_running(monkeypatch):
    running_task(process_pid=123)
    monkeypatch.setattr(runtime, "_is_process_alive", lambda _pid: True)

    def denied(_pid):
        raise PermissionError

    monkeypatch.setattr(task_monitor, "read_process_identity", denied)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "running"


def test_workspace_process_matching_does_not_confuse_task_prefixes(monkeypatch, tmp_path):
    outputs(tmp_path)
    root = tmp_path / "task-1"
    monkeypatch.setattr(runtime, "_iter_proc_pids", lambda: [123])
    monkeypatch.setattr(task_monitor, "read_process_identity", lambda _pid: ("S", 1))
    monkeypatch.setattr(Path, "read_bytes", lambda _path: str(root.parent / "task-10").encode())
    assert task_monitor.has_workspace_processes("task-1", runtime) is False
    monkeypatch.setattr(
        Path, "read_bytes", lambda _path: b"ffmpeg\x00" + str(root / "output.mp4").encode()
    )
    assert task_monitor.has_workspace_processes("task-1", runtime) is True


@pytest.mark.asyncio
async def test_recheck_does_not_override_a_new_attempt(monkeypatch):
    running_task()

    async def relaunch(_delay):
        state.set_task_metadata("task-1", execution_id="new-attempt")

    monkeypatch.setattr(task_monitor.asyncio, "sleep", relaunch)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "running"
    assert state.get_task_status("task-1")["execution_id"] == "new-attempt"


@pytest.mark.asyncio
async def test_terminal_status_survives_callback_failure(monkeypatch, tmp_path):
    running_task()
    outputs(tmp_path)

    async def unavailable(*_args):
        raise OSError("manager unavailable")

    monkeypatch.setattr(runtime, "notify_completion", unavailable)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "completed"
    assert state.is_available() is True


@pytest.mark.asyncio
async def test_other_instances_and_live_sessions_are_preserved():
    running_task("foreign")
    state.set_task_metadata("foreign", runner_id="another-runner")
    running_task("live-task", live={"state": "running"})
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("foreign")["status"] == "running"
    assert state.get_task_status("live-task")["status"] == "running"


@pytest.mark.asyncio
async def test_execution_identity_and_birth_survive_restart(tmp_path):
    running_task(process_pid=123, process_start_ticks=100)
    outputs(tmp_path)
    state._RUNNER_STATE["task_statuses"] = {}
    state.reload_task_statuses_from_disk()
    payload = state.get_task_status("task-1")
    assert payload["execution_id"] == "current-attempt"
    assert payload["process_start_ticks"] == 100
    assert await runtime._reconcile_recovered_task("task-1", payload) == "completed"


@pytest.mark.asyncio
async def test_new_startup_recovery_rejects_previous_attempt_outputs(tmp_path):
    payload = running_task()
    outputs(tmp_path, execution_id="old-attempt")
    assert await runtime._reconcile_recovered_task("task-1", payload) == "failed"


@pytest.mark.asyncio
async def test_publication_records_attempt_and_expected_files(tmp_path):
    running_task()
    output_dir = tmp_path / "task-1" / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "result.mp4").write_bytes(b"media")
    handler = VideoEncodingHandler()
    handler.save_task_metadata("task-1", {"success": True}, output_dir)
    metadata = json.loads((output_dir / "task_metadata.json").read_text())
    assert metadata["execution_id"] == "current-attempt"
    assert metadata["output_files"] == ["result.mp4"]
    await TaskDispatcher()._package_task_results("task-1", output_dir, {"success": True})
    manifest = json.loads((output_dir.parent / "manifest.json").read_text())
    assert manifest["execution_id"] == metadata["execution_id"]
    assert not list(output_dir.glob("tmp*"))


@pytest.mark.asyncio
async def test_cancelled_processing_is_reconciled_later(monkeypatch):
    async def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(runtime.task_dispatcher, "dispatch_task", cancel)
    request = TaskRequest(
        task_id="task-1",
        etab_name="Example institution",
        app_name="Example client",
        source_url="http://example.org/video.mp4",
        task_type="encoding",
        parameters={},
        notify_url="",
    )
    with pytest.raises(asyncio.CancelledError):
        await runtime.process_task("task-1", request)
    assert task_monitor.has_active_execution("task-1") is False
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"


@pytest.mark.parametrize("value,expected", [(None, 3600), ("600", 600), ("0", 0)])
def test_monitor_interval_configuration(monkeypatch, value, expected):
    monkeypatch.delenv("TASK_PROCESS_CHECK_INTERVAL_SECONDS", raising=False)
    if value is not None:
        monkeypatch.setenv("TASK_PROCESS_CHECK_INTERVAL_SECONDS", value)
    assert Config().TASK_PROCESS_CHECK_INTERVAL_SECONDS == expected


def test_negative_monitor_interval_is_rejected(monkeypatch):
    monkeypatch.setenv("TASK_PROCESS_CHECK_INTERVAL_SECONDS", "-1")
    with pytest.raises(ValueError, match="TASK_PROCESS_CHECK_INTERVAL_SECONDS"):
        Config().validate_configuration()


@pytest.mark.asyncio
async def test_monitor_loop_is_disabled_at_zero(monkeypatch):
    monkeypatch.setattr(task_monitor.config, "TASK_PROCESS_CHECK_INTERVAL_SECONDS", 0)
    await task_monitor.task_process_check_loop()


@pytest.mark.asyncio
async def test_monitor_loop_retries_after_errors_and_cancels(monkeypatch):
    monkeypatch.setattr(task_monitor.config, "TASK_PROCESS_CHECK_INTERVAL_SECONDS", 600)
    calls = []

    async def sleep(interval):
        calls.append(interval)
        if len(calls) == 3:
            raise asyncio.CancelledError

    async def broken_pass():
        raise RuntimeError("failed pass")

    monkeypatch.setattr(task_monitor.asyncio, "sleep", sleep)
    monkeypatch.setattr(task_monitor, "check_running_tasks_once", broken_pass)
    with pytest.raises(asyncio.CancelledError):
        await task_monitor.task_process_check_loop()
    assert calls == [600, 600, 600]


def test_atomic_metadata_write_does_not_follow_destination_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("original")
    destination = tmp_path / "result.json"
    destination.symlink_to(outside)
    task_monitor.write_result_document(destination, {"success": True})
    assert outside.read_text() == "original"
    assert not destination.is_symlink()
    assert json.loads(destination.read_text())["success"] is True


def test_atomic_document_write_cleans_up_on_storage_failure(monkeypatch, tmp_path):
    def disk_full(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(task_monitor.os, "fsync", disk_full)
    with pytest.raises(OSError):
        task_monitor.write_result_document(tmp_path / "metadata.json", {"success": True})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [[], [42], ["task_metadata.json"]])
async def test_invalid_or_metadata_only_deliverables_are_rejected(tmp_path, files):
    running_task()
    outputs(tmp_path, files=files)
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_non_object_metadata_is_not_a_success(tmp_path):
    running_task()
    output_dir = outputs(tmp_path)
    (output_dir / "task_metadata.json").write_text("[]")
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "failed"


@pytest.mark.asyncio
async def test_successful_metadata_and_manifest_are_both_verified(tmp_path):
    running_task()
    output_dir = outputs(tmp_path)
    await TaskDispatcher()._package_task_results("task-1", output_dir, {"success": True})
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "completed"


@pytest.mark.asyncio
async def test_explicit_success_can_recover_an_existing_unmarked_task(tmp_path):
    running_task()
    state.set_task_metadata("task-1", execution_id=None)
    output_dir = outputs(tmp_path, execution_id=None)
    metadata_path = output_dir / "task_metadata.json"
    document = json.loads(metadata_path.read_text())
    document.pop("output_files")
    metadata_path.write_text(json.dumps(document))
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "completed"


@pytest.mark.asyncio
async def test_process_appearing_during_recheck_prevents_failure(monkeypatch):
    running_task()
    checks = iter([False, True])
    monkeypatch.setattr(task_monitor, "has_task_processes", lambda *_args: next(checks))
    await task_monitor.check_running_tasks_once()
    assert state.get_task_status("task-1")["status"] == "running"


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_process_scan_handles_disappearing_or_inaccessible_entries(monkeypatch, tmp_path, error):
    outputs(tmp_path)
    monkeypatch.setattr(runtime, "_iter_proc_pids", lambda: [123])

    def unreadable(_path):
        raise error

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert task_monitor.has_workspace_processes("task-1", runtime) is False


def test_matching_child_with_unreadable_state_remains_active(monkeypatch, tmp_path):
    outputs(tmp_path)
    monkeypatch.setattr(runtime, "_iter_proc_pids", lambda: [123])
    monkeypatch.setattr(Path, "read_bytes", lambda _path: str(tmp_path / "task-1").encode())

    def unreadable(_pid):
        raise PermissionError

    monkeypatch.setattr(task_monitor, "read_process_identity", unreadable)
    assert task_monitor.has_workspace_processes("task-1", runtime) is True


@pytest.mark.asyncio
async def test_startup_inference_checks_new_execution_artifacts(tmp_path):
    payload = running_task()
    output_dir = outputs(tmp_path)
    assert runtime._infer_workspace_terminal_status("task-1", payload)[0] == "completed"
    (output_dir / "task_metadata.json").write_text("[]")
    assert runtime._infer_workspace_terminal_status("task-1", payload) is None
    assert await runtime._reconcile_recovered_task("task-1", payload) == "failed"


@pytest.mark.asyncio
async def test_startup_preserves_new_execution_surviving_process(monkeypatch):
    payload = running_task(process_pid=123, process_start_ticks=100)
    monkeypatch.setattr(runtime, "_is_process_alive", lambda _pid: True)
    monkeypatch.setattr(task_monitor, "read_process_identity", lambda _pid: ("S", 100))
    assert await runtime._reconcile_recovered_task("task-1", payload) == "running"


@pytest.mark.asyncio
async def test_callback_retries_keep_original_manager_run_id(monkeypatch):
    running_task(task_request={"run_id": "original-run"})
    monkeypatch.setattr(runtime.config, "MANAGER_URL", "http://manager.example")
    monkeypatch.setattr(runtime.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(runtime.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    sent = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            sent.append(dict(kwargs["json"]))
            if len(sent) == 1:
                state.set_task_metadata("task-1", task_request={"run_id": "new-run"})
                return httpx.Response(503, text="unavailable")
            return httpx.Response(204)

    monkeypatch.setattr(runtime.httpx, "AsyncClient", Client)
    # The fixture stubs delivery for most tests; exercise the actual route function here.
    assert await _NOTIFY_COMPLETION("http://manager.example/task/completion", "task-1", "completed")
    assert [payload["run_id"] for payload in sent] == ["original-run", "original-run"]


@pytest.mark.asyncio
async def test_manifest_status_fallback_retains_execution_identity(tmp_path):
    output_dir = outputs(tmp_path)
    (output_dir.parent / "manifest.json").write_text(
        json.dumps({"task_id": "task-1", "execution_id": "old-attempt", "files": ["result.mp4"]})
    )
    payload = await runtime.get_task_status_endpoint("task-1", "manager-token")
    assert payload["execution_id"] == "old-attempt"
    (output_dir.parent / "manifest.json").write_text("invalid-json")
    assert (await runtime.get_task_status_endpoint("task-1", "manager-token"))[
        "status"
    ] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["wrong-task", "missing-output-dir"])
async def test_manifest_status_fallback_rejects_invalid_new_execution(tmp_path, invalid):
    output_dir = outputs(tmp_path)
    (output_dir.parent / "manifest.json").write_text(
        json.dumps(
            {
                "task_id": "another-task" if invalid == "wrong-task" else "task-1",
                "execution_id": "attempt",
                "files": ["result.mp4"],
            }
        )
    )
    if invalid == "missing-output-dir":
        for path in output_dir.iterdir():
            path.unlink()
        output_dir.rmdir()
    assert (await runtime.get_task_status_endpoint("task-1", "manager-token"))[
        "status"
    ] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("manager_run,expected", [("old-run", True), ("new-run", False)])
async def test_recovery_confirms_manager_execution_before_retry(monkeypatch, manager_run, expected):
    running_task(task_request={"run_id": "old-run"})

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _url, **_kwargs):
            return httpx.Response(200, json={"task_id": "task-1", "run_id": manager_run})

    monkeypatch.setattr(manager_service.httpx, "AsyncClient", Client)
    assert await manager_service.manager_task_exists("task-1") is expected


@pytest.mark.asyncio
async def test_replaced_failed_attempt_is_not_restarted(monkeypatch):
    running_task(task_request={"run_id": "old-run"})
    state.set_task_status("task-1", "failed")

    async def replaced(_task_id):
        return False

    monkeypatch.setattr(runtime, "manager_task_exists", replaced)
    assert await runtime._recover_failed_task("task-1", state.get_task_status("task-1")) is False
    assert state.get_task_status("task-1") is None
