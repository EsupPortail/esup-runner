"""Validate task failure email metadata and public Manager links."""

from html import escape
from unittest.mock import AsyncMock

import pytest

from app.api.routes import task as task_routes
from app.core import state
from app.models.models import TaskRequest
from app.services import email_service


@pytest.fixture
def email_config(monkeypatch):
    """Configure notifications without contacting a real SMTP server."""
    monkeypatch.setattr(email_service.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service.config, "SMTP_SENDER", "runner@example.org")
    monkeypatch.setattr(email_service.config, "MANAGER_EMAIL", "admins@example.org")
    monkeypatch.setattr(email_service.config, "MANAGER_URL", "http://manager.internal:8081")
    monkeypatch.setattr(email_service.config, "MANAGER_PUBLIC_URL", "")
    monkeypatch.setattr(email_service, "get_runner_id", lambda: "runner-1")
    monkeypatch.setattr(email_service, "get_runner_instance_url", lambda: "http://runner:8082")


@pytest.mark.parametrize("video_id", [None, "", "  ", "  video-123  ", 123, 0, '<video&"123>'])
def test_failure_email_includes_only_defined_video_id(email_config, video_id):
    message = email_service._compose_failure_email(
        task_id="task-1",
        task_type="encoding",
        status="failed",
        error_message="Encoding failed",
        script_output=None,
        video_id=video_id,
    )
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    expected_id = str(video_id).strip() if video_id is not None else ""
    if expected_id:
        assert f"Video ID: {expected_id}\n" in plain
        assert "Video ID" in html
        assert escape(expected_id) in html
        if expected_id.startswith("<"):
            assert expected_id not in html
    else:
        assert "Video ID" not in plain
        assert "Video ID" not in html


@pytest.mark.parametrize(
    "public_url,api_url,expected_admin_url",
    [
        ("", "http://127.0.0.1:8081/", "http://127.0.0.1:8081/admin"),
        ("", "http://manager.internal/admin/", "http://manager.internal/admin"),
        ("http://127.0.0.1:8081", "http://private:8081", "http://127.0.0.1:8081/admin"),
        (
            "https://admin.example.org/runner-manager/",
            "http://private:8081",
            "https://admin.example.org/runner-manager/admin",
        ),
        (
            "https://admin.example.org/runner-manager/admin/",
            "http://private:8081",
            "https://admin.example.org/runner-manager/admin",
        ),
    ],
)
def test_failure_email_links_to_public_manager(
    email_config, monkeypatch, public_url, api_url, expected_admin_url
):
    monkeypatch.setattr(email_service.config, "MANAGER_PUBLIC_URL", public_url)
    monkeypatch.setattr(email_service.config, "MANAGER_URL", api_url)
    message = email_service._compose_failure_email(
        task_id="task-1",
        task_type="encoding",
        status="timeout",
        error_message="Script timeout after 18000 seconds",
        script_output=None,
    )
    assert f"Manager admin: {expected_admin_url}\n" in message.get_body(("plain",)).get_content()
    assert f'href="{expected_admin_url}"' in message.get_body(("html",)).get_content()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_type,error_message,raises_exception,expected_status",
    [
        ("encoding", "Encoding failed", False, "failed"),
        ("studio", "Script timeout after 18000 seconds", False, "timeout"),
        ("transcription", "Unexpected processing error", True, "failed"),
    ],
)
async def test_process_task_sends_video_id_and_public_link(
    email_config, tmp_path, monkeypatch, task_type, error_message, raises_exception, expected_status
):
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "statuses.json"))
    monkeypatch.setattr(state, "_RUNNER_STATE", {**state._RUNNER_STATE, "task_statuses": {}})
    monkeypatch.setattr(
        email_service.config, "MANAGER_PUBLIC_URL", "https://admin.example.org/runner-manager"
    )
    dispatch = AsyncMock(return_value={"success": False, "error": error_message})
    if raises_exception:
        dispatch.side_effect = RuntimeError(error_message)
    monkeypatch.setattr(task_routes.task_dispatcher, "dispatch_task", dispatch)
    notify = AsyncMock()
    monkeypatch.setattr(task_routes, "notify_completion", notify)
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "_send_notification_email", send)
    request = TaskRequest(
        task_id="task-1",
        etab_name="test",
        app_name="Pod",
        task_type=task_type,
        source_url="https://example.org/video.mp4",
        parameters={"video_id": 12345},
        notify_url="",
        completion_callback="http://manager.internal:8081/task/completion",
    )

    await task_routes.process_task(request.task_id, request)

    send.assert_awaited_once()
    message = send.await_args.args[0]
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    assert "Video ID: 12345\n" in plain
    assert "Video ID" in html and "12345" in html
    assert error_message in plain and error_message in html
    assert f"Status: {expected_status}\n" in plain
    assert 'href="https://admin.example.org/runner-manager/admin"' in html
    assert state.get_task_status(request.task_id)["status"] == expected_status
    assert notify.await_args.args[:4] == (
        request.completion_callback,
        request.task_id,
        expected_status,
        error_message,
    )


@pytest.mark.asyncio
async def test_gpu_warning_links_to_public_manager(email_config, monkeypatch):
    monkeypatch.setattr(email_service.config, "MANAGER_PUBLIC_URL", "https://admin.example.org")
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(email_service, "_send_notification_email", send)
    assert await email_service.send_gpu_health_warning_email(error_message="driver unavailable")
    html = send.await_args.args[0].get_body(("html",)).get_content()
    assert 'href="https://admin.example.org/admin"' in html
