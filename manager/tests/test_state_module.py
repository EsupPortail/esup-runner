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


def test_get_deleted_task_ids_handles_exception(monkeypatch):
    def _raise():
        raise RuntimeError("boom")

    monkeypatch.setattr(state.persistence, "get_deleted_task_ids", _raise)
    assert state._get_deleted_task_ids() == set()


def test_should_keep_local_only_task_returns_false_when_cleanup_days_zero(monkeypatch):
    task = _task("retention-zero")
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 0)

    assert state._should_keep_local_only_task_in_production(task) is False


def test_should_keep_local_only_task_falls_back_to_updated_at(monkeypatch):
    task = _task("fallback-updated", updated_at="2026-02-16T10:00:00")
    task.created_at = "invalid-date"
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 365)

    assert (
        state._should_keep_local_only_task_in_production(
            task, now=datetime.fromisoformat("2026-02-16T10:01:00")
        )
        is True
    )


def test_should_keep_local_only_task_returns_false_when_dates_invalid(monkeypatch):
    task = _task("invalid-dates")
    task.created_at = "invalid-created"
    task.updated_at = "invalid-updated"
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 365)

    assert state._should_keep_local_only_task_in_production(task) is False


def test_merge_tasks_with_persistence_skips_local_deleted(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["local-deleted"] = _task("local-deleted")

    monkeypatch.setattr(state.persistence, "get_deleted_task_ids", lambda: {"local-deleted"})
    monkeypatch.setattr(state.persistence, "load_tasks", lambda *_, **__: {})

    merged = state._merge_tasks_with_persistence()
    assert "local-deleted" not in merged

    state.tasks.clear()
    state.tasks.update(original_tasks)


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


def test_get_task_returns_none_for_deleted_task(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t-deleted"] = _task("t-deleted")

    monkeypatch.setattr(
        state.persistence, "is_task_deleted", lambda task_id: task_id == "t-deleted"
    )

    loaded = state.get_task("t-deleted")
    assert loaded is None
    assert "t-deleted" not in state.tasks

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


def test_get_task_returns_none_when_missing_everywhere_in_production(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(state.persistence, "load_task", lambda _task_id: None)

    loaded = state.get_task("absent")
    assert loaded is None

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_task_keeps_local_when_persisted_is_not_newer(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    local = _task("t1", updated_at="2026-02-16T10:10:00")
    persisted = _task("t1", updated_at="2026-02-16T10:10:00")
    persisted.status = "completed"
    local.status = "running"
    state.tasks["t1"] = local

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(state.persistence, "load_task", lambda _task_id: persisted.model_dump())

    loaded = state.get_task("t1")
    assert loaded is not None
    assert loaded.status == "running"
    assert state.tasks["t1"].status == "running"

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
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 365)
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


def test_get_tasks_snapshot_filters_deleted_tasks(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["shared"] = _task("shared", updated_at="2026-02-16T10:00:00")
    state.tasks["local-deleted"] = _task("local-deleted", updated_at="2026-02-16T10:00:00")

    persisted_shared = _task("shared", updated_at="2026-02-16T10:05:00")
    persisted_shared.status = "completed"

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(
        state.persistence, "get_deleted_task_ids", lambda: {"local-deleted", "persisted-deleted"}
    )
    monkeypatch.setattr(
        state.persistence,
        "load_tasks",
        lambda *_, **__: {
            "shared": persisted_shared.model_dump(),
            "persisted-deleted": _task("persisted-deleted").model_dump(),
        },
    )

    snapshot = state.get_tasks_snapshot()

    assert set(snapshot.keys()) == {"shared"}
    assert snapshot["shared"].status == "completed"
    assert set(state.tasks.keys()) == {"shared"}

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_get_tasks_snapshot_production_drops_stale_local_only_tasks(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()

    stale_terminal = _task("stale-terminal", updated_at="2026-01-30T10:00:00")
    stale_terminal.status = "completed"
    stale_running = _task("stale-running", updated_at="2026-01-30T10:00:00")
    stale_running.status = "running"
    state.tasks["stale-terminal"] = stale_terminal
    state.tasks["stale-running"] = stale_running

    monkeypatch.setattr(state, "IS_PRODUCTION", True)
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 7)
    monkeypatch.setattr(state.persistence, "get_deleted_task_ids", lambda: set())
    monkeypatch.setattr(state.persistence, "load_tasks", lambda *_, **__: {})

    snapshot = state.get_tasks_snapshot()

    assert "stale-terminal" not in snapshot
    assert "stale-running" not in snapshot
    assert "stale-terminal" not in state.tasks
    assert "stale-running" not in state.tasks

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
    monkeypatch.setattr(state.config, "CLEANUP_TASK_FILES_DAYS", 365)
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


def test_delete_task_removes_from_memory_when_persistence_succeeds(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t1"] = _task("t1")
    state.tasks["t1"].status = "completed"

    monkeypatch.setattr(state.persistence, "delete_task", lambda task_id: task_id == "t1")

    assert state.delete_task("t1") is True
    assert "t1" not in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_delete_task_for_retention_cleanup_deletes_running_task(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t-running"] = _task("t-running")
    state.tasks["t-running"].status = "running"

    monkeypatch.setattr(state.persistence, "delete_task", lambda task_id: task_id == "t-running")

    assert state.delete_task_for_retention_cleanup("t-running") is True
    assert "t-running" not in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_delete_task_for_retention_cleanup_returns_false_when_persistence_fails(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t-running"] = _task("t-running")
    state.tasks["t-running"].status = "running"

    monkeypatch.setattr(state.persistence, "delete_task", lambda _task_id: False)

    assert state.delete_task_for_retention_cleanup("t-running") is False
    assert "t-running" in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_delete_task_keeps_memory_when_persistence_fails(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t1"] = _task("t1")
    state.tasks["t1"].status = "completed"

    monkeypatch.setattr(state.persistence, "delete_task", lambda _task_id: False)

    assert state.delete_task("t1") is False
    assert "t1" in state.tasks

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_delete_task_refuses_pending_or_running(monkeypatch):
    original_tasks = dict(state.tasks)
    state.tasks.clear()
    state.tasks["t-running"] = _task("t-running")
    state.tasks["t-pending"] = _task("t-pending")
    state.tasks["t-pending"].status = "pending"

    called = {"count": 0}

    def _delete_task(_task_id):
        called["count"] += 1
        return True

    monkeypatch.setattr(state.persistence, "delete_task", _delete_task)

    assert state.delete_task("t-running") is False
    assert state.delete_task("t-pending") is False
    assert "t-running" in state.tasks
    assert "t-pending" in state.tasks
    assert called["count"] == 0

    state.tasks.clear()
    state.tasks.update(original_tasks)


def test_resolve_persistence_directory_returns_absolute_path():
    class Backend:
        data_directory = state.persistence.data_directory

    assert (
        state._resolve_persistence_directory(Backend())
        == state.persistence.data_directory.resolve()
    )


def test_resolve_persistence_directory_fallback_on_exception():
    class FailingDirectory:
        def resolve(self):
            raise RuntimeError("boom")

    class Backend:
        data_directory = FailingDirectory()

    assert state._resolve_persistence_directory(Backend()) is Backend.data_directory
