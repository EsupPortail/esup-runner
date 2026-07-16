"""Regression tests for task-route service adapters."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.routes import task as task_routes
from app.models.models import Runner, TaskRequest


@pytest.mark.asyncio
async def test_notify_url_adapter_keeps_historical_signature(monkeypatch):
    """The route helper delegates notify URL validation to the callback service."""
    captured = {}

    async def fake_validate(context, url):
        captured.update(context=context, url=url)
        return url

    monkeypatch.setattr(task_routes.task_callback_service, "validate_notify_url", fake_validate)

    url = "https://callback.example/complete"
    assert await task_routes._validate_notify_url(url) == url
    assert captured == {"context": task_routes, "url": url}


@pytest.mark.asyncio
async def test_dispatch_adapter_keeps_restart_arguments(monkeypatch):
    """The dispatch adapter forwards IDs and timestamps used by batch restarts."""
    captured = {}

    async def fake_queue(
        context,
        task_request,
        client_token,
        *,
        preferred_task_id=None,
        created_at=None,
    ):
        captured.update(
            context=context,
            task_request=task_request,
            client_token=client_token,
            preferred_task_id=preferred_task_id,
            created_at=created_at,
        )
        return {"task_id": preferred_task_id, "status": "running"}

    monkeypatch.setattr(task_routes.task_dispatch_service, "queue_task_execution", fake_queue)
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        parameters={},
        notify_url="https://callback.example/complete",
    )

    response = await task_routes._queue_task_execution(
        request,
        "client-token",
        preferred_task_id="task-1",
        created_at="2026-01-01T00:00:00",
    )

    assert response == {"task_id": "task-1", "status": "running"}
    assert captured == {
        "context": task_routes,
        "task_request": request,
        "client_token": "client-token",
        "preferred_task_id": "task-1",
        "created_at": "2026-01-01T00:00:00",
    }


def test_result_path_adapter_keeps_historical_signature(monkeypatch):
    """The route helper delegates path validation to the result service."""
    captured = SimpleNamespace(context=None, file_path=None)

    def fake_validate(context, file_path):
        captured.context = context
        captured.file_path = file_path

    monkeypatch.setattr(task_routes.task_result_service, "validate_result_path", fake_validate)

    task_routes._validate_result_path("nested/result.json")
    assert captured.context is task_routes
    assert captured.file_path == "nested/result.json"


@pytest.mark.asyncio
async def test_dispatch_skips_runner_with_incompatible_ping_payload(monkeypatch):
    """An unavailable ping payload must not reserve or enqueue work."""

    class FakeResponse:
        def json(self):
            return {"available": False, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url):
            return FakeResponse()

    async def fake_validate(url):
        return url

    runner = Runner(
        id="runner-1",
        url="https://runner.example",
        task_types=["encoding"],
        token="runner-token",
        version="1.0",
        availability="available",
    )
    monkeypatch.setattr(task_routes, "runners", {runner.id: runner})
    monkeypatch.setattr(task_routes.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_routes, "_validate_notify_url", fake_validate)
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        parameters={},
        notify_url="https://callback.example/complete",
    )

    with pytest.raises(HTTPException) as exc_info:
        await task_routes._queue_task_execution(request, "client-token")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "No runners available"
