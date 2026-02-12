"""Coverage-oriented tests for app.api.routes.task.

These tests are intentionally pragmatic: they mock httpx/network + persistence
so we can cover all branches deterministically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.auth import verify_admin, verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task, TaskCompletionNotification
from app.services import background_service


@pytest.fixture
def task_module():
    from app.api.routes import task as task_module  # type: ignore

    return task_module


@pytest.fixture
def client(monkeypatch, task_module):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: "test-token"
    app.dependency_overrides[verify_admin] = lambda: True

    # Neutralise persistence for tests.
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(verify_token, None)
    app.dependency_overrides.pop(verify_admin, None)


@pytest.fixture
def clean_state():
    original_runners = dict(runners)
    original_tasks = dict(tasks)

    runners.clear()
    tasks.clear()

    yield

    runners.clear()
    runners.update(original_runners)
    tasks.clear()
    tasks.update(original_tasks)


def _runner(runner_id: str, *, url: str = "http://r1.example", token: str = "tok") -> Runner:
    return Runner(
        id=runner_id,
        url=url,
        task_types=["encoding", "ingest"],
        token=token,
        version="1.0.0",
        last_heartbeat=datetime.now() - timedelta(seconds=1),
        availability="available",
        status="online",
    )


def _task(task_id: str, runner_id: str, *, status: str, notify_url: str | None = None) -> Task:
    now = datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id=runner_id,
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url=notify_url or "https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )


def test_append_task_stats_csv_handles_invalid_date(task_module, tmp_path, monkeypatch):
    monkeypatch.setattr(task_module, "PathlibPath", lambda path="": tmp_path / path)

    task = Task(
        task_id="t-invalid",
        runner_id="r1",
        status="completed",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at="not-a-date",
        updated_at="2026-02-02T00:00:00",
        error=None,
        script_output=None,
    )

    task_module._append_task_stats_csv(task)

    csv_path = tmp_path / "data" / "task_stats.csv"
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("task_id,date,task_type,status,app_name,app_version,etab_name")

    row = lines[1].split(",")
    # Invalid created_at should result in an empty date column
    assert row[0] == "t-invalid"
    assert row[1] == ""


def test_task_completion_appends_stats_errors_are_logged(
    client, clean_state, monkeypatch, task_module
):
    # Avoid real persistence and notifications
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    async def _send_ok(*_, **__):
        return True, None

    monkeypatch.setattr(task_module, "_send_notify_callback", _send_ok)
    monkeypatch.setattr(
        task_module,
        "_append_task_stats_csv",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("csv-fail")),
    )

    # Runner/token setup
    runner_token = "runner-token"
    runners["r1"] = _runner("r1", token=runner_token)

    now = datetime.now().isoformat()
    tasks["t1"] = Task(
        task_id="t1",
        runner_id="r1",
        status="running",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )

    # Ensure dependency override returns the same token as the runner
    previous_override = app.dependency_overrides.get(verify_token)
    app.dependency_overrides[verify_token] = lambda: runner_token

    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": "ok",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(verify_token, None)
        else:
            app.dependency_overrides[verify_token] = previous_override


# -----------------------------
# Notify helpers
# -----------------------------


@pytest.mark.asyncio
async def test_send_notify_callback_success(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed", notify_url="https://example.com/notify")

    notification = TaskCompletionNotification(
        task_id="t1",
        status="completed",
        error_message=None,
        script_output="ok",
    )

    class FakeResponse:
        status_code = 200
        text = "ok"

    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *_, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["content"] = kwargs.get("content")
            return FakeResponse()

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    async def _fake_resolve(_host: str):
        return ["93.184.216.34"]

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve)

    ok, err = await task_module._send_notify_callback(tasks["t1"], notification)
    assert ok is True
    assert err is None
    assert captured["url"] == "https://example.com/notify"
    assert "Authorization" not in (captured["headers"] or {})
    assert captured["headers"].get("Content-Type") == "application/json"
    assert isinstance(captured["content"], (bytes, bytearray))


@pytest.mark.asyncio
async def test_send_notify_callback_non_200_returns_error(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed", notify_url="https://example.com/notify")

    notification = TaskCompletionNotification(
        task_id="t1",
        status="completed",
        error_message=None,
        script_output=None,
    )

    class FakeResponse:
        status_code = 500
        text = "nope"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    ok, err = await task_module._send_notify_callback(tasks["t1"], notification)
    assert ok is False
    assert err and "500" in err


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_task_missing(task_module, clean_state):
    await task_module._retry_notify_callback(
        "missing", TaskCompletionNotification(task_id="missing", status="completed")
    )


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_no_notify_url(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].notify_url = None

    called = {"count": 0}

    async def fake_send(*_a, **_k):
        called["count"] += 1
        return True, None

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_retry_notify_callback_succeeds_after_retry(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 2)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    attempts: list[bool] = [False, True]

    async def fake_send(*_a, **_k):
        ok = attempts.pop(0)
        return ok, None

    restored: dict[str, Any] = {"called": False}

    def fake_restore(task_id: str, notification: TaskCompletionNotification) -> None:
        restored["called"] = True
        tasks[task_id].status = notification.status

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "_restore_status_after_notify", fake_restore)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert restored["called"] is True
    assert tasks["t1"].status == "completed"


@pytest.mark.asyncio
async def test_retry_notify_callback_exhausts_and_handles_exceptions(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_send(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)

    # Should swallow exceptions and just exhaust retries.
    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )


@pytest.mark.asyncio
async def test_retry_notify_callback_sleeps_when_delay_positive(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 2)

    slept: dict[str, Any] = {"seconds": None}

    async def fake_sleep(seconds: float):
        slept["seconds"] = seconds

    async def fake_send(*_a, **_k):
        return False, "nope"

    monkeypatch.setattr(task_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert slept["seconds"] == 1


def test_set_notify_warning_failure_without_existing_error(monkeypatch, task_module, clean_state):
    tasks["t1"] = _task("t1", "r1", status="failed")
    tasks["t1"].error = None
    previous_updated_at = tasks["t1"].updated_at

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)

    task_module._set_notify_warning("t1", "notify callback failed")

    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "notify callback failed"
    assert tasks["t1"].updated_at != previous_updated_at
    assert called["count"] == 1


def test_restore_status_after_notify_sets_error_for_non_completed(
    monkeypatch, task_module, clean_state
):
    tasks["t1"] = _task("t1", "r1", status="warning")
    tasks["t1"].error = None

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)

    task_module._restore_status_after_notify(
        "t1",
        TaskCompletionNotification(
            task_id="t1",
            status="failed",
            error_message="runner failed",
            script_output=None,
        ),
    )

    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "runner failed"
    assert called["count"] == 1


# -----------------------------
# Web UI endpoints
# -----------------------------


def test_view_tasks_filters_and_renders(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="failed")

    resp = client.get("/tasks?status=completed&limit=10")
    assert resp.status_code == 200
    assert "Tasks Management" in resp.text
    assert "t1" in resp.text
    assert "t2" not in resp.text


def test_view_tasks_search_task_type_and_status_counts_else(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="failed")
    tasks["t3"] = _task("t3", "r1", status="custom-status")
    tasks["t3"].task_type = "ingest"
    tasks["t3"].etab_name = "Université"

    captured: dict[str, Any] = {}

    def fake_template_response(_request: Any, _name: str, context: dict[str, Any]):
        captured.update(context)
        serializable = dict(context)
        serializable.pop("request", None)
        serializable.pop("now", None)
        return JSONResponse(serializable)

    monkeypatch.setattr(task_module.templates, "TemplateResponse", fake_template_response)

    resp = client.get("/tasks?task_type=ingest&search=univers")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["current_filters"]["task_type"] == "ingest"
    assert any(t["id"] == "t3" for t in payload["tasks"])
    # Exercises the status_counts "else" branch for unknown statuses.
    assert payload["status_counts"]["custom-status"] == 1


def test_get_task_details_api_404(client, clean_state):
    resp = client.get("/tasks/api/nope")
    assert resp.status_code == 404


def test_get_task_details_api_ok(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    resp = client.get("/tasks/api/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"


# -----------------------------
# Execute async
# -----------------------------


def test_execute_task_async_rejects_on_priority_quota(
    monkeypatch, client, task_module, clean_state
):
    monkeypatch.setattr(task_module.config, "PRIORITIES_ENABLED", True)
    monkeypatch.setattr(task_module.config, "PRIORITY_DOMAIN", "priority.example")
    monkeypatch.setattr(task_module.config, "MAX_OTHER_DOMAIN_TASK_PERCENT", 0)

    monkeypatch.setattr(task_module, "would_exceed_other_domain_quota", lambda **_: True)

    async def _fake_resolve(_host: str):
        return ["93.184.216.34"]

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 503


def test_execute_task_async_no_runners_available(monkeypatch, client, task_module, clean_state):
    # One runner in registry but it times out / is unavailable.
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "No runners available"


def test_execute_task_async_success_creates_task_and_schedules(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeResponse:
        def json(self):
            return {"available": True, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        # Avoid un-awaited coroutine warnings; we only need to assert scheduling.
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert task_id in tasks
    assert tasks[task_id].status == "running"
    assert "coro" in scheduled


# -----------------------------
# Basic API endpoints
# -----------------------------


def test_get_task_status_404(client, clean_state):
    resp = client.get("/task/status/nope")
    assert resp.status_code == 404


def test_get_task_status_ok(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    resp = client.get("/task/status/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"


def test_list_tasks_returns_dict(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    resp = client.get("/task/list")
    assert resp.status_code == 200
    assert "t1" in resp.json()


# -----------------------------
# Local storage helpers
# -----------------------------


def test_validate_result_path_rejects_traversal(task_module):
    with pytest.raises(Exception):
        task_module._validate_result_path("../secret")


def test_resolve_shared_storage_base_errors(monkeypatch, task_module, tmp_path: Path):
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path / "nope"))

    with pytest.raises(Exception):
        task_module._resolve_shared_storage_base()


def test_resolve_shared_storage_base_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    (tmp_path / "base").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path / "base"))

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._resolve_shared_storage_base()
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_resolve_shared_storage_base_happy_path(monkeypatch, task_module, tmp_path: Path):
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))
    base = task_module._resolve_shared_storage_base()
    assert base.exists() and base.is_dir()


def test_get_local_task_dir_rejects_outside_base(monkeypatch, task_module, tmp_path: Path):
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("../evil")
    assert exc.value.status_code == 500


def test_get_local_task_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("t-missing")
    assert exc.value.status_code == 404


def test_get_local_task_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    monkeypatch.setattr(task_module, "_resolve_shared_storage_base", lambda: tmp_path)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_task_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_output_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_rejects_symlink_outside(monkeypatch, task_module, tmp_path: Path):
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (task_dir / "output").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 500


def test_get_local_output_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 404


def test_mark_warning_as_completed_calls_save_tasks(monkeypatch, task_module, clean_state):
    tasks["t1"] = _task("t1", "r1", status="warning")

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)
    task_module._mark_warning_as_completed("t1")
    assert tasks["t1"].status == "completed"
    assert called["count"] == 1


def test_get_local_manifest_and_file_happy_path(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    base = tmp_path
    (base / "t1" / "output").mkdir(parents=True)
    (base / "t1" / "manifest.json").write_text(json.dumps({"files": ["a.txt"]}), encoding="utf-8")
    (base / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(base))

    manifest_resp = task_module._get_local_manifest(tasks["t1"])
    assert manifest_resp.status_code == 200
    assert manifest_resp.headers["X-Task-ID"] == "t1"
    assert tasks["t1"].status == "completed"  # warning -> completed

    file_resp = task_module._stream_local_file(tasks["t1"], "a.txt")
    assert file_resp.status_code == 200


def test_get_local_manifest_missing_file_404(monkeypatch, task_module, clean_state, tmp_path: Path):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_manifest(tasks["t1"])
    assert exc.value.status_code == 404


def test_get_local_manifest_resolve_exception_500(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    task_dir = tmp_path / "t1"
    task_dir.mkdir()
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_manifest(tasks["t1"])
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_manifest_invalid_json(monkeypatch, task_module, clean_state, tmp_path: Path):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    with pytest.raises(Exception):
        task_module._get_local_manifest(tasks["t1"])


def test_stream_local_file_missing(monkeypatch, task_module, clean_state, tmp_path: Path):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    with pytest.raises(Exception):
        task_module._stream_local_file(tasks["t1"], "missing.txt")


def test_stream_local_file_rejects_path_outside_output(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._stream_local_file(tasks["t1"], "../evil")
    assert exc.value.status_code == 400


def test_stream_local_file_resolve_exception(monkeypatch, task_module, clean_state, tmp_path: Path):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    output_dir = tmp_path / "t1" / "output"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_output_dir", lambda _task_id: output_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._stream_local_file(tasks["t1"], "a.txt")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


# -----------------------------
# Runner streaming helpers
# -----------------------------


class _FakeHTTPXResponse:
    def __init__(
        self, *, status_code: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None
    ):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = body.decode("utf-8", errors="ignore")
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


class _FakeHTTPXClient:
    def __init__(self, response: _FakeHTTPXResponse | Exception):
        self._response_or_exc = response
        self.closed = False

    async def get(self, *_args, **_kwargs):
        if isinstance(self._response_or_exc, Exception):
            raise self._response_or_exc
        return self._response_or_exc

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_runner_resource_non_200_raises(task_module):
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=500)
    client = _FakeHTTPXClient(resp)

    with pytest.raises(Exception):
        await task_module._fetch_runner_resource(
            client=client,
            runner=runner,
            url="http://r1.example/x",
            timeout=httpx.Timeout(1.0),
            accept="application/json",
        )


@pytest.mark.asyncio
async def test_fetch_runner_resource_200_returns_response(task_module):
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=200)
    client = _FakeHTTPXClient(resp)

    out = await task_module._fetch_runner_resource(
        client=client,
        runner=runner,
        url="http://r1.example/x",
        timeout=httpx.Timeout(1.0),
        accept="application/json",
    )
    assert out is resp


@pytest.mark.asyncio
async def test_build_streaming_response_sets_headers_and_closes(task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    response = _FakeHTTPXResponse(headers={"content-type": "application/json"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(
        task_id="t1",
        response=response,
        client=client,
        media_type="application/json",
        filename="manifest.json",
    )

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    chunks = await _collect(sr.body_iterator)  # type: ignore[attr-defined]
    assert chunks == b"ok"
    assert tasks["t1"].status == "completed"


@pytest.mark.asyncio
async def test_build_streaming_response_uses_response_content_disposition(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    response = _FakeHTTPXResponse(headers={"content-disposition": "attachment; filename=x.bin"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(task_id="t1", response=response, client=client)
    assert sr.headers["Content-Disposition"] == "attachment; filename=x.bin"


@pytest.mark.asyncio
async def test_stream_runner_manifest_success(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b'{"task_id": "t1"}'
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/json"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def get(self, url: str, *_args, **_kwargs):
            self.last_url = url
            return await super().get(url)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert tasks["t1"].status == "completed"
    assert created["client"].closed is True
    assert created["client"].last_url and created["client"].last_url.endswith("/task/result/t1")


@pytest.mark.asyncio
async def test_stream_runner_file_success_and_encodes_path(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b"bin"
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/octet-stream"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def get(self, url: str, *_args, **_kwargs):
            self.last_url = url
            return await super().get(url)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a b/c.txt")

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert "a%20b/c.txt" in (created["client"].last_url or "")


@pytest.mark.asyncio
async def test_stream_runner_manifest_timeout(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_manifest_request_error(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_runner_manifest_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_manifest_unexpected_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def aclose(self):
            self.closed = True

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = FakeAsyncClient()
        return created["client"]

    async def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(task_module, "_fetch_runner_resource", boom)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 500
    assert created["client"].closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_request_error(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(Exception):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")


@pytest.mark.asyncio
async def test_stream_runner_file_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_timeout(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_file_unexpected_exception(monkeypatch, task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise ValueError("boom")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 500


# -----------------------------
# Result endpoints
# -----------------------------


def test_get_valid_task_rejects_missing(task_module, clean_state):
    with pytest.raises(Exception):
        task_module._get_valid_task("nope")


def test_get_valid_task_rejects_failed(task_module, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="failed")
    tasks["t1"].error = "boom"

    with pytest.raises(Exception):
        task_module._get_valid_task("t1")


def test_get_task_runner_raises_when_runner_missing(task_module, clean_state):
    tasks["t1"] = _task("t1", "missing", status="completed")
    with pytest.raises(HTTPException) as exc:
        task_module._get_task_runner(tasks["t1"])
    assert exc.value.status_code == 500


def test_get_task_result_425_when_running(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="running")

    resp = client.get("/task/result/t1")
    assert resp.status_code == 425


def test_get_task_result_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({"task_id": "t1"}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"


def test_get_task_result_file_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(
        json.dumps({"files": ["a.txt"]}), encoding="utf-8"
    )
    (tmp_path / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(tmp_path))

    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200


def test_get_task_result_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner):
        return JSONResponse({"task_id": "t1", "proxied": True})

    monkeypatch.setattr(task_module, "_stream_runner_manifest", fake_stream)
    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["proxied"] is True


def test_get_task_result_file_rejects_traversal(client, clean_state):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    # Use an encoded traversal sequence; plain "../" may be normalized away by the ASGI stack
    # and never reach the route handler.
    resp = client.get("/task/result/t1/file/%2e%2e%2fsecret")
    assert resp.status_code == 400


def test_get_task_result_file_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner, _path: str):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(task_module, "_stream_runner_file", fake_stream)
    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# -----------------------------
# Completion endpoint
# -----------------------------


def test_task_completion_404_task(client, task_module, clean_state):
    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post("/task/completion", json={"task_id": "nope", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True
    assert resp.status_code == 404


def test_task_completion_notify_ok(monkeypatch, client, task_module, clean_state):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return True, None

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": "out",
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "completed"
    assert tasks["t1"].script_output == "out"
    assert runners["r1"].availability == "available"
    assert "coro" not in scheduled  # no retries scheduled


def test_task_completion_notify_non_200_sets_warning_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return False, "nope"

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "warning"
    assert "coro" in scheduled
    # The retry coroutine is scheduled but not awaited in this unit test; close it to
    # avoid "coroutine was never awaited" warnings.
    scheduled["coro"].close()


def test_task_completion_failed_sets_error_message(client, task_module, clean_state):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running")
    tasks["t1"].notify_url = None

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "failed",
                "error_message": "boom",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "boom"


def test_task_completion_timeout_sets_error_message(client, task_module, clean_state):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running")
    tasks["t1"].notify_url = None

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "timeout",
                "error_message": "download timeout",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "timeout"
    assert tasks["t1"].error == "download timeout"


def test_task_completion_notify_exception_sets_warning_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(*_a, **_k):
        raise RuntimeError("boom")

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "warning"
    assert tasks["t1"].error and "server error" in tasks["t1"].error
    assert "coro" in scheduled
    scheduled["coro"].close()


def test_task_completion_timeout_notify_non_200_keeps_timeout_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return False, "nope"

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "timeout",
                "error_message": "download timeout",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "timeout"
    assert "download timeout" in (tasks["t1"].error or "")
    assert "Notify callback warning:" in (tasks["t1"].error or "")
    assert "nope" in (tasks["t1"].error or "")
    assert "coro" in scheduled
    scheduled["coro"].close()


def test_task_completion_403_token_mismatch(client, task_module, clean_state):
    runners["r1"] = _runner("r1", token="tok-real")
    tasks["t1"] = _task("t1", "r1", status="running")

    app.dependency_overrides[verify_token] = lambda: "tok-bad"
    try:
        resp = client.post("/task/completion", json={"task_id": "t1", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 403


def test_task_completion_404_runner_missing(client, task_module, clean_state):
    tasks["t1"] = _task("t1", "r1", status="running")

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post("/task/completion", json={"task_id": "t1", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 404


# -----------------------------
# Background execution helper
# -----------------------------


@pytest.mark.asyncio
async def test_execute_task_async_background_success_sets_runner_busy(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert runners["r1"].availability == "busy"


@pytest.mark.asyncio
async def test_execute_task_async_background_failure_marks_task_failed(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeResponse:
        status_code = 500
        text = "no"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert tasks["t1"].status == "failed"
    assert runners["r1"].availability == "available"


@pytest.mark.asyncio
async def test_execute_task_async_background_exception_marks_failed(
    monkeypatch, task_module, clean_state
):
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            raise RuntimeError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert tasks["t1"].status == "failed"
