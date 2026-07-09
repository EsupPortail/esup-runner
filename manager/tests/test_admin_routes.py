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
    assert "Restart this task?" in html
    assert "This failed task can be deleted or restarted." in html
    assert "text-subtle fst-italic" in html
    assert "This failed task cannot be deleted or restarted." not in html


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
                "updated_at_display": "2026-01-01 00:00:30",
                "error_label": "Encoding aborted.",
                "stale_running_label": "",
                "video_id": "video-123",
            },
            {
                "id": "stale-running-task",
                "status": "running",
                "task_type": "encoding",
                "runner_id": "runner-2",
                "updated_at_display": "2026-01-01 00:00:30",
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
    runners["r1"] = _make_runner("r1")

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


def test_statistics_csv_download_returns_404_when_missing(admin_client, monkeypatch, tmp_path):
    """Validate Statistics csv download returns 404 when missing."""
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task stats CSV not found"
