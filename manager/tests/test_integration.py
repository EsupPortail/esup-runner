"""
Integration tests for the Manager (configuration, state management, internal logic).
All comments are in English for clarity.
"""

from datetime import datetime

from app.__version__ import __version__
from app.core.config import config
from app.core.state import runners, tasks
from app.models.models import Runner


def test_root_endpoint(client):
    """
    Test root endpoint returns API information.
    """
    response = client.get("/")
    assert response.status_code == 200, "Root endpoint should return 200."
    data = response.json()
    assert "message" in data, "Response should include message field."
    assert "version" in data, "Response should include version field."
    assert data["message"] == "Runner Manager", "Message should match current API title."
    assert data["version"] == __version__, "Version should match package version."


def test_manager_configuration():
    """
    Test manager configuration is properly loaded.
    """
    assert config.MANAGER_URL is not None, "Manager URL should be configured."
    assert len(config.AUTHORIZED_TOKENS) > 0, "Manager should have at least one authorized token."


def test_runner_state_management():
    """
    Test runner state dictionary operations.
    """
    original_runners = dict(runners)

    try:
        runners.clear()

        # Add a test runner
        test_runner = Runner(
            id="test_runner_state",
            url="http://localhost:9000",
            task_types=["test"],
            last_heartbeat=datetime.now(),
            token="test_token",
            version="1.0.0",
        )
        runners["test_runner_state"] = test_runner

        # Verify runner is in state
        assert "test_runner_state" in runners, "Runner should be in state."
        assert runners["test_runner_state"].id == "test_runner_state", "Runner ID should match."
        assert (
            runners["test_runner_state"].url == "http://localhost:9000"
        ), "Runner URL should match."
    finally:
        runners.clear()
        runners.update(original_runners)


def test_task_state_management():
    """
    Test task state dictionary operations.
    """
    original_tasks = dict(tasks)

    try:
        tasks.clear()

        # Import Task model
        from app.models.models import Task

        # Add a test task
        test_task = Task(
            task_id="test_task_state",
            etab_name="test_etab",
            app_name="test_app",
            app_version="1.0.0",
            task_type="test",
            source_url="http://example.com",
            affiliation="test",
            parameters={},
            status="pending",
            runner_id="test_runner",
            notify_url="http://example.com/notify",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        tasks["test_task_state"] = test_task

        # Verify task is in state
        assert "test_task_state" in tasks, "Task should be in state."
        assert tasks["test_task_state"].task_id == "test_task_state", "Task ID should match."
        assert tasks["test_task_state"].status == "pending", "Task status should match."
    finally:
        tasks.clear()
        tasks.update(original_tasks)


def test_runner_model_validation():
    """
    Test Runner model validation.
    """
    # Valid runner should be created successfully
    valid_runner = Runner(
        id="valid_runner",
        url="http://localhost:8081",
        task_types=["test", "video"],
        last_heartbeat=datetime.now(),
        token="valid_token",
        version="1.0.0",
    )

    assert valid_runner.id == "valid_runner", "Runner ID should be set."
    assert len(valid_runner.task_types) == 2, "Runner should have 2 task types."
    assert valid_runner.availability == "available", "Default availability should be 'available'."


def test_task_model_validation():
    """
    Test Task model validation.
    """
    from app.models.models import Task

    # Valid task should be created successfully
    valid_task = Task(
        task_id="valid_task",
        etab_name="test_etab",
        app_name="test_app",
        app_version="1.0.0",
        task_type="test",
        source_url="http://example.com",
        affiliation="test",
        parameters={"key": "value"},
        status="pending",
        runner_id="test_runner",
        notify_url="http://example.com/notify",
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )

    assert valid_task.task_id == "valid_task", "Task ID should be set."
    assert valid_task.status == "pending", "Task status should be set."
    assert valid_task.parameters == {"key": "value"}, "Task parameters should be set."


def test_authentication_tokens_configured():
    """
    Test that authentication tokens are properly configured.
    """
    tokens = config.AUTHORIZED_TOKENS

    assert isinstance(tokens, dict), "Authorized tokens should be a dictionary."
    assert len(tokens) > 0, "At least one token should be configured."

    # All tokens should be non-empty strings
    for token_name, token_value in tokens.items():
        assert isinstance(token_name, str), f"Token name {token_name} should be a string."
        assert isinstance(token_value, str), "Token value should be a string."
        assert len(token_value) > 0, f"Token {token_name} should not be empty."


def test_admin_users_configured():
    """
    Test that admin users are properly configured.
    """
    admin_users = config.ADMIN_USERS

    assert isinstance(admin_users, dict), "Admin users should be a dictionary."

    # If admin users are configured, verify they have hashed passwords
    for username, hashed_password in admin_users.items():
        assert isinstance(username, str), f"Username {username} should be a string."
        assert isinstance(hashed_password, str), "Password should be a hashed string."
        assert len(hashed_password) > 0, f"Hashed password for {username} should not be empty."


def test_manager_url_configuration():
    """
    Test manager URL configuration.
    """
    assert config.MANAGER_URL is not None, "Manager URL should be configured."
    assert isinstance(config.MANAGER_URL, str), "Manager URL should be a string."
    assert config.MANAGER_URL.startswith("http"), "Manager URL should start with http/https."


def test_password_context_configuration():
    """
    Test password hashing context configuration.
    """
    assert config.pwd_context is not None, "Password context should be configured."

    # Test password hashing
    test_password = "test_password_123"
    hashed = config.pwd_context.hash(test_password)

    assert len(hashed) > 0, "Hashed password should not be empty."
    assert config.pwd_context.verify(test_password, hashed), "Password verification should succeed."
    assert not config.pwd_context.verify(
        "wrong_password", hashed
    ), "Wrong password should not verify."
