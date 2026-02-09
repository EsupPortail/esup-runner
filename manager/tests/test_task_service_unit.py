"""Unit coverage for app.services.task_service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.core.config import config
from app.core.state import tasks
from app.models.models import Task
from app.services import task_service


@pytest.fixture
def clean_tasks():
    original = dict(tasks)
    tasks.clear()
    yield
    tasks.clear()
    tasks.update(original)


def _task(
    task_id: str,
    status: str,
    *,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Task:
    created = (created_at or datetime.now()).isoformat()
    updated = (updated_at or created_at or datetime.now()).isoformat()
    return Task(
        task_id=task_id,
        runner_id="r1",
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=created,
        updated_at=updated,
        error=None,
        script_output=None,
    )


@pytest.mark.asyncio
async def test_cleanup_old_tasks_removes_expired(monkeypatch, clean_tasks):
    old = datetime.now() - timedelta(days=3)
    tasks["old"] = _task("old", "completed", created_at=old)
    tasks["new"] = _task("new", "completed")
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", 1)

    stop = asyncio.Event()
    coroutine = task_service.cleanup_old_tasks(poll_interval=0, stop_event=stop)
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert "old" not in tasks
    assert "new" in tasks


@pytest.mark.asyncio
async def test_check_task_timeouts_marks_timeout(clean_tasks):
    long_ago = datetime.now() - timedelta(hours=25)
    tasks["run"] = _task("run", "running", updated_at=long_ago)

    stop = asyncio.Event()
    coroutine = task_service.check_task_timeouts(poll_interval=0, stop_event=stop)
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0)
    stop.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert tasks["run"].status == "timeout"
    assert tasks["run"].error.startswith("Task timeout")


def test_update_and_get_tasks(clean_tasks):
    tasks["t1"] = _task("t1", "pending")
    assert task_service.update_task_status("t1", "running") is True
    assert task_service.update_task_status("missing", "running") is False
    assert tasks["t1"].status == "running"

    assert task_service.update_task_status("t1", "failed", "boom") is True
    assert tasks["t1"].error == "boom"

    assert task_service.get_task("t1").task_id == "t1"
    assert task_service.get_task("missing") is None


def test_get_all_tasks_and_stats(clean_tasks):
    tasks["a"] = _task("a", "completed")
    tasks["b"] = _task("b", "failed")
    tasks["c"] = _task("c", "running")

    all_tasks = task_service.get_all_tasks()
    assert set(all_tasks.keys()) == {"a", "b", "c"}

    stats = task_service.get_task_stats()
    assert stats["total"] == 3
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["running"] == 1
