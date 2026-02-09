# manager/app/core/persistence.py
"""
Module for JSON-based persistence with daily directory rotation.
Each day gets its own directory, with one JSON file per task.
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from filelock import FileLock, Timeout

from app.models.models import Task

logger = logging.getLogger(__name__)


class DailyJSONPersistence:
    """
    JSON-based persistence with daily directory rotation.
    Each day gets its own directory, with one JSON file per task.
    """

    def __init__(self, data_directory: str = "data", lock_timeout: int = 10):
        self.data_directory = Path(data_directory)
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.lock_timeout = lock_timeout
        self._current_date: Optional[date] = None
        self._current_lock: Optional[FileLock] = None

    def _get_date_suffix(self, target_date: Optional[date] = None) -> str:
        """
        Get directory suffix for a specific date.

        Args:
            target_date: Date to use (defaults to current date)

        Returns:
            str: Date suffix in YYYY-MM-DD format
        """
        if target_date is None:
            target_date = datetime.now().date()
        return target_date.strftime("%Y-%m-%d")

    def _get_directory_path(self, target_date: Optional[date] = None) -> Path:
        """
        Get directory path for a specific date.

        Args:
            target_date: Date to use (defaults to current date)

        Returns:
            Path: Full path to the date directory
        """
        date_suffix = self._get_date_suffix(target_date)
        return self.data_directory / date_suffix

    def _get_task_file_path(self, task_id: str, target_date: Optional[date] = None) -> Path:
        """
        Get file path for a specific task.

        Args:
            task_id: ID of the task
            target_date: Date to use (defaults to current date)

        Returns:
            Path: Full path to the task JSON file
        """
        directory = self._get_directory_path(target_date)
        # Sanitize task_id for filename
        safe_task_id = task_id.replace("/", "_").replace("\\", "_")
        return directory / f"{safe_task_id}.json"

    def _get_lock_path(self, target_date: Optional[date] = None) -> Path:
        """
        Get lock file path for a specific date.

        Args:
            target_date: Date to use (defaults to current date)

        Returns:
            Path: Full path to the lock file
        """
        directory = self._get_directory_path(target_date)
        return directory / ".lock"

    def _get_current_lock(self) -> FileLock:
        """
        Get lock for current date. Creates new lock if date changed.

        Returns:
            FileLock: Lock for current date's directory
        """
        current_date = datetime.now().date()

        # Create new lock if date changed or lock doesn't exist
        if self._current_date != current_date or self._current_lock is None:
            self._current_date = current_date
            lock_path = self._get_lock_path(current_date)
            # Ensure directory exists
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._current_lock = FileLock(lock_path, timeout=self.lock_timeout)

        assert self._current_lock is not None  # Always set in the if block above
        return self._current_lock

    def save_tasks(self, tasks: Dict[str, Task]) -> bool:
        """
        Save tasks to today's directory with one JSON file per task.

        Args:
            tasks: Dictionary of tasks to save

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            directory = self._get_directory_path()
            directory.mkdir(parents=True, exist_ok=True)

            # Use filelock for atomic write operation
            lock = self._get_current_lock()
            with lock:
                # Get existing task files to detect deletions
                existing_files = set(directory.glob("*.json"))
                current_task_files = set()

                # Save each task to its own file
                for task_id, task in tasks.items():
                    task_file = self._get_task_file_path(task_id)
                    current_task_files.add(task_file)

                    # Convert Task object to dictionary with metadata
                    if hasattr(task, "model_dump"):
                        task_data = task.model_dump()
                    else:
                        task_data = task.dict()
                    task_data["_metadata"] = {
                        "saved_at": datetime.now().isoformat(),
                        "task_id": task_id,
                        "date": self._get_date_suffix(),
                    }

                    # Write to temporary file first, then rename (atomic)
                    temp_path = task_file.with_suffix(".tmp")
                    with open(temp_path, "w", encoding="utf-8") as f:
                        json.dump(task_data, f, indent=2, ensure_ascii=False)

                    # Atomic replace
                    temp_path.replace(task_file)

                # Delete task files that no longer exist in tasks dict
                files_to_delete = existing_files - current_task_files
                for file_path in files_to_delete:
                    if file_path.suffix == ".json":  # Only delete .json files, not .lock
                        try:
                            file_path.unlink()
                            logger.debug(f"Deleted removed task file: {file_path}")
                        except OSError as e:
                            logger.error(f"Error deleting {file_path}: {e}")

            logger.info(f"Successfully saved {len(tasks)} tasks to {directory}")
            return True

        except Timeout:
            logger.error(
                f"Could not acquire lock to save tasks (timeout after {self.lock_timeout}s)"
            )
            return False
        except Exception as e:
            logger.error(f"Error saving tasks: {e}")
            return False

    def _read_task_file(self, task_file, keep_metadata: bool = False):
        """Read a single task file and return (task_id, task_data, metadata) or None on error."""
        try:
            with open(task_file, "r", encoding="utf-8") as f:
                task_data = json.load(f)
            metadata = None
            if "_metadata" in task_data:
                metadata = task_data["_metadata"]
                task_id = metadata.get("task_id", task_file.stem)
                if not keep_metadata:
                    del task_data["_metadata"]
            else:
                task_id = task_file.stem
            return task_id, task_data, metadata
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error in {task_file}: {e}")
            self._backup_corrupted_file(task_file)
        except Exception as e:
            logger.error(f"Error loading task from {task_file}: {e}")
        return None

    def load_tasks(
        self, target_date: Optional[date] = None, load_all: bool = True
    ) -> Dict[str, Any]:
        """
        Load tasks from directory for specific date or all available dates.

        Args:
            target_date: Date to load tasks from (if None and load_all=False, uses current date)
            load_all: If True, loads tasks from all available directories (default: True)

        Returns:
            Dict[str, Any]: Dictionary of loaded task data
        """
        if load_all and target_date is None:
            return self._load_tasks_from_all_dates()

        date_to_load = target_date or datetime.now().date()
        return self._load_single_date_tasks(date_to_load)

    def _load_tasks_from_all_dates(self) -> Dict[str, Any]:
        """Load and merge tasks from all available date directories."""
        logger.info("Loading tasks from all available directories")
        available_dates = self.list_available_dates()

        if not available_dates:
            logger.info("No task directories found")
            return {}

        tasks_data: Dict[str, Any] = {}
        for date_to_load in sorted(available_dates, reverse=True):
            self._merge_tasks_for_date(date_to_load, tasks_data)

        logger.info(
            f"Successfully loaded {len(tasks_data)} tasks from {len(available_dates)} directories"
        )
        return tasks_data

    def _merge_tasks_for_date(self, date_to_load: date, tasks_data: Dict[str, Any]) -> None:
        """Merge tasks from a single date directory into tasks_data without overwriting newer entries."""
        directory = self._get_directory_path(date_to_load)
        lock_path = self._get_lock_path(date_to_load)
        lock = FileLock(lock_path, timeout=self.lock_timeout)

        try:
            with lock:
                for task_file in directory.glob("*.json"):
                    result = self._read_task_file(task_file, keep_metadata=False)
                    if not result:
                        continue

                    task_id, task_data, metadata = result
                    if task_id in tasks_data:
                        logger.info(
                            f"✗ Skipped older version of task {task_id} from {date_to_load}"
                        )
                        continue

                    tasks_data[task_id] = task_data
                    logger.info(f"✓ Loaded task {task_id} from {date_to_load}")
        except Timeout:
            logger.warning(
                f"Could not acquire lock for {date_to_load} (timeout after {self.lock_timeout}s), skipping"
            )
        except Exception as e:
            logger.warning(f"Error loading tasks from {directory}: {e}, skipping")

    def _load_single_date_tasks(self, target_date: date) -> Dict[str, Any]:
        """Load tasks from a specific date directory."""
        directory = self._get_directory_path(target_date)

        if not directory.exists():
            logger.info(f"No tasks directory found for date {target_date}")
            return {}

        lock_path = self._get_lock_path(target_date)
        lock = FileLock(lock_path, timeout=self.lock_timeout)
        tasks_data: Dict[str, Any] = {}

        try:
            with lock:
                for task_file in directory.glob("*.json"):
                    result = self._read_task_file(task_file, keep_metadata=False)
                    if result:
                        task_id, task_data, metadata = result
                        tasks_data[task_id] = task_data
            logger.info(f"Successfully loaded {len(tasks_data)} tasks from {directory}")
            return tasks_data
        except Timeout:
            logger.error(
                f"Could not acquire lock to load tasks (timeout after {self.lock_timeout}s)"
            )
        except Exception as e:
            logger.error(f"Error loading tasks: {e}")

        return {}

    def load_historical_tasks(self, start_date: date, end_date: date) -> Dict[str, Any]:
        """
        Load tasks from a date range for reporting or analysis.

        Args:
            start_date: Start date of range
            end_date: End date of range

        Returns:
            Dict[str, Any]: Combined tasks from the date range
        """
        all_tasks = {}
        current_date = start_date

        while current_date <= end_date:
            date_tasks = self.load_tasks(current_date)
            # Add date prefix to task IDs to avoid conflicts
            date_prefix = current_date.strftime("%Y%m%d")
            for task_id, task_data in date_tasks.items():
                all_tasks[f"{date_prefix}_{task_id}"] = task_data
            current_date = current_date + timedelta(days=1)

        return all_tasks

    def list_available_dates(self) -> List[date]:
        """
        List all dates for which task directories exist.

        Returns:
            List[date]: Sorted list of available dates
        """
        task_dirs = [d for d in self.data_directory.iterdir() if d.is_dir()]
        dates = []

        for dir_path in task_dirs:
            try:
                # Parse directory name as date
                dir_date = datetime.strptime(dir_path.name, "%Y-%m-%d").date()
                dates.append(dir_date)
            except ValueError:
                continue

        return sorted(dates)

    def cleanup_old_files(self, days_to_keep: int = 30) -> int:
        """
        Remove task directories older than specified number of days.

        Args:
            days_to_keep: Number of days to keep directories

        Returns:
            int: Number of directories deleted
        """
        cutoff_date = datetime.now().date() - timedelta(days=days_to_keep)
        available_dates = self.list_available_dates()
        deleted_count = 0

        for file_date in available_dates:
            if file_date < cutoff_date:
                directory = self._get_directory_path(file_date)

                try:
                    # Delete all files in directory
                    for file_path in directory.iterdir():
                        file_path.unlink()
                    # Delete the directory itself
                    directory.rmdir()
                    deleted_count += 1
                    logger.info(f"Deleted old tasks directory: {directory}")
                except OSError as e:
                    logger.error(f"Error deleting {directory}: {e}")

        return deleted_count

    def _backup_corrupted_file(self, file_path: Path):
        """Create a backup of corrupted JSON file for recovery."""
        backup_path = file_path.with_suffix(".json.bak")
        try:
            if file_path.exists():
                import shutil

                shutil.copy2(file_path, backup_path)
                logger.warning(f"Created backup of corrupted file at {backup_path}")
        except Exception as e:
            logger.error(f"Failed to create backup file: {e}")

    def get_storage_info(self) -> Dict[str, Any]:
        """
        Get information about task storage.

        Returns:
            Dict with storage information
        """
        available_dates = self.list_available_dates()
        current_directory = self._get_directory_path()

        # Count tasks in current directory
        current_task_count = 0
        if current_directory.exists():
            current_task_count = len(list(current_directory.glob("*.json")))

        return {
            "data_directory": str(self.data_directory),
            "current_date": self._get_date_suffix(),
            "current_directory": str(current_directory),
            "current_directory_exists": current_directory.exists(),
            "current_task_count": current_task_count,
            "available_dates": [d.strftime("%Y-%m-%d") for d in available_dates],
            "total_days_stored": len(available_dates),
        }


class SafeDailyJSONPersistence(DailyJSONPersistence):
    """
    Enhanced daily JSON persistence with retry mechanism.
    """

    def __init__(self, data_directory: str = "data", lock_timeout: int = 10, max_retries: int = 3):
        super().__init__(data_directory, lock_timeout)
        self.max_retries = max_retries

    def save_tasks(self, tasks: Dict[str, Task]) -> bool:
        """Save tasks with retry mechanism."""
        for attempt in range(self.max_retries):
            try:
                result = super().save_tasks(tasks)
                if result:
                    return True
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"Failed to save tasks after {self.max_retries} attempts: {e}")
                    return False
                else:
                    logger.warning(f"Save attempt {attempt + 1} failed, retrying: {e}")
                    continue
        return False

    def load_tasks(
        self, target_date: Optional[date] = None, load_all: bool = True
    ) -> Dict[str, Any]:
        """Load tasks with retry mechanism."""
        for attempt in range(self.max_retries):
            try:
                return super().load_tasks(target_date, load_all)
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"Failed to load tasks after {self.max_retries} attempts: {e}")
                    return {}
                else:
                    logger.warning(f"Load attempt {attempt + 1} failed, retrying: {e}")
                    continue
        return {}
