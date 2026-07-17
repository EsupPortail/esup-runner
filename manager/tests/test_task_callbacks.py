"""Tests for task completion callbacks and notifications."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from task_routes_helpers import (
    clean_state,
    client,
)
from task_routes_helpers import fake_resolve_public_ips as _fake_resolve_public_ips
from task_routes_helpers import make_runner as _runner
from task_routes_helpers import make_task as _task
from task_routes_helpers import (
    task_module,
)

from app.core.auth import verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Task, TaskCompletionNotification

__all__ = ["clean_state", "client", "task_module"]


def test_append_task_stats_csv_handles_invalid_date(task_module, tmp_path, monkeypatch):
    """Validate Append task stats csv handles invalid date."""
    monkeypatch.setattr(task_module, "_is_pytest_run", lambda: False)
    monkeypatch.setattr(task_module, "PathlibPath", lambda path="": tmp_path / path)

    task = Task(
        task_id="t-invalid",
        runner_id="r1",
        status="completed",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at="not-a-date",
        updated_at="2026-02-02T00:00:00",
        error=None,
        script_output=None,
    )

    task_module._append_task_stats_csv(task)

    csv_path = tmp_path / "data" / "task_stats.csv"
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("task_id,date,task_type,status,app_name,app_version,etab_name")

    row = lines[1].split(",")
    # Invalid created_at should result in an empty date column
    assert row[0] == "t-invalid"
    assert row[1] == ""


def test_append_task_stats_csv_skips_during_pytest(task_module, tmp_path, monkeypatch):
    """Validate Append task stats csv skips during pytest."""
    monkeypatch.setattr(task_module, "_is_pytest_run", lambda: True)
    monkeypatch.setattr(task_module, "PathlibPath", lambda path="": tmp_path / path)

    task = Task(
        task_id="test-task-1",
        runner_id="r1",
        status="completed",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at="2026-02-02T00:00:00",
        updated_at="2026-02-02T00:00:00",
        error=None,
        script_output=None,
    )

    task_module._append_task_stats_csv(task)

    csv_path = tmp_path / "data" / "task_stats.csv"
    assert not csv_path.exists()


def test_append_task_stats_csv_skips_quick_manual_test_etab(task_module, tmp_path, monkeypatch):
    """Validate Append task stats csv skips quick manual test etab."""
    monkeypatch.setattr(task_module, "_is_pytest_run", lambda: False)
    monkeypatch.setattr(task_module, "PathlibPath", lambda path="": tmp_path / path)

    task = Task(
        task_id="manual-check-task-1",
        runner_id="r1",
        status="completed",
        etab_name=" Quick manual test ",
        app_name="check_pipeline_tasks.py",
        app_version="0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation="manual-test",
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at="2026-02-02T00:00:00",
        updated_at="2026-02-02T00:00:00",
        error=None,
        script_output=None,
    )

    task_module._append_task_stats_csv(task)

    csv_path = tmp_path / "data" / "task_stats.csv"
    assert not csv_path.exists()


def test_task_completion_appends_stats_errors_are_logged(
    client, clean_state, monkeypatch, task_module
):
    # Avoid real persistence and notifications
    """Validate Task completion appends stats errors are logged."""
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    async def _send_ok(*_, **__):
        return True, None

    monkeypatch.setattr(task_module, "_send_notify_callback", _send_ok)
    monkeypatch.setattr(
        task_module,
        "_append_task_stats_csv",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("csv-fail")),
    )

    # Runner/token setup
    runner_token = "runner-token"
    test_task_id = "test-task-1"
    runners["r1"] = _runner("r1", token=runner_token)

    now = datetime.now().isoformat()
    tasks[test_task_id] = Task(
        task_id=test_task_id,
        runner_id="r1",
        status="running",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )

    # Ensure dependency override returns the same token as the runner
    previous_override = app.dependency_overrides.get(verify_token)
    app.dependency_overrides[verify_token] = lambda: runner_token

    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": test_task_id,
                "status": "completed",
                "error_message": None,
                "script_output": "ok",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(verify_token, None)
        else:
            app.dependency_overrides[verify_token] = previous_override


# -----------------------------
# Notify helpers
# -----------------------------


@pytest.mark.asyncio
async def test_send_notify_callback_success(monkeypatch, task_module, clean_state):
    """Validate Send notify callback success."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed", notify_url="https://example.com/notify")

    notification = TaskCompletionNotification(
        task_id="t1",
        status="completed",
        error_message=None,
        script_output="ok",
    )

    class FakeResponse:
        status_code = 204
        text = "ok"

    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *_, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            captured["content"] = kwargs.get("content")
            return FakeResponse()

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    ok, err = await task_module._send_notify_callback(tasks["t1"], notification)
    assert ok is True
    assert err is None
    assert captured["url"] == "https://example.com/notify"
    assert "Authorization" not in (captured["headers"] or {})
    assert captured["headers"].get("Content-Type") == "application/json"
    assert isinstance(captured["content"], (bytes, bytearray))
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 15.0
    assert captured["timeout"].write == 5.0
    assert captured["timeout"].pool == 5.0


@pytest.mark.asyncio
async def test_send_notify_callback_non_2xx_returns_error(monkeypatch, task_module, clean_state):
    """Validate Send notify callback non 2xx returns error."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed", notify_url="https://example.com/notify")

    notification = TaskCompletionNotification(
        task_id="t1",
        status="completed",
        error_message=None,
        script_output=None,
    )

    class FakeResponse:
        status_code = 500
        text = "nope"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    ok, err = await task_module._send_notify_callback(tasks["t1"], notification)
    assert ok is False
    assert err and "500" in err


@pytest.mark.asyncio
async def test_send_notify_callback_read_timeout_stops_retries(
    monkeypatch, task_module, clean_state
):
    """Treat an ambiguous read timeout as delivered to prevent duplicate processing."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed", notify_url="https://example.com/notify")

    notification = TaskCompletionNotification(
        task_id="t1",
        status="completed",
        error_message=None,
        script_output=None,
    )

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **_kwargs):
            request = task_module.httpx.Request("POST", url)
            raise task_module.httpx.ReadTimeout("read timed out", request=request)

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    ok, err = await task_module._send_notify_callback(tasks["t1"], notification)

    assert ok is True
    assert err is None


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_task_missing(task_module, clean_state):
    """Validate Retry notify callback returns when task missing."""
    await task_module._retry_notify_callback(
        "missing", TaskCompletionNotification(task_id="missing", status="completed")
    )


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_no_notify_url(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback returns when no notify url."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].notify_url = None

    called = {"count": 0}

    async def fake_send(*_a, **_k):
        called["count"] += 1
        return True, None

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_retry_notify_callback_succeeds_after_retry(monkeypatch, task_module, clean_state):
    """Validate Retry notify callback succeeds after retry."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 2)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    attempts: list[bool] = [False, True]

    async def fake_send(*_a, **_k):
        ok = attempts.pop(0)
        return ok, None

    restored: dict[str, Any] = {"called": False}

    def fake_restore(task_id: str, notification: TaskCompletionNotification) -> None:
        restored["called"] = True
        tasks[task_id].status = notification.status

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "_restore_status_after_notify", fake_restore)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert restored["called"] is True
    assert tasks["t1"].status == "completed"


@pytest.mark.asyncio
async def test_retry_notify_callback_exhausts_and_handles_exceptions(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback exhausts and handles exceptions."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_send(*_a, **_k):
        raise RuntimeError("boom")

    emailed: dict[str, Any] = {"called": False}

    async def fake_email(**_kwargs):
        emailed["called"] = True
        return True

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "send_notify_retry_exhausted_email", fake_email)

    # Should swallow exceptions and just exhaust retries.
    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert emailed["called"] is True


@pytest.mark.asyncio
async def test_retry_notify_callback_does_not_email_when_status_not_warning(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback does not email when status not warning."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_send(*_a, **_k):
        tasks["t1"].status = "completed"
        return False, "nope"

    emailed: dict[str, Any] = {"called": False}

    async def fake_email(**_kwargs):
        emailed["called"] = True
        return True

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "send_notify_retry_exhausted_email", fake_email)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert emailed["called"] is False


@pytest.mark.asyncio
async def test_retry_notify_callback_ignores_email_errors_after_exhaustion(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback ignores email errors after exhaustion."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_send(*_a, **_k):
        return False, "nope"

    called: dict[str, Any] = {"email": 0}

    async def fake_email(**_kwargs):
        called["email"] += 1
        raise RuntimeError("smtp down")

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "send_notify_retry_exhausted_email", fake_email)

    # Email errors must be swallowed after retries are exhausted.
    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert called["email"] == 1


@pytest.mark.asyncio
async def test_retry_notify_callback_sleeps_when_delay_positive(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback sleeps when delay positive."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")

    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 2)

    slept: dict[str, Any] = {"seconds": None}

    async def fake_sleep(seconds: float):
        slept["seconds"] = seconds

    async def fake_send(*_a, **_k):
        return False, "nope"

    async def fake_email(**_kwargs):
        return True

    monkeypatch.setattr(task_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "send_notify_retry_exhausted_email", fake_email)

    await task_module._retry_notify_callback(
        "t1", TaskCompletionNotification(task_id="t1", status="completed")
    )
    assert slept["seconds"] == 1


def test_task_run_matches_expected_run_id(task_module):
    """Validate Task run matches expected run id."""
    task = _task("t1", "r1", status="completed")
    task.run_id = "run-1"

    assert task_module._task_run_matches(task, "run-1") is True
    assert task_module._task_run_matches(task, "run-2") is False


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_task_becomes_stale_after_sleep(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback returns when task becomes stale after sleep."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")
    tasks["t1"].run_id = "run-1"

    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_sleep(_seconds: float):
        tasks["t1"].run_id = "run-2"

    called = {"count": 0}

    async def fake_send(*_a, **_k):
        called["count"] += 1
        return True, None

    monkeypatch.setattr(task_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)

    await task_module._retry_notify_callback(
        "t1",
        TaskCompletionNotification(task_id="t1", status="completed"),
        expected_run_id="run-1",
    )
    assert called["count"] == 0


@pytest.mark.asyncio
async def test_retry_notify_callback_returns_when_run_changes_after_notify_success(
    monkeypatch, task_module, clean_state
):
    """Validate Retry notify callback returns when run changes after notify success."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning", notify_url="https://example.com/notify")
    tasks["t1"].run_id = "run-1"

    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1)

    async def fake_send(*_a, **_k):
        tasks["t1"].run_id = "run-2"
        return True, None

    restored = {"called": False}

    def fake_restore(*_a, **_k):
        restored["called"] = True

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "_restore_status_after_notify", fake_restore)

    await task_module._retry_notify_callback(
        "t1",
        TaskCompletionNotification(task_id="t1", status="completed"),
        expected_run_id="run-1",
    )
    assert restored["called"] is False


def test_set_notify_warning_failure_without_existing_error(monkeypatch, task_module, clean_state):
    """Validate Set notify warning failure without existing error."""
    tasks["t1"] = _task("t1", "r1", status="failed")
    tasks["t1"].error = None
    previous_updated_at = tasks["t1"].updated_at

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)

    task_module._set_notify_warning("t1", "notify callback failed")

    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "notify callback failed"
    assert tasks["t1"].updated_at != previous_updated_at
    assert called["count"] == 1


def test_restore_status_after_notify_sets_error_for_non_completed(
    monkeypatch, task_module, clean_state
):
    """Validate Restore status after notify sets error for non completed."""
    tasks["t1"] = _task("t1", "r1", status="warning")
    tasks["t1"].error = None

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)

    task_module._restore_status_after_notify(
        "t1",
        TaskCompletionNotification(
            task_id="t1",
            status="failed",
            error_message="runner failed",
            script_output=None,
        ),
    )

    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "runner failed"
    assert called["count"] == 1


# -----------------------------
# Web UI endpoints
# -----------------------------


@pytest.mark.parametrize("invalid_status", ["pending", "running", "warning", "error"])
def test_task_completion_rejects_non_terminal_status(
    invalid_status, client, task_module, clean_state
):
    """Reject invalid completion states before mutating task or runner state."""
    runner = _runner("r1", token="tok")
    runner.availability = "busy"
    runners["r1"] = runner
    tasks["t1"] = _task("t1", "r1", status="running")

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        response = client.post(
            "/task/completion",
            json={"task_id": "t1", "status": invalid_status},
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert response.status_code == 422
    assert tasks["t1"].status == "running"
    assert runners["r1"].availability == "busy"


def test_task_completion_404_task(client, task_module, clean_state):
    """Validate Task completion 404 task."""
    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post("/task/completion", json={"task_id": "nope", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True
    assert resp.status_code == 404


def test_task_completion_notify_ok(monkeypatch, client, task_module, clean_state):
    """Validate Task completion notify ok."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return True, None

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": "out",
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "completed"
    assert tasks["t1"].script_output == "out"
    assert runners["r1"].availability == "available"
    assert "coro" not in scheduled  # no retries scheduled


def test_task_completion_saves_before_notify(monkeypatch, client, task_module, clean_state):
    """Validate Task completion saves before notify."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    events = []

    def fake_save():
        events.append("save")
        return True

    async def fake_handle(_task: Task, _notification: TaskCompletionNotification):
        events.append("notify")

    monkeypatch.setattr(task_module, "save_tasks", fake_save)
    monkeypatch.setattr(task_module, "_handle_notify_callback", fake_handle)
    monkeypatch.setattr(task_module, "_append_task_stats_csv", lambda *_: None)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert events
    assert events[0] == "save"
    assert "notify" in events


def test_task_completion_notify_non_2xx_sets_warning_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    """Validate Task completion notify non 2xx sets warning and schedules retry."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return False, "nope"

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "warning"
    assert "coro" in scheduled
    # The retry coroutine is scheduled but not awaited in this unit test; close it to
    # avoid "coroutine was never awaited" warnings.
    scheduled["coro"].close()


def test_task_completion_failed_sets_error_message(client, task_module, clean_state):
    """Validate Task completion failed sets error message."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running")
    tasks["t1"].notify_url = None

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "failed",
                "error_message": "boom",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "failed"
    assert tasks["t1"].error == "boom"


def test_task_completion_timeout_sets_error_message(client, task_module, clean_state):
    """Validate Task completion timeout sets error message."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running")
    tasks["t1"].notify_url = None

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "timeout",
                "error_message": "download timeout",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "timeout"
    assert tasks["t1"].error == "download timeout"


def test_task_completion_notify_exception_sets_warning_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    """Validate Task completion notify exception sets warning and schedules retry."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(*_a, **_k):
        raise RuntimeError("boom")

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "completed",
                "error_message": None,
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "warning"
    assert tasks["t1"].error and "server error" in tasks["t1"].error
    assert "coro" in scheduled
    scheduled["coro"].close()


@pytest.mark.asyncio
async def test_handle_notify_callback_ignores_stale_run_after_success(
    monkeypatch, task_module, clean_state
):
    """Validate Handle notify callback ignores stale run after success."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")
    tasks["t1"].run_id = "run-new"
    stale_task = Task(**tasks["t1"].model_dump())
    stale_task.run_id = "run-old"

    async def fake_send(*_a, **_k):
        return True, None

    restored = {"called": False}
    warned = {"called": False}

    def fake_restore(*_a, **_k):
        restored["called"] = True

    def fake_warn(*_a, **_k):
        warned["called"] = True

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "_restore_status_after_notify", fake_restore)
    monkeypatch.setattr(task_module, "_set_notify_warning", fake_warn)

    await task_module._handle_notify_callback(
        stale_task,
        TaskCompletionNotification(task_id="t1", status="completed", error_message=None),
    )

    assert restored["called"] is False
    assert warned["called"] is False


@pytest.mark.asyncio
async def test_handle_notify_callback_ignores_stale_run_after_exception(
    monkeypatch, task_module, clean_state
):
    """Validate Handle notify callback ignores stale run after exception."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")
    tasks["t1"].run_id = "run-new"
    stale_task = Task(**tasks["t1"].model_dump())
    stale_task.run_id = "run-old"

    async def fake_send(*_a, **_k):
        raise RuntimeError("boom")

    warned = {"called": False}
    scheduled = {"count": 0}

    def fake_warn(*_a, **_k):
        warned["called"] = True

    def fake_create_task(_coro):
        scheduled["count"] += 1
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module, "_set_notify_warning", fake_warn)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    await task_module._handle_notify_callback(
        stale_task,
        TaskCompletionNotification(task_id="t1", status="completed", error_message=None),
    )

    assert warned["called"] is False
    assert scheduled["count"] == 0


def test_task_completion_timeout_notify_non_2xx_keeps_timeout_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    """Validate Task completion timeout notify non 2xx keeps timeout and schedules retry."""
    runners["r1"] = _runner("r1", token="tok")
    tasks["t1"] = _task("t1", "r1", status="running", notify_url="https://example.com/notify")

    async def fake_send(task: Task, notification: TaskCompletionNotification):
        return False, "nope"

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        return SimpleNamespace()

    monkeypatch.setattr(task_module, "_send_notify_callback", fake_send)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post(
            "/task/completion",
            json={
                "task_id": "t1",
                "status": "timeout",
                "error_message": "download timeout",
                "script_output": None,
            },
        )
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 200
    assert tasks["t1"].status == "timeout"
    assert "download timeout" in (tasks["t1"].error or "")
    assert "Notify callback warning:" in (tasks["t1"].error or "")
    assert "nope" in (tasks["t1"].error or "")
    assert "coro" in scheduled
    scheduled["coro"].close()


def test_task_completion_403_token_mismatch(client, task_module, clean_state):
    """Validate Task completion 403 token mismatch."""
    runners["r1"] = _runner("r1", token="tok-real")
    tasks["t1"] = _task("t1", "r1", status="running")

    app.dependency_overrides[verify_token] = lambda: "tok-bad"
    try:
        resp = client.post("/task/completion", json={"task_id": "t1", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 403


def test_task_completion_404_runner_missing(client, task_module, clean_state):
    """Validate Task completion 404 runner missing."""
    tasks["t1"] = _task("t1", "r1", status="running")

    app.dependency_overrides[verify_token] = lambda: "tok"
    try:
        resp = client.post("/task/completion", json={"task_id": "t1", "status": "completed"})
    finally:
        app.dependency_overrides[verify_token] = lambda: True

    assert resp.status_code == 404
