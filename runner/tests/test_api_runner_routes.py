import pytest
from fastapi.testclient import TestClient

import app.services.manager_service as manager_service
from app.main import app, background_manager


@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch):
    async def _fake_register():
        return True

    async def _noop():
        return None

    monkeypatch.setattr(manager_service, "register_with_manager", _fake_register)
    import app.main as main

    monkeypatch.setattr(main, "register_with_manager", _fake_register)
    monkeypatch.setattr(background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_manager, "stop_all_services", _noop)


def test_runner_health_and_ping(monkeypatch):
    with TestClient(app) as client:
        health = client.get("/runner/health")
        assert health.status_code == 200
        body = health.json()
        assert body["status"] == "healthy"

        ping = client.get("/runner/ping")
        assert ping.status_code == 200
        ping_body = ping.json()
        assert "task_types" in ping_body


def test_runner_status_uses_storage_stats(monkeypatch):
    fake_stats = {"total_size": 10, "file_count": 1, "available_space": 100}

    from app.api.routes import runner as runner_module

    monkeypatch.setattr(runner_module.storage_manager, "get_usage_stats", lambda: fake_stats)

    with TestClient(app) as client:
        resp = client.get("/runner/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["storage_stats"] == fake_stats
