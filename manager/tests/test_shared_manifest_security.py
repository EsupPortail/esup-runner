"""Shared manifests must never disclose files outside the task directory."""

import json
import os

import pytest
from task_routes_helpers import clean_state, client, make_task, task_module

from app.core.state import tasks
from app.services import task_result_service

__all__ = ["clean_state", "client", "task_module"]


@pytest.fixture
def manifest_storage(monkeypatch, tmp_path, task_module, clean_state):
    """Configure an isolated shared task and a private JSON file."""
    base = tmp_path / "shared"
    task_dir = base / "t1"
    task_dir.mkdir(parents=True)
    manifest = task_dir / "manifest.json"
    manifest.write_text(json.dumps({"task_id": "t1", "files": ["video.mp4"]}))
    private = tmp_path / "private"
    private.mkdir()
    (private / "manifest.json").write_text(json.dumps({"token": "private-test-secret"}))
    tasks["t1"] = make_task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(base))
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)
    monkeypatch.setattr(task_result_service.time, "sleep", lambda _: None)

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(task_module.asyncio, "to_thread", run_inline)
    return manifest, private


@pytest.mark.parametrize("target", ["private", "inside", "missing"])
def test_manifest_symlinks_rejected_before_read(client, manifest_storage, monkeypatch, target):
    manifest, private = manifest_storage
    destination = private / "manifest.json"
    if target != "private":
        destination = manifest.parent / target
        if target == "inside":
            destination.write_text('{"files": []}')
    manifest.unlink()
    manifest.symlink_to(destination)
    monkeypatch.setattr(
        task_result_service,
        "_read_manifest_file",
        lambda _: pytest.fail("A symbolic manifest must not be read"),
    )
    response = client.get("/task/result/t1")
    assert response.status_code == 500
    assert "private-test-secret" not in response.text
    assert tasks["t1"].status == "warning"


@pytest.mark.parametrize("swapped_part", ["manifest.json", "t1", "shared"])
def test_symlink_replacement_before_open_is_rejected(
    client, manifest_storage, monkeypatch, swapped_part
):
    manifest, private = manifest_storage
    original_open = os.open
    replaced = False

    def racing_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == swapped_part and not replaced:
            replaced = True
            victim = {
                "manifest.json": manifest,
                "t1": manifest.parent,
                "shared": manifest.parent.parent,
            }[swapped_part]
            victim.rename(victim.with_name(victim.name + ".old"))
            victim.symlink_to(private / "manifest.json" if victim == manifest else private)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(task_result_service.os, "open", racing_open)
    response = client.get("/task/result/t1")
    assert replaced
    assert response.status_code == 500
    assert "private-test-secret" not in response.text
    assert tasks["t1"].status == "warning"


@pytest.mark.parametrize("swap_directory", [False, True])
def test_open_manifest_descriptor_remains_bound_to_original_file(
    client, manifest_storage, monkeypatch, swap_directory
):
    manifest, private = manifest_storage
    original_fdopen = os.fdopen
    opened_files = []

    def racing_fdopen(fd, *args, **kwargs):
        victim = manifest.parent if swap_directory else manifest
        victim.rename(victim.with_name(victim.name + ".old"))
        victim.symlink_to(private if swap_directory else private / "manifest.json")
        file = original_fdopen(fd, *args, **kwargs)
        opened_files.append(file)
        return file

    monkeypatch.setattr(task_result_service.os, "fdopen", racing_fdopen)
    response = client.get("/task/result/t1")
    assert response.status_code == 200
    assert response.json() == {"task_id": "t1", "files": ["video.mp4"]}
    assert tasks["t1"].status == "completed"
    assert all(file.closed for file in opened_files)


@pytest.mark.parametrize("kind", ["fifo", "directory", "permission"])
def test_manifest_must_be_readable_regular_file(client, manifest_storage, monkeypatch, kind):
    manifest, _ = manifest_storage
    manifest.unlink()
    if kind == "fifo":
        os.mkfifo(manifest)
    elif kind == "directory":
        manifest.mkdir()
    else:
        original_open = os.open

        def deny_manifest(path, flags, *args, **kwargs):
            if path == "manifest.json":
                raise PermissionError("private path must not be disclosed")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(task_result_service.os, "open", deny_manifest)
    response = client.get("/task/result/t1")
    assert response.status_code == 500
    assert "private path" not in response.text
    assert tasks["t1"].status == "warning"


def test_manifest_retry_accepts_atomic_publication(client, manifest_storage, monkeypatch):
    manifest, _ = manifest_storage
    manifest.unlink()
    attempts = []

    def publish(_delay):
        attempts.append(1)
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text("{" if len(attempts) == 1 else '{"files": ["ready.mp4"]}')
        temporary.replace(manifest)

    monkeypatch.setattr(task_result_service.time, "sleep", publish)
    response = client.get("/task/result/t1")
    assert response.status_code == 200
    assert response.json() == {"task_id": "t1", "files": ["ready.mp4"]}
    assert len(attempts) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"{",
        b"null",
        b"[]",
        b'{"files": [1]}',
        b'{"files": {}}',
        b'{"task_id": "another-task"}',
    ],
)
def test_invalid_manifest_preserves_warning_status(client, manifest_storage, payload):
    manifest, _ = manifest_storage
    manifest.write_bytes(payload)
    response = client.get("/task/result/t1")
    assert response.status_code == 500
    assert tasks["t1"].status == "warning"


def test_only_manifest_fields_are_returned(client, manifest_storage):
    manifest, _ = manifest_storage
    manifest.write_text('{"files": [], "token": "private-test-secret"}')
    response = client.get("/task/result/t1")
    assert response.status_code == 200
    assert response.json() == {"task_id": "t1", "files": []}
