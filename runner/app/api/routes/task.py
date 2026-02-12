# runner/app/api/routes/task.py
"""
Task management routes for Runner.
Handles task execution, status tracking, and result streaming endpoints.
"""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.auth import get_current_manager
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import is_available, is_registered, set_available
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest, TaskResultResponse
from app.services.email_service import send_task_failure_email
from app.services.task_dispatcher import task_dispatcher

# Configure logging
logger = setup_default_logging()

# Create API router for task-related endpoints
router = APIRouter(prefix="/task", tags=["Task"])

# ======================================================
# Utility Functions
# ======================================================


def _derive_failure_status(error_message: str) -> str:
    """Derive a failure status from the error message.

    Returns "timeout" when the message indicates a timeout; otherwise "failed".
    """
    if "timeout" in (error_message or "").lower():
        return "timeout"
    return "failed"


def _resolve_task_manifest_path(task_id: str) -> Path:
    """Resolve canonical manifest path (<base>/<task_id>/manifest.json)."""
    task_root = _resolve_task_root(task_id)
    manifest_path = task_root / "manifest.json"
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return manifest_path


def _resolve_task_root(task_id: str) -> Path:
    """Resolve task root directory and reject path traversal."""
    base_path = Path(storage_manager.base_path).resolve()
    task_root = (base_path / task_id).resolve(strict=False)

    if task_root != base_path and base_path not in task_root.parents:
        raise HTTPException(status_code=404, detail="File not found")

    return task_root


async def process_task(task_id: str, task_request: TaskRequest):
    """
    Process task using task dispatcher.
    """
    completion_callback = task_request.completion_callback
    try:
        # Mark runner as busy
        set_available(False)

        logger.info(
            f"Starting task {task_id} of type {task_request.task_type} with parameters: {task_request.parameters}"
        )

        # Dispatch task to appropriate handler
        results = await task_dispatcher.dispatch_task(task_id=task_id, task_request=task_request)

        # Notify manager of task completion if callback provided
        if results.get("success"):
            script_output = results.get("script_output", "")
            logger.info(f"Task {task_id} completed successfully")
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    "completed",
                    None,
                    json.dumps(script_output, indent=2, ensure_ascii=False),
                )
        else:
            error_msg = results.get("error", "Unknown error")
            failure_status = _derive_failure_status(error_msg)
            # Detailed error from script output if available
            script_output = results.get("script_output", "")
            logger.error(f"Task {task_id} failed: {error_msg}")
            logger.info(f"Sending failure email for task {task_id}")
            await send_task_failure_email(
                task_id=task_id,
                task_type=task_request.task_type,
                status=failure_status,
                error_message=error_msg,
                script_output=json.dumps(script_output, indent=2, ensure_ascii=False),
            )
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    failure_status,
                    error_msg,
                    json.dumps(script_output, indent=2, ensure_ascii=False),
                )

    except Exception as e:
        error_msg = str(e)
        failure_status = _derive_failure_status(error_msg)
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

    output_dir = _resolve_task_root(task_id) / "output"
    if not output_dir.exists():
        raise HTTPException(status_code=404, detail="File not found")

    output_dir_resolved = output_dir.resolve()
    file_path = (output_dir / filename).resolve()
    try:
        if not file_path.is_relative_to(output_dir_resolved):
            raise HTTPException(status_code=404, detail="File not found")
    except AttributeError:  # pragma: no cover - only for Python<3.9
        if output_dir_resolved not in file_path.parents:
            raise HTTPException(status_code=404, detail="File not found")

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
    task_root = _resolve_task_root(task_id)
    if task_root.exists():
        shutil.rmtree(task_root)

    # Backward compatibility cleanup for legacy flat manifest files.
    try:
        legacy_manifest = Path(storage_manager.get_path(task_id))
    except ValueError:
        legacy_manifest = None

    if legacy_manifest and legacy_manifest.exists():
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

    # Start task processing in background
    background_tasks.add_task(
        process_task, task_id=task_request.task_id, task_request=task_request
    )  # pragma: no cover (background scheduling side-effect)

    return {"status": "started", "task_id": task_request.task_id}  # pragma: no cover
