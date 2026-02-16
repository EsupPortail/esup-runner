# manager/app/services/task_service.py
"""
Service for managing tasks and their lifecycle.
"""

import asyncio
from datetime import datetime
from typing import Dict, Optional

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_task as get_task_from_state
from app.core.state import tasks
from app.models.models import Task

logger = setup_default_logging()


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
        **status_counts,
    }
    return stats
