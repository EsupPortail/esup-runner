"""Extended coverage for app.core.persistence."""

from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta

from filelock import Timeout

from app.core import persistence as persistence_module
from app.core.persistence import DailyJSONPersistence, SafeDailyJSONPersistence
from app.models.models import Task


def _task(task_id: str = "t1", status: str = "pending", created: datetime | None = None) -> Task:
    now = (created or datetime.now()).isoformat()
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
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )


def test_save_tasks_writes_and_deletes(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    today_dir = tmp_path / datetime.now().strftime("%Y-%m-%d")

    assert persistence.save_tasks({"t1": _task("t1")})

    task_file = today_dir / "t1.json"
    with open(task_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["_metadata"]["task_id"] == "t1"

    assert persistence.save_tasks({})
    assert not task_file.exists()


def test_save_tasks_uses_dict_when_model_dump_missing(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)

    class LegacyTask:
        def dict(self):
            return {"legacy": True}

    assert persistence.save_tasks({"legacy": LegacyTask()})
    task_file = tmp_path / datetime.now().strftime("%Y-%m-%d") / "legacy.json"
    with open(task_file, "r", encoding="utf-8") as f:
        stored = json.load(f)
    assert stored["legacy"] is True


def test_save_tasks_handles_timeout_and_error(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)

    def raise_timeout():
        raise Timeout("boom")

    monkeypatch.setattr(persistence, "_get_current_lock", raise_timeout)
    assert persistence.save_tasks({"t": _task("t")}) is False

    def raise_generic(*_args, **_kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(persistence, "_get_directory_path", raise_generic)
    assert persistence.save_tasks({"t": _task("t")}) is False


def test_read_task_file_metadata_and_json_error(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    task_file = tmp_path / "with_meta.json"
    task_file.write_text(
        json.dumps({"_metadata": {"task_id": "abc"}, "payload": 1}), encoding="utf-8"
    )

    task_id, task_data, metadata = persistence._read_task_file(task_file, keep_metadata=False)
    assert task_id == "abc"
    assert metadata == {"task_id": "abc"}
    assert "_metadata" not in task_data

    task_file.write_text("{not-json}", encoding="utf-8")
    result = persistence._read_task_file(task_file)
    assert result is None
    assert task_file.with_suffix(".json.bak").exists()


def test_load_tasks_from_all_dates_handles_empty_and_duplicates(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    assert persistence._load_tasks_from_all_dates() == {}

    newest = datetime.now().date()
    older = newest - timedelta(days=1)
    newest_dir = tmp_path / newest.strftime("%Y-%m-%d")
    older_dir = tmp_path / older.strftime("%Y-%m-%d")

    newest_dir.mkdir(parents=True)
    older_dir.mkdir(parents=True)
    with open(newest_dir / "t1.json", "w", encoding="utf-8") as f:
        json.dump({"task_id": "t1", "status": "completed"}, f)
    with open(older_dir / "t1.json", "w", encoding="utf-8") as f:
        json.dump({"task_id": "t1", "status": "pending"}, f)
    with open(newest_dir / "t2.json", "w", encoding="utf-8") as f:
        json.dump({"task_id": "t2", "status": "running"}, f)

    tasks_data = {}
    persistence._merge_tasks_for_date(newest, tasks_data)
    persistence._merge_tasks_for_date(older, tasks_data)

    assert tasks_data["t1"]["status"] == "completed"
    assert tasks_data["t2"]["status"] == "running"


def test_merge_tasks_handles_timeout(monkeypatch, tmp_path):
    calls = {"count": 0}

    class FailingLock:
        def __init__(self, *_args, **_kwargs):
            calls["count"] += 1

        def __enter__(self):
            raise Timeout("lock-timeout")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(persistence_module, "FileLock", FailingLock)
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    persistence._merge_tasks_for_date(datetime.now().date(), {})
    assert calls["count"] == 1


def test_merge_tasks_skips_invalid_files(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    today = datetime.now().date()
    day_dir = tmp_path / today.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True)
    (day_dir / "bad.json").write_text("{oops}", encoding="utf-8")

    tasks_data = {}
    persistence._merge_tasks_for_date(today, tasks_data)
    assert tasks_data == {}


def test_load_single_date_tasks_paths(tmp_path, monkeypatch):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    missing = persistence._load_single_date_tasks(date(2020, 1, 1))
    assert missing == {}

    day_dir = tmp_path / date.today().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True)
    with open(day_dir / "t1.json", "w", encoding="utf-8") as f:
        json.dump({"task_id": "t1", "status": "pending"}, f)

    class TimeoutLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            raise Timeout("late")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(persistence_module, "FileLock", TimeoutLock)
    assert persistence._load_single_date_tasks(date.today()) == {}

    # Restore normal lock and trigger generic exception inside loader
    monkeypatch.setattr(persistence_module, "FileLock", persistence_module.FileLock)
    monkeypatch.setattr(
        persistence_module.Path,
        "glob",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fail")),
    )
    day_dir.mkdir(parents=True, exist_ok=True)
    assert persistence._load_single_date_tasks(date.today()) == {}


def test_load_single_date_tasks_read_failure(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    day_dir = tmp_path / date.today().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True)
    (day_dir / "task.json").write_text("{}", encoding="utf-8")

    def failing_read(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(persistence, "_read_task_file", failing_read)
    assert persistence._load_single_date_tasks(date.today()) == {}


def test_load_historical_tasks_and_available_dates(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)

    for offset in range(2):
        day = date.today() - timedelta(days=offset)
        day_dir = tmp_path / day.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True)
        with open(day_dir / f"t{offset}.json", "w", encoding="utf-8") as f:
            json.dump({"task_id": f"t{offset}", "status": "pending"}, f)

    invalid_dir = tmp_path / "not-a-date"
    invalid_dir.mkdir()

    ranges = persistence.load_historical_tasks(date.today() - timedelta(days=1), date.today())
    prefixes = {key[:8] for key in ranges}
    expected = {
        date.today().strftime("%Y%m%d"),
        (date.today() - timedelta(days=1)).strftime("%Y%m%d"),
    }
    assert prefixes == expected

    dates = persistence.list_available_dates()
    assert len(dates) == 2


def test_cleanup_old_files_and_storage_info(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    old_day = date.today() - timedelta(days=2)
    keep_day = date.today()

    old_dir = tmp_path / old_day.strftime("%Y-%m-%d")
    keep_dir = tmp_path / keep_day.strftime("%Y-%m-%d")
    old_dir.mkdir(parents=True)
    keep_dir.mkdir(parents=True)

    (old_dir / "obsolete.json").write_text("{}", encoding="utf-8")
    (keep_dir / "keep.json").write_text("{}", encoding="utf-8")

    deleted = persistence.cleanup_old_files(days_to_keep=1)
    assert deleted == 1
    assert not old_dir.exists()
    assert keep_dir.exists()

    info = persistence.get_storage_info()
    assert info["current_directory_exists"] is True
    assert info["total_days_stored"] == 1


def test_save_tasks_deletion_error(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    persistence.save_tasks({"t1": _task("t1")})

    def failing_unlink(self, *args, **kwargs):
        raise OSError("delete fail")

    monkeypatch.setattr(persistence_module.Path, "unlink", failing_unlink)
    assert persistence.save_tasks({})


def test_read_task_file_missing_file(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    missing = tmp_path / "does_not_exist.json"
    assert persistence._read_task_file(missing) is None


def test_read_task_file_invalid_root_type(tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    task_file = tmp_path / "invalid_root.json"
    task_file.write_text(json.dumps(["not-an-object"]), encoding="utf-8")
    assert persistence._read_task_file(task_file) is None


def test_merge_tasks_generic_error(monkeypatch, tmp_path):
    def failing_glob(self, *_args, **_kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(persistence_module.Path, "glob", failing_glob)
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    persistence._merge_tasks_for_date(date.today(), {})


def test_cleanup_old_files_handles_unlink_error(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    old_day = date.today() - timedelta(days=2)
    old_dir = tmp_path / old_day.strftime("%Y-%m-%d")
    old_dir.mkdir(parents=True)
    file_path = old_dir / "file.json"
    file_path.write_text("{}", encoding="utf-8")

    def failing_unlink(self, *args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(persistence_module.Path, "unlink", failing_unlink)
    deleted = persistence.cleanup_old_files(days_to_keep=1)
    assert deleted == 0


def test_backup_corrupted_file_handles_copy_error(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)
    file_path = tmp_path / "corrupt.json"
    file_path.write_text("{}", encoding="utf-8")

    def failing_copy(*_args, **_kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(shutil, "copy2", failing_copy)
    persistence._backup_corrupted_file(file_path)


def test_upsert_tasks_branches(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)

    assert persistence.upsert_tasks({}) is True

    class LegacyTask:
        def dict(self):
            return {"legacy": True}

    assert persistence.upsert_tasks({"legacy": LegacyTask()})
    task_file = tmp_path / datetime.now().strftime("%Y-%m-%d") / "legacy.json"
    assert task_file.exists()

    def raise_timeout():
        raise Timeout("boom")

    monkeypatch.setattr(persistence, "_get_current_lock", raise_timeout)
    assert persistence.upsert_tasks({"t1": _task("t1")}) is False

    def raise_generic(*_args, **_kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(persistence, "_get_directory_path", raise_generic)
    assert persistence.upsert_tasks({"t2": _task("t2")}) is False


def test_load_task_branches(monkeypatch, tmp_path):
    persistence = DailyJSONPersistence(data_directory=tmp_path, lock_timeout=1)

    # No directory for today -> continue and return None.
    assert persistence.load_task("missing") is None

    day_dir = tmp_path / date.today().strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True)
    (day_dir / "t1.json").write_text(json.dumps({"task_id": "t1", "status": "pending"}))

    class TimeoutLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            raise Timeout("late")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(persistence_module, "FileLock", TimeoutLock)
    assert persistence.load_task("t1") is None

    class GenericFailLock:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            raise RuntimeError("boom")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(persistence_module, "FileLock", GenericFailLock)
    assert persistence.load_task("t1") is None


def test_safe_persistence_zero_retries(tmp_path):
    safe = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=0)
    assert safe.save_tasks({"t": _task("t")}) is False
    assert safe.load_tasks() == {}
    assert safe.upsert_tasks({"t": _task("t")}) is False
    assert safe.load_task("t") is None


def test_safe_persistence_retries_save_and_load(monkeypatch, tmp_path):
    calls = {"save": 0, "load": 0}

    def flaky_save(self, tasks):
        calls["save"] += 1
        if calls["save"] == 1:
            raise RuntimeError("boom")
        return True

    def failing_save(self, tasks):
        raise RuntimeError("fail")

    def flaky_load(self, *_args, **_kwargs):
        calls["load"] += 1
        if calls["load"] == 1:
            raise RuntimeError("boom")
        return {"x": {"task_id": "x"}}

    def failing_load(self, *_args, **_kwargs):
        raise RuntimeError("fail")

    monkeypatch.setattr(DailyJSONPersistence, "save_tasks", flaky_save)
    safe = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe.save_tasks({"t": _task("t")})
    assert calls["save"] == 2

    monkeypatch.setattr(DailyJSONPersistence, "save_tasks", failing_save)
    safe_fail = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe_fail.save_tasks({"t": _task("t")}) is False

    monkeypatch.setattr(DailyJSONPersistence, "load_tasks", flaky_load)
    safe_load = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe_load.load_tasks() == {"x": {"task_id": "x"}}
    assert calls["load"] == 2

    monkeypatch.setattr(DailyJSONPersistence, "load_tasks", failing_load)
    safe_load_fail = SafeDailyJSONPersistence(
        data_directory=tmp_path, lock_timeout=1, max_retries=2
    )
    assert safe_load_fail.load_tasks() == {}


def test_safe_persistence_retries_upsert_and_load_task(monkeypatch, tmp_path):
    calls = {"upsert": 0, "load_task": 0}

    def flaky_upsert(self, _tasks):
        calls["upsert"] += 1
        if calls["upsert"] == 1:
            raise RuntimeError("boom")
        return True

    def failing_upsert(self, _tasks):
        raise RuntimeError("fail")

    def flaky_load_task(self, _task_id):
        calls["load_task"] += 1
        if calls["load_task"] == 1:
            raise RuntimeError("boom")
        return {"task_id": "x"}

    def failing_load_task(self, _task_id):
        raise RuntimeError("fail")

    monkeypatch.setattr(DailyJSONPersistence, "upsert_tasks", flaky_upsert)
    safe = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe.upsert_tasks({"t": _task("t")}) is True

    monkeypatch.setattr(DailyJSONPersistence, "upsert_tasks", failing_upsert)
    safe_fail = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe_fail.upsert_tasks({"t": _task("t")}) is False

    monkeypatch.setattr(DailyJSONPersistence, "load_task", flaky_load_task)
    safe_load = SafeDailyJSONPersistence(data_directory=tmp_path, lock_timeout=1, max_retries=2)
    assert safe_load.load_task("x") == {"task_id": "x"}

    monkeypatch.setattr(DailyJSONPersistence, "load_task", failing_load_task)
    safe_load_fail = SafeDailyJSONPersistence(
        data_directory=tmp_path, lock_timeout=1, max_retries=2
    )
    assert safe_load_fail.load_task("x") is None
