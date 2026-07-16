"""Tests for task querying, dispatch, and stopping."""

from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from task_routes_helpers import (
    clean_state,
    client,
)
from task_routes_helpers import fake_resolve_public_ips as _fake_resolve_public_ips
from task_routes_helpers import make_runner as _runner
from task_routes_helpers import make_task as _task
from task_routes_helpers import (
    task_module,
)

from app.core.state import runners, tasks
from app.models.models import Task, TaskRequest

__all__ = ["clean_state", "client", "task_module"]


def test_view_tasks_filters_and_renders(client, clean_state):
    """Validate View tasks filters and renders."""
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
    """Validate View tasks search task type and status counts else."""
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


def test_view_tasks_search_matches_video_identification_fields(client, clean_state):
    """Validate View tasks search matches video identification fields."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="completed")
    tasks["t1"].parameters = {
        "video_id": "vid-2026-0001",
        "video_slug": "my-course-video",
        "video_title": "My Course Video",
    }

    resp_by_id = client.get("/tasks?search=vid-2026")
    assert resp_by_id.status_code == 200
    assert "t1" in resp_by_id.text
    assert "t2" not in resp_by_id.text

    resp_by_slug = client.get("/tasks?search=my-course-video")
    assert resp_by_slug.status_code == 200
    assert "t1" in resp_by_slug.text
    assert "t2" not in resp_by_slug.text

    resp_by_title = client.get("/tasks?search=course video")
    assert resp_by_title.status_code == 200
    assert "t1" in resp_by_title.text
    assert "t2" not in resp_by_title.text


def test_get_task_details_api_404(client, clean_state):
    """Validate Get task details api 404."""
    resp = client.get("/tasks/api/nope")
    assert resp.status_code == 404


def test_get_task_details_api_ok(client, clean_state):
    """Validate Get task details api ok."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/tasks/api/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"
    assert "client_token" not in resp.json()


def test_redact_task_for_api_legacy_copy_branch(task_module):
    """Validate Redact task for api legacy copy branch."""

    class LegacyTask:
        def __init__(self):
            self.client_token = "legacy-secret"
            self.copy_deep_arg = None

        def copy(self, deep: bool = False):
            self.copy_deep_arg = deep
            copied = LegacyTask()
            copied.client_token = self.client_token
            return copied

    legacy = LegacyTask()

    redacted = task_module._redact_task_for_api(legacy)  # type: ignore[arg-type]

    assert legacy.copy_deep_arg is True
    assert redacted is not legacy
    assert redacted.client_token is None


@pytest.mark.asyncio
async def test_stop_task_proxies_to_runner_success(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint proxies to the assigned runner."""
    runners["r1"] = _runner("r1", url="http://r1.example", token="runner-token")
    tasks["t1"] = _task("t1", "r1", status="running")
    captured: dict[str, Any] = {}

    class FakeRunnerResponse:
        status_code = 202
        text = ""

        def json(self):
            return {"status": "stop_requested"}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    response = await task_module.stop_task("t1")

    assert response.status_code == 202
    assert json.loads(response.body) == {
        "task_id": "t1",
        "status": "stop_requested",
        "runner_id": "r1",
        "runner_status_code": 202,
    }
    assert captured["url"] == "http://r1.example/task/stop/t1"
    assert captured["headers"]["Authorization"] == "Bearer runner-token"
    assert tasks["t1"].status == "running"


@pytest.mark.asyncio
async def test_stop_task_rejects_non_running_task(task_module, clean_state):
    """Validate manager stop endpoint rejects non-running tasks."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 409
    assert exc.value.detail == "Task not running"


@pytest.mark.asyncio
async def test_stop_task_returns_404_when_task_missing(task_module, clean_state):
    """Validate manager stop endpoint returns 404 when task is missing."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("missing")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Task not found"


@pytest.mark.asyncio
async def test_stop_task_returns_404_when_runner_missing(task_module, clean_state):
    """Validate manager stop endpoint returns 404 when the assigned runner is missing."""
    tasks["t1"] = _task("t1", "missing-runner", status="running")

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Runner not found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_status", "runner_detail"),
    [
        (409, "no killable process"),
        (404, "task unknown on runner"),
    ],
)
async def test_stop_task_runner_404_and_409_are_forwarded(
    monkeypatch, task_module, clean_state, runner_status: int, runner_detail: str
):
    """Validate manager stop endpoint forwards runner 404/409 details."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeRunnerResponse:
        status_code = runner_status
        text = ""

        def json(self):
            return {"detail": runner_detail}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == runner_status
    assert exc.value.detail == runner_detail


@pytest.mark.asyncio
async def test_stop_task_runner_request_error_maps_to_502(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner request errors to 502."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "http://r1.example/task/stop/t1")
            raise httpx.RequestError("runner unreachable", request=request)

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 502
    assert exc.value.detail == "Error contacting runner: runner unreachable"


@pytest.mark.asyncio
async def test_stop_task_runner_error_maps_to_502(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner 5xx responses to 502."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeRunnerResponse:
        status_code = 500
        text = "boom"

        def json(self):
            raise ValueError("not json")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 502
    assert "Runner stop request failed: 500 - boom" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_stop_task_runner_timeout_maps_to_504(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner timeouts to 504."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 504
    assert exc.value.detail == "Timeout contacting runner"


# -----------------------------
# Execute async
# -----------------------------


def test_execute_task_async_rejects_on_priority_quota(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async rejects on priority quota."""
    monkeypatch.setattr(task_module.config, "PRIORITIES_ENABLED", True)
    monkeypatch.setattr(task_module.config, "PRIORITY_DOMAIN", "priority.example")
    monkeypatch.setattr(task_module.config, "MAX_OTHER_DOMAIN_TASK_PERCENT", 0)

    monkeypatch.setattr(task_module, "would_exceed_other_domain_quota", lambda **_: True)

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

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
    """Validate Execute task async no runners available."""
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
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

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


def test_execute_task_async_returns_existing_inflight_duplicate(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async reuses an equivalent in-flight task."""
    now = datetime.now().isoformat()
    tasks["existing-task"] = Task(
        task_id="existing-task",
        runner_id="r1",
        status="running",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )

    scheduled: dict[str, bool] = {"called": False}

    def fake_create_task(_coro):
        scheduled["called"] = True
        raise AssertionError("background scheduling should not happen for duplicates")

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
            "parameters": {"video_id": "vid-001"},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["task_id"] == "existing-task"
    assert resp.json()["status"] == "running"
    assert len(tasks) == 1
    assert scheduled["called"] is False


def test_find_inflight_duplicate_ignores_non_inflight_statuses(task_module):
    """Validate Find inflight duplicate ignores non inflight statuses."""
    now = datetime.now().isoformat()
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
    )

    completed = Task(
        task_id="t-completed",
        runner_id="r1",
        status="completed",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )
    pending = completed.model_copy(update={"task_id": "t-pending", "status": "pending"})

    duplicate_task_id = task_module._find_inflight_duplicate_task_id(
        request,
        {"t-completed": completed, "t-pending": pending},
    )
    assert duplicate_task_id == "t-pending"


def test_try_reserve_runner_for_dispatch_fallback_without_store_method(task_module, monkeypatch):
    """Validate Try reserve runner fallback when store has no try_reserve method."""
    fallback_runners = {"r1": _runner("r1")}
    monkeypatch.setattr(task_module, "runners", fallback_runners)

    reserved = task_module._try_reserve_runner_for_dispatch("r1")
    assert reserved is not None
    assert reserved.availability == "busy"
    assert fallback_runners["r1"].availability == "busy"
    assert task_module._try_reserve_runner_for_dispatch("r1") is None
    assert task_module._try_reserve_runner_for_dispatch("missing") is None


def test_try_reuse_inflight_duplicate_with_fresh_snapshot_disabled(task_module, monkeypatch):
    """Validate fresh-snapshot dedup returns None when disabled."""
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    monkeypatch.setattr(
        task_module,
        "get_tasks_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot should not be called")),
    )

    result = task_module._try_reuse_inflight_duplicate_with_fresh_snapshot(
        request,
        dedup_enabled=False,
        log_message="unused",
    )
    assert result is None


def test_execute_task_async_reservation_race_skips_runner(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async skips runner when reservation fails."""
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

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_try_reserve_runner_for_dispatch", lambda _runner_id: None)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

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
    assert tasks == {}


def test_execute_task_async_reuses_duplicate_after_reservation_race(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async reuses duplicate when it appears during reservation race."""
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

    def fake_try_reserve(_runner_id: str):
        if "existing-task" not in tasks:
            tasks["existing-task"] = _task("existing-task", "r1", status="running")
        return None

    def fail_if_scheduled(coro):
        coro.close()
        raise AssertionError("No new background task should be scheduled in dedup fallback")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_try_reserve_runner_for_dispatch", fake_try_reserve)
    monkeypatch.setattr(task_module.asyncio, "create_task", fail_if_scheduled)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

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
    assert resp.json()["task_id"] == "existing-task"
    assert resp.json()["status"] == "running"
    assert len(tasks) == 1


def test_execute_task_async_success_creates_task_and_schedules(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async success creates task and schedules."""
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
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

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
    """Validate Get task status 404."""
    resp = client.get("/task/status/nope")
    assert resp.status_code == 404


def test_get_task_status_ok(client, clean_state):
    """Validate Get task status ok."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/task/status/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"
    assert "client_token" not in resp.json()


def test_list_tasks_returns_dict(client, clean_state):
    """Validate List tasks returns dict."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/task/list")
    assert resp.status_code == 200
    assert "t1" in resp.json()
    assert "client_token" not in resp.json()["t1"]


# -----------------------------
# Local storage helpers
# -----------------------------


@pytest.mark.asyncio
async def test_execute_task_async_background_success_sets_runner_busy(
    monkeypatch, task_module, clean_state
):
    """Validate Execute task async background success sets runner busy."""
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
    """Validate Execute task async background failure marks task failed."""
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
    """Validate Execute task async background exception marks failed."""
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
