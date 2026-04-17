# runner/app/api/routes/task.py
"""
Task management routes for Runner.
Handles task execution, status tracking, and result streaming endpoints.
"""

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.auth import get_current_manager
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import (
    get_task_status,
    is_available,
    is_registered,
    set_available,
    set_task_status,
)
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest, TaskResultResponse
from app.services.email_service import send_task_failure_email
from app.services.task_dispatcher import task_dispatcher

# Configure logging
logger = setup_default_logging()

# Create API router for task-related endpoints
router = APIRouter(prefix="/task", tags=["Task"])

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RESULT_PATH_PART_RE = re.compile(r"^[A-Za-z0-9._ -]+$")

# ======================================================
# Utility Functions
# ======================================================


def _validate_task_id(task_id: str) -> str:
    """Validate a task identifier before using it in filesystem paths."""
    safe_task_id = (task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(safe_task_id):
        raise HTTPException(status_code=404, detail="File not found")
    return safe_task_id


def _validate_result_relative_path(file_path: str) -> tuple[str, ...]:
    """Validate a relative file path under a task output directory."""
    raw_path = (file_path or "").strip().replace("\\", "/")
    if not raw_path:
        raise HTTPException(status_code=404, detail="File not found")

    relative_path = Path(raw_path)
    if relative_path.is_absolute():
        raise HTTPException(status_code=404, detail="File not found")

    safe_parts = []
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            raise HTTPException(status_code=404, detail="File not found")
        if not _RESULT_PATH_PART_RE.fullmatch(part):
            raise HTTPException(status_code=404, detail="File not found")
        safe_parts.append(part)

    if not safe_parts:
        raise HTTPException(status_code=404, detail="File not found")

    return tuple(safe_parts)


def _resolve_storage_base_path() -> Path:
    """Resolve storage base path."""
    return Path(storage_manager.base_path).resolve()


def _find_direct_child_entry(directory: Path, entry_name: str) -> Path | None:
    """Find a direct child entry by name without composing a user-controlled path."""
    try:
        for candidate in directory.iterdir():
            if candidate.name == entry_name:
                return candidate
    except OSError:
        return None
    return None


def _resolve_within_base(candidate: Path, base_path: Path) -> Path:
    """Resolve a candidate path and enforce that it stays under base_path."""
    resolved = candidate.resolve(strict=False)
    if resolved != base_path and base_path not in resolved.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def _resolve_output_file_path(output_dir: Path, relative_parts: tuple[str, ...]) -> Path:
    """Resolve a relative output path by traversing directory entries safely."""
    current_path = output_dir

    for index, part in enumerate(relative_parts):
        next_candidate = _find_direct_child_entry(current_path, part)
        if next_candidate is None:
            raise HTTPException(status_code=404, detail="File not found")

        next_path = _resolve_within_base(next_candidate, output_dir)
        if index < len(relative_parts) - 1 and not next_path.is_dir():
            raise HTTPException(status_code=404, detail="File not found")

        current_path = next_path

    return current_path


def _derive_failure_status(error_message: str) -> str:
    """Derive a failure status from the error message.

    Returns "timeout" when the message indicates a timeout; otherwise "failed".
    """
    if "timeout" in (error_message or "").lower():
        return "timeout"
    return "failed"


def _normalize_script_output(script_output: object) -> Optional[str]:
    """Normalize script output into a serializable string for status payloads."""
    if script_output is None:
        return None
    if isinstance(script_output, str):
        return script_output
    try:
        return json.dumps(script_output, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(script_output)


def _resolve_task_manifest_path(task_id: str) -> Path:
    """Resolve canonical manifest path (<base>/<task_id>/manifest.json)."""
    task_root = _resolve_task_root(task_id)
    manifest_candidate = _find_direct_child_entry(task_root, "manifest.json")
    if manifest_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    manifest_path = _resolve_within_base(manifest_candidate, task_root)
    if manifest_path.parent != task_root:
        raise HTTPException(status_code=404, detail="File not found")
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return manifest_path


def _resolve_task_root(task_id: str) -> Path:
    """Resolve task root directory and reject path traversal."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    task_candidate = _find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    task_root = _resolve_within_base(task_candidate, base_path)
    if not task_root.is_dir():
        raise HTTPException(status_code=404, detail="File not found")

    return task_root


def _resolve_task_root_if_exists(task_id: str) -> Path | None:
    """Resolve task root for delete flows (None when missing/invalid)."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    task_candidate = _find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        return None

    try:
        task_root = _resolve_within_base(task_candidate, base_path)
    except HTTPException:
        return None

    if not task_root.is_dir():
        return None

    return task_root


def _resolve_legacy_manifest_if_exists(task_id: str) -> Path | None:
    """Resolve legacy flat manifest path (<base>/<task_id>.json) when present."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    legacy_name = f"{safe_task_id}.json"

    candidate = _find_direct_child_entry(base_path, legacy_name)
    if candidate is None:
        return None

    try:
        resolved = _resolve_within_base(candidate, base_path)
    except HTTPException:
        return None

    if resolved.parent != base_path:
        return None
    if not resolved.is_file():
        return None

    return resolved


async def process_task(task_id: str, task_request: TaskRequest):
    """
    Process task using task dispatcher.
    """
    completion_callback = task_request.completion_callback
    try:
        # Mark runner as busy
        set_available(False)
        set_task_status(task_id, "running")

        logger.info(
            f"Starting task {task_id} of type {task_request.task_type} with parameters: {task_request.parameters}"
        )

        # Dispatch task to appropriate handler
        results = await task_dispatcher.dispatch_task(task_id=task_id, task_request=task_request)

        # Notify manager of task completion if callback provided
        if results.get("success"):
            script_output_text = _normalize_script_output(results.get("script_output"))
            set_task_status(task_id, "completed", script_output=script_output_text)
            logger.info(f"Task {task_id} completed successfully")
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    "completed",
                    None,
                    script_output_text,
                )
        else:
            error_msg = results.get("error", "Unknown error")
            failure_status = _derive_failure_status(error_msg)
            # Detailed error from script output if available
            script_output_text = _normalize_script_output(results.get("script_output"))
            set_task_status(
                task_id,
                failure_status,
                error_message=error_msg,
                script_output=script_output_text,
            )
            logger.error(f"Task {task_id} failed: {error_msg}")
            logger.info(f"Sending failure email for task {task_id}")
            await send_task_failure_email(
                task_id=task_id,
                task_type=task_request.task_type,
                status=failure_status,
                error_message=error_msg,
                script_output=script_output_text,
            )
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    failure_status,
                    error_msg,
                    script_output_text,
                )

    except Exception as e:
        error_msg = str(e)
        failure_status = _derive_failure_status(error_msg)
        set_task_status(task_id, failure_status, error_message=error_msg)
        logger.error(f"Error processing task {task_id}: {error_msg}")
        logger.info(f"Sending failure email for task {task_id}")
        await send_task_failure_email(
            task_id=task_id,
            task_type=task_request.task_type,
            status=failure_status,
            error_message=error_msg,
        )
        if completion_callback:
            await notify_completion(completion_callback, task_id, failure_status, error_msg)
    finally:
        # Mark runner as available again
        set_available(True)


@router.get(
    "/status/{task_id}",
    response_model=dict,
    summary="Get task execution status",
    description="Return the runner-side status for a specific task",
    tags=["Task"],
)
async def get_task_status_endpoint(
    task_id: str,
    current_manager: str = Depends(get_current_manager),
) -> dict:
    """
    Return the current task status known by the runner.

    Status source priority:
    1) In-memory task status tracked during execution.
    2) Result manifest presence on disk (completed).
    3) Unknown task (not_found).
    """
    safe_task_id = _validate_task_id(task_id)

    tracked_status = get_task_status(safe_task_id)
    if tracked_status is not None:
        return tracked_status

    try:
        _resolve_task_manifest_path(safe_task_id)
        return {"task_id": safe_task_id, "status": "completed"}
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    return {"task_id": safe_task_id, "status": "not_found"}


async def notify_completion(
    callback_url: str,
    task_id: str,
    status: str,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
):
    """
    Notify manager about task completion status.

    Args:
        callback_url: Manager endpoint to receive notification
        task_id: Task identifier being reported
        status: Completion status ('completed' or 'failed')
        error_message: Optional error details for failed tasks
        script_output: Optional script output for debugging
    """
    max_retries = max(0, int(config.COMPLETION_NOTIFY_MAX_RETRIES))
    base_delay = max(0, int(config.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS))
    backoff_factor = max(1.0, float(config.COMPLETION_NOTIFY_BACKOFF_FACTOR))
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)

    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    callback_url,
                    json={
                        "task_id": task_id,
                        "status": status,
                        "error_message": error_message,
                        "script_output": script_output,
                    },
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    },
                )

                if response.status_code == 200:
                    if attempt > 1:
                        logger.info(
                            f"Completion notification sent for task {task_id} after {attempt} attempts"
                        )
                    else:
                        logger.info(f"Completion notification sent for task {task_id}")
                    return True

                logger.warning(
                    "Completion notification failed (attempt %s/%s): %s - %s",
                    attempt,
                    max_retries + 1,
                    response.status_code,
                    response.text,
                )

        except Exception as e:
            logger.error(
                "Error sending completion notification to %s (attempt %s/%s): %s",
                callback_url,
                attempt,
                max_retries + 1,
                str(e),
            )

        if attempt > max_retries:
            return False

        delay = int(base_delay * (backoff_factor ** (attempt - 1)))
        if delay > 0:  # pragma: no cover - timing branch covered by config
            logger.warning(
                "Retrying completion notification for task %s in %s seconds (attempt %s/%s)",
                task_id,
                delay,
                attempt + 1,
                max_retries + 1,
            )
            await asyncio.sleep(delay)


# ======================================================
# API Endpoints
# ======================================================


@router.get(
    "/result/{task_id}",
    responses={
        200: {"description": "Task result manifest", "content": {"application/json": {}}},
        404: {"description": "Result file not found"},
    },
    summary="Get task result",
    description="Stream task result directly from runner storage",
    tags=["Task"],
)
async def get_task_result(
    task_id: str,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Stream task result directly from runner storage.

    Args:
        task_id: Unique identifier of the task
        background_tasks: FastAPI background tasks for cleanup
        current_manager: Authenticated manager instance

    Returns:
        FileResponse: Streamed JSON manifest containing task results
    """
    logger.info(f"Retrieving task result: {task_id}")

    file_path = _resolve_task_manifest_path(task_id)

    # Schedule cleanup after file delivery if necessary
    # background_tasks.add_task(storage_manager.cleanup, task_id)

    # Return file with streaming support
    return FileResponse(
        str(file_path),
        media_type="application/json",
        filename="manifest.json",
    )


@router.get(
    "/result/{task_id}/file/{filename}",
    responses={
        200: {"description": "Task result file", "content": {"application/octet-stream": {}}},
        404: {"description": "Result file not found"},
    },
    summary="Get task result file",
    description="Stream a single task output file from runner storage",
    tags=["Task"],
)
async def get_task_result_file(
    task_id: str,
    filename: str,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Stream a single task output file from runner storage.

    Args:
        task_id: Unique identifier of the task
        filename: Relative file path from the task output directory
        background_tasks: FastAPI background tasks for cleanup
        current_manager: Authenticated manager instance

    Returns:
        FileResponse: Streamed file content
    """
    logger.info(f"Retrieving task result file: {task_id}/{filename}")

    task_root = _resolve_task_root(task_id)
    output_candidate = _find_direct_child_entry(task_root, "output")
    if output_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    output_dir = _resolve_within_base(output_candidate, task_root)
    if not output_dir.is_dir():
        raise HTTPException(status_code=404, detail="File not found")

    safe_relative_parts = _validate_result_relative_path(filename)
    file_path = _resolve_output_file_path(output_dir, safe_relative_parts)

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        str(file_path),
        media_type="application/octet-stream",
        filename=file_path.name,
    )


@router.delete(
    "/delete/{task_id}",
    response_model=TaskResultResponse,
    summary="Delete task result",
    description="Remove result files for a specific task",
    tags=["Storage"],
)
async def delete_task_result(task_id: str, current_manager: str = Depends(get_current_manager)):
    """
    Delete task result files and free up storage.

    Args:
        task_id: Unique identifier of the task to delete
        current_manager: Authenticated manager instance

    Returns:
        dict: Deletion status confirmation
    """
    task_root = _resolve_task_root_if_exists(task_id)
    if task_root is not None:
        shutil.rmtree(task_root)

    # Backward compatibility cleanup for legacy flat manifest files.
    legacy_manifest = _resolve_legacy_manifest_if_exists(task_id)
    if legacy_manifest is not None:
        legacy_manifest.unlink()

    # Mark runner as available
    set_available(True)
    return {"status": "deleted"}


@router.post(
    "/run",
    response_model=dict,
    summary="Execute task",
    description="Start task execution on the runner",
    tags=["Task"],
)
async def run_task(
    task_request: TaskRequest,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Initiate task execution on the runner.

    Args:
        task_request: Task execution request details
        background_tasks: FastAPI background tasks for async processing
        current_manager: Authenticated manager instance

    Returns:
        dict: Task execution status and identifier
    """
    logger.info(f"Received task execution request, registered: {is_registered()}")

    # Verify runner is registered with manager
    if not is_registered():
        raise HTTPException(status_code=503, detail="Runner not registered with manager")

    if not is_available():
        raise HTTPException(status_code=400, detail="Runner is busy")

    # Mark runner as busy
    set_available(False)
    set_task_status(task_request.task_id, "running")

    # Start task processing in background
    background_tasks.add_task(
        process_task, task_id=task_request.task_id, task_request=task_request
    )  # pragma: no cover (background scheduling side-effect)

    return {"status": "started", "task_id": task_request.task_id}  # pragma: no cover
