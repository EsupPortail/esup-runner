import errno
import os
import threading
import time
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api import openapi as openapi_module
from app.api.routes import task as task_module
from app.managers.storage_manager import StorageServiceManager


def test_openapi_enhance_schemas_creates_components_when_missing():
    schema = {}
    openapi_module._enhance_schemas_with_examples(schema)
    assert schema["components"]["schemas"] == {}


def test_storage_manager_error_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        os,
        "makedirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )
    with pytest.raises(PermissionError, match="Permission denied"):
        StorageServiceManager(base_path=str(tmp_path / "forbidden"))

    monkeypatch.setattr(
        os,
        "makedirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("broken")),
    )
    with pytest.raises(OSError, match="Failed to create storage directory"):
        StorageServiceManager(base_path=str(tmp_path / "broken"))


def test_storage_manager_file_operation_error_paths(monkeypatch, tmp_path):
    storage = StorageServiceManager(base_path=str(tmp_path))

    monkeypatch.setattr(
        storage,
        "get_path",
        lambda _task_id: (_ for _ in ()).throw(OSError("exists failed")),
    )
    assert storage.exists("task-1") is False

    with pytest.raises(ValueError, match="no valid characters"):
        StorageServiceManager(base_path=str(tmp_path)).get_path("!!!")

    storage = StorageServiceManager(base_path=str(tmp_path))
    monkeypatch.setattr(
        "builtins.open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("readonly")),
    )
    with pytest.raises(PermissionError, match="Cannot write"):
        storage.save_file("task-2", b"content")

    storage = StorageServiceManager(base_path=str(tmp_path))

    class DummyFile:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def write(self, _content):
            return None

    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: DummyFile())

    def no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(os, "rename", no_space)
    monkeypatch.setattr(storage, "get_available_space", lambda: 123)
    with pytest.raises(OSError, match="Insufficient disk space"):
        storage.save_file("task-3", b"abc")

    monkeypatch.setattr(
        os, "rename", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    with pytest.raises(OSError, match="io"):
        storage.save_file("task-3b", b"abc")

    storage = StorageServiceManager(base_path=str(tmp_path))
    monkeypatch.setattr(storage, "exists", lambda _task_id: True)
    monkeypatch.setattr(
        "builtins.open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read"))
    )
    with pytest.raises(OSError, match="Failed to read file"):
        storage.read_file("task-4")

    storage = StorageServiceManager(base_path=str(tmp_path))
    monkeypatch.setattr(storage, "exists", lambda _task_id: True)
    monkeypatch.setattr(
        os,
        "remove",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("blocked")),
    )
    with pytest.raises(PermissionError, match="Cannot delete file"):
        storage.cleanup("task-5")

    monkeypatch.setattr(
        os,
        "remove",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("broken")),
    )
    with pytest.raises(OSError, match="Failed to delete file"):
        storage.cleanup("task-5")


def test_storage_manager_cleanup_and_stats_error_paths(monkeypatch, tmp_path):
    storage = StorageServiceManager(base_path=str(tmp_path / "missing"))
    original_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: False if path == storage.base_path else original_exists(path),
    )
    assert storage.cleanup_all() == 0
    monkeypatch.setattr(os.path, "exists", original_exists)

    base = tmp_path / "storage"
    base.mkdir()
    (base / "one.json").write_text("1", encoding="utf-8")
    (base / "two.json").write_text("2", encoding="utf-8")
    storage = StorageServiceManager(base_path=str(base))

    original_remove = os.remove
    seen = {"count": 0}

    def flaky_remove(path):
        seen["count"] += 1
        if seen["count"] == 1:
            raise OSError("ignore me")
        return original_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)
    assert storage.cleanup_all() == 1

    monkeypatch.setattr(
        os,
        "listdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("listdir failed")),
    )
    with pytest.raises(OSError, match="Failed to clean up storage directory"):
        storage.cleanup_all()

    monkeypatch.setattr(
        os,
        "statvfs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("statvfs failed")),
    )
    with pytest.raises(OSError, match="Failed to get available space"):
        storage.get_available_space()

    missing_storage = StorageServiceManager(base_path=str(tmp_path / "no-stats"))
    original_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: False if path == missing_storage.base_path else original_exists(path),
    )
    monkeypatch.setattr(missing_storage, "get_available_space", lambda: 77)
    assert missing_storage.get_usage_stats() == {
        "total_size": 0,
        "file_count": 0,
        "available_space": 77,
    }

    normal_storage = StorageServiceManager(base_path=str(tmp_path / "stats"))
    monkeypatch.setattr(
        os,
        "listdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    with pytest.raises(OSError, match="Failed to get usage statistics"):
        normal_storage.get_usage_stats()


def test_storage_manager_delayed_cleanup_and_old_file_helpers(monkeypatch, tmp_path):
    storage = StorageServiceManager(base_path=str(tmp_path))
    messages = {"cleanup": [], "started": 0}

    monkeypatch.setattr(
        storage, "cleanup", lambda task_id: messages["cleanup"].append(task_id) or True
    )
    monkeypatch.setattr(
        time,
        "sleep",
        lambda *_args, **_kwargs: None,
    )

    class ImmediateThread:
        def __init__(self, target):
            self.target = target
            self.daemon = False

        def start(self):
            messages["started"] += 1
            self.target()

    monkeypatch.setattr(threading, "Thread", ImmediateThread)
    storage.delayed_cleanup("task-delay", delay_seconds=0)
    assert messages["cleanup"] == ["task-delay"]
    assert messages["started"] == 1

    monkeypatch.setattr(
        storage,
        "cleanup",
        lambda _task_id: (_ for _ in ()).throw(RuntimeError("cleanup failed")),
    )
    storage.delayed_cleanup("task-delay-2", delay_seconds=0)

    old_file = tmp_path / "old.json"
    old_file.write_text("{}", encoding="utf-8")
    assert storage._delete_old_item(str(old_file), 172800) is True

    old_dir = tmp_path / "old-dir"
    old_dir.mkdir()
    assert storage._delete_old_item(str(old_dir), 172800) is True

    failing_file = tmp_path / "failing.json"
    failing_file.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        os,
        "remove",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cannot delete")),
    )
    assert storage._delete_old_item(str(failing_file), 172800) is False

    monkeypatch.setattr(
        os.path,
        "getmtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mtime failed")),
    )
    assert storage._process_item("task.json", 10, 100) is False


def test_storage_manager_cleanup_old_files_misc_paths(monkeypatch, tmp_path):
    storage = StorageServiceManager(base_path=str(tmp_path))
    assert storage.cleanup_old_files(0) == 0

    missing_storage = StorageServiceManager(base_path=str(tmp_path / "missing-cleanup"))
    original_exists = os.path.exists
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda path: False if path == missing_storage.base_path else original_exists(path),
    )
    assert missing_storage.cleanup_old_files(7) == 0

    base = tmp_path / "cleanup"
    base.mkdir()
    storage = StorageServiceManager(base_path=str(base))
    monkeypatch.setattr(os, "listdir", lambda *_args, **_kwargs: ["recent.json"])
    monkeypatch.setattr(storage, "_process_item", lambda *_args, **_kwargs: False)
    assert storage.cleanup_old_files(1) == 0

    monkeypatch.setattr(
        os,
        "listdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("list failed")),
    )
    assert storage.cleanup_old_files(1) == 0


def test_task_result_path_validation_error_branches():
    with pytest.raises(HTTPException) as empty_path_error:
        task_module._validate_result_relative_path("   ")
    assert empty_path_error.value.status_code == 404

    with pytest.raises(HTTPException) as absolute_path_error:
        task_module._validate_result_relative_path("/etc/passwd")
    assert absolute_path_error.value.status_code == 404

    with pytest.raises(HTTPException) as invalid_part_error:
        task_module._validate_result_relative_path("bad|name.txt")
    assert invalid_part_error.value.status_code == 404

    with pytest.raises(HTTPException) as no_part_error:
        task_module._validate_result_relative_path(".")
    assert no_part_error.value.status_code == 404


def test_task_path_helpers_additional_branches(monkeypatch, tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)
    base_path = Path(task_module.storage_manager.base_path).resolve()

    # _find_direct_child_entry normal and OSError branches
    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    assert task_module._find_direct_child_entry(base_path, "existing") == existing_dir
    assert task_module._find_direct_child_entry(base_path, "missing") is None

    with monkeypatch.context() as context:
        context.setattr(Path, "iterdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
        assert task_module._find_direct_child_entry(base_path, "anything") is None

    # _resolve_within_base outside-base rejection
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-helper"
    outside_dir.mkdir()
    escape_link = tmp_path / "escape-link"
    escape_link.symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(HTTPException) as outside_error:
        task_module._resolve_within_base(escape_link, base_path)
    assert outside_error.value.status_code == 404

    # _resolve_output_file_path branches
    output_dir = tmp_path / "helper-output"
    output_dir.mkdir()
    regular_file = output_dir / "file.txt"
    regular_file.write_text("x", encoding="utf-8")

    with pytest.raises(HTTPException) as missing_part_error:
        task_module._resolve_output_file_path(output_dir, ("missing.txt",))
    assert missing_part_error.value.status_code == 404

    with pytest.raises(HTTPException) as non_dir_part_error:
        task_module._resolve_output_file_path(output_dir, ("file.txt", "nested.txt"))
    assert non_dir_part_error.value.status_code == 404

    # _resolve_task_root branches: missing candidate and non-directory
    with pytest.raises(HTTPException) as missing_task_error:
        task_module._resolve_task_root("task-missing")
    assert missing_task_error.value.status_code == 404

    (tmp_path / "task-file").write_text("not-a-dir", encoding="utf-8")
    with pytest.raises(HTTPException) as not_dir_task_error:
        task_module._resolve_task_root("task-file")
    assert not_dir_task_error.value.status_code == 404

    # _resolve_task_manifest_path missing-manifest branch
    (tmp_path / "task-no-manifest").mkdir()
    with pytest.raises(HTTPException) as missing_manifest_error:
        task_module._resolve_task_manifest_path("task-no-manifest")
    assert missing_manifest_error.value.status_code == 404

    # _resolve_task_root_if_exists branches
    assert task_module._resolve_task_root_if_exists("nope") is None
    symlink_task = tmp_path / "task-symlink"
    symlink_task.symlink_to(outside_dir, target_is_directory=True)
    assert task_module._resolve_task_root_if_exists("task-symlink") is None
    assert task_module._resolve_task_root_if_exists("task-file") is None

    # _resolve_legacy_manifest_if_exists branches
    assert task_module._resolve_legacy_manifest_if_exists("legacy-missing") is None

    # Candidate resolves outside base -> caught and returned as None.
    legacy_outside_target = outside_dir / "outside-legacy.json"
    legacy_outside_target.write_text("{}", encoding="utf-8")
    legacy_outside_link = tmp_path / "legacy-outside.json"
    legacy_outside_link.symlink_to(legacy_outside_target)
    assert task_module._resolve_legacy_manifest_if_exists("legacy-outside") is None

    # Candidate resolves inside base but not directly under base -> parent check branch.
    nested_dir = tmp_path / "nested"
    nested_dir.mkdir()
    nested_file = nested_dir / "real.json"
    nested_file.write_text("{}", encoding="utf-8")
    legacy_nested_link = tmp_path / "legacy-nested.json"
    legacy_nested_link.symlink_to(nested_file)
    assert task_module._resolve_legacy_manifest_if_exists("legacy-nested") is None

    # Candidate exists but is not a file.
    legacy_dir_candidate = tmp_path / "legacy-dir.json"
    legacy_dir_candidate.mkdir()
    assert task_module._resolve_legacy_manifest_if_exists("legacy-dir") is None

    # Success branch.
    legacy_ok = tmp_path / "legacy-ok.json"
    legacy_ok.write_text("{}", encoding="utf-8")
    assert task_module._resolve_legacy_manifest_if_exists("legacy-ok") == legacy_ok


def test_task_root_resolution_rejects_symlink_escape(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside"
    outside_dir.mkdir()
    (tmp_path / "task-link").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(HTTPException) as symlink_error:
        task_module._resolve_task_root("task-link")
    assert symlink_error.value.status_code == 404


def test_task_manifest_resolution_rejects_symlink_manifest(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)
    task_dir = tmp_path / "task-manifest"
    task_dir.mkdir(parents=True)

    outside_dir = tmp_path / "outside-manifest"
    outside_dir.mkdir()
    outside_manifest = outside_dir / "manifest.json"
    outside_manifest.write_text("{}", encoding="utf-8")

    (task_dir / "manifest.json").symlink_to(outside_manifest)

    with pytest.raises(HTTPException) as manifest_error:
        task_module._resolve_task_manifest_path("task-manifest")
    assert manifest_error.value.status_code == 404


def test_task_manifest_resolution_rejects_nested_manifest_symlink(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_dir = tmp_path / "task-manifest-nested"
    nested_dir = task_dir / "nested"
    nested_dir.mkdir(parents=True)
    nested_manifest = nested_dir / "manifest.json"
    nested_manifest.write_text("{}", encoding="utf-8")

    # Direct child exists, but resolves to nested/manifest.json: parent != task_root
    (task_dir / "manifest.json").symlink_to(nested_manifest)

    with pytest.raises(HTTPException) as manifest_error:
        task_module._resolve_task_manifest_path("task-manifest-nested")
    assert manifest_error.value.status_code == 404


def test_task_manifest_resolution_rejects_manifest_directory(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_dir = tmp_path / "task-manifest-dir"
    task_dir.mkdir(parents=True)
    # Candidate exists and parent matches task root, but it is not a file.
    (task_dir / "manifest.json").mkdir()

    with pytest.raises(HTTPException) as manifest_error:
        task_module._resolve_task_manifest_path("task-manifest-dir")
    assert manifest_error.value.status_code == 404


@pytest.mark.asyncio
async def test_task_route_additional_branches(monkeypatch, tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    with pytest.raises(HTTPException) as traversal_error:
        task_module._resolve_task_root("../outside")
    assert traversal_error.value.status_code == 404

    statuses = [500, 200]

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = f"status={status_code}"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse(statuses.pop(0))

    async def fake_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(task_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1.0)
    assert await task_module.notify_completion("http://cb", "task-1", "completed") is True

    task_id = "task-output"
    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)

    with pytest.raises(HTTPException) as relative_error:
        await task_module.get_task_result_file(
            task_id,
            "../secret.txt",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert relative_error.value.status_code == 404

    with pytest.raises(HTTPException) as missing_file_error:
        await task_module.get_task_result_file(
            task_id,
            "missing.txt",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert missing_file_error.value.status_code == 404

    monkeypatch.setattr(
        task_module.storage_manager,
        "get_path",
        lambda _task_id: (_ for _ in ()).throw(ValueError("bad id")),
    )
    result = await task_module.delete_task_result("bad-task", current_manager="manager-token")
    assert result == {"status": "deleted"}

    monkeypatch.setattr(
        task_module.storage_manager, "get_path", lambda task_id: str(tmp_path / f"{task_id}.json")
    )
    legacy_manifest = tmp_path / "legacy-task.json"
    legacy_manifest.write_text("{}", encoding="utf-8")
    result = await task_module.delete_task_result("legacy-task", current_manager="manager-token")
    assert result == {"status": "deleted"}
    assert not legacy_manifest.exists()


@pytest.mark.asyncio
async def test_task_result_file_rejects_symlink_escape(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_id = "task-symlink"
    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)

    outside_dir = tmp_path / "outside-files"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("secret", encoding="utf-8")

    (output_dir / "safe").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(HTTPException) as symlink_file_error:
        await task_module.get_task_result_file(
            task_id,
            "safe/secret.txt",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert symlink_file_error.value.status_code == 404


@pytest.mark.asyncio
async def test_task_result_file_rejects_output_path_when_not_a_directory(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_id = "task-with-output-file"
    task_dir = tmp_path / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "output").write_text("not-dir", encoding="utf-8")

    with pytest.raises(HTTPException) as output_not_dir_error:
        await task_module.get_task_result_file(
            task_id,
            "x.txt",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert output_not_dir_error.value.status_code == 404


@pytest.mark.asyncio
async def test_task_result_file_rejects_missing_output_directory(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_id = "task-no-output"
    task_dir = tmp_path / task_id
    task_dir.mkdir(parents=True)

    with pytest.raises(HTTPException) as missing_output_error:
        await task_module.get_task_result_file(
            task_id,
            "x.txt",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert missing_output_error.value.status_code == 404


@pytest.mark.asyncio
async def test_task_result_file_rejects_directory_target(tmp_path):
    task_module.storage_manager.base_path = str(tmp_path)

    task_id = "task-dir-target"
    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "subdir").mkdir()

    with pytest.raises(HTTPException) as directory_target_error:
        await task_module.get_task_result_file(
            task_id,
            "subdir",
            BackgroundTasks(),
            current_manager="manager-token",
        )
    assert directory_target_error.value.status_code == 404
