"""Unit coverage for app.services.task_service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.core.config import config
from app.core.state import runners, tasks
from app.models.models import Runner, Task
from app.services import task_service


@pytest.fixture
def clean_tasks():
    original = dict(tasks)
    tasks.clear()
    yield
    tasks.clear()
    tasks.update(original)


@pytest.fixture
def clean_runners():
    original = dict(runners)
    runners.clear()
    yield
    runners.clear()
    runners.update(original)


def _task(
    task_id: str,
    status: str,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Task:
    created = (created_at or datetime.now()).isoformat()
    updated = (updated_at or created_at or datetime.now()).isoformat()
    return Task(
        task_id=task_id,
        runner_id="r1",
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=created,
        updated_at=updated,
        error=None,
        script_output=None,
    )


def _runner(runner_id: str = "r1", token: str | None = "runner-token") -> Runner:
    return Runner(
        id=runner_id,
        url="http://runner.example",
        task_types=["encoding"],
        availability="busy",
        token=token,
    )


@pytest.mark.asyncio
async def test_cleanup_old_tasks_removes_expired(monkeypatch, clean_tasks):
    old = datetime.now() - timedelta(days=3)
    tasks["old"] = _task("old", "completed", created_at=old)
    tasks["old-running"] = _task("old-running", "running", created_at=old)
    tasks["new"] = _task("new", "completed")
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", 1)
    monkeypatch.setattr(
        task_service,
        "delete_task_for_retention_cleanup",
        lambda task_id: tasks.pop(task_id, None) is not None,
    )

    stop = asyncio.Event()
    coroutine = task_service.cleanup_old_tasks(poll_interval=0, stop_event=stop)
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert "old" not in tasks
    assert "old-running" not in tasks
    assert "new" in tasks


@pytest.mark.asyncio
async def test_cleanup_old_tasks_handles_invalid_created_at(monkeypatch, clean_tasks):
    tasks["bad"] = _task("bad", "completed")
    tasks["bad"].created_at = "not-a-date"
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", 1)
    monkeypatch.setattr(task_service, "delete_task_for_retention_cleanup", lambda _task_id: True)

    stop = asyncio.Event()
    coroutine = task_service.cleanup_old_tasks(poll_interval=0, stop_event=stop)
    bg_task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(bg_task, timeout=0.1)

    assert "bad" in tasks


@pytest.mark.asyncio
async def test_cleanup_old_tasks_handles_timezone_created_at(monkeypatch, clean_tasks):
    aware_old = datetime.now(timezone.utc) - timedelta(days=3)
    tasks["old-tz"] = _task("old-tz", "running")
    tasks["old-tz"].created_at = aware_old.isoformat()
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", 1)
    monkeypatch.setattr(
        task_service,
        "delete_task_for_retention_cleanup",
        lambda task_id: tasks.pop(task_id, None) is not None,
    )

    stop = asyncio.Event()
    coroutine = task_service.cleanup_old_tasks(poll_interval=0, stop_event=stop)
    bg_task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(bg_task, timeout=0.1)

    assert "old-tz" not in tasks


@pytest.mark.asyncio
async def test_cleanup_old_tasks_logs_when_persistent_delete_fails(monkeypatch, clean_tasks):
    old = datetime.now() - timedelta(days=3)
    tasks["old"] = _task("old", "running", created_at=old)
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", 1)
    monkeypatch.setattr(task_service, "delete_task_for_retention_cleanup", lambda _task_id: False)

    stop = asyncio.Event()
    coroutine = task_service.cleanup_old_tasks(poll_interval=0, stop_event=stop)
    bg_task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(bg_task, timeout=0.1)

    assert "old" in tasks


@pytest.mark.asyncio
async def test_check_task_timeouts_marks_timeout(clean_tasks):
    long_ago = datetime.now() - timedelta(hours=25)
    tasks["run"] = _task("run", "running", updated_at=long_ago)

    stop = asyncio.Event()
    coroutine = task_service.check_task_timeouts(poll_interval=0, stop_event=stop)
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert tasks["run"].status == "timeout"
    assert tasks["run"].error.startswith("Task timeout")


@pytest.mark.asyncio
async def test_reconcile_running_tasks_with_runners_updates_terminal_status(
    monkeypatch, clean_tasks, clean_runners
):
    long_ago = datetime.now() - timedelta(hours=2)
    tasks["run"] = _task("run", "running", updated_at=long_ago)
    runners["r1"] = Runner(
        id="r1",
        url="http://runner.example",
        task_types=["encoding"],
        availability="busy",
        token="runner-token",
    )

    async def fake_fetch(_runner: Runner, _task_id: str):
        return {
            "task_id": "run",
            "status": "completed",
            "error_message": None,
            "script_output": "done",
        }

    saved = {"count": 0}

    def fake_save():
        saved["count"] += 1
        return True

    monkeypatch.setattr(task_service, "_fetch_runner_task_status", fake_fetch)
    monkeypatch.setattr(task_service, "save_tasks", fake_save)

    stop = asyncio.Event()
    coroutine = task_service.reconcile_running_tasks_with_runners(poll_interval=0, stop_event=stop)
    bg_task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(bg_task, timeout=0.1)

    assert tasks["run"].status == "completed"
    assert tasks["run"].error is None
    assert tasks["run"].script_output == "done"
    assert saved["count"] >= 1


@pytest.mark.asyncio
async def test_reconcile_running_tasks_with_runners_keeps_running_and_refreshes_updated_at(
    monkeypatch, clean_tasks, clean_runners
):
    old_updated = datetime.now() - timedelta(hours=3)
    tasks["run"] = _task("run", "running", updated_at=old_updated)
    previous_updated = tasks["run"].updated_at
    runners["r1"] = Runner(
        id="r1",
        url="http://runner.example",
        task_types=["encoding"],
        availability="busy",
        token="runner-token",
    )

    async def fake_fetch(_runner: Runner, _task_id: str):
        return {"task_id": "run", "status": "running"}

    monkeypatch.setattr(task_service, "_fetch_runner_task_status", fake_fetch)

    stop = asyncio.Event()
    coroutine = task_service.reconcile_running_tasks_with_runners(poll_interval=0, stop_event=stop)
    bg_task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(bg_task, timeout=0.1)

    assert tasks["run"].status == "running"
    assert tasks["run"].updated_at != previous_updated


def test_build_runner_task_status_headers():
    assert task_service._build_runner_task_status_headers(_runner(token=None)) is None

    headers = task_service._build_runner_task_status_headers(_runner(token="tok"))
    assert headers == {"Accept": "application/json", "Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_fetch_runner_task_status_returns_none_without_token():
    payload = await task_service._fetch_runner_task_status(_runner(token=None), "t1")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_runner_task_status_handles_request_error(monkeypatch):
    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            raise httpx.RequestError("boom")

    monkeypatch.setattr(task_service.httpx, "AsyncClient", FakeClient)

    payload = await task_service._fetch_runner_task_status(_runner(), "t1")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_runner_task_status_handles_non_200(monkeypatch):
    class Response:
        status_code = 503
        text = "unavailable"

        def json(self):
            return {"status": "running"}

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            return Response()

    monkeypatch.setattr(task_service.httpx, "AsyncClient", FakeClient)

    payload = await task_service._fetch_runner_task_status(_runner(), "t1")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_runner_task_status_handles_invalid_json(monkeypatch):
    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            raise ValueError("invalid json")

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            return Response()

    monkeypatch.setattr(task_service.httpx, "AsyncClient", FakeClient)

    payload = await task_service._fetch_runner_task_status(_runner(), "t1")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_runner_task_status_handles_non_object_payload(monkeypatch):
    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return ["not", "a", "dict"]

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            return Response()

    monkeypatch.setattr(task_service.httpx, "AsyncClient", FakeClient)

    payload = await task_service._fetch_runner_task_status(_runner(), "t1")
    assert payload is None


@pytest.mark.asyncio
async def test_fetch_runner_task_status_success(monkeypatch):
    class Response:
        status_code = 200
        text = "ok"

        def json(self):
            return {"task_id": "t1", "status": "running"}

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_a, **_k):
            return Response()

    monkeypatch.setattr(task_service.httpx, "AsyncClient", FakeClient)

    payload = await task_service._fetch_runner_task_status(_runner(), "t1")
    assert payload == {"task_id": "t1", "status": "running"}


def test_apply_runner_task_status_rejects_unknown_status(clean_tasks):
    task = _task("t1", "running")
    assert task_service._apply_runner_task_status(task, {"status": "bogus"}) is False


def test_refresh_running_task_returns_false_when_timestamp_unchanged(clean_tasks):
    task = _task("t1", "running")
    same_timestamp = task.updated_at
    assert task_service._refresh_running_task(task, same_timestamp) is False


def test_apply_terminal_runner_task_status_sets_terminal_fields(clean_tasks):
    task = _task("t1", "running")
    task.error = "old error"
    now_iso = datetime.now().isoformat()

    changed = task_service._apply_terminal_runner_task_status(
        task,
        "completed",
        {"status": "completed", "script_output": "done"},
        now_iso,
    )

    assert changed is True
    assert task.status == "completed"
    assert task.error is None
    assert task.script_output == "done"
    assert task.updated_at == now_iso


def test_apply_terminal_error_branches(clean_tasks):
    task = _task("t1", "failed")
    task.error = None
    assert task_service._apply_terminal_error(task, "completed", {}) is False

    task.error = "same"
    assert task_service._apply_terminal_error(task, "failed", {}) is False
    assert task_service._apply_terminal_error(task, "failed", {"error_message": "same"}) is False

    changed = task_service._apply_terminal_error(task, "failed", {"error_message": "new"})
    assert changed is True
    assert task.error == "new"


def test_apply_script_output_branches(clean_tasks):
    task = _task("t1", "running")
    task.script_output = "same"

    assert task_service._apply_script_output(task, {}) is False
    assert task_service._apply_script_output(task, {"script_output": 42}) is False
    assert task_service._apply_script_output(task, {"script_output": "same"}) is False
    assert task_service._apply_script_output(task, {"script_output": "new"}) is True
    assert task.script_output == "new"


@pytest.mark.asyncio
async def test_reconcile_single_running_task_branches(monkeypatch, clean_tasks, clean_runners):
    # task missing
    assert await task_service._reconcile_single_running_task("missing") is False

    # task not running
    tasks["t-not-running"] = _task("t-not-running", "completed")
    assert await task_service._reconcile_single_running_task("t-not-running") is False

    # runner missing
    tasks["t-no-runner"] = _task("t-no-runner", "running")
    tasks["t-no-runner"].runner_id = "runner-missing"
    assert await task_service._reconcile_single_running_task("t-no-runner") is False

    # payload missing
    tasks["t-no-payload"] = _task("t-no-payload", "running")
    runners["r1"] = _runner("r1")

    async def _fetch_none(_runner_obj: Runner, _task_id: str):
        return None

    monkeypatch.setattr(task_service, "_fetch_runner_task_status", _fetch_none)
    assert await task_service._reconcile_single_running_task("t-no-payload") is False

    # apply returns false
    tasks["t-apply-false"] = _task("t-apply-false", "running")

    async def _fetch_payload(_runner_obj: Runner, _task_id: str):
        return {"status": "running"}

    monkeypatch.setattr(task_service, "_fetch_runner_task_status", _fetch_payload)
    monkeypatch.setattr(task_service, "_apply_runner_task_status", lambda *_a, **_k: False)
    assert await task_service._reconcile_single_running_task("t-apply-false") is False


@pytest.mark.asyncio
async def test_reconcile_running_tasks_with_runners_handles_loop_exception(monkeypatch):
    stop = asyncio.Event()
    errors = {"count": 0}

    async def _boom_once():
        stop.set()
        raise RuntimeError("boom")

    def _fake_error(*_a, **_k):
        errors["count"] += 1

    monkeypatch.setattr(task_service, "_reconcile_running_tasks_once", _boom_once)
    monkeypatch.setattr(task_service.logger, "error", _fake_error)

    worker = asyncio.create_task(
        task_service.reconcile_running_tasks_with_runners(poll_interval=0, stop_event=stop)
    )
    await asyncio.wait_for(worker, timeout=0.1)

    assert errors["count"] >= 1


def test_update_and_get_tasks(clean_tasks):
    tasks["t1"] = _task("t1", "pending")
    assert task_service.update_task_status("t1", "running") is True
    assert task_service.update_task_status("missing", "running") is False
    assert tasks["t1"].status == "running"

    assert task_service.update_task_status("t1", "failed", "boom") is True
    assert tasks["t1"].error == "boom"

    assert task_service.get_task("t1").task_id == "t1"
    assert task_service.get_task("missing") is None


def test_get_all_tasks_and_stats(clean_tasks):
    tasks["a"] = _task("a", "completed")
    tasks["b"] = _task("b", "failed")
    tasks["c"] = _task("c", "running")

    all_tasks = task_service.get_all_tasks()
    assert set(all_tasks.keys()) == {"a", "b", "c"}

    stats = task_service.get_task_stats()
    assert stats["total"] == 3
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["running"] == 1
