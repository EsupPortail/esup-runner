# manager/app/core/state.py
"""
Global state for Runner Manager.
Stores active runners and tasks to avoid circular imports.
Includes persistence with filelock for multi-worker safety.
"""

import atexit
import logging
from datetime import datetime
from typing import Any, Dict

from app.core.config import config
from app.core.persistence import SafeDailyJSONPersistence
from app.models.models import Runner, Task

logger = logging.getLogger(__name__)

# Initialize persistence with safe retry mechanism
persistence = SafeDailyJSONPersistence(
    data_directory="data",
    lock_timeout=10,  # 10 second timeout for acquiring lock
    max_retries=3,  # Retry failed operations up to 3 times
)

# Stores runners and tasks to avoid circular imports
runners: Dict[str, Runner] = {}

# Load only today's tasks at startup
tasks_data = persistence.load_tasks()
tasks: Dict[str, Task] = {task_id: Task(**task_data) for task_id, task_data in tasks_data.items()}

logger.info(f"Loaded {len(tasks)} tasks from persistence")


def save_tasks() -> bool:
    """
    Save current tasks to today's persistent storage.

    Returns:
        bool: True if save was successful
    """
    success: bool = persistence.save_tasks(tasks)
    if success:
        logger.debug("Tasks successfully persisted to today's file")
    else:
        logger.error("Failed to persist tasks")
    return success


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
