"""Tests for bulk task actions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from task_routes_helpers import (
    clean_state,
    client,
)
from task_routes_helpers import make_runner as _runner
from task_routes_helpers import make_task as _task
from task_routes_helpers import (
    task_module,
)

from app.core.state import runners, tasks
from app.models.models import Runner

__all__ = ["clean_state", "client", "task_module"]


def test_normalize_task_ids_filters_invalid_empty_and_duplicates(task_module):
    """Validate Normalize task ids filters invalid empty and duplicates."""
    normalized = task_module._normalize_task_ids(["  ", "t1", "t1", " t2 ", 123, "t2"])
    assert normalized == ["t1", "t2"]


def test_http_exception_detail_to_text_variants(task_module):
    """Validate Http exception detail to text variants."""
    assert task_module._http_exception_detail_to_text("simple") == "simple"
    assert task_module._http_exception_detail_to_text({"detail": "nested"}) == "nested"
    assert task_module._http_exception_detail_to_text({"detail": {"code": 123}}) == "{'code': 123}"
    assert task_module._http_exception_detail_to_text(42) == "42"


def test_delete_selected_tasks_deletes_and_reports(monkeypatch, client, task_module, clean_state):
    """Validate Delete selected tasks deletes and reports."""
    runners["r1"] = _runner("r1")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-failed"] = _task("t-failed", "r1", status="failed")
    tasks["t-running"] = _task("t-running", "r1", status="running")

    deleted_calls: list[str] = []

    def fake_delete(task_id: str) -> bool:
        deleted_calls.append(task_id)
        if task_id == "t-failed":
            return False
        tasks.pop(task_id, None)
        return True

    monkeypatch.setattr(task_module, "delete_task_from_state", fake_delete)

    resp = client.post(
        "/tasks/delete-selected",
        json={"task_ids": ["t-completed", "t-running", "missing", "t-failed"]},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["requested"] == 4
    assert payload["deleted"] == [{"task_id": "t-completed"}]
    assert payload["failed"] == [{"task_id": "t-failed", "reason": "Task deletion failed"}]

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert skipped_by_task_id["missing"] == "Task not found"
    assert "cannot be deleted" in skipped_by_task_id["t-running"]
    assert deleted_calls == ["t-completed", "t-failed"]
    assert "t-completed" not in tasks


def test_delete_selected_tasks_rejects_empty_task_ids(client, clean_state):
    """Validate Delete selected tasks rejects empty task ids."""
    resp = client.post("/tasks/delete-selected", json={"task_ids": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_delete_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate Delete selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.delete_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


def test_delete_selected_tasks_rejects_too_many_task_ids(
    monkeypatch, client, task_module, clean_state
):
    """Validate Delete selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_DELETE_TASKS", 2)
    resp = client.post("/tasks/delete-selected", json={"task_ids": ["a", "b", "c"]})
    assert resp.status_code == 400
    assert "Too many task IDs" in resp.json()["detail"]


def test_delete_selected_tasks_collects_unexpected_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Delete selected tasks collects unexpected failures."""
    tasks["t1"] = _task("t1", "r1", status="completed")

    def fake_delete(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "delete_task_from_state", fake_delete)

    resp = client.post("/tasks/delete-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["deleted"] == []
    assert payload["failed"] == [
        {"task_id": "t1", "reason": "Unexpected error while deleting task"}
    ]


def test_restart_selected_tasks_restarts_and_reports(monkeypatch, client, task_module, clean_state):
    """Validate Restart selected tasks restarts and reports."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t-failed"] = _task("t-failed", "r1", status="failed")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-running"] = _task("t-running", "r1", status="running")
    tasks["t-failed"].client_token = "client-123"

    class FakePingResponse:
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
            return FakePingResponse()

    async def _fake_resolve(_host: str):
        return ["93.184.216.34"]

    scheduled = {"count": 0}

    def fake_create_task(coro):
        scheduled["count"] += 1
        # Avoid un-awaited coroutine warnings; we only need to assert scheduling.
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve)
    monkeypatch.setattr(task_module.config, "PRIORITIES_ENABLED", False)

    resp = client.post(
        "/tasks/restart-selected",
        json={"task_ids": ["t-failed", "t-running", "missing", "t-completed"]},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["requested"] == 4
    assert len(payload["restarted"]) == 2
    assert len(payload["skipped"]) == 2
    assert payload["failed"] == []
    assert scheduled["count"] == 2

    restarted_ids = {item["task_id"] for item in payload["restarted"]}
    assert restarted_ids == {"t-failed", "t-completed"}
    assert tasks["t-failed"].client_token == "client-123"
    assert tasks["t-failed"].status == "running"
    assert tasks["t-completed"].status == "running"
    assert tasks["t-failed"].run_id is not None
    assert tasks["t-completed"].run_id is not None

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert skipped_by_task_id["missing"] == "Task not found"
    assert "cannot be restarted" in skipped_by_task_id["t-running"]


def test_restart_selected_tasks_rejects_empty_task_ids(client, clean_state):
    """Validate Restart selected tasks rejects empty task ids."""
    resp = client.post("/tasks/restart-selected", json={"task_ids": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_restart_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate Restart selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.restart_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


def test_restart_selected_tasks_rejects_too_many_task_ids(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_RESTART_TASKS", 2)
    resp = client.post("/tasks/restart-selected", json={"task_ids": ["a", "b", "c"]})
    assert resp.status_code == 400
    assert "Too many task IDs" in resp.json()["detail"]


def test_restart_selected_tasks_collects_http_exception_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks collects http exception failures."""
    tasks["t1"] = _task("t1", "r1", status="failed")

    async def fake_queue(*_a, **_k):
        raise HTTPException(status_code=503, detail={"detail": "no runner"})

    monkeypatch.setattr(task_module, "_queue_task_execution", fake_queue)

    resp = client.post("/tasks/restart-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restarted"] == []
    assert payload["failed"] == [{"task_id": "t1", "reason": "no runner"}]


def test_restart_selected_tasks_collects_unexpected_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks collects unexpected failures."""
    tasks["t1"] = _task("t1", "r1", status="failed")

    async def fake_queue(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "_queue_task_execution", fake_queue)

    resp = client.post("/tasks/restart-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restarted"] == []
    assert payload["failed"] == [
        {"task_id": "t1", "reason": "Unexpected error while restarting task"}
    ]


@pytest.mark.asyncio
async def test_stop_selected_tasks_stops_and_reports(monkeypatch, task_module, clean_state):
    """Validate stop selected tasks proxies running tasks and reports all outcomes."""
    runners["r1"] = _runner("r1")
    tasks["t-running"] = _task("t-running", "r1", status="running")
    tasks["t-fail"] = _task("t-fail", "r1", status="running")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-no-runner"] = _task("t-no-runner", "missing-runner", status="running")

    async def fake_stop(task_id: str, runner: Runner):
        if task_id == "t-fail":
            raise HTTPException(status_code=409, detail="no killable process")
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(task_module, "_request_runner_task_stop", fake_stop)

    payload = await task_module.stop_selected_tasks(
        {
            "task_ids": [
                "t-running",
                "t-completed",
                "missing",
                "t-no-runner",
                "t-fail",
            ]
        }
    )

    assert payload["requested"] == 5
    assert payload["stopped"] == [
        {"task_id": "t-running", "runner_id": "r1", "runner_status_code": 202}
    ]
    assert tasks["t-running"].status == "running"

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert "cannot be stopped" in skipped_by_task_id["t-completed"]
    assert skipped_by_task_id["missing"] == "Task not found"

    failed_by_task_id = {item["task_id"]: item["reason"] for item in payload["failed"]}
    assert failed_by_task_id["t-no-runner"] == "Runner not found"
    assert failed_by_task_id["t-fail"] == "no killable process"


@pytest.mark.asyncio
async def test_stop_selected_tasks_collects_unexpected_failures(
    monkeypatch, task_module, clean_state
):
    """Validate stop selected tasks collects unexpected failures."""
    runners["r1"] = _runner("r1")
    tasks["t-bug"] = _task("t-bug", "r1", status="running")

    async def fake_stop(_task_id: str, _runner: Runner):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "_request_runner_task_stop", fake_stop)

    payload = await task_module.stop_selected_tasks({"task_ids": ["t-bug"]})

    assert payload["requested"] == 1
    assert payload["stopped"] == []
    assert payload["skipped"] == []
    assert payload["failed"] == [
        {"task_id": "t-bug", "reason": "Unexpected error while stopping task"}
    ]


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_empty_task_ids(task_module):
    """Validate stop selected tasks rejects empty task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": []})
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate stop selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_too_many_task_ids(monkeypatch, task_module):
    """Validate stop selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_STOP_TASKS", 2)
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": ["a", "b", "c"]})
    assert exc.value.status_code == 400
    assert "Too many task IDs" in str(exc.value.detail)
