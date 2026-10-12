"""Ensure completion callbacks never send credentials to request-selected destinations."""

import json
from unittest.mock import Mock

import httpx
import pytest

from app.api.routes import task
from app.core import state
from app.services import task_recovery


@pytest.fixture
def manager_endpoint(monkeypatch):
    monkeypatch.setattr(task.config, "MANAGER_URL", "https://manager.example/private/")
    monkeypatch.setattr(task.config, "RUNNER_TOKEN", "test-runner-token")
    monkeypatch.setattr(task.config, "COMPLETION_NOTIFY_MAX_RETRIES", 0)
    return "https://manager.example/private/task/completion"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback",
    [
        "https://attacker.example/task/completion",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "https://manager.example.attacker.example/private/task/completion",
        "https://manager.example@attacker.example/private/task/completion",
        "https://attacker.example/#https://manager.example/private/task/completion",
        "http://manager.example/private/task/completion",
        "https://manager.example:8443/private/task/completion",
        "https://manager.example/private/task/completion?target=attacker",
        "https://manager.example/private/task/completion#fragment",
        "https://manager.example/private/task/completion/",
        "https://manager.example/private/admin",
        "file:///etc/passwd",
        "",
    ],
)
async def test_untrusted_callback_never_opens_client(
    manager_endpoint, monkeypatch, callback, caplog
):
    client = Mock(side_effect=AssertionError("Untrusted callbacks must not open a client"))
    monkeypatch.setattr(task.httpx, "AsyncClient", client)

    assert await task.notify_completion(callback, "task-security", "completed") is False

    client.assert_not_called()
    assert "Rejected completion callback" in caplog.text
    assert "test-runner-token" not in caplog.text
    if callback:
        assert callback not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manager_url",
    [
        "http://127.0.0.1:8081",
        "http://manager.internal:8081/",
        "https://manager.example/prefix/",
    ],
)
async def test_configured_manager_receives_token_and_payload(
    manager_endpoint, monkeypatch, manager_url
):
    monkeypatch.setattr(task.config, "MANAGER_URL", manager_url)
    callback = f"{manager_url.rstrip('/')}/task/completion"
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
    monkeypatch.setattr(task.httpx, "AsyncClient", lambda **kwargs: client)

    assert await task.notify_completion(callback, "task-security", "failed", "failure", "output")

    assert len(requests) == 1
    assert str(requests[0].url) == callback
    assert requests[0].headers["Authorization"] == "Bearer test-runner-token"
    assert json.loads(requests[0].content) == {
        "task_id": "task-security",
        "status": "failed",
        "error_message": "failure",
        "script_output": "output",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_manager_redirect_never_reaches_another_endpoint(
    manager_endpoint, monkeypatch, status
):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "http://169.254.169.254/private"})

    client_class = httpx.AsyncClient

    def make_client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return client_class(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(task.httpx, "AsyncClient", make_client)

    assert await task.notify_completion(manager_endpoint, "task-security", "completed") is False
    assert len(requests) == 1
    assert str(requests[0].url) == manager_endpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [False, True])
async def test_recovered_completion_checks_persisted_callback(
    manager_endpoint, monkeypatch, tmp_path, trusted
):
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "task-status.json"))
    monkeypatch.setitem(state._RUNNER_STATE, "task_statuses", {})
    callback = manager_endpoint if trusted else "https://attacker.example/steal-token"
    state.set_task_status("task-recovered", "running")
    state.set_task_metadata("task-recovered", completion_callback=callback)
    payload = state.get_task_status("task-recovered")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(204)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        task.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    await task_recovery.finalize_recovered_task(
        "task-recovered", payload, status="completed", runtime=task
    )

    assert state.get_task_status("task-recovered")["status"] == "completed"
    assert len(requests) == int(trusted)
    if trusted:
        assert str(requests[0].url) == manager_endpoint
