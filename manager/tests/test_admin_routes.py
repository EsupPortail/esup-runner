"""Coverage-oriented tests for app.api.routes.admin."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from app.api.routes import admin as admin_routes
from app.api.routes import statistics as statistics_routes
from app.core import config as config_module
from app.core.auth import verify_admin
from app.core.config import config
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task
from app.services import background_service


@pytest.fixture
def admin_client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_admin] = lambda: True

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.pop(verify_admin, None)


@pytest.fixture
def clean_state():
    original_runners = dict(runners)
    original_tasks = dict(tasks)

    runners.clear()
    tasks.clear()

    yield

    runners.clear()
    runners.update(original_runners)
    tasks.clear()
    tasks.update(original_tasks)


def _make_runner(runner_id: str, *, seconds_ago: int = 0) -> Runner:
    return Runner(
        id=runner_id,
        url=f"http://{runner_id}.example",
        task_types=["encoding"],
        token="",
        version="1.0.0",
        last_heartbeat=datetime.now() - timedelta(seconds=seconds_ago),
        availability="available",
        status="offline",
    )


def _make_task(
    task_id: str,
    runner_id: str,
    *,
    created_at: str,
    status: str = "completed",
    parameters: dict | None = None,
) -> Task:
    now = datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id=runner_id,
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters=parameters or {},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=created_at,
        updated_at=now,
        error=None,
        script_output=None,
    )


def _render_task_detail_template(status: str) -> str:
    task = SimpleNamespace(
        task_id="detail-task",
        runner_id="runner-1",
        status=status,
        task_type="encoding",
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:05:00",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        affiliation=None,
        parameters={"video_id": "video-123"},
        source_url="https://example.com/video.mp4",
        notify_url="https://example.com/notify",
        error=None,
        script_output=None,
    )
    env = Environment(loader=FileSystemLoader("app/web/templates"))
    template = env.get_template("task_detail.html")
    return template.render(
        version="test",
        dark_mode_enabled=False,
        task=task,
        task_actions=admin_routes._build_task_detail_actions(task),
        last_update="2026-01-01 00:00:00",
    )


def _render_tasks_template() -> str:
    env = Environment(loader=FileSystemLoader("app/web/templates"))
    template = env.get_template("tasks.html")
    return template.render(
        version="test",
        dark_mode_enabled=False,
        available_statuses=[],
        status_counts={},
        current_filters={
            "statuses": [],
            "search": None,
            "task_type": None,
            "auto_refresh": 0,
            "limit": 50,
        },
        available_task_types=[],
        tasks=[],
        total_tasks=0,
        now=datetime(2026, 1, 1, 0, 0, 0),
    )


def _render_credentials_template() -> str:
    env = Environment(loader=FileSystemLoader("app/web/templates"))
    template = env.get_template("credentials.html")
    return template.render(
        version="test",
        dark_mode_enabled=False,
        last_update="2026-01-01 00:00:00",
        feedback_message=None,
        feedback_level="info",
        admin_users=[{"name": "alice", "preview": "hash...", "value": "admin-hash"}],
        authorized_tokens=[{"name": "client_1", "preview": "token...", "value": "token-value"}],
    )


def test_admin_dashboard_rate_limit_allows_auto_refresh_margin(monkeypatch):
    """Validate dashboard rate limit leaves margin above built-in auto-refresh."""

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_admin] = lambda: True

    try:
        with TestClient(app, client=("198.51.100.77", 50000)) as client:
            responses = [client.get("/admin") for _ in range(11)]
    finally:
        app.dependency_overrides.pop(verify_admin, None)

    assert {response.status_code for response in responses} == {200}


def test_format_datetime_without_milliseconds_formats_iso_values():
    """Validate Format datetime without milliseconds formats iso values."""
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05.123456")
        == "2026-01-02 03:04:05"
    )
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05Z")
        == "2026-01-02 03:04:05"
    )


def test_format_datetime_without_milliseconds_handles_empty_and_invalid_values():
    """Validate Format datetime without milliseconds handles empty and invalid values."""
    assert admin_routes._format_datetime_without_milliseconds(None) == ""
    assert admin_routes._format_datetime_without_milliseconds("") == ""
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05.abc")
        == "2026-01-02 03:04:05"
    )


def test_format_attention_error_label_returns_short_first_line():
    """Validate attention error label returns a compact first line."""
    assert (
        admin_routes._format_attention_error_label(
            "\n  Encoding aborted: input video duration is 0 seconds.  \nTraceback..."
        )
        == "Encoding aborted: input video duration is 0 seconds."
    )
    assert admin_routes._format_attention_error_label(None) == ""
    assert admin_routes._format_attention_error_label("abcdefghijk", limit=10) == "abcdefg..."


def test_format_secret_preview_masks_sensitive_values():
    """Validate secret previews never expose the full value."""
    assert admin_routes._format_secret_preview("A" * 25) == "AAAAAAAAAA...AAAA"
    assert admin_routes._format_secret_preview("abcdef") == "abcd..."
    assert admin_routes._format_secret_preview("xyz") == "***"
    assert admin_routes._format_secret_preview("") == "not configured"


def test_admin_dashboard_datetime_helpers_support_stale_running_tasks():
    """Validate dashboard datetime helpers support stale running task labels."""
    assert admin_routes._parse_datetime(None) is None
    assert admin_routes._parse_datetime("not-a-date") is None
    assert admin_routes._parse_datetime("2026-01-01T10:00:00") == datetime(2026, 1, 1, 10, 0, 0)
    assert admin_routes._parse_datetime("2026-01-01T10:00:00Z") is not None

    assert admin_routes._format_duration_label(300) == "5m"
    assert admin_routes._format_duration_label(7200) == "2h"
    assert admin_routes._format_duration_label(7260) == "2h 1m"
    assert admin_routes._format_duration_label(24 * 3600) == "1d"
    assert admin_routes._format_duration_label((24 + 3) * 3600) == "1d 3h"
    assert admin_routes._format_duration_label(146 * 3600) == "6d 2h"


def test_build_runner_heartbeat_metadata_uses_last_heartbeat():
    """Validate runner display status is derived from heartbeat age."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    online = SimpleNamespace(last_heartbeat=now - timedelta(seconds=30))
    offline = SimpleNamespace(last_heartbeat=now - timedelta(seconds=90), status="online")

    assert admin_routes._build_runner_heartbeat_metadata(online, now=now) == {
        "status": "online",
        "last_heartbeat": "2026-01-01 11:59:30",
        "age_seconds": 30,
    }
    assert admin_routes._build_runner_heartbeat_metadata(offline, now=now)["status"] == "offline"
    assert admin_routes._build_runner_heartbeat_metadata(
        SimpleNamespace(last_heartbeat="bad-date"), now=now
    ) == {
        "status": "offline",
        "last_heartbeat": "bad-date",
        "age_seconds": 0,
    }


def test_admin_dashboard_builds_task_age_metadata():
    """Validate dashboard task age metadata uses compact labels."""
    now = datetime(2026, 1, 1, 12, 0, 0)

    assert admin_routes._build_task_age_metadata(
        "running",
        "2026-01-01T11:18:00",
        "2026-01-01T11:59:00",
        now=now,
    ) == {"label": "Started 42m ago", "is_warning": False}
    assert admin_routes._build_task_age_metadata(
        "pending",
        "2026-01-01T07:59:00",
        "2026-01-01T07:59:00",
        now=now,
    ) == {"label": "Waiting 4h 1m", "is_warning": True}
    assert admin_routes._build_task_age_metadata(
        "failed",
        "2026-01-01T09:00:00",
        "2026-01-01T10:00:00",
        now=now,
    ) == {"label": "Failed 2h ago", "is_warning": False}
    assert admin_routes._build_task_age_metadata(
        "unknown",
        "2026-01-01T09:00:00",
        "2026-01-01T10:00:00",
        now=now,
    ) == {"label": "", "is_warning": False}
    assert admin_routes._build_task_age_metadata(
        "running",
        "not-a-date",
        "2026-01-01T10:00:00",
        now=now,
    ) == {"label": "", "is_warning": False}


def test_admin_task_detail_actions_respect_api_constraints():
    """Validate task detail action state mirrors task API constraints."""
    completed_task = SimpleNamespace(status="completed")
    pending_task = SimpleNamespace(status="pending")
    running_task = SimpleNamespace(status="running")

    assert admin_routes._build_task_detail_actions(completed_task) == {
        "can_delete": True,
        "can_restart": True,
        "can_stop": False,
        "delete_disabled_reason": "",
        "restart_disabled_reason": "",
        "stop_disabled_reason": "Task status 'completed' cannot be stopped",
    }
    assert admin_routes._build_task_detail_actions(pending_task) == {
        "can_delete": False,
        "can_restart": False,
        "can_stop": False,
        "delete_disabled_reason": "Task status 'pending' cannot be deleted",
        "restart_disabled_reason": "Task status 'pending' cannot be restarted",
        "stop_disabled_reason": "Task status 'pending' cannot be stopped",
    }
    assert admin_routes._build_task_detail_actions(running_task) == {
        "can_delete": False,
        "can_restart": False,
        "can_stop": True,
        "delete_disabled_reason": "Task status 'running' cannot be deleted",
        "restart_disabled_reason": "Task status 'running' cannot be restarted",
        "stop_disabled_reason": "",
    }


def test_runner_status_headers_include_runner_token():
    """Validate runner status headers include runner token when present."""
    assert admin_routes._runner_status_headers(SimpleNamespace(token="runner-token")) == {
        "X-API-Token": "runner-token"
    }
    assert admin_routes._runner_status_headers(SimpleNamespace(token="")) == {}


@pytest.mark.asyncio
async def test_fetch_runner_live_status_success(monkeypatch):
    """Validate live runner status fetch returns disk usage."""
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"disk_usage": {"ok": True}}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeAsyncClient)

    payload = await admin_routes._fetch_runner_live_status(
        SimpleNamespace(url="http://runner.example/", token="runner-token")
    )

    assert payload["available"] is True
    assert payload["disk_usage"] == {"ok": True}
    assert captured["url"] == "http://runner.example/runner/status"
    assert captured["headers"] == {"X-API-Token": "runner-token"}


@pytest.mark.asyncio
async def test_fetch_runner_live_status_handles_request_error(monkeypatch):
    """Validate live runner status fetch handles request errors."""

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            raise admin_routes.httpx.RequestError("boom")

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeAsyncClient)

    payload = await admin_routes._fetch_runner_live_status(
        SimpleNamespace(url="http://runner.example", token="runner-token")
    )

    assert payload == {
        "available": False,
        "url": "http://runner.example/runner/status",
        "error": "Runner status request failed.",
        "disk_usage": None,
    }


@pytest.mark.asyncio
async def test_fetch_runner_live_status_handles_non_200(monkeypatch):
    """Validate live runner status fetch handles non-200 responses."""

    class FakeResponse:
        status_code = 503

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeAsyncClient)

    payload = await admin_routes._fetch_runner_live_status(
        SimpleNamespace(url="http://runner.example", token="runner-token")
    )

    assert payload == {
        "available": False,
        "url": "http://runner.example/runner/status",
        "error": "Runner status returned HTTP 503.",
        "disk_usage": None,
    }


@pytest.mark.asyncio
async def test_fetch_runner_live_status_handles_invalid_json(monkeypatch):
    """Validate live runner status fetch handles invalid JSON."""

    class FakeResponse:
        status_code = 200

        def json(self):
            raise ValueError("bad json")

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeAsyncClient)

    payload = await admin_routes._fetch_runner_live_status(
        SimpleNamespace(url="http://runner.example", token="runner-token")
    )

    assert payload == {
        "available": False,
        "url": "http://runner.example/runner/status",
        "error": "Runner status returned invalid JSON.",
        "disk_usage": None,
    }


@pytest.mark.asyncio
async def test_fetch_runner_live_status_handles_non_object_payload(monkeypatch):
    """Validate live runner status fetch handles unexpected payloads."""

    class FakeResponse:
        status_code = 200

        def json(self):
            return []

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(admin_routes.httpx, "AsyncClient", FakeAsyncClient)

    payload = await admin_routes._fetch_runner_live_status(
        SimpleNamespace(url="http://runner.example", token="runner-token")
    )

    assert payload == {
        "available": False,
        "url": "http://runner.example/runner/status",
        "error": "Runner status returned an unexpected payload.",
        "disk_usage": None,
    }


def test_task_detail_template_renders_delete_and_restart_actions():
    """Validate task detail template renders task actions without TestClient."""
    html = _render_task_detail_template("failed")

    assert 'id="taskActionFeedback"' in html
    assert 'id="deleteTaskBtn"' in html
    assert 'id="restartTaskBtn"' in html
    assert 'id="stopTaskBtn"' in html
    assert 'data-task-action="delete"' in html
    assert 'data-task-action="restart"' in html
    assert 'data-task-action="stop"' in html
    assert "/tasks/delete-selected" in html
    assert "/tasks/restart-selected" in html
    assert "/tasks/stop-selected" in html
    assert "redirectUrl: '/admin'" in html
    assert "Delete this task? This cannot be undone." in html
    assert "await window.esupConfirm(options.confirmOptions)" in html
    assert "This task will be permanently removed from the manager history." in html
    assert "subject: deleteTaskBtn.dataset.taskId" in html
    assert "confirmLabel: 'Delete task'" in html
    assert "bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" in html
    assert html.find("bootstrap.bundle.min.js") < html.find("const taskActionFeedback")
    assert "Restart this task?" in html
    assert "This failed task can be deleted or restarted." in html
    assert "text-subtle fst-italic" in html
    assert "This failed task cannot be deleted or restarted." not in html


def test_tasks_template_renders_visual_delete_confirmation():
    """Validate bulk deletion uses the shared visual confirmation."""
    html = _render_tasks_template()

    assert "await window.esupConfirm" in html
    assert "Delete selected task?" in html
    assert "This action cannot be undone." in html
    assert "Delete tasks" in html
    assert "bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js" in html
    assert "refreshBtn.addEventListener" not in html


def test_credentials_template_uses_visual_delete_confirmations():
    """Validate credential deletions use the shared visual confirmation."""
    html = _render_credentials_template()

    assert html.count("data-delete-confirm") == 2
    assert 'data-confirm-title="Delete this administrator?"' in html
    assert 'data-confirm-subject="alice"' in html
    assert 'data-confirm-subject-label="Administrator"' in html
    assert 'data-confirm-title="Delete this authorized token?"' in html
    assert 'data-confirm-subject="client_1"' in html
    assert 'data-confirm-subject-label="Authorized token"' in html
    assert "onsubmit=" not in html


def test_task_detail_template_disables_actions_for_pending_task():
    """Validate protected task statuses disable detail actions."""
    html = _render_task_detail_template("pending")

    assert "This pending task cannot be stopped, deleted, or restarted." in html
    assert "Task status 'pending' cannot be deleted" in html
    assert "Task status 'pending' cannot be restarted" in html
    assert "Task status 'pending' cannot be stopped" in html
    assert html.count('disabled aria-disabled="true"') == 3


def test_task_detail_template_enables_stop_for_running_task():
    """Validate running tasks expose the stop action."""
    html = _render_task_detail_template("running")

    assert "This running task can be stopped, but cannot be deleted or restarted." in html
    assert "text-subtle fst-italic" in html
    assert "Stop this running task?" in html
    assert "Task status 'running' cannot be stopped" not in html


def test_admin_dashboard_renders_and_orders_tasks(admin_client, clean_state):
    """Validate Admin dashboard renders and orders tasks."""
    runners["r1"] = _make_runner("r1", seconds_ago=10)

    tasks["t_old"] = _make_task("t_old", "r1", created_at="2026-01-01T00:00:00")
    tasks["t_new"] = _make_task(
        "t_new",
        "r1",
        created_at="2026-01-02T00:00:00.123456",
        parameters={"video_id": "vid-001", "video_slug": "new-video", "video_title": "New Video"},
    )

    admin_client.cookies.set("theme", "dark")
    resp = admin_client.get("/admin")
    assert resp.status_code == 200

    # Newer task should appear before older one in rendered HTML
    assert resp.text.find("t_new") < resp.text.find("t_old")
    assert "vid-001" in resp.text
    assert "new-video" not in resp.text
    assert "New Video" not in resp.text
    assert "2026-01-02 00:00:00" in resp.text
    assert "2026-01-02T00:00:00.123456" not in resp.text


def test_build_attention_summary_collects_limited_items():
    """Validate attention summary collects offline runners and task incidents."""
    runners_data = [
        {"id": "online_runner", "status": "online", "age_seconds": 10},
        {"id": "old_offline_runner", "status": "offline", "age_seconds": 120},
        {"id": "new_offline_runner", "status": "offline", "age_seconds": 90},
    ]
    tasks_data = [
        {"id": "running_task", "status": "running"},
        {
            "id": "failed_task",
            "status": "failed",
            "error_label": "Encoding aborted: input video duration is 0 seconds.",
            "video_id": "video-123",
        },
        {"id": "warning_task", "status": "warning"},
        {"id": "timeout_task", "status": "timeout"},
        {"id": "completed_task", "status": "completed"},
    ]

    summary = admin_routes._build_attention_summary(runners_data, tasks_data)

    assert summary["attention_count"] == 5
    assert summary["offline_runners_count"] == 2
    assert [runner["id"] for runner in summary["offline_runners"]] == [
        "old_offline_runner",
        "new_offline_runner",
    ]
    assert summary["attention_task_status_counts"] == {
        "failed": 1,
        "warning": 1,
        "timeout": 1,
    }
    assert [task["id"] for task in summary["attention_tasks"]] == [
        "failed_task",
        "warning_task",
        "timeout_task",
    ]
    assert (
        summary["attention_tasks"][0]["error_label"]
        == "Encoding aborted: input video duration is 0 seconds."
    )
    assert summary["attention_tasks"][0]["video_id"] == "video-123"


def test_build_attention_summary_flags_stale_running_tasks():
    """Validate attention summary includes running tasks stale for at least 300 minutes."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    tasks_data = [
        {
            "id": "stale_running_task",
            "status": "running",
            "updated_at": "2026-01-01T06:59:00",
        },
        {
            "id": "fresh_running_task",
            "status": "running",
            "updated_at": "2026-01-01T07:01:00",
        },
        {"id": "invalid_running_task", "status": "running", "updated_at": "not-a-date"},
    ]

    summary = admin_routes._build_attention_summary([], tasks_data, now=now)

    assert summary["attention_count"] == 1
    assert summary["stale_running_tasks_count"] == 1
    assert summary["attention_tasks_count"] == 1
    assert summary["attention_tasks"][0]["id"] == "stale_running_task"
    assert (
        summary["attention_tasks"][0]["stale_running_label"] == "Running without update for 5h 1m."
    )


def test_build_attention_summary_orders_tasks_by_creation_date():
    """Validate attention tasks are ordered by creation date across incident types."""
    now = datetime(2026, 1, 1, 12, 0, 0)
    tasks_data = [
        {
            "id": "older_failed_task",
            "status": "failed",
            "created_at": "2026-01-01T08:00:00",
        },
        {
            "id": "newer_stale_running_task",
            "status": "running",
            "created_at": "2026-01-01T10:00:00",
            "updated_at": "2026-01-01T06:00:00",
        },
        {
            "id": "newest_timeout_task",
            "status": "timeout",
            "created_at": "2026-01-01T11:00:00",
        },
    ]

    summary = admin_routes._build_attention_summary([], tasks_data, now=now)

    assert [task["id"] for task in summary["attention_tasks"]] == [
        "newest_timeout_task",
        "newer_stale_running_task",
        "older_failed_task",
    ]


def test_admin_template_renders_copy_task_id_controls():
    """Validate admin template exposes task ID copy controls."""
    env = Environment(loader=FileSystemLoader("app/web/templates"))
    template = env.get_template("admin.html")

    html = template.render(
        version="test",
        dark_mode_enabled=False,
        admin_count=1,
        online_runners=1,
        total_tasks=2,
        tasks_this_month=2,
        attention_count=3,
        offline_runners_count=1,
        attention_task_status_counts={"failed": 1, "warning": 0, "timeout": 0},
        stale_running_tasks_count=1,
        attention_tasks_count=2,
        offline_runners=[
            {
                "id": "offline-runner",
                "last_heartbeat": "2026-01-01 00:00:00",
                "age_seconds": 120,
            }
        ],
        attention_tasks=[
            {
                "id": "attention-task",
                "status": "failed",
                "task_type": "encoding",
                "runner_id": "runner-1",
                "created_at_display": "2026-01-01 00:00:00",
                "error_label": "Encoding aborted.",
                "stale_running_label": "",
                "video_id": "video-123",
            },
            {
                "id": "stale-running-task",
                "status": "running",
                "task_type": "encoding",
                "runner_id": "runner-2",
                "created_at_display": "2026-01-01 00:00:10",
                "error_label": "",
                "stale_running_label": "Running without update for 5h.",
                "video_id": "video-789",
            },
        ],
        runners=[],
        tasks=[
            {
                "id": "dashboard-task",
                "status": "running",
                "task_type": "encoding",
                "runner_id": "runner-1",
                "created_at_display": "2026-01-01 00:00:00",
                "age_label": "Started 4h 1m ago",
                "age_is_warning": True,
                "video_id": "video-456",
            }
        ],
        last_update="2026-01-01 00:00:00",
    )

    assert 'id="copy-task-feedback"' in html
    assert 'id="auto-refresh-status"' in html
    assert 'id="auto-refresh-toggle"' in html
    assert 'id="auto-refresh-feedback"' in html
    assert 'title="Reload manager config"' in html
    assert "AUTO_REFRESH_INTERVAL_SECONDS = 15" in html
    assert "AUTO_REFRESH_STORAGE_KEY" in html
    assert "localStorage" in html
    assert "window.location.reload()" in html
    assert 'http-equiv="refresh"' not in html
    assert 'data-copy-task-id="attention-task"' in html
    assert 'aria-label="Copy task ID attention-task"' in html
    assert 'data-copy-task-id="dashboard-task"' in html
    assert 'aria-label="Copy task ID dashboard-task"' in html
    assert 'data-task-detail-url="/admin/task/attention-task"' in html
    assert 'data-task-detail-url="/admin/task/dashboard-task"' in html
    assert 'title="Open task detail"' in html
    assert 'title="Copy task ID"' in html
    assert 'data-attention-filter="all"' in html
    assert 'data-attention-filter="offline-runner"' in html
    assert 'data-attention-filter="task-incident"' in html
    assert 'data-attention-filter="stale-running"' in html
    assert 'class="btn btn-sm btn-outline-primary active"' in html
    assert 'class="btn btn-sm btn-outline-danger"' in html
    assert "text-bg-primary" in html
    assert "text-bg-danger" in html
    assert 'data-attention-kind="offline-runner"' in html
    assert 'data-attention-kind="task-incident"' in html
    assert 'data-attention-kind="stale-running"' in html
    assert 'id="attention-filter-empty"' in html
    assert "setAttentionFilter" in html
    assert "attentionFilterButtons" in html
    assert "Started 4h 1m ago" in html
    assert "task-age-label-warning" in html
    assert "Created: 2026-01-01 00:00:00" in html
    assert "Created: 2026-01-01 00:00:10" in html
    assert "Updated:" not in html
    assert "bi-clock-history" in html
    assert "task-list-title" in html
    assert "task-id-link" in html
    assert "task-list-status-badge" in html
    assert "task-meta-line" in html
    assert "copyTaskButtons" in html
    assert "taskRows" in html
    assert "stretched-link" not in html


def test_task_detail_not_found(admin_client, clean_state):
    """Validate Task detail not found."""
    resp = admin_client.get("/admin/task/does-not-exist")
    assert resp.status_code == 404


def test_task_detail_ok(admin_client, clean_state):
    """Validate Task detail ok."""
    runners["r1"] = _make_runner("r1")
    tasks["t1"] = _make_task(
        "t1",
        "r1",
        created_at="2026-01-01T00:00:00",
        parameters={
            "video_id": "video-123",
            "video_slug": "video-slug-123",
            "video_title": "Video Title 123",
        },
    )

    resp = admin_client.get("/admin/task/t1")
    assert resp.status_code == 200
    assert "t1" in resp.text
    assert "video-123" in resp.text
    assert "video-slug-123" in resp.text
    assert "Video Title 123" in resp.text


def test_runner_detail_not_found(admin_client, clean_state):
    """Validate Runner detail not found."""
    resp = admin_client.get("/admin/runner/does-not-exist")
    assert resp.status_code == 404


def test_runner_detail_ok(admin_client, clean_state, monkeypatch):
    """Validate Runner detail ok."""
    runner = _make_runner("r1", seconds_ago=90)
    runner.status = "online"
    runner.token = "runner-token-secret-value-1234"
    runners["r1"] = runner

    async def _fake_runner_live_status(_runner):
        return {
            "available": True,
            "url": "http://r1.example/runner/status",
            "error": "",
            "disk_usage": {
                "ok": True,
                "status": "orange",
                "checked_at": "2026-01-01T00:00:00",
                "output_dir_pattern": "/tmp/esup-runner/<task_id>/output",
                "directories": {
                    "STORAGE_DIR": {
                        "path": "/tmp/esup-runner",
                        "description": "Runner storage and task output root",
                        "total_human": "100.0G",
                        "used_human": "76.0G",
                        "free_human": "24.0G",
                        "used_percent_display": "76.0%",
                        "status": "orange",
                    }
                },
            },
        }

    monkeypatch.setattr(admin_routes, "_fetch_runner_live_status", _fake_runner_live_status)

    resp = admin_client.get("/admin/runner/r1")
    assert resp.status_code == 200
    assert "r1" in resp.text
    assert "Disk Usage" in resp.text
    assert "OFFLINE" in resp.text
    assert "runner-tok...1234" in resp.text
    assert "runner-token-secret-value-1234" not in resp.text
    assert "76.0%" in resp.text
    assert "bi-exclamation-triangle-fill" in resp.text
    assert "Storage usage is elevated" in resp.text


def test_admin_tasks_page_renders(admin_client, clean_state):
    """Validate Admin tasks page renders."""
    runners["r1"] = _make_runner("r1")
    tasks["t1"] = _make_task("t1", "r1", created_at="2026-01-01T00:00:00", status="running")
    tasks["t2"] = _make_task("t2", "r1", created_at="2026-01-01T00:00:01", status="failed")

    resp = admin_client.get("/admin/tasks")
    assert resp.status_code == 200
    assert "Tasks Management" in resp.text
    assert "t1" in resp.text
    assert "t2" in resp.text


def test_toggle_theme_redirects_and_sets_cookie(admin_client):
    # With current=dark -> new=light
    """Validate Toggle theme redirects and sets cookie."""
    admin_client.cookies.set("theme", "dark")
    resp1 = admin_client.post("/admin/toggle-theme", follow_redirects=False)
    assert resp1.status_code == 303
    assert "theme=light" in resp1.headers.get("set-cookie", "")

    # With no cookie -> new=dark
    admin_client.cookies.clear()
    resp2 = admin_client.post("/admin/toggle-theme", follow_redirects=False)
    assert resp2.status_code == 303
    assert "theme=dark" in resp2.headers.get("set-cookie", "")


def test_credentials_page_renders_token_previews(admin_client, monkeypatch):
    """Validate Credentials page renders token previews."""
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(
        config,
        "AUTHORIZED_TOKENS",
        {
            "long": "A" * 25,
            "short": "abcdef",
            "tiny": "xyz",
        },
    )
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hashed"})

    admin_client.cookies.set("theme", "dark")
    resp = admin_client.get("/admin/credentials")
    assert resp.status_code == 200

    # Preview rules from route implementation
    assert "AAAAAAAAAA...AAAA" in resp.text  # 10 + ... + last 4
    assert "abcd..." in resp.text
    assert "***" in resp.text
    assert "hash..." in resp.text
    assert 'action="/admin/credentials/admins"' in resp.text
    assert 'data-copy-admin="true"' in resp.text
    assert 'action="/admin/credentials/admins/admin/delete"' in resp.text
    assert 'id="reload-config-btn-top"' in resp.text
    assert 'id="reload-config-status-top"' in resp.text
    assert 'id="copy-admin-feedback"' in resp.text
    assert 'action="/admin/credentials/tokens"' in resp.text
    assert 'data-copy-token="true"' in resp.text
    assert 'id="copy-token-feedback"' in resp.text
    assert 'aria-atomic="true"' in resp.text
    assert 'aria-label="Delete token long"' in resp.text


def test_generate_authorized_token_clamps_requested_length(monkeypatch):
    """Validate generated token lengths stay within the supported bounds."""
    requested_lengths = []

    def _fake_token_urlsafe(length: int) -> str:
        requested_lengths.append(length)
        return f"generated-{length}"

    monkeypatch.setattr(admin_routes.secrets, "token_urlsafe", _fake_token_urlsafe)

    assert admin_routes._generate_authorized_token(1) == "generated-16"
    assert admin_routes._generate_authorized_token(999) == "generated-128"
    assert requested_lengths == [16, 128]


def test_hash_admin_password_delegates_to_password_context(monkeypatch):
    """Validate admin password hashing uses the configured password context."""
    captured = {}

    class FakePasswordContext:
        def hash(self, password: str) -> str:
            captured["password"] = password
            return f"hashed:{password}"

    monkeypatch.setattr(admin_routes, "_PASSWORD_CONTEXT", FakePasswordContext())

    assert admin_routes._hash_admin_password("secret-password") == "hashed:secret-password"
    assert captured == {"password": "secret-password"}


def test_read_env_lines_returns_empty_when_env_file_is_missing(tmp_path):
    """Validate missing .env files are treated as empty."""
    assert admin_routes._read_env_lines(tmp_path / ".env") == []


def test_delete_env_helpers_return_false_when_env_file_is_missing(monkeypatch, tmp_path):
    """Validate delete helpers no-op cleanly when the .env file does not exist."""
    env_path = tmp_path / "missing.env"
    monkeypatch.setattr(config_module, "get_env_file_path", lambda: env_path)

    assert admin_routes._delete_authorized_token_from_env("client") is False
    assert admin_routes._delete_admin_user_from_env("admin") is False


@pytest.mark.parametrize(
    ("query_params", "expected_level", "expected_message"),
    [
        (
            {"feedback": "token_created", "token_name": "api_client"},
            "success",
            "Token 'api_client' created.",
        ),
        (
            {"feedback": "admin_deleted", "admin_name": "alice@example.org"},
            "success",
            "Administrator 'alice@example.org' deleted.",
        ),
    ],
)
def test_build_credentials_feedback_returns_known_messages(
    query_params, expected_level, expected_message
):
    """Validate credentials feedback maps known token and admin outcomes."""
    request = SimpleNamespace(query_params=query_params)

    feedback = admin_routes._build_credentials_feedback(request)

    assert feedback["level"] == expected_level
    assert expected_message in feedback["message"]


def test_upsert_authorized_token_in_env_updates_and_appends(monkeypatch, tmp_path):
    """Validate .env token upsert updates existing labels and appends new labels."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MANAGER_HOST=localhost\n"
        "AUTHORIZED_TOKENS__existing=old-value\n"
        "ADMIN_USERS__admin=hash\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "get_env_file_path", lambda: env_path)

    admin_routes._upsert_authorized_token_in_env("existing", "new-value")
    admin_routes._upsert_authorized_token_in_env("new_label", "new-token")

    content = env_path.read_text(encoding="utf-8")
    assert "AUTHORIZED_TOKENS__existing=new-value" in content
    assert "AUTHORIZED_TOKENS__new_label=new-token" in content
    assert content.index("AUTHORIZED_TOKENS__existing=new-value") < content.index(
        "AUTHORIZED_TOKENS__new_label=new-token"
    )


def test_delete_authorized_token_from_env_returns_status(monkeypatch, tmp_path):
    """Validate .env token delete returns whether a token line was removed."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "AUTHORIZED_TOKENS__keep=keep-token\n" "AUTHORIZED_TOKENS__drop=drop-token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "get_env_file_path", lambda: env_path)

    assert admin_routes._delete_authorized_token_from_env("drop") is True
    content = env_path.read_text(encoding="utf-8")
    assert "AUTHORIZED_TOKENS__drop=drop-token" not in content
    assert "AUTHORIZED_TOKENS__keep=keep-token" in content
    assert admin_routes._delete_authorized_token_from_env("missing") is False


def test_upsert_admin_user_in_env_updates_and_appends(monkeypatch, tmp_path):
    """Validate .env admin upsert updates existing labels and appends new labels."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MANAGER_HOST=localhost\n"
        'ADMIN_USERS__existing="old-hash"\n'
        "AUTHORIZED_TOKENS__client=token\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "get_env_file_path", lambda: env_path)

    admin_routes._upsert_admin_user_in_env("existing", "new-hash")
    admin_routes._upsert_admin_user_in_env("new_admin", "new-admin-hash")

    content = env_path.read_text(encoding="utf-8")
    assert 'ADMIN_USERS__existing="new-hash"' in content
    assert 'ADMIN_USERS__new_admin="new-admin-hash"' in content
    assert content.index('ADMIN_USERS__existing="new-hash"') < content.index(
        'ADMIN_USERS__new_admin="new-admin-hash"'
    )


def test_delete_admin_user_from_env_returns_status(monkeypatch, tmp_path):
    """Validate .env admin delete returns whether an admin line was removed."""
    env_path = tmp_path / ".env"
    env_path.write_text(
        'ADMIN_USERS__keep="keep-hash"\n' 'ADMIN_USERS__drop="drop-hash"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "get_env_file_path", lambda: env_path)

    assert admin_routes._delete_admin_user_from_env("drop") is True
    content = env_path.read_text(encoding="utf-8")
    assert 'ADMIN_USERS__drop="drop-hash"' not in content
    assert 'ADMIN_USERS__keep="keep-hash"' in content
    assert admin_routes._delete_admin_user_from_env("missing") is False


def test_create_authorized_token_endpoint_persists_and_reloads(admin_client, monkeypatch):
    """Validate token creation endpoint writes .env then triggers config reload."""
    captured = {}
    reload_calls = {"count": 0}
    publish_calls = {"count": 0}

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {})
    monkeypatch.setattr(admin_routes, "_generate_authorized_token", lambda _length=32: "generated")

    def _fake_upsert(token_name: str, token_value: str):
        captured["token_name"] = token_name
        captured["token_value"] = token_value

    def _fake_reload():
        reload_calls["count"] += 1
        return config

    def _fake_publish():
        publish_calls["count"] += 1
        return 0

    monkeypatch.setattr(admin_routes, "_upsert_authorized_token_in_env", _fake_upsert)
    monkeypatch.setattr(config_module, "reload_config_env", _fake_reload)
    monkeypatch.setattr(config_module, "publish_config_reload_event", _fake_publish)

    resp = admin_client.post(
        "/admin/credentials/tokens",
        data={"token_name": "new_token"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=token_created&token_name=new_token"
    )
    assert captured == {"token_name": "new_token", "token_value": "generated"}
    assert reload_calls["count"] == 1
    assert publish_calls["count"] == 1


def test_create_authorized_token_endpoint_rejects_invalid_label(admin_client):
    """Validate token creation endpoint rejects labels incompatible with .env keys."""
    resp = admin_client.post(
        "/admin/credentials/tokens",
        data={"token_name": "invalid-label"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=token_invalid"


def test_create_authorized_token_endpoint_rejects_duplicate_label(admin_client, monkeypatch):
    """Validate token creation endpoint rejects already existing token labels."""
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"existing": "tok"})

    resp = admin_client.post(
        "/admin/credentials/tokens",
        data={"token_name": "existing"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=token_exists&token_name=existing"
    )


def test_create_authorized_token_endpoint_reports_write_failure(admin_client, monkeypatch):
    """Validate token creation reports .env write failures."""
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {})
    monkeypatch.setattr(admin_routes, "_generate_authorized_token", lambda _length=32: "generated")

    def _raise_oserror(_token_name: str, _token_value: str):
        raise OSError("permission denied")

    monkeypatch.setattr(admin_routes, "_upsert_authorized_token_in_env", _raise_oserror)

    resp = admin_client.post(
        "/admin/credentials/tokens",
        data={"token_name": "new_token"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=token_write_failed&token_name=new_token"
    )


def test_delete_authorized_token_endpoint_updates_config(admin_client, monkeypatch):
    """Validate token delete endpoint updates .env and reloads config."""
    reload_calls = {"count": 0}
    publish_calls = {"count": 0}

    def _fake_reload():
        reload_calls["count"] += 1
        return config

    def _fake_publish():
        publish_calls["count"] += 1
        return 0

    monkeypatch.setattr(admin_routes, "_delete_authorized_token_from_env", lambda _name: True)
    monkeypatch.setattr(config_module, "reload_config_env", _fake_reload)
    monkeypatch.setattr(config_module, "publish_config_reload_event", _fake_publish)

    resp = admin_client.post("/admin/credentials/tokens/legacy/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=token_deleted&token_name=legacy"
    assert reload_calls["count"] == 1
    assert publish_calls["count"] == 1


def test_delete_authorized_token_endpoint_handles_missing_label(admin_client, monkeypatch):
    """Validate token delete endpoint reports missing labels gracefully."""
    monkeypatch.setattr(admin_routes, "_delete_authorized_token_from_env", lambda _name: False)

    resp = admin_client.post("/admin/credentials/tokens/missing/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=token_missing&token_name=missing"
    )


def test_delete_authorized_token_endpoint_rejects_invalid_label(admin_client):
    """Validate token delete rejects labels incompatible with .env keys."""
    resp = admin_client.post(
        "/admin/credentials/tokens/invalid-label/delete",
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=token_invalid"


def test_delete_authorized_token_endpoint_reports_write_failure(admin_client, monkeypatch):
    """Validate token delete reports .env write failures."""

    def _raise_oserror(_token_name: str):
        raise OSError("permission denied")

    monkeypatch.setattr(admin_routes, "_delete_authorized_token_from_env", _raise_oserror)

    resp = admin_client.post("/admin/credentials/tokens/legacy/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=token_write_failed&token_name=legacy"
    )


def test_create_admin_user_endpoint_persists_and_reloads(admin_client, monkeypatch):
    """Validate admin creation endpoint writes .env then triggers config reload."""
    captured = {}
    reload_calls = {"count": 0}
    publish_calls = {"count": 0}

    monkeypatch.setattr(config, "ADMIN_USERS", {})
    monkeypatch.setattr(admin_routes, "_hash_admin_password", lambda _password: "hashed-generated")

    def _fake_upsert(admin_name: str, hashed_password: str):
        captured["admin_name"] = admin_name
        captured["hashed_password"] = hashed_password

    def _fake_reload():
        reload_calls["count"] += 1
        return config

    def _fake_publish():
        publish_calls["count"] += 1
        return 0

    monkeypatch.setattr(admin_routes, "_upsert_admin_user_in_env", _fake_upsert)
    monkeypatch.setattr(config_module, "reload_config_env", _fake_reload)
    monkeypatch.setattr(config_module, "publish_config_reload_event", _fake_publish)

    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "new_admin",
            "admin_password": "secret-password",
            "admin_password_confirm": "secret-password",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=admin_created&admin_name=new_admin"
    )
    assert captured == {"admin_name": "new_admin", "hashed_password": "hashed-generated"}
    assert reload_calls["count"] == 1
    assert publish_calls["count"] == 1


def test_create_admin_user_endpoint_rejects_invalid_label(admin_client):
    """Validate admin creation endpoint rejects labels incompatible with .env keys."""
    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "invalid label",
            "admin_password": "password",
            "admin_password_confirm": "password",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=admin_invalid"


def test_create_admin_user_endpoint_rejects_duplicate_label(admin_client, monkeypatch):
    """Validate admin creation endpoint rejects already existing admin labels."""
    monkeypatch.setattr(config, "ADMIN_USERS", {"existing": "hash"})

    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "existing",
            "admin_password": "password",
            "admin_password_confirm": "password",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=admin_exists&admin_name=existing"
    )


def test_create_admin_user_endpoint_rejects_password_mismatch(admin_client):
    """Validate admin creation endpoint rejects mismatching passwords."""
    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "alice",
            "admin_password": "password-a",
            "admin_password_confirm": "password-b",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=admin_password_mismatch&admin_name=alice"
    )


def test_create_admin_user_endpoint_rejects_empty_password(admin_client):
    """Validate admin creation endpoint rejects empty passwords."""
    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "alice",
            "admin_password": "",
            "admin_password_confirm": "",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=admin_password_empty&admin_name=alice"
    )


def test_create_admin_user_endpoint_reports_write_failure(admin_client, monkeypatch):
    """Validate admin creation reports .env write failures."""
    monkeypatch.setattr(config, "ADMIN_USERS", {})
    monkeypatch.setattr(admin_routes, "_hash_admin_password", lambda _password: "hashed-generated")

    def _raise_oserror(_admin_name: str, _hashed_password: str):
        raise OSError("permission denied")

    monkeypatch.setattr(admin_routes, "_upsert_admin_user_in_env", _raise_oserror)

    resp = admin_client.post(
        "/admin/credentials/admins",
        data={
            "admin_name": "new_admin",
            "admin_password": "secret-password",
            "admin_password_confirm": "secret-password",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=admin_write_failed&admin_name=new_admin"
    )


def test_delete_admin_user_endpoint_updates_config(admin_client, monkeypatch):
    """Validate admin delete endpoint updates .env and reloads config."""
    reload_calls = {"count": 0}
    publish_calls = {"count": 0}

    def _fake_reload():
        reload_calls["count"] += 1
        return config

    def _fake_publish():
        publish_calls["count"] += 1
        return 0

    monkeypatch.setattr(admin_routes, "_delete_admin_user_from_env", lambda _name: True)
    monkeypatch.setattr(config_module, "reload_config_env", _fake_reload)
    monkeypatch.setattr(config_module, "publish_config_reload_event", _fake_publish)

    resp = admin_client.post("/admin/credentials/admins/alice/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=admin_deleted&admin_name=alice"
    assert reload_calls["count"] == 1
    assert publish_calls["count"] == 1


def test_delete_admin_user_endpoint_handles_missing_label(admin_client, monkeypatch):
    """Validate admin delete endpoint reports missing labels gracefully."""
    monkeypatch.setattr(admin_routes, "_delete_admin_user_from_env", lambda _name: False)

    resp = admin_client.post("/admin/credentials/admins/missing/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert (
        resp.headers["location"] == "/admin/credentials?feedback=admin_missing&admin_name=missing"
    )


def test_delete_admin_user_endpoint_rejects_invalid_label(admin_client):
    """Validate admin delete rejects labels incompatible with .env keys."""
    resp = admin_client.post(
        "/admin/credentials/admins/invalid%20label/delete",
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/credentials?feedback=admin_invalid"


def test_delete_admin_user_endpoint_reports_write_failure(admin_client, monkeypatch):
    """Validate admin delete reports .env write failures."""

    def _raise_oserror(_admin_name: str):
        raise OSError("permission denied")

    monkeypatch.setattr(admin_routes, "_delete_admin_user_from_env", _raise_oserror)

    resp = admin_client.post("/admin/credentials/admins/alice/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert (
        resp.headers["location"]
        == "/admin/credentials?feedback=admin_write_failed&admin_name=alice"
    )


def test_reload_config_endpoint_returns_expected_payload(admin_client, monkeypatch):
    """Validate Reload config endpoint returns expected payload."""
    fake = SimpleNamespace(
        API_DOCS_VISIBILITY="public",
        AUTHORIZED_TOKENS={"x": "y", "z": "w"},
        ADMIN_USERS={"alice": "h1", "bob": "h2"},
    )

    monkeypatch.setattr(config_module, "reload_config_env", lambda: fake)
    monkeypatch.setattr(config_module, "publish_config_reload_event", lambda: 0)

    resp = admin_client.post("/admin/reload-config")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["api_docs_visibility"] == "public"
    assert payload["authorized_tokens"] == ["x", "z"]
    assert payload["authorized_tokens_count"] == 2
    assert payload["admin_users"] == ["alice", "bob"]
    assert payload["admin_users_count"] == 2


@pytest.mark.parametrize(
    "visibility,expected_cookie_set",
    [
        ("private", True),
        ("public", False),
    ],
)
def test_documentation_page_uses_cookie_without_query_token(  # noqa: D401
    admin_client, monkeypatch, visibility: str, expected_cookie_set: bool
):
    """Validate Documentation page uses cookie without query token."""
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", visibility)
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"t": "sekret"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)

    resp = admin_client.get("/admin/docs")
    assert resp.status_code == 200

    assert "?token=sekret" not in resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    if expected_cookie_set:
        assert "openapi_token=" in set_cookie
        assert "openapi_token=sekret" not in set_cookie
        assert "Max-Age=900" in set_cookie
    else:
        assert "openapi_token=" in set_cookie


def test_documentation_page_deletes_cookie_when_builder_returns_none(admin_client, monkeypatch):
    """Validate Documentation page deletes cookie when builder returns none."""
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"t": "sekret"})
    monkeypatch.setattr(admin_routes, "build_openapi_cookie_value", lambda _token: None)

    resp = admin_client.get("/admin/docs")
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie", "")
    assert "openapi_token=" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_admin_dashboard_logs_csv_errors(admin_client, clean_state, monkeypatch, tmp_path):
    """Validate Admin dashboard logs csv errors."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text("task_id,date\n1,2026-02-03\n")

    # Ensure csv_path.exists() is True and DictReader raises to hit error branch
    monkeypatch.setattr(admin_routes, "Path", lambda *_a, **_k: csv_dir)

    def _raising_reader(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(admin_routes.csv, "DictReader", _raising_reader)

    resp = admin_client.get("/admin")
    assert resp.status_code == 200


def test_admin_dashboard_counts_tasks_this_month_from_csv(
    admin_client, clean_state, monkeypatch, tmp_path
):
    """Validate Admin dashboard counts tasks this month from csv."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    month_key = datetime.now().strftime("%Y-%m")
    csv_content = "task_id,date\n" f"1,{month_key}-01\n" f"2,{month_key}-15\n" "3,1999-01-01\n"
    (csv_dir / "task_stats.csv").write_text(csv_content, encoding="utf-8")

    monkeypatch.setattr(admin_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/admin")
    assert resp.status_code == 200
    assert "2 tasks this month" in resp.text


def test_statistics_helpers_cover_branches(tmp_path, monkeypatch):
    """Validate Statistics helpers cover branches."""
    missing = tmp_path / "missing.csv"
    assert statistics_routes._load_task_stats_csv(missing) == []

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("task_id,date\n1,2026-02-01\n")

    def _raising_reader(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(statistics_routes.csv, "DictReader", _raising_reader)
    assert statistics_routes._load_task_stats_csv(csv_path) == []

    counter_out = statistics_routes._sorted_counter(statistics_routes.Counter({"b": 1, "a": 2}))
    assert counter_out[0]["label"] == "a"
    assert counter_out[0]["count"] == 2

    assert statistics_routes._parse_iso_date("2026-02-01") == date(2026, 2, 1)
    assert statistics_routes._parse_iso_date("2026-02-31") is None
    assert statistics_routes._parse_iso_date(None) is None

    rows = [
        {"date": "2026-02-01"},
        {"date": "2026-02-10"},
        {"date": "invalid"},
        {"date": ""},
    ]
    start_bound, end_bound = statistics_routes._available_date_bounds(rows)
    assert start_bound == date(2026, 2, 1)
    assert end_bound == date(2026, 2, 10)

    assert statistics_routes._filter_rows_by_date_range(rows, None, None) == rows
    assert statistics_routes._filter_rows_by_date_range(rows, date(2026, 2, 5), None) == [
        {"date": "2026-02-10"}
    ]
    assert statistics_routes._normalize_date_range(
        date(2026, 3, 1),
        date(2026, 2, 1),
    ) == (date(2026, 2, 1), date(2026, 3, 1))
    assert (
        statistics_routes._download_filename(
            "20260709",
            date(2026, 2, 1),
            date(2026, 3, 1),
        )
        == "task_stats_20260709_from_20260201_to_20260301.csv"
    )
    assert (
        statistics_routes._csv_rows_to_text(
            [{"task_id": "1", "date": "2026-02-01", "ignored": "x"}],
            ["task_id", "date"],
        )
        == "task_id,date\n1,2026-02-01\n"
    )


def test_statistics_dashboard_renders_with_data(admin_client, monkeypatch, tmp_path):
    """Validate Statistics dashboard renders with data."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text(
        "task_id,date,task_type,etab_name\n" "1,2026-02-01,encode,UM\n" "2,2026-02-02,other,UA\n"
    )

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics")
    assert resp.status_code == 200
    assert "2026-02-01" in resp.text
    assert "2026-02-02" in resp.text
    assert "2026-02-01 \u2192 2026-02-02" in resp.text
    assert "encode" in resp.text
    assert "Download CSV" in resp.text
    assert "/statistics/task-stats.csv" in resp.text


def test_statistics_dashboard_filters_by_date_range(admin_client, monkeypatch, tmp_path):
    """Validate Statistics dashboard filters by date range."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text(
        "task_id,date,task_type,etab_name\n"
        "1,2026-02-01,encode,UM\n"
        "2,2026-02-10,other,UA\n"
        "3,2026-03-01,encode,UB\n"
        "4,,encode,UC\n"
    )

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics?start_date=2026-02-05&end_date=2026-02-28")
    assert resp.status_code == 200
    assert 'name="start_date"' in resp.text
    assert 'name="end_date"' in resp.text
    assert 'value="2026-02-05"' in resp.text
    assert 'value="2026-02-28"' in resp.text
    assert "2026-02-05 \u2192 2026-02-28" in resp.text
    assert "Download filtered CSV" in resp.text
    assert "/statistics/task-stats.csv?start_date=2026-02-05&amp;end_date=2026-02-28" in resp.text

    data_match = re.search(
        r'<script id="statistics-data" type="application/json">\s*(.*?)\s*</script>',
        resp.text,
        re.DOTALL,
    )
    assert data_match is not None
    stats_data = json.loads(data_match.group(1))
    assert stats_data["by_date"] == [{"label": "2026-02-10", "count": 1}]
    assert stats_data["by_type"] == [{"label": "other", "count": 1}]
    assert stats_data["by_etab"] == [{"label": "UA", "count": 1}]


def test_statistics_csv_download_returns_attachment(admin_client, monkeypatch, tmp_path):
    """Validate Statistics csv download returns attachment."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    csv_content = "task_id,date,task_type,etab_name\n1,2026-02-01,encode,UM\n"
    (csv_dir / "task_stats.csv").write_text(csv_content)

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv")
    assert resp.status_code == 200
    assert resp.text == csv_content

    content_disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in content_disposition
    assert re.search(r"task_stats_\d{8}\.csv", content_disposition)


def test_statistics_csv_download_can_filter_by_date_range(admin_client, monkeypatch, tmp_path):
    """Validate statistics CSV download can export the active date range."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text(
        "task_id,date,task_type,etab_name\n"
        "1,2026-02-01,encode,UM\n"
        "2,2026-02-10,other,UA\n"
        "3,2026-03-01,encode,UB\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv?start_date=2026-02-05&end_date=2026-02-28")
    assert resp.status_code == 200
    assert resp.text == "task_id,date,task_type,etab_name\n2,2026-02-10,other,UA\n"

    content_disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in content_disposition
    assert re.search(
        r"task_stats_\d{8}_from_20260205_to_20260228\.csv",
        content_disposition,
    )


def test_statistics_csv_download_returns_404_when_missing(admin_client, monkeypatch, tmp_path):
    """Validate Statistics csv download returns 404 when missing."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task stats CSV not found"
