# manager/app/services/task_service.py
"""
Service for managing tasks and their lifecycle.
"""

import asyncio
from datetime import datetime
from typing import Dict, Optional

import httpx

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_task as get_task_from_state
from app.core.state import get_tasks_snapshot, runners, save_tasks, tasks
from app.models.models import Runner, Task

logger = setup_default_logging()

_RUNNER_REPORTED_STATUSES = {"running", "completed", "failed", "timeout"}


async def cleanup_old_tasks(
    poll_interval: float = 3600.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """
    Periodically clean up old tasks to prevent memory accumulation.

    Removes completed/failed tasks older than CLEANUP_TASK_FILES_DAYS.
    Runs every hour.
    """
    logger.info("Starting task cleanup service")
    cleanup_days = config.CLEANUP_TASK_FILES_DAYS
    cleanup_seconds = cleanup_days * 86400  # Convert days to seconds

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Stopping task cleanup service")
            break

        await asyncio.sleep(poll_interval)

        now = datetime.now()
        tasks_to_remove = []

        for task_id, task in tasks.items():
            created_at = datetime.fromisoformat(task.created_at.replace("Z", "+00:00"))
            task_age = now - created_at

            # Remove tasks completed/failed more than CLEANUP_TASK_FILES_DAYS ago
            if (
                task.status in ["completed", "failed"]
                and task_age.total_seconds() > cleanup_seconds
            ):
                tasks_to_remove.append(task_id)

        for task_id in tasks_to_remove:
            del tasks[task_id]
            logger.info(f"Task {task_id} cleaned up (age: {cleanup_days}+ days)")


async def check_task_timeouts(
    poll_interval: float = 600.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """
    Monitor tasks for timeouts and mark them appropriately.

    Checks every 10 minutes and marks running tasks older than 24 hours as timeout.
    """
    logger.info("Starting task timeout monitoring")
    while True:
        if stop_event and stop_event.is_set():
            logger.info("Stopping task timeout monitoring")
            break

        await asyncio.sleep(poll_interval)

        now = datetime.now()
        for task_id, task in tasks.items():
            if task.status == "running":
                updated_at = datetime.fromisoformat(task.updated_at.replace("Z", "+00:00"))
                task_duration = now - updated_at

                # Mark as timeout after 24 hours
                if task_duration.total_seconds() > 86400:
                    tasks[task_id].status = "timeout"
                    tasks[task_id].error = "Task timeout after 24 hours"
                    tasks[task_id].updated_at = datetime.now().isoformat()
                    logger.warning(f"Task {task_id} marked as timeout")


def _build_runner_task_status_headers(runner: Runner) -> dict[str, str] | None:
    """Build manager->runner auth headers for task status polling."""
    token = getattr(runner, "token", None)
    if not token:
        return None
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


async def _fetch_runner_task_status(runner: Runner, task_id: str) -> dict | None:
    """Query runner status endpoint for one task."""
    headers = _build_runner_task_status_headers(runner)
    if headers is None:
        logger.warning(
            "Skipping task status reconciliation for task %s: runner %s has no token",
            task_id,
            runner.id,
        )
        return None

    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    url = f"{runner.url}/task/status/{task_id}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        logger.warning(
            "Task status reconciliation failed for %s on runner %s: %s",
            task_id,
            runner.id,
            exc,
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "Task status reconciliation got HTTP %s for task %s on runner %s",
            response.status_code,
            task_id,
            runner.id,
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "Task status reconciliation got invalid JSON for task %s on runner %s",
            task_id,
            runner.id,
        )
        return None

    if not isinstance(payload, dict):
        logger.warning(
            "Task status reconciliation got non-object payload for task %s on runner %s",
            task_id,
            runner.id,
        )
        return None

    return payload


def _apply_runner_task_status(task: Task, payload: dict) -> bool:
    """Apply a runner-reported status on a manager task.

    Returns True when the task was modified.
    """
    raw_status = payload.get("status")
    normalized_status = str(raw_status).strip().lower() if raw_status is not None else ""
    if normalized_status not in _RUNNER_REPORTED_STATUSES:
        return False

    now_iso = datetime.now().isoformat()
    if normalized_status == "running":
        return _refresh_running_task(task, now_iso)

    return _apply_terminal_runner_task_status(task, normalized_status, payload, now_iso)


def _refresh_running_task(task: Task, now_iso: str) -> bool:
    """Refresh updated_at when runner confirms task is still running."""
    if task.updated_at == now_iso:
        return False
    task.updated_at = now_iso
    return True


def _apply_terminal_runner_task_status(
    task: Task,
    normalized_status: str,
    payload: dict,
    now_iso: str,
) -> bool:
    """Apply completed/failed/timeout status payload to a manager task."""
    changed = False

    if task.status != normalized_status:
        task.status = normalized_status
        changed = True

    if task.updated_at != now_iso:
        task.updated_at = now_iso
        changed = True

    if _apply_terminal_error(task, normalized_status, payload):
        changed = True

    if _apply_script_output(task, payload):
        changed = True

    return changed


def _apply_terminal_error(task: Task, normalized_status: str, payload: dict) -> bool:
    """Apply terminal error details from runner payload."""
    if normalized_status == "completed":
        if task.error is None:
            return False
        task.error = None
        return True

    error_message = payload.get("error_message")
    if not error_message:
        return False

    error_text = str(error_message)
    if task.error == error_text:
        return False
    task.error = error_text
    return True


def _apply_script_output(task: Task, payload: dict) -> bool:
    """Apply script output from runner payload when provided."""
    script_output = payload.get("script_output")
    if not isinstance(script_output, str) or not script_output:
        return False
    if task.script_output == script_output:
        return False
    task.script_output = script_output
    return True


def _get_running_task_ids() -> list[str]:
    """Return IDs of tasks currently marked as running in manager snapshot."""
    snapshot = get_tasks_snapshot()
    return [task_id for task_id, task in snapshot.items() if task.status == "running"]


async def _reconcile_single_running_task(task_id: str) -> bool:
    """Reconcile one running task against its assigned runner."""
    task = get_task_from_state(task_id)
    if task is None or task.status != "running":
        return False

    runner = runners.get(task.runner_id)
    if runner is None:
        logger.warning(
            "Skipping task status reconciliation for %s: runner %s not found",
            task_id,
            task.runner_id,
        )
        return False

    payload = await _fetch_runner_task_status(runner, task_id)
    if payload is None:
        return False

    if not _apply_runner_task_status(task, payload):
        return False

    logger.info(
        "Task %s reconciled from runner %s: status=%s",
        task_id,
        runner.id,
        task.status,
    )
    return True


async def _reconcile_running_tasks_once() -> int:
    """Execute one reconciliation pass and persist state when tasks changed."""
    changed_count = 0

    for task_id in _get_running_task_ids():
        if await _reconcile_single_running_task(task_id):
            changed_count += 1

    if changed_count > 0:
        save_tasks()

    return changed_count


async def reconcile_running_tasks_with_runners(
    poll_interval: float = 3600.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """
    Periodically reconcile manager running tasks with runner-reported statuses.

    Runs every hour by default and updates each running task based on runner
    status endpoint responses (`running`, `completed`, `failed`, `timeout`).
    """
    logger.info("Starting running-task reconciliation service")
    while True:
        if stop_event and stop_event.is_set():
            logger.info("Stopping running-task reconciliation service")
            break

        await asyncio.sleep(poll_interval)

        try:
            await _reconcile_running_tasks_once()
        except Exception as exc:
            logger.error("Error during running-task reconciliation: %s", exc, exc_info=True)


def update_task_status(task_id: str, status: str, error_message: Optional[str] = None) -> bool:
    """
    Update the status of a task.

    Args:
        task_id: Unique identifier of the task
        status: New status of the task
        error_message: Optional error message if task failed

    Returns:
        bool: True if task was found and updated
    """
    if task_id not in tasks:
        return False

    tasks[task_id].status = status
    tasks[task_id].updated_at = datetime.now().isoformat()

    if status == "failed" and error_message:
        tasks[task_id].error = error_message

    logger.info(f"Task {task_id} status updated to {status}")
    return True


def get_task(task_id: str) -> Optional[Task]:
    """
    Retrieve a task by its ID.

    Args:
        task_id: Unique identifier of the task

    Returns:
        Optional[Task]: Task if found, None otherwise
    """
    return get_task_from_state(task_id)


def get_all_tasks() -> Dict[str, Task]:
    """
    Get all tasks with their current status.

    Returns:
        Dict[str, Task]: Dictionary of task IDs to task objects
    """
    all_tasks: Dict[str, Task] = tasks
    return all_tasks


def get_task_stats() -> Dict[str, int]:
    """
    Get statistics about tasks.

    Returns:
        Dict[str, int]: Task statistics by status
    """
    status_counts: Dict[str, int] = {}
    for task in tasks.values():
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    stats: Dict[str, int] = {
        "total": len(tasks),
        "completed": len([t for t in tasks.values() if t.status == "completed"]),
        "failed": len([t for t in tasks.values() if t.status == "failed"]),
        "running": len([t for t in tasks.values() if t.status == "running"]),
        "pending": len([t for t in tasks.values() if t.status == "pending"]),
        "warning": len([t for t in tasks.values() if t.status == "warning"]),
        "timeout": len([t for t in tasks.values() if t.status == "timeout"]),
        **status_counts,
    }
    return stats
