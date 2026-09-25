"""Regression tests for tasks deleted on the Manager while recovery state remains."""

import asyncio
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.api.routes import task as task_module
from app.core import state
from app.services import manager_service


@pytest.fixture
def recovery_context(monkeypatch, tmp_path):
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "statuses.json"))
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)
    monkeypatch.setitem(state._RUNNER_STATE, "task_statuses", {})
    monkeypatch.setitem(state._RUNNER_STATE, "is_available", True)
    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")
    monkeypatch.setattr(task_module.storage_manager, "base_path", str(tmp_path))
    monkeypatch.setattr(task_module, "process_task", AsyncMock())
    monkeypatch.setattr(task_module, "send_task_failure_email", AsyncMock())
    monkeypatch.setattr(task_module, "notify_completion", AsyncMock())
    monkeypatch.setattr(task_module, "_terminate_stale_task_processes", Mock())
    return tmp_path


def persist_failed_task(status="failed", callback_location="metadata"):
    task_id = "deleted-task"
    callback = "https://manager.example/task/completion"
    request = {
        "task_id": task_id,
        "etab_name": "UM",
        "app_name": "Pod",
        "task_type": "encoding",
        "source_url": "https://example.org/video.mp4",
        "notify_url": "https://client.example/notify",
    }
    metadata = {"runner_id": "runner-a", "task_request": request}
    if callback_location == "metadata":
        metadata["completion_callback"] = callback
    elif callback_location == "request":
        request["completion_callback"] = callback
    state.set_task_status(task_id, status, error_message="Original failure")
    state.set_task_metadata(task_id, **metadata)
    return task_id


def stub_manager(monkeypatch, responder):
    requests = []

    def respond(request):
        requests.append(request)
        return responder(request)

    async_client = httpx.AsyncClient
    monkeypatch.setattr(manager_service.config, "MANAGER_URL", "https://manager.example")
    monkeypatch.setattr(
        manager_service.httpx,
        "AsyncClient",
        lambda **kwargs: async_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    return requests


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "timeout"])
@pytest.mark.parametrize("callback_location", ["metadata", "request"])
async def test_deleted_task_is_forgotten_across_restarts_without_email(
    monkeypatch, recovery_context, status, callback_location
):
    """A persisted failure must not resurrect after deletion on the Manager."""
    task_id = persist_failed_task(status, callback_location)
    state.set_task_status("foreign-task", "failed")
    state.set_task_metadata("foreign-task", runner_id="runner-b")
    workspace = recovery_context / task_id
    workspace.mkdir()
    diagnostic = workspace / "failure.log"
    diagnostic.write_text("Original failure", encoding="utf-8")
    requests = stub_manager(
        monkeypatch, lambda _: httpx.Response(404, json={"detail": "Task not found"})
    )

    for _ in range(2):
        state.reload_task_statuses_from_disk()
        task_module.initialize_startup_availability()
        await task_module.recover_running_tasks_after_restart()
        await asyncio.sleep(0)
        assert state.get_task_status(task_id) is None
        assert state.get_task_status("foreign-task")["status"] == "failed"
        assert state.is_available()

    assert len(requests) == 1
    assert requests[0].url.path == f"/task/status/{task_id}"
    assert diagnostic.read_text(encoding="utf-8") == "Original failure"
    task_module.process_task.assert_not_awaited()
    task_module.send_task_failure_email.assert_not_awaited()
    task_module.notify_completion.assert_not_awaited()
    task_module._terminate_stale_task_processes.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "503", "403", "proxy-404"])
async def test_unconfirmed_task_is_preserved_and_can_recover_on_next_restart(
    monkeypatch, recovery_context, failure
):
    """A Manager outage must neither rerun a deleted task nor lose recoverable work."""
    task_id = persist_failed_task()
    original = state.get_task_status(task_id)
    manager_ready = False

    def respond(_request):
        if manager_ready:
            return httpx.Response(200, json={"task_id": task_id, "status": "failed"})
        if failure == "timeout":
            raise httpx.ReadTimeout("manager restarting")
        if failure == "proxy-404":
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(int(failure))

    requests = stub_manager(monkeypatch, respond)
    state.reload_task_statuses_from_disk()
    task_module.initialize_startup_availability()
    assert not state.is_available()
    await task_module.recover_running_tasks_after_restart()
    state.reload_task_statuses_from_disk()
    assert state.get_task_status(task_id) == original
    assert state.is_available()
    task_module.process_task.assert_not_awaited()
    task_module.send_task_failure_email.assert_not_awaited()
    task_module.notify_completion.assert_not_awaited()

    manager_ready = True
    task_module.initialize_startup_availability()
    await task_module.recover_running_tasks_after_restart()
    await asyncio.sleep(0)
    task_module.process_task.assert_awaited_once()
    assert state.get_task_status(task_id)["recovery_restart_attempts"] == 1
    assert state.get_task_status(task_id)["status"] == "running"
    assert not state.is_available()
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_recovery_without_manager_check_for_standalone_or_stopped_tasks(
    monkeypatch, recovery_context, stopped
):
    """Keep standalone recovery and intentional cancellation semantics."""
    task_id = persist_failed_task(callback_location="metadata" if stopped else "none")
    if stopped:
        state.set_task_metadata(task_id, stop_requested="true")
    exists = AsyncMock(side_effect=AssertionError("unexpected Manager check"))
    monkeypatch.setattr(task_module, "manager_task_exists", exists)

    state.reload_task_statuses_from_disk()
    await task_module.recover_running_tasks_after_restart()
    await asyncio.sleep(0)

    exists.assert_not_awaited()
    if stopped:
        task_module.process_task.assert_not_awaited()
        assert state.get_task_status(task_id)["status"] == "failed"
    else:
        task_module.process_task.assert_awaited_once()
        assert state.get_task_status(task_id)["status"] == "running"
