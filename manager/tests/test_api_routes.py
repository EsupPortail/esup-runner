"""Coverage-oriented tests for app.api.routes.api."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.__version__ import __version__, __version_info__
from app.core import state as state_module
from app.core.auth import verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task
from app.services import background_service


@pytest.fixture
def api_client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: True

    with TestClient(app) as client:
        yield client

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


def _make_runner(runner_id: str, *, last_heartbeat: datetime) -> Runner:
    return Runner(
        id=runner_id,
        url=f"http://{runner_id}.example",
        task_types=["encoding"],
        token="",
        version="1.0.0",
        last_heartbeat=last_heartbeat,
        availability="available",
        status="offline",
    )


def _make_task(task_id: str, runner_id: str, *, status: str) -> Task:
    now = datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id=runner_id,
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="4.0",
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


def test_api_version(api_client):
    resp = api_client.get("/api/version")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["version"] == __version__
    assert payload["version_info"] == {
        "major": __version_info__[0],
        "minor": __version_info__[1],
        "patch": __version_info__[2],
    }


def test_api_tasks_returns_task_status(api_client, clean_state):
    runners["r1"] = _make_runner("r1", last_heartbeat=datetime.now())
    tasks["t1"] = _make_task("t1", "r1", status="running")
    tasks["t2"] = _make_task("t2", "r1", status="completed")

    resp = api_client.get("/api/tasks")
    assert resp.status_code == 200

    payload = resp.json()
    assert "tasks" in payload

    # Order is dict iteration order; compare as sets of tuples
    got = {(t["id"], t["runner_id"], t["status"]) for t in payload["tasks"]}
    assert got == {("t1", "r1", "running"), ("t2", "r1", "completed")}


def test_api_runners_includes_online_and_offline(api_client, clean_state):
    now = datetime.now()
    runners["online"] = _make_runner("online", last_heartbeat=now - timedelta(seconds=5))
    runners["offline"] = _make_runner("offline", last_heartbeat=now - timedelta(seconds=120))

    resp = api_client.get("/api/runners")
    assert resp.status_code == 200

    payload = resp.json()
    got = {r["id"]: r for r in payload["runners"]}

    assert got["online"]["status"] == "online"
    assert got["offline"]["status"] == "offline"

    assert isinstance(got["online"]["last_heartbeat"], str)
    assert isinstance(got["online"]["age_seconds"], int)
    assert got["offline"]["age_seconds"] >= 60
