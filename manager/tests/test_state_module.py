"""Unit coverage for app.core.state helpers."""

from __future__ import annotations

from datetime import datetime

from app.core import state
from app.models.models import Task


def _task(task_id: str, updated_at: str | None = None) -> Task:
    now_iso = updated_at or datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id="r1",
        status="running",
        etab_name="etab",
        app_name="app",
        app_version="1.0.0",
        task_type="encoding",
        source_url="http://example.com/source",
        affiliation="staff",
        parameters={},
        notify_url="http://example.com/notify",
        created_at=now_iso,
        updated_at=now_iso,
    )


def test_save_tasks_branches(monkeypatch):
    monkeypatch.setattr(state, "IS_PRODUCTION", False)
    monkeypatch.setattr(state.persistence, "save_tasks", lambda *_: True)
    assert state.save_tasks() is True

    monkeypatch.setattr(state.persistence, "save_tasks", lambda *_: False)
    assert state.save_tasks() is False


def test_force_save_tasks_calls_save(monkeypatch):
    called = {"count": 0}

    def _save():
        called["count"] += 1
        return True

    monkeypatch.setattr(state, "save_tasks", _save)
    assert state.force_save_tasks() is True
    assert called["count"] == 1


def test_get_storage_info(monkeypatch):
    monkeypatch.setattr(state.persistence, "get_storage_info", lambda: {"ok": True})
    assert state.get_storage_info() == {"ok": True}


def test_cleanup_old_task_files(monkeypatch):
    monkeypatch.setattr(state.persistence, "cleanup_old_files", lambda days: days)
    assert state.cleanup_old_task_files(days_to_keep=7) == 7


def test_load_historical_tasks(monkeypatch):
    monkeypatch.setattr(state.persistence, "load_historical_tasks", lambda *_: {"a": 1})
    start = datetime(2024, 1, 1)
    end = datetime(2024, 1, 2)
    assert state.load_historical_tasks(start, end) == {"a": 1}


def test_shutdown_handler(monkeypatch):
    calls = {"save": 0, "cleanup": 0}

    def _save():
        calls["save"] += 1
        return True

    def _cleanup(*_):
        calls["cleanup"] += 1
        return 0

    monkeypatch.setattr(state, "save_tasks", _save)
    monkeypatch.setattr(state, "cleanup_old_task_files", _cleanup)

    state.shutdown_handler()
    assert calls["save"] == 1
    assert calls["cleanup"] == 1


def test_get_task_loads_from_persistence_in_production(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    persisted_task = _task("t-persisted")

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        state.persistence,
        "load_task",
        lambda task_id: persisted_task.model_dump() if task_id == "t-persisted" else None,
    )

    loaded = state.get_task("t-persisted")

    assert loaded is not None
    assert loaded.task_id == "t-persisted"
    assert "t-persisted" in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_task_refreshes_stale_memory_from_persistence(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    stale_task = _task("t-refresh", updated_at="2026-02-16T10:00:00")
    fresh_task = _task("t-refresh", updated_at="2026-02-16T10:05:00")
    fresh_task.status = "completed"
    state.tasks["t-refresh"] = stale_task

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        state.persistence,
        "load_task",
        lambda task_id: fresh_task.model_dump() if task_id == "t-refresh" else None,
    )

    loaded = state.get_task("t-refresh")
    assert loaded is not None
    assert loaded.status == "completed"
    assert state.tasks["t-refresh"].status == "completed"

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_parse_updated_at_branches():
    assert state._parse_updated_at(None) == datetime.min
    assert state._parse_updated_at("not-a-date") == datetime.min


def test_save_tasks_production_skips_invalid_persisted_task(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["local"] = _task("local")

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(state.persistence, "load_tasks", lambda *_, **__: {"bad": {"x": "y"}})
    monkeypatch.setattr(state.persistence, "upsert_tasks", lambda *_: True)

    assert state.save_tasks() is True
    assert "local" in state.tasks
    assert "bad" not in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_task_non_production_returns_local_without_persistence(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t-local"] = _task("t-local")

    monkeypatch.setattr(state, "IS_PRODUCTION", False)

    called = {"count": 0}

    def _load_task(_task_id):
        called["count"] += 1
        return None

    monkeypatch.setattr(state.persistence, "load_task", _load_task)

    loaded = state.get_task("t-local")
    assert loaded is not None
    assert loaded.task_id == "t-local"
    assert called["count"] == 0

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_task_invalid_persisted_payload_uses_local(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t1"] = _task("t1", updated_at="2026-02-16T10:00:00")

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(state.persistence, "load_task", lambda _task_id: {"bad": "payload"})

    loaded = state.get_task("t1")
    assert loaded is not None
    assert loaded.task_id == "t1"

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_tasks_snapshot_non_production_returns_copy(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t1"] = _task("t1")

    monkeypatch.setattr(state, "IS_PRODUCTION", False)
    snapshot = state.get_tasks_snapshot()

    assert "t1" in snapshot
    assert snapshot is not state.tasks

    snapshot.clear()
    assert "t1" in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_tasks_snapshot_production_merges_and_refreshes_cache(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["shared"] = _task("shared", updated_at="2026-02-16T10:00:00")
    state.tasks["local-only"] = _task("local-only", updated_at="2026-02-16T10:00:00")

    persisted_shared = _task("shared", updated_at="2026-02-16T10:05:00")
    persisted_shared.status = "completed"
    persisted_only = _task("persisted-only", updated_at="2026-02-16T10:03:00")

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        state.persistence,
        "load_tasks",
        lambda *_, **__: {
            "shared": persisted_shared.model_dump(),
            "persisted-only": persisted_only.model_dump(),
        },
    )

    snapshot = state.get_tasks_snapshot()

    assert set(snapshot.keys()) == {"shared", "local-only", "persisted-only"}
    assert snapshot["shared"].status == "completed"
    assert "persisted-only" in state.tasks
    assert state.tasks["shared"].status == "completed"

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_save_tasks_production_merges_and_upserts(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    local_task = _task("local", updated_at="2026-02-16T10:00:00")
    persisted_task = _task("persisted", updated_at="2026-02-16T09:00:00")
    state.tasks["local"] = local_task

    captured = {"payload": None}

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        state.persistence,
        "load_tasks",
        lambda *_, **__: {"persisted": persisted_task.model_dump()},
    )

    def _upsert(payload):
        captured["payload"] = dict(payload)
        return True

    monkeypatch.setattr(state.persistence, "upsert_tasks", _upsert)

    assert state.save_tasks() is True
    assert captured["payload"] is not None
    assert "local" in captured["payload"]
    assert "persisted" in captured["payload"]
    assert "local" in state.tasks
    assert "persisted" in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)
