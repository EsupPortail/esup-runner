"""Ensure completion callbacks never send credentials to request-selected destinations."""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.routes import task
from app.core import state
from app.models.models import TaskRequest
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
@pytest.mark.parametrize(
    "manager_base", ["https://manager.example", "https://manager.example/private/"]
)
@pytest.mark.parametrize("base_slash", ["/", "//"])
@pytest.mark.parametrize("endpoint_slash", ["", "/", "//"])
async def test_completion_slashes_use_canonical_configured_destination(
    manager_endpoint, monkeypatch, manager_base, base_slash, endpoint_slash
):
    """Accept redundant boundary/trailing slashes without requesting a redirect."""
    monkeypatch.setattr(task.config, "MANAGER_URL", manager_base)
    callback = f"{manager_base.rstrip('/')}{base_slash}task/completion{endpoint_slash}"
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

    assert await task.notify_completion(callback, "task-slashes", "completed")
    assert len(requests) == 1
    assert str(requests[0].url) == f"{manager_base.rstrip('/')}/task/completion"
    assert requests[0].headers["Authorization"] == "Bearer test-runner-token"
    assert requests[0].extensions["timeout"]["read"] == 30.0


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_address", ["private", "public", "public_only"])
async def test_run_task_accepts_private_or_public_manager_and_persists_canonical_callback(
    manager_endpoint, monkeypatch, tmp_path, matching_address
):
    """Accept either advertised Manager address and retain only the trusted destination."""
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "statuses.json"))
    monkeypatch.setitem(state._RUNNER_STATE, "task_statuses", {})
    monkeypatch.setattr(task.storage_manager, "base_path", str(tmp_path))
    monkeypatch.setattr(task, "is_registered", lambda: True)
    monkeypatch.setattr(task, "is_available", lambda: True)
    monkeypatch.setattr(task, "set_available", Mock())
    dispatch = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(task.task_dispatcher, "dispatch_task", dispatch)
    private_callback = "http://manager.internal:8081/task/completion"
    public_callback = "https://manager.example/private//task/completion/"
    if matching_address == "private":
        private_callback, public_callback = public_callback, private_callback
    elif matching_address == "public_only":
        private_callback = None
    request = TaskRequest(
        task_id="task-advertised",
        etab_name="UM",
        app_name="test",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        notify_url="",
        completion_callback=private_callback,
        completion_callback_public=public_callback,
    )
    background = BackgroundTasks()

    result = await task.run_task(request, background, current_manager="manager-token")

    assert result["status"] == "started"
    assert len(background.tasks) == 1
    dispatch.assert_not_awaited()
    assert request.completion_callback == manager_endpoint
    payload = state.get_task_status(request.task_id)
    assert payload["completion_callback"] == manager_endpoint
    assert payload["task_request"]["completion_callback"] == manager_endpoint
    state.reload_task_statuses_from_disk()
    payload = state.get_task_status(request.task_id)
    assert payload["completion_callback"] == manager_endpoint
    requests = []

    def respond(outgoing_request):
        requests.append(outgoing_request)
        return httpx.Response(204)

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        task.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    await task_recovery.finalize_recovered_task(
        request.task_id, payload, status="completed", runtime=task
    )
    assert state.get_task_status(request.task_id)["status"] == "completed"
    assert len(requests) == 1
    assert str(requests[0].url) == manager_endpoint
    assert requests[0].headers["Authorization"] == "Bearer test-runner-token"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback",
    [
        "http://manager.internal:8081/task/completion",
        "https://user:url-password@other.example/task/completion?token=query-secret#fragment-secret",
        "https://manager.example:invalid/task/completion",
    ],
)
async def test_run_task_rejects_callback_before_execution_with_clear_redacted_error(
    manager_endpoint, monkeypatch, callback, caplog
):
    """Expose configuration mismatches before any task, workspace or process is created."""
    set_available = Mock()
    set_status = Mock()
    resolve_workspace = Mock()
    monkeypatch.setattr(task, "set_available", set_available)
    monkeypatch.setattr(task, "set_task_status", set_status)
    monkeypatch.setattr(task.task_results, "resolve_task_workspace", resolve_workspace)
    request = TaskRequest(
        task_id="task-rejected",
        etab_name="UM",
        app_name="test",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        notify_url="",
        completion_callback=callback,
        completion_callback_public="https://other-manager.example/task/completion",
    )
    background = BackgroundTasks()

    with pytest.raises(HTTPException) as error:
        await task.run_task(request, background, current_manager="manager-token")

    assert error.value.status_code == 422
    detail = error.value.detail
    assert "Completion callback rejected" in detail
    assert "Runner expects" in detail
    assert "Manager private callback" in detail
    assert "Manager public callback" in detail
    assert "MANAGER_PROTOCOL, MANAGER_HOST and MANAGER_PORT" in detail
    assert "MANAGER_PUBLIC_URL" in detail
    assert "manager.example" in detail
    assert "task-rejected" in caplog.text
    for secret in ("test-runner-token", "url-password", "query-secret", "fragment-secret"):
        assert secret not in detail
        assert secret not in caplog.text
    set_available.assert_not_called()
    set_status.assert_not_called()
    resolve_workspace.assert_not_called()
    assert background.tasks == []


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
