"""Secure task result path resolution and cleanup."""

import re
import shutil
from pathlib import Path

from fastapi import HTTPException

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
RESULT_PATH_PART_RE = re.compile(r"^[A-Za-z0-9._ -]+$")


def _file_not_found() -> HTTPException:
    """Build the uniform response used for invalid or missing result paths."""
    return HTTPException(status_code=404, detail="File not found")


def validate_task_id(task_id: str) -> str:
    """Validate a task identifier before using it in filesystem paths."""
    safe_task_id = (task_id or "").strip()
    if not TASK_ID_RE.fullmatch(safe_task_id):
        raise _file_not_found()
    return safe_task_id


def validate_result_relative_path(file_path: str) -> tuple[str, ...]:
    """Validate a relative file path under a task output directory."""
    raw_path = (file_path or "").strip().replace("\\", "/")
    if not raw_path:
        raise _file_not_found()

    relative_path = Path(raw_path)
    if relative_path.is_absolute():
        raise _file_not_found()

    safe_parts = []
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            raise _file_not_found()
        if not RESULT_PATH_PART_RE.fullmatch(part):
            raise _file_not_found()
        safe_parts.append(part)

    if not safe_parts:
        raise _file_not_found()

    return tuple(safe_parts)


def resolve_storage_base_path(storage_base_path: str | Path) -> Path:
    """Resolve the configured storage base path."""
    return Path(storage_base_path).resolve()


def find_direct_child_entry(directory: Path, entry_name: str) -> Path | None:
    """Find a direct child entry by name without composing a user-controlled path."""
    try:
        for candidate in directory.iterdir():
            if candidate.name == entry_name:
                return candidate
    except OSError:
        return None
    return None


def resolve_within_base(candidate: Path, base_path: Path) -> Path:
    """Resolve a candidate path and enforce that it stays under ``base_path``."""
    resolved = candidate.resolve(strict=False)
    if resolved != base_path and base_path not in resolved.parents:
        raise _file_not_found()
    return resolved


def resolve_output_file_path(output_dir: Path, relative_parts: tuple[str, ...]) -> Path:
    """Resolve a relative output path by traversing directory entries safely."""
    current_path = output_dir

    for index, part in enumerate(relative_parts):
        next_candidate = find_direct_child_entry(current_path, part)
        if next_candidate is None:
            raise _file_not_found()

        next_path = resolve_within_base(next_candidate, output_dir)
        if index < len(relative_parts) - 1 and not next_path.is_dir():
            raise _file_not_found()

        current_path = next_path

    return current_path


def resolve_task_root(task_id: str, storage_base_path: str | Path) -> Path:
    """Resolve a task root directory and reject path traversal."""
    safe_task_id = validate_task_id(task_id)
    base_path = resolve_storage_base_path(storage_base_path)
    task_candidate = find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        raise _file_not_found()

    task_root = resolve_within_base(task_candidate, base_path)
    if not task_root.is_dir():
        raise _file_not_found()

    return task_root


def resolve_task_root_if_exists(task_id: str, storage_base_path: str | Path) -> Path | None:
    """Resolve a task root for delete flows, returning ``None`` when missing or invalid."""
    safe_task_id = validate_task_id(task_id)
    base_path = resolve_storage_base_path(storage_base_path)
    task_candidate = find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        return None

    try:
        task_root = resolve_within_base(task_candidate, base_path)
    except HTTPException:
        return None

    if not task_root.is_dir():
        return None

    return task_root


def resolve_task_manifest_path(task_id: str, storage_base_path: str | Path) -> Path:
    """Resolve the canonical manifest path (``<base>/<task_id>/manifest.json``)."""
    task_root = resolve_task_root(task_id, storage_base_path)
    manifest_candidate = find_direct_child_entry(task_root, "manifest.json")
    if manifest_candidate is None:
        raise _file_not_found()

    manifest_path = resolve_within_base(manifest_candidate, task_root)
    if manifest_path.parent != task_root:
        raise _file_not_found()
    if not manifest_path.is_file():
        raise _file_not_found()
    return manifest_path


def resolve_legacy_manifest_if_exists(
    task_id: str,
    storage_base_path: str | Path,
) -> Path | None:
    """Resolve a legacy flat manifest path (``<base>/<task_id>.json``) when present."""
    safe_task_id = validate_task_id(task_id)
    base_path = resolve_storage_base_path(storage_base_path)
    legacy_name = f"{safe_task_id}.json"

    candidate = find_direct_child_entry(base_path, legacy_name)
    if candidate is None:
        return None

    try:
        resolved = resolve_within_base(candidate, base_path)
    except HTTPException:
        return None

    if resolved.parent != base_path:
        return None
    if not resolved.is_file():
        return None

    return resolved


def resolve_task_result_file_path(
    task_id: str,
    file_path: str,
    storage_base_path: str | Path,
) -> Path:
    """Resolve one downloadable file from a task output directory."""
    task_root = resolve_task_root(task_id, storage_base_path)
    output_candidate = find_direct_child_entry(task_root, "output")
    if output_candidate is None:
        raise _file_not_found()

    output_dir = resolve_within_base(output_candidate, task_root)
    if not output_dir.is_dir():
        raise _file_not_found()

    relative_parts = validate_result_relative_path(file_path)
    result_path = resolve_output_file_path(output_dir, relative_parts)
    if not result_path.is_file():
        raise _file_not_found()

    return result_path


def delete_task_results(task_id: str, storage_base_path: str | Path) -> None:
    """Delete canonical task results and a legacy flat manifest when present."""
    task_root = resolve_task_root_if_exists(task_id, storage_base_path)
    if task_root is not None:
        shutil.rmtree(task_root)

    legacy_manifest = resolve_legacy_manifest_if_exists(task_id, storage_base_path)
    if legacy_manifest is not None:
        legacy_manifest.unlink()
