"""Regression tests for the task result service extraction."""

from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import FileResponse

from app.api.routes import task as task_routes
from app.services import task_results


def test_task_results_resolves_manifest_and_nested_output_file(tmp_path):
    task_root = tmp_path / "task-123"
    output_dir = task_root / "output" / "nested"
    output_dir.mkdir(parents=True)
    manifest_path = task_root / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    result_path = output_dir / "result file.txt"
    result_path.write_text("result", encoding="utf-8")

    assert task_results.resolve_task_manifest_path(" task-123 ", tmp_path) == manifest_path
    assert (
        task_results.resolve_task_result_file_path(
            "task-123",
            "nested/result file.txt",
            tmp_path,
        )
        == result_path
    )


def test_task_results_rejects_output_symlink_escape(tmp_path):
    output_dir = tmp_path / "task-link" / "output"
    output_dir.mkdir(parents=True)
    outside_dir = tmp_path.parent / f"{tmp_path.name}-outside-results"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
    (output_dir / "linked").symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(HTTPException) as exc_info:
        task_results.resolve_task_result_file_path(
            "task-link",
            "linked/secret.txt",
            tmp_path,
        )

    assert exc_info.value.status_code == 404


def test_task_results_deletes_canonical_and_legacy_results(tmp_path):
    task_root = tmp_path / "task-delete"
    task_root.mkdir()
    legacy_manifest = tmp_path / "task-delete.json"
    legacy_manifest.write_text("{}", encoding="utf-8")

    task_results.delete_task_results("task-delete", tmp_path)

    assert not task_root.exists()
    assert not legacy_manifest.exists()


@pytest.mark.asyncio
async def test_result_endpoints_delegate_to_result_service(monkeypatch, tmp_path):
    result_path = tmp_path / "result.txt"
    result_path.write_text("result", encoding="utf-8")
    resolved_calls: list[tuple[str, str]] = []
    deleted_calls: list[str] = []

    def resolve_result(task_id: str, file_path: str) -> Path:
        resolved_calls.append((task_id, file_path))
        return result_path

    monkeypatch.setattr(task_routes, "_resolve_task_result_file_path", resolve_result)
    monkeypatch.setattr(task_routes, "_delete_task_results", deleted_calls.append)
    monkeypatch.setattr(task_routes, "_refresh_availability_from_recovered_state", lambda: None)
    monkeypatch.setattr(task_routes.storage_manager, "base_path", str(tmp_path))

    assert task_routes._resolve_storage_base_path() == tmp_path.resolve()

    response = await task_routes.get_task_result_file(
        "task-route",
        "nested/result.txt",
        BackgroundTasks(),
        current_manager="manager-token",
    )
    deletion = await task_routes.delete_task_result(
        "task-route",
        current_manager="manager-token",
    )

    assert isinstance(response, FileResponse)
    assert response.path == str(result_path)
    assert resolved_calls == [("task-route", "nested/result.txt")]
    assert deleted_calls == ["task-route"]
    assert deletion == {"status": "deleted"}
