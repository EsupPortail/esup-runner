"""Regression tests for retained, acknowledged task incidents."""

from datetime import datetime, timedelta

import pytest
from jinja2 import Environment, FileSystemLoader
from task_routes_helpers import clean_state, client, make_task, task_module

from app.api.routes import admin as admin_routes
from app.core import state
from app.core.auth import verify_admin
from app.core.persistence import SafeDailyJSONPersistence
from app.main import app
from app.models.models import Task, TaskCompletionNotification

__all__ = ["clean_state", "client", "task_module"]


@pytest.fixture
def acknowledgment_client(client, clean_state, monkeypatch, task_module, tmp_path):
    """Use real task persistence in an isolated directory."""
    monkeypatch.setattr(state, "persistence", SafeDailyJSONPersistence(str(tmp_path)))
    monkeypatch.setattr(task_module, "save_tasks", state.save_tasks)
    return client


@pytest.mark.parametrize("task_status", ["failed", "warning", "timeout"])
def test_acknowledgment_retains_history_and_is_idempotent(acknowledgment_client, task_status):
    task = make_task("incident", "r1", status=task_status)
    task.error = "Original execution error"
    task.script_output = "Original output"
    state.tasks[task.task_id] = task
    before = task.model_dump()

    response = acknowledgment_client.post("/task/acknowledge/incident")

    assert response.status_code == 200
    payload = response.json()
    assert payload["acknowledged_at"] is not None
    assert payload["updated_at"] == payload["acknowledged_at"]
    for field, value in before.items():
        if field not in {"updated_at", "acknowledged_at"}:
            assert payload[field] == value
    assert state.persistence.load_task("incident") == payload

    task_status_response = acknowledgment_client.get("/task/status/incident")
    assert task_status_response.status_code == 200
    assert task_status_response.json()["acknowledged_at"] == payload["acknowledged_at"]
    assert task_status_response.json()["status"] == task_status

    repeated = acknowledgment_client.post("/task/acknowledge/incident")
    assert repeated.status_code == 200
    assert repeated.json() == payload


@pytest.mark.parametrize("task_status", ["pending", "running", "completed"])
def test_acknowledgment_rejects_non_incidents(acknowledgment_client, task_status):
    task = make_task("task", "r1", status=task_status)
    state.tasks[task.task_id] = task
    before = task.model_dump()

    response = acknowledgment_client.post("/task/acknowledge/task")

    assert response.status_code == 409
    assert state.tasks[task.task_id].model_dump() == before
    assert state.persistence.load_task(task.task_id) is None


def test_acknowledgment_rejects_missing_task(acknowledgment_client):
    response = acknowledgment_client.post("/task/acknowledge/missing")
    assert response.status_code == 404
    assert state.tasks == {}


@pytest.mark.parametrize("raises", [False, True])
def test_acknowledgment_restores_memory_on_save_failure(
    acknowledgment_client, monkeypatch, task_module, raises
):
    task = make_task("incident", "r1", status="failed")
    state.tasks[task.task_id] = task
    assert state.save_tasks()
    before = task.model_dump()

    def fail_save():
        if raises:
            raise OSError("Disk full")
        return False

    monkeypatch.setattr(task_module, "save_tasks", fail_save)
    response = acknowledgment_client.post("/task/acknowledge/incident")

    assert response.status_code == 503
    assert state.tasks[task.task_id].model_dump() == before
    assert state.persistence.load_task(task.task_id) == before


def test_acknowledgment_survives_reload_and_refreshes_another_worker(
    acknowledgment_client, monkeypatch
):
    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    task = make_task("incident", "r1", status="failed")
    state.tasks[task.task_id] = task
    assert state.save_tasks()
    sibling_snapshot = task.model_copy(deep=True)

    response = acknowledgment_client.post("/task/acknowledge/incident")
    assert response.status_code == 200
    timestamp = response.json()["acknowledged_at"]

    state.tasks[task.task_id] = sibling_snapshot
    refreshed = state.get_task(task.task_id)
    assert refreshed is not None
    assert refreshed.acknowledged_at == timestamp
    assert state.save_tasks()

    state.tasks.clear()
    reloaded = state.get_tasks_snapshot()[task.task_id]
    assert reloaded.acknowledged_at == timestamp
    assert reloaded.status == "failed"


def test_duplicate_completion_preserves_acknowledgment(acknowledgment_client, task_module):
    task = make_task("incident", "r1", status="failed")
    task.run_id = "execution"
    task.error = "Execution failed"
    state.tasks[task.task_id] = task
    response = acknowledgment_client.post("/task/acknowledge/incident")
    assert response.status_code == 200

    refreshed = state.tasks[task.task_id]
    task_module._apply_task_completion_update(
        refreshed,
        TaskCompletionNotification(
            task_id=task.task_id,
            run_id=task.run_id,
            status="failed",
            error_message=task.error,
        ),
    )
    assert state.save_tasks()
    assert state.tasks[task.task_id].acknowledged_at == response.json()["acknowledged_at"]


@pytest.mark.parametrize("operation", ["lookup", "snapshot", "save"])
def test_late_worker_update_preserves_acknowledgment(
    acknowledgment_client, monkeypatch, task_module, operation
):
    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    task = make_task("incident", "r1", status="failed")
    task.run_id = "execution"
    state.tasks[task.task_id] = task
    assert state.save_tasks()
    stale_worker_task = task.model_copy(deep=True)
    response = acknowledgment_client.post("/task/acknowledge/incident")
    assert response.status_code == 200

    task_module._apply_task_completion_update(
        stale_worker_task,
        TaskCompletionNotification(task_id=task.task_id, run_id=task.run_id, status="failed"),
    )
    state.tasks[task.task_id] = stale_worker_task
    if operation == "lookup":
        state.get_task(task.task_id)
    elif operation == "snapshot":
        state.get_tasks_snapshot()
    else:
        assert state.save_tasks()

    assert state.tasks[task.task_id].acknowledged_at == response.json()["acknowledged_at"]
    assert state.tasks[task.task_id].updated_at == stale_worker_task.updated_at


@pytest.mark.parametrize("newest_is_local", [False, True])
def test_acknowledgment_merge_is_scoped_to_execution(newest_is_local):
    previous = make_task("incident", "r1", status="failed")
    previous.run_id = "previous"
    previous.acknowledged_at = previous.updated_at
    restarted = previous.model_copy(
        update={
            "run_id": "new-execution",
            "acknowledged_at": None,
            "status": "running",
            "updated_at": (datetime.now() + timedelta(seconds=1)).isoformat(),
        }
    )
    local, persisted = (restarted, previous) if newest_is_local else (previous, restarted)
    merged = state._pick_newest_task(local, persisted)
    assert merged is restarted
    assert merged.acknowledged_at is None


def test_acknowledged_incident_leaves_dashboard_but_stays_in_task_history(acknowledgment_client):
    task = make_task("incident", "r1", status="failed")
    task.error = "Original execution error"
    state.tasks[task.task_id] = task
    before = acknowledgment_client.get("/admin")
    assert 'data-acknowledge-task-id="incident"' in before.text

    assert acknowledgment_client.post("/task/acknowledge/incident").status_code == 200

    dashboard = acknowledgment_client.get("/admin")
    assert dashboard.status_code == 200
    assert 'id="needs-attention"' not in dashboard.text
    assert 'data-task-detail-url="/admin/task/incident"' in dashboard.text

    history = acknowledgment_client.get("/tasks?status=failed")
    assert history.status_code == 200
    assert "incident" in history.text
    assert ">acknowledged</span>" in history.text
    assert ">failed</span>" in history.text
    assert state.tasks[task.task_id].error == task.error


def test_attention_counts_exclude_acknowledged_incidents_and_keep_running_alerts():
    now = datetime.now()
    acknowledged = [
        {"id": status, "status": status, "acknowledged_at": now.isoformat()}
        for status in ["failed", "warning", "timeout"]
    ]
    pending_attention = [
        {"id": "new-failure", "status": "failed"},
        {
            "id": "stale",
            "status": "running",
            "updated_at": (now - timedelta(hours=6)).isoformat(),
        },
    ]
    summary = admin_routes._build_attention_summary(
        [{"id": "offline", "status": "offline", "age_seconds": 100}],
        acknowledged + pending_attention,
        now=now,
    )
    assert summary["attention_count"] == 3
    assert summary["attention_task_status_counts"] == {"failed": 1, "warning": 0, "timeout": 0}
    assert {task["id"] for task in summary["attention_tasks"]} == {"new-failure", "stale"}


@pytest.mark.parametrize("task_status", ["failed", "warning", "timeout", "running", "completed"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_task_detail_renders_acknowledgment_state(task_status, acknowledged):
    task = make_task("incident", "r1", status=task_status)
    if acknowledged:
        task.acknowledged_at = "2026-01-01T12:00:00"
    template = Environment(loader=FileSystemLoader("app/web/templates")).get_template(
        "task_detail.html"
    )
    html = template.render(
        task=task,
        task_actions=admin_routes._build_task_detail_actions(task),
        root_path="/manager",
        csrf_token="test-csrf-token",
        version="test",
    )
    eligible = task_status in {"failed", "warning", "timeout"} and not acknowledged
    assert ('data-acknowledge-task-id="incident"' in html) == eligible
    assert (">acknowledged</span>" in html) == acknowledged
    if eligible:
        assert 'data-acknowledge-url="/manager/task/acknowledge/incident"' in html


def test_acknowledgment_requires_admin_authentication(acknowledgment_client):
    app.dependency_overrides.pop(verify_admin)
    response = acknowledgment_client.post("/task/acknowledge/incident")
    assert response.status_code == 401
    assert state.tasks == {}


def test_existing_task_records_default_to_unacknowledged():
    task = make_task("incident", "r1", status="failed")
    record = task.model_dump(exclude={"acknowledged_at"})
    assert Task(**record).acknowledged_at is None
