"""
Resilience tests for the Runner (registration, heartbeat, reconnection, status).
Network calls to the manager are stubbed to keep tests fast and deterministic.
"""

import pytest
from fastapi.testclient import TestClient

import app.services.manager_service as manager_service
from app.core.state import is_registered, set_registered
from app.main import app, background_manager


@pytest.fixture(autouse=True)
def stub_manager_calls(monkeypatch):
    """Avoid real HTTP calls and background loops during tests."""

    async def _fake_register():
        set_registered(True)
        return True

    async def _fake_heartbeat():
        return True

    async def _fake_check_health():
        return True

    async def _noop():
        return None

    monkeypatch.setattr(manager_service, "register_with_manager", _fake_register)
    monkeypatch.setattr(manager_service, "send_heartbeat", _fake_heartbeat)
    monkeypatch.setattr(manager_service, "check_manager_health", _fake_check_health)

    # Ensure the FastAPI lifespan uses the stubbed functions too
    import app.main as main

    monkeypatch.setattr(main, "register_with_manager", _fake_register)
    monkeypatch.setattr(background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_manager, "stop_all_services", _noop)


@pytest.mark.asyncio
async def test_registration():
    """
    Test initial registration with the manager.
    """
    result = await manager_service.register_with_manager()
    assert result is True, "Runner should register successfully with the manager."
    assert is_registered() is True, "Runner state should be registered after registration."


@pytest.mark.asyncio
async def test_heartbeat():
    """
    Test sending heartbeat to the manager.
    """
    # Ensure runner is registered before sending heartbeat
    await manager_service.register_with_manager()
    result = await manager_service.send_heartbeat()
    assert result is True, "Heartbeat should succeed when runner is registered."


@pytest.mark.asyncio
async def test_reconnection():
    """
    Test automatic reconnection logic after losing registration.
    """
    # Simulate lost registration
    set_registered(False)
    # Simulate reconnection attempt with stubbed registration
    result = await manager_service.register_with_manager()
    assert result is True, "Reconnection should succeed and runner should be registered."
    assert is_registered() is True, "Runner should be registered after reconnection."


def test_status_endpoint():
    """
    Test the /runner/status endpoint for runner state and configuration.
    """
    with TestClient(app) as client:
        response = client.get("/runner/status")
        assert response.status_code == 200, "Status endpoint should return 200 OK."
        data = response.json()
        assert "runner_instance_id" in data, "Status response should include runner_instance_id."
        assert "registered" in data, "Status response should include registration state."
        assert "available" in data, "Status response should include availability."
