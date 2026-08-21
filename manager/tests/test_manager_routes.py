"""Coverage-oriented tests for app.api.routes.manager."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.manager import router as manager_router
from app.core import state as state_module
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
def clean_state(monkeypatch):
    original_runners = dict(runners)
    original_tasks = dict(tasks)
    original_is_production = state_module.IS_PRODUCTION

    monkeypatch.setattr(state_module, "IS_PRODUCTION", False)
    runners.clear()
    tasks.clear()

    yield

    monkeypatch.setattr(state_module, "IS_PRODUCTION", original_is_production)
    runners.clear()
    runners.update(original_runners)
    tasks.clear()
    tasks.update(original_tasks)


def test_manager_health_includes_counts(client, clean_state):
    """Validate Manager health includes counts."""
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

    resp = client.get("/api/health")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["status"] == "healthy"
    assert payload["runners"] == 1
    assert payload["tasks"] == 1
    assert isinstance(payload["timestamp"], str)


def test_manager_health_openapi_path_with_root_path():
    """Keep the API path distinct from the public Manager prefix."""
    test_app = FastAPI(root_path="/manager")
    test_app.include_router(manager_router)

    paths = test_app.openapi()["paths"]
    assert "/api/health" in paths
    assert f"{test_app.root_path}/api/health" == "/manager/api/health"
