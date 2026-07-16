"""Shared fixtures and factories for task route tests."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_admin, verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task
from app.services import background_service


@pytest.fixture
def task_module():
    """Expose the task routes module for monkeypatching."""
    from app.api.routes import task as task_module  # type: ignore

    return task_module


@pytest.fixture
def client(monkeypatch, task_module):
    """Build an authenticated client without background services."""

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: "test-token"
    app.dependency_overrides[verify_admin] = lambda: True
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(verify_token, None)
    app.dependency_overrides.pop(verify_admin, None)


@pytest.fixture
def clean_state():
    """Isolate mutable runner and task state for each test."""
    original_runners = dict(runners)
    original_tasks = dict(tasks)

    runners.clear()
    tasks.clear()

    yield

    runners.clear()
    runners.update(original_runners)
    tasks.clear()
    tasks.update(original_tasks)


def make_runner(
    runner_id: str,
    *,
    url: str = "http://r1.example",
    token: str = "tok",
) -> Runner:
    """Build an available runner with stable test values."""
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


def make_task(
    task_id: str,
    runner_id: str,
    *,
    status: str,
    notify_url: str | None = None,
) -> Task:
    """Build a task with stable test values."""
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


async def fake_resolve_public_ips(_host: str) -> list[str]:
    """Return a deterministic public address for dispatch tests."""
    return ["93.184.216.34"]
