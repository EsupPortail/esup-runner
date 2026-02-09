"""
Integration tests for the Runner (configuration, state management, internal logic).
Network interactions and background services are stubbed for deterministic runs.
"""

import pytest
from fastapi.testclient import TestClient

import app.services.manager_service as manager_service
from app.core.config import config
from app.core.state import (
    get_runner_id,
    get_runner_instance_id,
    get_runner_instance_url,
    get_runner_state,
    is_available,
    is_registered,
    set_available,
    set_registered,
)
from app.main import app, background_manager


@pytest.fixture(autouse=True)
def stub_manager_calls(monkeypatch):
    """Stub network calls and background services during integration tests."""

    async def _fake_register():
        set_registered(True)
        return True

    async def _noop():
        return None

    monkeypatch.setattr(manager_service, "register_with_manager", _fake_register)

    # Ensure the FastAPI lifespan uses the stubbed registration
    import app.main as main

    monkeypatch.setattr(main, "register_with_manager", _fake_register)
    monkeypatch.setattr(background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_manager, "stop_all_services", _noop)


def test_root_endpoint():
    """
    Test root endpoint returns API information.
    """
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200, "Root endpoint should return 200."
    data = response.json()
    assert "message" in data, "Response should include message field."
    assert "version" in data, "Response should include version field."
    assert data["message"] == "Runner API", "Message should be 'Runner API'."


def test_runner_configuration():
    """
    Test runner configuration is properly loaded.
    """
    assert config.RUNNER_HOST is not None, "Runner host should be configured."
    assert config.RUNNER_BASE_NAME is not None, "Runner base name should be configured."
    assert config.MANAGER_URL is not None, "Manager URL should be configured."
    assert len(config.RUNNER_TASK_TYPES) > 0, "Runner should have at least one task type."


def test_runner_state_management():
    """
    Test runner state management functions.
    """
    # Test initial state
    runner_id = get_runner_id()
    assert runner_id is not None, "Runner ID should be set."
    assert isinstance(runner_id, str), "Runner ID should be a string."

    runner_instance_id = get_runner_instance_id()
    assert isinstance(runner_instance_id, int), "Runner instance ID should be an integer."

    runner_url = get_runner_instance_url()
    assert runner_url is not None, "Runner URL should be set."
    assert isinstance(runner_url, str), "Runner URL should be a string."


def test_runner_registration_state():
    """
    Test runner registration state management.
    """
    # Test setting registered state
    set_registered(True)
    assert is_registered() is True, "Runner should be registered after setting True."

    set_registered(False)
    assert is_registered() is False, "Runner should be unregistered after setting False."

    # Reset to default for other tests
    set_registered(False)


def test_runner_availability_state():
    """
    Test runner availability state management.
    """
    # Test setting available state
    set_available(True)
    assert is_available() is True, "Runner should be available after setting True."

    set_available(False)
    assert is_available() is False, "Runner should be unavailable after setting False."

    # Reset to default for other tests
    set_available(True)


def test_runner_state_snapshot():
    """
    Test getting complete runner state snapshot.
    """
    state = get_runner_state()

    assert isinstance(state, dict), "Runner state should be a dictionary."
    assert "runner_id" in state, "State should include runner_id."
    assert "runner_instance_id" in state, "State should include runner_instance_id."
    assert "runner_instance_url" in state, "State should include runner_instance_url."
    assert "is_registered" in state, "State should include is_registered."
    assert "is_available" in state, "State should include is_available."
    assert "startup_time" in state, "State should include startup_time."


def test_storage_configuration():
    """
    Test storage configuration for task results.
    """
    assert config.STORAGE_DIR is not None, "Storage directory should be configured."
    assert isinstance(config.STORAGE_DIR, str), "Storage directory should be a string."


def test_manager_communication_config():
    """
    Test manager communication configuration.
    """
    assert config.MANAGER_URL is not None, "Manager URL should be configured."
    assert isinstance(config.MANAGER_URL, str), "Manager URL should be a string."


def test_task_types_configuration():
    """
    Test task types configuration.
    """
    task_types = config.RUNNER_TASK_TYPES
    assert isinstance(task_types, set), "Task types should be a set."
    assert len(task_types) > 0, "Runner should support at least one task type."

    # All task types should be strings
    for task_type in task_types:
        assert isinstance(task_type, str), f"Task type {task_type} should be a string."
        assert len(task_type) > 0, f"Task type {task_type} should not be empty."
