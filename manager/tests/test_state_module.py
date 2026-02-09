"""Unit coverage for app.core.state helpers."""

from __future__ import annotations

from datetime import datetime

from app.core import state


def test_save_tasks_branches(monkeypatch):
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
