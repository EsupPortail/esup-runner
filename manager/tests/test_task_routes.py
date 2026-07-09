"""Coverage-oriented tests for app.api.routes.task.

These tests are intentionally pragmatic: they mock httpx/network + persistence
so we can cover all branches deterministically.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.core.auth import verify_admin, verify_token
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task, TaskCompletionNotification, TaskRequest
from app.services import background_service


@pytest.fixture
def task_module():
    from app.api.routes import task as task_module  # type: ignore

    return task_module


@pytest.fixture
def client(monkeypatch, task_module):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: "test-token"
    app.dependency_overrides[verify_admin] = lambda: True

    # Neutralise persistence for tests.
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(verify_token, None)
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


def _runner(runner_id: str, *, url: str = "http://r1.example", token: str = "tok") -> Runner:
    return Runner(
        id=runner_id,
        url=url,
        task_types=["encoding", "ingest"],
        token=token,
        version="1.0.0",
        last_heartbeat=datetime.now() - timedelta(seconds=1),
        availability="available",
        status="online",
    )


def _task(task_id: str, runner_id: str, *, status: str, notify_url: str | None = None) -> Task:
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
        parameters={},
        notify_url=notify_url or "https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )


async def _fake_resolve_public_ips(_host: str) -> list[str]:
    return ["93.184.216.34"]


def test_runner_auth_headers_raises_when_runner_token_missing(task_module):
    """Validate Runner auth headers raises when runner token missing."""
    runner = Runner(id="r1", url="http://r1.example", task_types=["encoding"], token=None)

    with pytest.raises(HTTPException) as exc:
        task_module._runner_auth_headers(runner, accept="application/json")

    assert exc.value.status_code == 503


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
        status_code = 200
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


@pytest.mark.asyncio
async def test_send_notify_callback_non_200_returns_error(monkeypatch, task_module, clean_state):
    """Validate Send notify callback non 200 returns error."""
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


def test_normalize_task_ids_filters_invalid_empty_and_duplicates(task_module):
    """Validate Normalize task ids filters invalid empty and duplicates."""
    normalized = task_module._normalize_task_ids(["  ", "t1", "t1", " t2 ", 123, "t2"])
    assert normalized == ["t1", "t2"]


def test_http_exception_detail_to_text_variants(task_module):
    """Validate Http exception detail to text variants."""
    assert task_module._http_exception_detail_to_text("simple") == "simple"
    assert task_module._http_exception_detail_to_text({"detail": "nested"}) == "nested"
    assert task_module._http_exception_detail_to_text({"detail": {"code": 123}}) == "{'code': 123}"
    assert task_module._http_exception_detail_to_text(42) == "42"


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


def test_view_tasks_filters_and_renders(client, clean_state):
    """Validate View tasks filters and renders."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="failed")

    resp = client.get("/tasks?status=completed&limit=10")
    assert resp.status_code == 200
    assert "Tasks Management" in resp.text
    assert "t1" in resp.text
    assert "t2" not in resp.text


def test_view_tasks_search_task_type_and_status_counts_else(
    monkeypatch, client, task_module, clean_state
):
    """Validate View tasks search task type and status counts else."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="failed")
    tasks["t3"] = _task("t3", "r1", status="custom-status")
    tasks["t3"].task_type = "ingest"
    tasks["t3"].etab_name = "Université"

    captured: dict[str, Any] = {}

    def fake_template_response(_request: Any, _name: str, context: dict[str, Any]):
        captured.update(context)
        serializable = dict(context)
        serializable.pop("request", None)
        serializable.pop("now", None)
        return JSONResponse(serializable)

    monkeypatch.setattr(task_module.templates, "TemplateResponse", fake_template_response)

    resp = client.get("/tasks?task_type=ingest&search=univers")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["current_filters"]["task_type"] == "ingest"
    assert any(t["id"] == "t3" for t in payload["tasks"])
    # Exercises the status_counts "else" branch for unknown statuses.
    assert payload["status_counts"]["custom-status"] == 1


def test_view_tasks_search_matches_video_identification_fields(client, clean_state):
    """Validate View tasks search matches video identification fields."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t2"] = _task("t2", "r1", status="completed")
    tasks["t1"].parameters = {
        "video_id": "vid-2026-0001",
        "video_slug": "my-course-video",
        "video_title": "My Course Video",
    }

    resp_by_id = client.get("/tasks?search=vid-2026")
    assert resp_by_id.status_code == 200
    assert "t1" in resp_by_id.text
    assert "t2" not in resp_by_id.text

    resp_by_slug = client.get("/tasks?search=my-course-video")
    assert resp_by_slug.status_code == 200
    assert "t1" in resp_by_slug.text
    assert "t2" not in resp_by_slug.text

    resp_by_title = client.get("/tasks?search=course video")
    assert resp_by_title.status_code == 200
    assert "t1" in resp_by_title.text
    assert "t2" not in resp_by_title.text


def test_get_task_details_api_404(client, clean_state):
    """Validate Get task details api 404."""
    resp = client.get("/tasks/api/nope")
    assert resp.status_code == 404


def test_get_task_details_api_ok(client, clean_state):
    """Validate Get task details api ok."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/tasks/api/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"
    assert "client_token" not in resp.json()


def test_redact_task_for_api_legacy_copy_branch(task_module):
    """Validate Redact task for api legacy copy branch."""

    class LegacyTask:
        def __init__(self):
            self.client_token = "legacy-secret"
            self.copy_deep_arg = None

        def copy(self, deep: bool = False):
            self.copy_deep_arg = deep
            copied = LegacyTask()
            copied.client_token = self.client_token
            return copied

    legacy = LegacyTask()

    redacted = task_module._redact_task_for_api(legacy)  # type: ignore[arg-type]

    assert legacy.copy_deep_arg is True
    assert redacted is not legacy
    assert redacted.client_token is None


def test_delete_selected_tasks_deletes_and_reports(monkeypatch, client, task_module, clean_state):
    """Validate Delete selected tasks deletes and reports."""
    runners["r1"] = _runner("r1")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-failed"] = _task("t-failed", "r1", status="failed")
    tasks["t-running"] = _task("t-running", "r1", status="running")

    deleted_calls: list[str] = []

    def fake_delete(task_id: str) -> bool:
        deleted_calls.append(task_id)
        if task_id == "t-failed":
            return False
        tasks.pop(task_id, None)
        return True

    monkeypatch.setattr(task_module, "delete_task_from_state", fake_delete)

    resp = client.post(
        "/tasks/delete-selected",
        json={"task_ids": ["t-completed", "t-running", "missing", "t-failed"]},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["requested"] == 4
    assert payload["deleted"] == [{"task_id": "t-completed"}]
    assert payload["failed"] == [{"task_id": "t-failed", "reason": "Task deletion failed"}]

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert skipped_by_task_id["missing"] == "Task not found"
    assert "cannot be deleted" in skipped_by_task_id["t-running"]
    assert deleted_calls == ["t-completed", "t-failed"]
    assert "t-completed" not in tasks


def test_delete_selected_tasks_rejects_empty_task_ids(client, clean_state):
    """Validate Delete selected tasks rejects empty task ids."""
    resp = client.post("/tasks/delete-selected", json={"task_ids": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_delete_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate Delete selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.delete_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


def test_delete_selected_tasks_rejects_too_many_task_ids(
    monkeypatch, client, task_module, clean_state
):
    """Validate Delete selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_DELETE_TASKS", 2)
    resp = client.post("/tasks/delete-selected", json={"task_ids": ["a", "b", "c"]})
    assert resp.status_code == 400
    assert "Too many task IDs" in resp.json()["detail"]


def test_delete_selected_tasks_collects_unexpected_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Delete selected tasks collects unexpected failures."""
    tasks["t1"] = _task("t1", "r1", status="completed")

    def fake_delete(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "delete_task_from_state", fake_delete)

    resp = client.post("/tasks/delete-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["deleted"] == []
    assert payload["failed"] == [
        {"task_id": "t1", "reason": "Unexpected error while deleting task"}
    ]


def test_restart_selected_tasks_restarts_and_reports(monkeypatch, client, task_module, clean_state):
    """Validate Restart selected tasks restarts and reports."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t-failed"] = _task("t-failed", "r1", status="failed")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-running"] = _task("t-running", "r1", status="running")
    tasks["t-failed"].client_token = "client-123"

    class FakePingResponse:
        def json(self):
            return {"available": True, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakePingResponse()

    async def _fake_resolve(_host: str):
        return ["93.184.216.34"]

    scheduled = {"count": 0}

    def fake_create_task(coro):
        scheduled["count"] += 1
        # Avoid un-awaited coroutine warnings; we only need to assert scheduling.
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve)
    monkeypatch.setattr(task_module.config, "PRIORITIES_ENABLED", False)

    resp = client.post(
        "/tasks/restart-selected",
        json={"task_ids": ["t-failed", "t-running", "missing", "t-completed"]},
    )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["requested"] == 4
    assert len(payload["restarted"]) == 2
    assert len(payload["skipped"]) == 2
    assert payload["failed"] == []
    assert scheduled["count"] == 2

    restarted_ids = {item["task_id"] for item in payload["restarted"]}
    assert restarted_ids == {"t-failed", "t-completed"}
    assert tasks["t-failed"].client_token == "client-123"
    assert tasks["t-failed"].status == "running"
    assert tasks["t-completed"].status == "running"
    assert tasks["t-failed"].run_id is not None
    assert tasks["t-completed"].run_id is not None

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert skipped_by_task_id["missing"] == "Task not found"
    assert "cannot be restarted" in skipped_by_task_id["t-running"]


def test_restart_selected_tasks_rejects_empty_task_ids(client, clean_state):
    """Validate Restart selected tasks rejects empty task ids."""
    resp = client.post("/tasks/restart-selected", json={"task_ids": []})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_restart_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate Restart selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.restart_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


def test_restart_selected_tasks_rejects_too_many_task_ids(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_RESTART_TASKS", 2)
    resp = client.post("/tasks/restart-selected", json={"task_ids": ["a", "b", "c"]})
    assert resp.status_code == 400
    assert "Too many task IDs" in resp.json()["detail"]


def test_restart_selected_tasks_collects_http_exception_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks collects http exception failures."""
    tasks["t1"] = _task("t1", "r1", status="failed")

    async def fake_queue(*_a, **_k):
        raise HTTPException(status_code=503, detail={"detail": "no runner"})

    monkeypatch.setattr(task_module, "_queue_task_execution", fake_queue)

    resp = client.post("/tasks/restart-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restarted"] == []
    assert payload["failed"] == [{"task_id": "t1", "reason": "no runner"}]


def test_restart_selected_tasks_collects_unexpected_failures(
    monkeypatch, client, task_module, clean_state
):
    """Validate Restart selected tasks collects unexpected failures."""
    tasks["t1"] = _task("t1", "r1", status="failed")

    async def fake_queue(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "_queue_task_execution", fake_queue)

    resp = client.post("/tasks/restart-selected", json={"task_ids": ["t1"]})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["restarted"] == []
    assert payload["failed"] == [
        {"task_id": "t1", "reason": "Unexpected error while restarting task"}
    ]


@pytest.mark.asyncio
async def test_stop_selected_tasks_stops_and_reports(monkeypatch, task_module, clean_state):
    """Validate stop selected tasks proxies running tasks and reports all outcomes."""
    runners["r1"] = _runner("r1")
    tasks["t-running"] = _task("t-running", "r1", status="running")
    tasks["t-fail"] = _task("t-fail", "r1", status="running")
    tasks["t-completed"] = _task("t-completed", "r1", status="completed")
    tasks["t-no-runner"] = _task("t-no-runner", "missing-runner", status="running")

    async def fake_stop(task_id: str, runner: Runner):
        if task_id == "t-fail":
            raise HTTPException(status_code=409, detail="no killable process")
        return SimpleNamespace(status_code=202)

    monkeypatch.setattr(task_module, "_request_runner_task_stop", fake_stop)

    payload = await task_module.stop_selected_tasks(
        {
            "task_ids": [
                "t-running",
                "t-completed",
                "missing",
                "t-no-runner",
                "t-fail",
            ]
        }
    )

    assert payload["requested"] == 5
    assert payload["stopped"] == [
        {"task_id": "t-running", "runner_id": "r1", "runner_status_code": 202}
    ]
    assert tasks["t-running"].status == "running"

    skipped_by_task_id = {item["task_id"]: item["reason"] for item in payload["skipped"]}
    assert "cannot be stopped" in skipped_by_task_id["t-completed"]
    assert skipped_by_task_id["missing"] == "Task not found"

    failed_by_task_id = {item["task_id"]: item["reason"] for item in payload["failed"]}
    assert failed_by_task_id["t-no-runner"] == "Runner not found"
    assert failed_by_task_id["t-fail"] == "no killable process"


@pytest.mark.asyncio
async def test_stop_selected_tasks_collects_unexpected_failures(
    monkeypatch, task_module, clean_state
):
    """Validate stop selected tasks collects unexpected failures."""
    runners["r1"] = _runner("r1")
    tasks["t-bug"] = _task("t-bug", "r1", status="running")

    async def fake_stop(_task_id: str, _runner: Runner):
        raise RuntimeError("boom")

    monkeypatch.setattr(task_module, "_request_runner_task_stop", fake_stop)

    payload = await task_module.stop_selected_tasks({"task_ids": ["t-bug"]})

    assert payload["requested"] == 1
    assert payload["stopped"] == []
    assert payload["skipped"] == []
    assert payload["failed"] == [
        {"task_id": "t-bug", "reason": "Unexpected error while stopping task"}
    ]


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_empty_task_ids(task_module):
    """Validate stop selected tasks rejects empty task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": []})
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must contain at least one task ID"


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_non_list_task_ids(task_module):
    """Validate stop selected tasks rejects non list task ids."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": "t1"})  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    assert exc.value.detail == "task_ids must be a list"


@pytest.mark.asyncio
async def test_stop_selected_tasks_rejects_too_many_task_ids(monkeypatch, task_module):
    """Validate stop selected tasks rejects too many task ids."""
    monkeypatch.setattr(task_module, "_MAX_BULK_STOP_TASKS", 2)
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_selected_tasks({"task_ids": ["a", "b", "c"]})
    assert exc.value.status_code == 400
    assert "Too many task IDs" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_stop_task_proxies_to_runner_success(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint proxies to the assigned runner."""
    runners["r1"] = _runner("r1", url="http://r1.example", token="runner-token")
    tasks["t1"] = _task("t1", "r1", status="running")
    captured: dict[str, Any] = {}

    class FakeRunnerResponse:
        status_code = 202
        text = ""

        def json(self):
            return {"status": "stop_requested"}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers")
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    response = await task_module.stop_task("t1")

    assert response.status_code == 202
    assert json.loads(response.body) == {
        "task_id": "t1",
        "status": "stop_requested",
        "runner_id": "r1",
        "runner_status_code": 202,
    }
    assert captured["url"] == "http://r1.example/task/stop/t1"
    assert captured["headers"]["Authorization"] == "Bearer runner-token"
    assert tasks["t1"].status == "running"


@pytest.mark.asyncio
async def test_stop_task_rejects_non_running_task(task_module, clean_state):
    """Validate manager stop endpoint rejects non-running tasks."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 409
    assert exc.value.detail == "Task not running"


@pytest.mark.asyncio
async def test_stop_task_returns_404_when_task_missing(task_module, clean_state):
    """Validate manager stop endpoint returns 404 when task is missing."""
    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("missing")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Task not found"


@pytest.mark.asyncio
async def test_stop_task_returns_404_when_runner_missing(task_module, clean_state):
    """Validate manager stop endpoint returns 404 when the assigned runner is missing."""
    tasks["t1"] = _task("t1", "missing-runner", status="running")

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Runner not found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_status", "runner_detail"),
    [
        (409, "no killable process"),
        (404, "task unknown on runner"),
    ],
)
async def test_stop_task_runner_404_and_409_are_forwarded(
    monkeypatch, task_module, clean_state, runner_status: int, runner_detail: str
):
    """Validate manager stop endpoint forwards runner 404/409 details."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeRunnerResponse:
        status_code = runner_status
        text = ""

        def json(self):
            return {"detail": runner_detail}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == runner_status
    assert exc.value.detail == runner_detail


@pytest.mark.asyncio
async def test_stop_task_runner_request_error_maps_to_502(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner request errors to 502."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "http://r1.example/task/stop/t1")
            raise httpx.RequestError("runner unreachable", request=request)

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 502
    assert exc.value.detail == "Error contacting runner: runner unreachable"


@pytest.mark.asyncio
async def test_stop_task_runner_error_maps_to_502(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner 5xx responses to 502."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeRunnerResponse:
        status_code = 500
        text = "boom"

        def json(self):
            raise ValueError("not json")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeRunnerResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 502
    assert "Runner stop request failed: 500 - boom" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_stop_task_runner_timeout_maps_to_504(monkeypatch, task_module, clean_state):
    """Validate manager stop endpoint maps runner timeouts to 504."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="running")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module.stop_task("t1")

    assert exc.value.status_code == 504
    assert exc.value.detail == "Timeout contacting runner"


# -----------------------------
# Execute async
# -----------------------------


def test_execute_task_async_rejects_on_priority_quota(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async rejects on priority quota."""
    monkeypatch.setattr(task_module.config, "PRIORITIES_ENABLED", True)
    monkeypatch.setattr(task_module.config, "PRIORITY_DOMAIN", "priority.example")
    monkeypatch.setattr(task_module.config, "MAX_OTHER_DOMAIN_TASK_PERCENT", 0)

    monkeypatch.setattr(task_module, "would_exceed_other_domain_quota", lambda **_: True)

    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 503


def test_execute_task_async_no_runners_available(monkeypatch, client, task_module, clean_state):
    # One runner in registry but it times out / is unavailable.
    """Validate Execute task async no runners available."""
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "No runners available"


def test_execute_task_async_returns_existing_inflight_duplicate(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async reuses an equivalent in-flight task."""
    now = datetime.now().isoformat()
    tasks["existing-task"] = Task(
        task_id="existing-task",
        runner_id="r1",
        status="running",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )

    scheduled: dict[str, bool] = {"called": False}

    def fake_create_task(_coro):
        scheduled["called"] = True
        raise AssertionError("background scheduling should not happen for duplicates")

    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {"video_id": "vid-001"},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["task_id"] == "existing-task"
    assert resp.json()["status"] == "running"
    assert len(tasks) == 1
    assert scheduled["called"] is False


def test_find_inflight_duplicate_ignores_non_inflight_statuses(task_module):
    """Validate Find inflight duplicate ignores non inflight statuses."""
    now = datetime.now().isoformat()
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
    )

    completed = Task(
        task_id="t-completed",
        runner_id="r1",
        status="completed",
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={"video_id": "vid-001"},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )
    pending = completed.model_copy(update={"task_id": "t-pending", "status": "pending"})

    duplicate_task_id = task_module._find_inflight_duplicate_task_id(
        request,
        {"t-completed": completed, "t-pending": pending},
    )
    assert duplicate_task_id == "t-pending"


def test_try_reserve_runner_for_dispatch_fallback_without_store_method(task_module, monkeypatch):
    """Validate Try reserve runner fallback when store has no try_reserve method."""
    fallback_runners = {"r1": _runner("r1")}
    monkeypatch.setattr(task_module, "runners", fallback_runners)

    reserved = task_module._try_reserve_runner_for_dispatch("r1")
    assert reserved is not None
    assert reserved.availability == "busy"
    assert fallback_runners["r1"].availability == "busy"
    assert task_module._try_reserve_runner_for_dispatch("r1") is None
    assert task_module._try_reserve_runner_for_dispatch("missing") is None


def test_try_reuse_inflight_duplicate_with_fresh_snapshot_disabled(task_module, monkeypatch):
    """Validate fresh-snapshot dedup returns None when disabled."""
    request = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    monkeypatch.setattr(
        task_module,
        "get_tasks_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("snapshot should not be called")),
    )

    result = task_module._try_reuse_inflight_duplicate_with_fresh_snapshot(
        request,
        dedup_enabled=False,
        log_message="unused",
    )
    assert result is None


def test_execute_task_async_reservation_race_skips_runner(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async skips runner when reservation fails."""
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeResponse:
        def json(self):
            return {"available": True, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_try_reserve_runner_for_dispatch", lambda _runner_id: None)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 503
    assert resp.json()["detail"] == "No runners available"
    assert tasks == {}


def test_execute_task_async_reuses_duplicate_after_reservation_race(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async reuses duplicate when it appears during reservation race."""
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeResponse:
        def json(self):
            return {"available": True, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    def fake_try_reserve(_runner_id: str):
        if "existing-task" not in tasks:
            tasks["existing-task"] = _task("existing-task", "r1", status="running")
        return None

    def fail_if_scheduled(coro):
        coro.close()
        raise AssertionError("No new background task should be scheduled in dedup fallback")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module, "_try_reserve_runner_for_dispatch", fake_try_reserve)
    monkeypatch.setattr(task_module.asyncio, "create_task", fail_if_scheduled)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["task_id"] == "existing-task"
    assert resp.json()["status"] == "running"
    assert len(tasks) == 1


def test_execute_task_async_success_creates_task_and_schedules(
    monkeypatch, client, task_module, clean_state
):
    """Validate Execute task async success creates task and schedules."""
    runners["r1"] = _runner("r1", url="http://r1.example")

    class FakeResponse:
        def json(self):
            return {"available": True, "registered": True, "task_types": ["encoding"]}

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    scheduled: dict[str, Any] = {}

    def fake_create_task(coro):
        scheduled["coro"] = coro
        # Avoid un-awaited coroutine warnings; we only need to assert scheduling.
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(task_module.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(task_module, "_resolve_host_ips", _fake_resolve_public_ips)

    resp = client.post(
        "/task/execute",
        json={
            "etab_name": "UM",
            "app_name": "pod",
            "app_version": "1.0",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "affiliation": None,
            "parameters": {},
            "notify_url": "https://example.com/notify",
        },
    )

    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    assert task_id in tasks
    assert tasks[task_id].status == "running"
    assert "coro" in scheduled


# -----------------------------
# Basic API endpoints
# -----------------------------


def test_get_task_status_404(client, clean_state):
    """Validate Get task status 404."""
    resp = client.get("/task/status/nope")
    assert resp.status_code == 404


def test_get_task_status_ok(client, clean_state):
    """Validate Get task status ok."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/task/status/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"
    assert "client_token" not in resp.json()


def test_list_tasks_returns_dict(client, clean_state):
    """Validate List tasks returns dict."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    tasks["t1"].client_token = "client-secret"

    resp = client.get("/task/list")
    assert resp.status_code == 200
    assert "t1" in resp.json()
    assert "client_token" not in resp.json()["t1"]


# -----------------------------
# Local storage helpers
# -----------------------------


def test_validate_result_path_rejects_traversal(task_module):
    """Validate Validate result path rejects traversal."""
    with pytest.raises(Exception):
        task_module._validate_result_path("../secret")


def test_resolve_shared_storage_base_errors(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base errors."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path / "nope"))

    with pytest.raises(Exception):
        task_module._resolve_shared_storage_base()


def test_resolve_shared_storage_base_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base resolve exception."""
    (tmp_path / "base").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path / "base"))

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._resolve_shared_storage_base()
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_resolve_shared_storage_base_happy_path(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base happy path."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    base = task_module._resolve_shared_storage_base()
    assert base.exists() and base.is_dir()


def test_resolve_shared_storage_base_prefers_new_var_name(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base prefers new var name."""
    new_base = tmp_path / "new-storage"
    legacy_base = tmp_path / "legacy-storage"
    new_base.mkdir()
    legacy_base.mkdir()

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(new_base), raising=False)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(legacy_base), raising=False)

    base = task_module._resolve_shared_storage_base()
    assert base == new_base.resolve()


def test_get_local_task_dir_rejects_outside_base(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir rejects outside base."""
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("../evil")
    assert exc.value.status_code == 500


def test_get_local_task_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir 404 when missing."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("t-missing")
    assert exc.value.status_code == 404


def test_get_local_task_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir resolve exception."""
    monkeypatch.setattr(task_module, "_resolve_shared_storage_base", lambda: tmp_path)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_task_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir resolve exception."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_output_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_rejects_symlink_outside(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir rejects symlink outside."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (task_dir / "output").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 500


def test_get_local_output_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir 404 when missing."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 404


def test_mark_warning_as_completed_calls_save_tasks(monkeypatch, task_module, clean_state):
    """Validate Mark warning as completed calls save tasks."""
    tasks["t1"] = _task("t1", "r1", status="warning")

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)
    task_module._mark_warning_as_completed("t1")
    assert tasks["t1"].status == "completed"
    assert called["count"] == 1


def test_get_local_manifest_and_file_happy_path(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Get local manifest and file happy path."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    base = tmp_path
    (base / "t1" / "output").mkdir(parents=True)
    (base / "t1" / "manifest.json").write_text(json.dumps({"files": ["a.txt"]}), encoding="utf-8")
    (base / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(base))

    manifest_resp = task_module._get_local_manifest(tasks["t1"])
    assert manifest_resp.status_code == 200
    assert manifest_resp.headers["X-Task-ID"] == "t1"
    assert tasks["t1"].status == "completed"  # warning -> completed

    file_resp = task_module._stream_local_file(tasks["t1"], "a.txt")
    assert file_resp.status_code == 200


def test_get_local_manifest_missing_file_404(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Get local manifest missing file 404."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_manifest(tasks["t1"])
    assert exc.value.status_code == 404


def test_get_local_manifest_resolve_exception_500(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Get local manifest resolve exception 500."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    task_dir = tmp_path / "t1"
    task_dir.mkdir()
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_manifest(tasks["t1"])
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_manifest_invalid_json(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Get local manifest invalid json."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(Exception):
        task_module._get_local_manifest(tasks["t1"])


def test_stream_local_file_missing(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Stream local file missing."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(Exception):
        task_module._stream_local_file(tasks["t1"], "missing.txt")


def test_stream_local_file_rejects_path_outside_output(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Stream local file rejects path outside output."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._stream_local_file(tasks["t1"], "../evil")
    assert exc.value.status_code == 400


def test_stream_local_file_resolve_exception(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Stream local file resolve exception."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    output_dir = tmp_path / "t1" / "output"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_output_dir", lambda _task_id: output_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._stream_local_file(tasks["t1"], "a.txt")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


# -----------------------------
# Runner streaming helpers
# -----------------------------


class _FakeHTTPXResponse:
    def __init__(
        self, *, status_code: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None
    ):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = body.decode("utf-8", errors="ignore")
        self.closed = False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        yield self._body

    async def aclose(self) -> None:
        self.closed = True


class _FakeHTTPXClient:
    def __init__(self, response: _FakeHTTPXResponse | Exception):
        self._response_or_exc = response
        self.closed = False

    async def get(self, *_args, **_kwargs):
        if isinstance(self._response_or_exc, Exception):
            raise self._response_or_exc
        return self._response_or_exc

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_fetch_runner_resource_non_200_raises(task_module):
    """Validate Fetch runner resource non 200 raises."""
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=500)
    client = _FakeHTTPXClient(resp)

    with pytest.raises(Exception):
        await task_module._fetch_runner_resource(
            client=client,
            runner=runner,
            url="http://r1.example/x",
            timeout=httpx.Timeout(1.0),
            accept="application/json",
        )


@pytest.mark.asyncio
async def test_fetch_runner_resource_200_returns_response(task_module):
    """Validate Fetch runner resource 200 returns response."""
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=200)
    client = _FakeHTTPXClient(resp)

    out = await task_module._fetch_runner_resource(
        client=client,
        runner=runner,
        url="http://r1.example/x",
        timeout=httpx.Timeout(1.0),
        accept="application/json",
    )
    assert out is resp


@pytest.mark.asyncio
async def test_build_streaming_response_sets_headers_and_closes(task_module, clean_state):
    """Validate Build streaming response sets headers and closes."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    response = _FakeHTTPXResponse(headers={"content-type": "application/json"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(
        task_id="t1",
        response=response,
        client=client,
        media_type="application/json",
        filename="manifest.json",
    )

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    chunks = await _collect(sr.body_iterator)  # type: ignore[attr-defined]
    assert chunks == b"ok"
    assert tasks["t1"].status == "completed"


@pytest.mark.asyncio
async def test_build_streaming_response_uses_response_content_disposition(
    monkeypatch, task_module, clean_state
):
    """Validate Build streaming response uses response content disposition."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    response = _FakeHTTPXResponse(headers={"content-disposition": "attachment; filename=x.bin"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(task_id="t1", response=response, client=client)
    assert sr.headers["Content-Disposition"] == "attachment; filename=x.bin"


@pytest.mark.asyncio
async def test_stream_runner_manifest_success(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest success."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b'{"task_id": "t1"}'
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/json"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def get(self, url: str, *_args, **_kwargs):
            self.last_url = url
            return await super().get(url)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert tasks["t1"].status == "completed"
    assert created["client"].closed is True
    assert created["client"].last_url and created["client"].last_url.endswith("/task/result/t1")


@pytest.mark.asyncio
async def test_stream_runner_file_success_and_encodes_path(monkeypatch, task_module, clean_state):
    """Validate Stream runner file success and encodes path."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b"bin"
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/octet-stream"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def get(self, url: str, *_args, **_kwargs):
            self.last_url = url
            return await super().get(url)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a b/c.txt")

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert "a%20b/c.txt" in (created["client"].last_url or "")


@pytest.mark.asyncio
async def test_stream_runner_manifest_timeout(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest timeout."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_manifest_request_error(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest request error."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_runner_manifest_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner manifest http exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_manifest_unexpected_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner manifest unexpected exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def aclose(self):
            self.closed = True

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = FakeAsyncClient()
        return created["client"]

    async def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(task_module, "_fetch_runner_resource", boom)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 500
    assert created["client"].closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_request_error(monkeypatch, task_module, clean_state):
    """Validate Stream runner file request error."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(Exception):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")


@pytest.mark.asyncio
async def test_stream_runner_file_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner file http exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_timeout(monkeypatch, task_module, clean_state):
    """Validate Stream runner file timeout."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_file_unexpected_exception(monkeypatch, task_module, clean_state):
    """Validate Stream runner file unexpected exception."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            self.closed = False

        async def get(self, *_a, **_k):
            raise ValueError("boom")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 500


# -----------------------------
# Result endpoints
# -----------------------------


def test_get_valid_task_rejects_missing(task_module, clean_state):
    """Validate Get valid task rejects missing."""
    with pytest.raises(Exception):
        task_module._get_valid_task("nope")


def test_get_valid_task_rejects_failed(task_module, clean_state):
    """Validate Get valid task rejects failed."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="failed")
    tasks["t1"].error = "boom"

    with pytest.raises(Exception):
        task_module._get_valid_task("t1")


def test_get_task_runner_raises_when_runner_missing(task_module, clean_state):
    """Validate Get task runner raises when runner missing."""
    tasks["t1"] = _task("t1", "missing", status="completed")
    with pytest.raises(HTTPException) as exc:
        task_module._get_task_runner(tasks["t1"])
    assert exc.value.status_code == 500


def test_get_task_result_425_when_running(client, clean_state):
    """Validate Get task result 425 when running."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="running")

    resp = client.get("/task/result/t1")
    assert resp.status_code == 425


def test_get_task_result_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    """Validate Get task result local storage."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({"task_id": "t1"}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"


def test_get_task_result_file_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    """Validate Get task result file local storage."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(
        json.dumps({"files": ["a.txt"]}), encoding="utf-8"
    )
    (tmp_path / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200


def test_get_task_result_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    """Validate Get task result proxies to runner when storage disabled."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner):
        return JSONResponse({"task_id": "t1", "proxied": True})

    monkeypatch.setattr(task_module, "_stream_runner_manifest", fake_stream)
    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["proxied"] is True


def test_get_task_result_file_rejects_traversal(client, clean_state):
    """Validate Get task result file rejects traversal."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    # Use an encoded traversal sequence; plain "../" may be normalized away by the ASGI stack
    # and never reach the route handler.
    resp = client.get("/task/result/t1/file/%2e%2e%2fsecret")
    assert resp.status_code == 400


def test_get_task_result_file_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    """Validate Get task result file proxies to runner when storage disabled."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner, _path: str):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(task_module, "_stream_runner_file", fake_stream)
    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# -----------------------------
# Completion endpoint
# -----------------------------


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


def test_task_completion_notify_non_200_sets_warning_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    """Validate Task completion notify non 200 sets warning and schedules retry."""
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


def test_task_completion_timeout_notify_non_200_keeps_timeout_and_schedules_retry(
    monkeypatch, client, task_module, clean_state
):
    """Validate Task completion timeout notify non 200 keeps timeout and schedules retry."""
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


# -----------------------------
# Background execution helper
# -----------------------------


@pytest.mark.asyncio
async def test_execute_task_async_background_success_sets_runner_busy(
    monkeypatch, task_module, clean_state
):
    """Validate Execute task async background success sets runner busy."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert runners["r1"].availability == "busy"


@pytest.mark.asyncio
async def test_execute_task_async_background_failure_marks_task_failed(
    monkeypatch, task_module, clean_state
):
    """Validate Execute task async background failure marks task failed."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeResponse:
        status_code = 500
        text = "no"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert tasks["t1"].status == "failed"
    assert runners["r1"].availability == "available"


@pytest.mark.asyncio
async def test_execute_task_async_background_exception_marks_failed(
    monkeypatch, task_module, clean_state
):
    """Validate Execute task async background exception marks failed."""
    runners["r1"] = _runner("r1", url="http://r1.example")
    tasks["t1"] = _task("t1", "r1", status="pending")

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            raise RuntimeError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    from app.models.models import TaskRequest

    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
    )

    await task_module.execute_task_async_background("t1", runners["r1"], req)
    assert tasks["t1"].status == "failed"
