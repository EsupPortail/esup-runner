"""Tests for SafeDailyJSONPersistence.load_tasks."""

import json
from datetime import date, timedelta

from app.core.persistence import SafeDailyJSONPersistence


def _write_task_file(directory, task_id, data):
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{task_id}.json", "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_load_tasks_from_specific_date(tmp_path):
    persistence = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=1)

    target_date = date(2024, 1, 2)
    day_dir = tmp_path / target_date.strftime("%Y-%m-%d")
    _write_task_file(day_dir, "task-1", {"task_id": "task-1", "status": "pending"})

    loaded = persistence.load_tasks(target_date=target_date, load_all=False)

    assert loaded == {"task-1": {"task_id": "task-1", "status": "pending"}}


def test_load_tasks_prefers_newest_copy(tmp_path):
    persistence = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=1)

    newest_date = date.today()
    older_date = newest_date - timedelta(days=1)

    newest_dir = tmp_path / newest_date.strftime("%Y-%m-%d")
    older_dir = tmp_path / older_date.strftime("%Y-%m-%d")

    _write_task_file(older_dir, "shared-task", {"task_id": "shared-task", "status": "pending"})
    _write_task_file(newest_dir, "shared-task", {"task_id": "shared-task", "status": "completed"})
    _write_task_file(newest_dir, "unique-task", {"task_id": "unique-task", "status": "running"})

    loaded = persistence.load_tasks(load_all=True)

    assert loaded["shared-task"]["status"] == "completed"
    assert loaded["unique-task"]["status"] == "running"
