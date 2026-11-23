"""Keep incomplete-encoding warnings across completion, storage and result retrieval."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import ValidationError
from task_routes_helpers import clean_state, make_runner, make_task, task_module

from app.core.persistence import DailyJSONPersistence
from app.core.state import runners, tasks
from app.models.models import Task, TaskCompletionNotification
from app.services import task_result_service, task_service

__all__ = ["clean_state", "task_module"]

WARNING = "Encoding incomplete: '360p_video.m3u8', video stream 0: 786s produced, 6929s expected"


@pytest.mark.asyncio
async def test_completed_encoding_preserves_warnings_in_history_api_and_callback(
    monkeypatch, tmp_path, task_module, clean_state
):
    runners["r1"] = make_runner("r1", token="token")
    tasks["t1"] = make_task("t1", "r1", status="running")
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    monkeypatch.setattr(task_module, "save_tasks", lambda: persistence.save_tasks(dict(tasks)))
    monkeypatch.setattr(task_module, "_validate_notify_url", AsyncMock())
    sent = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        @asynccontextmanager
        async def stream(self, _method, _url, **kwargs):
            # Completion must be durable before the client can fetch the result.
            assert persistence.load_task("t1")["warnings"] == [WARNING]
            sent.append(json.loads(kwargs["content"]))
            yield SimpleNamespace(status_code=204)

    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda **_kwargs: Client())
    notification = TaskCompletionNotification(task_id="t1", status="completed", warnings=[WARNING])
    await task_module.task_completion(notification, current_token="token")
    assert tasks["t1"].status == "completed"
    assert tasks["t1"].error is None
    assert sent[0]["warnings"] == [WARNING]
    assert sent[0]["status"] == "completed"
    restored = Task.model_validate(persistence.load_task("t1"))
    assert restored.warnings == [WARNING]
    assert (await task_module.get_task_details_api("t1")).warnings == [WARNING]

    # Notification warnings can be cleared without erasing encoding warnings.
    task_module._set_notify_warning("t1", "Client unavailable")
    task_result_service.mark_warning_as_completed(task_module, "t1")
    task_module._restore_status_after_notify("t1", notification)
    assert tasks["t1"].status == "completed"
    assert tasks["t1"].error is None
    assert tasks["t1"].warnings == [WARNING]
    assert persistence.load_task("t1")["warnings"] == [WARNING]


def test_reconciliation_keeps_encoding_warnings_and_clears_them_for_a_clean_run():
    task = make_task("t1", "r1", status="running")
    assert task_service._apply_runner_task_status(
        task, {"status": "completed", "warnings": [WARNING, "Packet corrupt", None]}
    )
    assert task.status == "completed"
    assert task.warnings == [WARNING]
    task_service._apply_runner_task_status(task, {"status": "completed"})
    assert task.warnings == []


def test_unrelated_warnings_are_not_accepted_as_encoding_warnings():
    with pytest.raises(ValidationError, match="Encoding incomplete"):
        TaskCompletionNotification(task_id="t1", status="completed", warnings=["Packet corrupt"])
    task = make_task("t1", "r1", status="failed")
    assert task.warnings == []


def test_task_details_display_and_escape_encoding_warnings():
    task = make_task("t1", "r1", status="completed")
    task.warnings = [WARNING + " <script>alert('unsafe')</script>"]
    env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape())
    rendered = env.get_template("task_detail.html").render(
        task=task,
        version="test",
        dark_mode_enabled=False,
        csrf_token="test",
        root_path="",
        task_actions={},
        last_update="2026-11-16",
    )
    assert "Encoding warnings" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>alert('unsafe')</script>" not in rendered
