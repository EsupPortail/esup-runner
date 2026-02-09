import json
from datetime import datetime

from app.core.config import config
from app.core.state import tasks
from app.models.models import Task


def _create_manifest(task_dir, task_id: str, files: list[str]) -> dict:
    manifest = {"task_id": task_id, "files": files}
    (task_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_get_task_manifest_from_shared_storage_when_enabled(
    client, auth_headers, monkeypatch, tmp_path
):
    task_id = "test_task_shared_storage"
    task_dir = tmp_path / task_id
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True)
    manifest = _create_manifest(task_dir, task_id, ["output.txt"])
    (output_dir / "output.txt").write_bytes(b"shared")

    original_tasks = dict(tasks)
    try:
        tasks.clear()
        tasks[task_id] = Task(
            task_id=task_id,
            etab_name="test_etab",
            app_name="test_app",
            app_version="1.0.0",
            task_type="test",
            source_url="http://example.com/source",
            affiliation=None,
            parameters={},
            status="completed",
            runner_id="missing_runner",
            notify_url="http://example.com/notify",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            error=None,
            script_output=None,
        )

        monkeypatch.setattr(config, "RUNNERS_STORAGE_ENABLED", True)
        monkeypatch.setattr(config, "RUNNERS_STORAGE_PATH", str(tmp_path))

        resp = client.get(f"/task/result/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers.get("X-Task-ID") == task_id
        assert resp.json() == manifest
    finally:
        tasks.clear()
        tasks.update(original_tasks)


def test_get_task_manifest_from_shared_storage_turns_warning_to_completed(
    client, auth_headers, monkeypatch, tmp_path
):
    task_id = "test_task_shared_storage_warning"
    task_dir = tmp_path / task_id
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True)
    _create_manifest(task_dir, task_id, ["output.txt"])
    (output_dir / "output.txt").write_bytes(b"shared-warning")

    original_tasks = dict(tasks)
    try:
        tasks.clear()
        tasks[task_id] = Task(
            task_id=task_id,
            etab_name="test_etab",
            app_name="test_app",
            app_version="1.0.0",
            task_type="test",
            source_url="http://example.com/source",
            affiliation=None,
            parameters={},
            status="warning",
            runner_id="missing_runner",
            notify_url="http://example.com/notify",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            error="notify failed",
            script_output=None,
        )

        monkeypatch.setattr(config, "RUNNERS_STORAGE_ENABLED", True)
        monkeypatch.setattr(config, "RUNNERS_STORAGE_PATH", str(tmp_path))

        resp = client.get(f"/task/result/{task_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert tasks[task_id].status == "completed"
        assert tasks[task_id].error is None
    finally:
        tasks.clear()
        tasks.update(original_tasks)


def test_get_task_result_file_from_shared_storage_when_enabled(
    client, auth_headers, monkeypatch, tmp_path
):
    task_id = "test_task_shared_storage_file"
    task_dir = tmp_path / task_id
    output_dir = task_dir / "output"
    output_dir.mkdir(parents=True)
    _create_manifest(task_dir, task_id, ["output.txt"])
    (output_dir / "output.txt").write_bytes(b"shared-file")

    original_tasks = dict(tasks)
    try:
        tasks.clear()
        tasks[task_id] = Task(
            task_id=task_id,
            etab_name="test_etab",
            app_name="test_app",
            app_version="1.0.0",
            task_type="test",
            source_url="http://example.com/source",
            affiliation=None,
            parameters={},
            status="completed",
            runner_id="missing_runner",
            notify_url="http://example.com/notify",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
            error=None,
            script_output=None,
        )

        monkeypatch.setattr(config, "RUNNERS_STORAGE_ENABLED", True)
        monkeypatch.setattr(config, "RUNNERS_STORAGE_PATH", str(tmp_path))

        resp = client.get(f"/task/result/{task_id}/file/output.txt", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.headers.get("X-Task-ID") == task_id
        assert resp.content == b"shared-file"
    finally:
        tasks.clear()
        tasks.update(original_tasks)
