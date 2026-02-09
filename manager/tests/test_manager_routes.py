"""Coverage-oriented tests for app.api.routes.manager."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task
from app.services import background_service


@pytest.fixture
def client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: True

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(verify_token, None)


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


def test_manager_health_includes_counts(client, clean_state):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

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

    resp = client.get("/manager/health")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["status"] == "healthy"
    assert payload["runners"] == 1
    assert payload["tasks"] == 1
    assert isinstance(payload["timestamp"], str)
