# manager/app/core/state.py
"""
Global state for Runner Manager.
Stores active runners and tasks to avoid circular imports.
Includes persistence with filelock for multi-worker safety.
"""

import atexit
import logging
from datetime import datetime
from typing import Any, Dict, MutableMapping, Optional

from app.core.config import config
from app.core.persistence import SafeDailyJSONPersistence
from app.core.runner_store import RunnerStore
from app.models.models import Runner, Task

logger = logging.getLogger(__name__)
IS_PRODUCTION = config.ENVIRONMENT.lower() == "production"

# Initialize persistence with safe retry mechanism
persistence = SafeDailyJSONPersistence(
    data_directory="data",
    lock_timeout=10,  # 10 second timeout for acquiring lock
    max_retries=3,  # Retry failed operations up to 3 times
)

# Stores runners and tasks to avoid circular imports
runners: MutableMapping[str, Runner] = RunnerStore(
    shared_enabled=config.ENVIRONMENT.lower() == "production",
)

# Load only today's tasks at startup
tasks_data = persistence.load_tasks()
tasks: Dict[str, Task] = {task_id: Task(**task_data) for task_id, task_data in tasks_data.items()}

logger.info(f"Loaded {len(tasks)} tasks from persistence")


def _parse_updated_at(value: Optional[str]) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return datetime.min


def _pick_newest_task(local_task: Task, persisted_task: Task) -> Task:
    """
    Resolve conflicts between local/persisted task versions by most recent updated_at.
    """
    local_ts = _parse_updated_at(getattr(local_task, "updated_at", None))
    persisted_ts = _parse_updated_at(getattr(persisted_task, "updated_at", None))
    return local_task if local_ts >= persisted_ts else persisted_task


def _merge_tasks_with_persistence() -> Dict[str, Task]:
    """
    Merge local in-memory tasks with persisted tasks, keeping freshest updates.
    """
    persisted_data = persistence.load_tasks()
    merged_tasks: Dict[str, Task] = {}

    for task_id, task_data in persisted_data.items():
        try:
            merged_tasks[task_id] = Task(**task_data)
        except Exception as exc:
            logger.warning(f"Skipping invalid persisted task {task_id}: {exc}")

    for task_id, local_task in tasks.items():
        if task_id in merged_tasks:
            merged_tasks[task_id] = _pick_newest_task(local_task, merged_tasks[task_id])
        else:
            merged_tasks[task_id] = local_task

    return merged_tasks


def save_tasks() -> bool:
    """
    Save current tasks to today's persistent storage.

    Returns:
        bool: True if save was successful
    """
    if IS_PRODUCTION:
        # Multi-worker mode: upsert merged state without deleting sibling worker tasks.
        merged_tasks = _merge_tasks_with_persistence()
        success = persistence.upsert_tasks(merged_tasks)
        if success:
            tasks.clear()
            tasks.update(merged_tasks)
    else:
        success = persistence.save_tasks(tasks)

    if success:
        logger.debug("Tasks successfully persisted to today's file")
    else:
        logger.error("Failed to persist tasks")
    return success


def get_task(task_id: str) -> Optional[Task]:
    """
    Retrieve a task from memory and, in production mode, fallback to persistence.
    """
    local_task = tasks.get(task_id)

    if not IS_PRODUCTION:
        return local_task

    persisted = persistence.load_task(task_id)
    persisted_task: Optional[Task] = None
    if persisted:
        try:
            persisted_task = Task(**persisted)
        except Exception as exc:
            logger.warning(f"Failed to load persisted task {task_id}: {exc}")

    if local_task is None and persisted_task is None:
        return None

    if local_task is None and persisted_task is not None:
        tasks[task_id] = persisted_task
        logger.info(f"Loaded task {task_id} from shared persistence")
        return persisted_task

    if local_task is not None and persisted_task is None:
        return local_task

    # Both versions exist: keep the freshest one to avoid stale per-worker caches.
    assert local_task is not None
    assert persisted_task is not None
    local_ts = _parse_updated_at(getattr(local_task, "updated_at", None))
    persisted_ts = _parse_updated_at(getattr(persisted_task, "updated_at", None))
    if persisted_ts > local_ts:
        tasks[task_id] = persisted_task
        logger.info(f"Refreshed task {task_id} from shared persistence")
        return persisted_task

    return local_task


def get_tasks_snapshot() -> Dict[str, Task]:
    """
    Return a consistent task snapshot.

    In production, refreshes from shared persistence and updates local cache.
    """
    if not IS_PRODUCTION:
        return dict(tasks)

    merged_tasks = _merge_tasks_with_persistence()
    tasks.clear()
    tasks.update(merged_tasks)
    return dict(merged_tasks)


def force_save_tasks() -> bool:
    """
    Force immediate save of tasks to today's file.

    Returns:
        bool: True if save was successful
    """
    logger.info("Force saving tasks to today's persistence file")
    return save_tasks()


def get_storage_info() -> Dict[str, Any]:
    """
    Get information about task storage.

    Returns:
        Dict with storage information
    """
    info: Dict[str, Any] = persistence.get_storage_info()
    return info


def cleanup_old_task_files(days_to_keep: int = 30) -> int:
    """
    Clean up old task files to prevent storage bloat.

    Args:
        days_to_keep: Number of days to keep files

    Returns:
        int: Number of files deleted
    """
    deleted_count: int = persistence.cleanup_old_files(days_to_keep)
    return deleted_count


def load_historical_tasks(start_date: datetime, end_date: datetime) -> Dict[str, Any]:
    """
    Load tasks from a date range for reporting purposes.
    Note: This does not affect the current in-memory tasks.

    Args:
        start_date: Start date of range
        end_date: End date of range

    Returns:
        Dict with historical tasks
    """
    historical_tasks: Dict[str, Any] = persistence.load_historical_tasks(
        start_date.date(), end_date.date()
    )
    return historical_tasks


# Register automatic save on application exit
def shutdown_handler():
    """Handle application shutdown with task persistence."""
    logger.info("Application shutting down, saving today's tasks...")
    save_tasks()

    # Clean up old files on shutdown
    deleted_count = cleanup_old_task_files(config.CLEANUP_TASK_FILES_DAYS)
    logger.info(f"Cleaned up {deleted_count} old task files")


atexit.register(shutdown_handler)
