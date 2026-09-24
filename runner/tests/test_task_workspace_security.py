"""Task admission and execution reject unsafe workspace paths."""

from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.routes import task as task_routes
from app.models.models import TaskRequest
from app.services.task_dispatcher import TaskDispatcher
from app.services.task_results import resolve_task_workspace
from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler
from app.task_handlers.studio.studio_handler import StudioEncodingHandler
from app.task_handlers.transcription.transcription_handler import TranscriptionHandler


def _request(task_id, task_type="studio"):
    return TaskRequest(
        task_id=task_id,
        task_type=task_type,
        etab_name="Test",
        app_name="Test",
        source_url="https://example.org/input.mp4",
        notify_url="http://manager/notify",
    )


@pytest.fixture
def isolated_admission(monkeypatch, tmp_path):
    monkeypatch.setattr(task_routes.storage_manager, "base_path", str(tmp_path / "storage"))
    monkeypatch.setattr(task_routes, "is_registered", lambda: True)
    monkeypatch.setattr(task_routes, "is_available", lambda: True)
    for name in ("set_available", "set_task_status", "set_task_metadata"):
        monkeypatch.setattr(
            task_routes, name, lambda *_a, **_k: pytest.fail("State must be preserved")
        )
    return tmp_path / "storage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_id",
    [
        "../outside",
        "/tmp/outside",
        "nested/task",
        "..",
        ".",
        "",
        " task ",
        "task/../outside",
        "task\\outside",
        "task\x00",
    ],
)
async def test_unsafe_id_rejected_before_state_or_scheduling(isolated_admission, task_id):
    background = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await task_routes.run_task(_request(task_id), background, current_manager="test")
    assert exc.value.status_code == 400
    assert background.tasks == []
    assert not isolated_admission.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", ["workspace", "output"])
@pytest.mark.parametrize("inside", [False, True])
async def test_symbolic_workspace_rejected_before_scheduling(
    isolated_admission, tmp_path, directory, inside
):
    base = isolated_admission
    base.mkdir()
    target = (base if inside else tmp_path) / "target"
    target.mkdir()
    marker = target / "marker"
    marker.write_text("preserved")
    workspace = base / "task"
    link = workspace
    if directory == "output":
        workspace.mkdir()
        link = workspace / "output"
    link.symlink_to(target, target_is_directory=True)
    background = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        await task_routes.run_task(_request("task"), background, current_manager="test")
    assert exc.value.status_code == 400
    assert background.tasks == []
    assert marker.read_text() == "preserved"


@pytest.mark.asyncio
async def test_valid_workspace_allows_existing_task_restart(isolated_admission, monkeypatch):
    workspace = isolated_admission / "task-123_abc"
    (workspace / "output").mkdir(parents=True)
    marker = workspace / "output" / "existing.mp4"
    marker.write_bytes(b"existing")
    changes = []
    for name in ("set_available", "set_task_status", "set_task_metadata"):
        monkeypatch.setattr(task_routes, name, lambda *a, **k: changes.append((a, k)))
    background = BackgroundTasks()
    request = _request("task-123_abc")
    response = await task_routes.run_task(request, background, current_manager="test")
    assert response == {"status": "started", "task_id": request.task_id}
    assert changes[0] == ((False,), {})
    assert changes[1] == ((request.task_id, "running"), {})
    assert len(background.tasks) == 1
    assert background.tasks[0].kwargs["task_id"] == request.task_id
    assert marker.read_bytes() == b"existing"


@pytest.mark.asyncio
async def test_dispatcher_revalidates_id_for_recovery(isolated_admission):
    result = await TaskDispatcher().dispatch_task("../outside", _request("../outside"))
    assert result["success"] is False
    assert not isolated_admission.exists()


@pytest.mark.parametrize(
    "handler_class", [VideoEncodingHandler, StudioEncodingHandler, TranscriptionHandler]
)
def test_direct_handler_execution_rejects_symbolic_workspace(
    isolated_admission, tmp_path, monkeypatch, handler_class
):
    isolated_admission.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (isolated_admission / "task").symlink_to(target, target_is_directory=True)
    handler = handler_class()
    monkeypatch.setattr(handler, "prepare_workspace", lambda: pytest.fail("No file may be written"))
    result = handler.execute_task("task", _request("task", handler.task_type))
    assert result["success"] is False
    assert list(target.iterdir()) == []


def test_resolved_workspace_must_remain_below_base(tmp_path, monkeypatch):
    base = tmp_path / "storage"
    original_resolve = Path.resolve

    def displaced_resolve(path, *args, **kwargs):
        if path == base / "task":
            return tmp_path / "outside"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", displaced_resolve)
    with pytest.raises(HTTPException):
        resolve_task_workspace("task", base)
