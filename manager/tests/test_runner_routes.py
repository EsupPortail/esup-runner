"""Coverage-oriented tests for app.api.routes.runner."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_runner_version, verify_token
from app.core.state import runners
from app.main import app
from app.models.models import Runner
from app.services import background_service


@pytest.fixture
def runner_client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: "tok-ok"
    app.dependency_overrides[verify_runner_version] = lambda: "1.0.0"

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.pop(verify_token, None)
    app.dependency_overrides.pop(verify_runner_version, None)


@pytest.fixture
def clean_runners_state():
    original = dict(runners)
    runners.clear()
    yield
    runners.clear()
    runners.update(original)


@pytest.fixture
def runner_module():
    from app.api.routes import runner as runner_module  # type: ignore

    return runner_module


def test_verify_runner_token_false_when_missing(clean_runners_state, runner_module):
    assert runner_module.verify_runner_token("nope", "tok") is False


def test_verify_runner_token_true_when_matches(clean_runners_state, runner_module):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok1",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    assert runner_module.verify_runner_token("r1", "tok1") is True


def test_verify_runner_token_false_when_mismatch(clean_runners_state, runner_module):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok1",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    assert runner_module.verify_runner_token("r1", "wrong") is False


def test_register_runner_sets_token_version_and_heartbeat(runner_client, clean_runners_state):
    payload = {
        "id": "r1",
        "url": "http://r1.example",
        "task_types": ["encoding"],
        "status": "offline",
        "availability": "available",
        "token": "ignored",
        "version": "ignored",
        "last_heartbeat": datetime.now().isoformat(),
    }

    resp = runner_client.post("/runner/register", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "registered"}

    assert runners["r1"].token == "tok-ok"
    assert runners["r1"].version == "1.0.0"


def test_runner_heartbeat_ok_after_register(runner_client, clean_runners_state):
    payload = {
        "id": "r1",
        "url": "http://r1.example",
        "task_types": ["encoding"],
        "status": "offline",
        "availability": "available",
        "token": "ignored",
        "version": "ignored",
        "last_heartbeat": datetime.now().isoformat(),
    }
    runner_client.post("/runner/register", json=payload)

    before = runners["r1"].last_heartbeat
    resp = runner_client.post("/runner/heartbeat/r1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert runners["r1"].last_heartbeat >= before


def test_runner_heartbeat_404_when_runner_missing(runner_client, clean_runners_state):
    resp = runner_client.post("/runner/heartbeat/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Runner not found"


def test_runner_heartbeat_403_when_token_mismatch(runner_client, clean_runners_state):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok-ok",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    # Override token for this request
    app.dependency_overrides[verify_token] = lambda: "tok-bad"
    try:
        resp = runner_client.post("/runner/heartbeat/r1")
    finally:
        app.dependency_overrides[verify_token] = lambda: "tok-ok"

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not authorized for this runner"
