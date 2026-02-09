# runner/app/managers/storage_manager.py
"""
Storage service management for task result files.
Provides file-based storage with cleanup and monitoring capabilities.
"""

import os
import threading
import time
from typing import Optional

from app.core.config import config
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()


class StorageServiceManager:
    """
    Manages file storage for task results with safety features and cleanup.

    Provides atomic file operations, path sanitization, and automatic
    cleanup to prevent disk space issues from accumulated task results.
    """

    def __init__(self, base_path: str = "/tmp/esup-runner"):
        """
        Initialize storage manager with specified base path.

        Args:
            base_path: Root directory for storing task result files
        """
        self.base_path = base_path
        self._ensure_directory_exists()

    def _ensure_directory_exists(self):
        """Ensure storage directory exists, create if necessary."""
        try:
            os.makedirs(self.base_path, exist_ok=True)
            logger.info(f"Storage directory initialized: {self.base_path}")
        except PermissionError as e:
            raise PermissionError(
                f"Permission denied: Cannot create storage directory '{self.base_path}'. "
                f"Please check permissions or specify a different path."
            ) from e
        except OSError as e:
            raise OSError(f"Failed to create storage directory '{self.base_path}': {str(e)}") from e

    def exists(self, task_id: str) -> bool:
        """
        Check if a file exists for the given task.

        Args:
            task_id: Unique task identifier

        Returns:
            bool: True if file exists, False otherwise
        """
        try:
            return os.path.exists(self.get_path(task_id))
        except OSError:
            return False

    def get_path(self, task_id: str) -> str:
        """
        Get full file path for a given task.

        Args:
            task_id: Unique task identifier

        Returns:
            str: Full filesystem path to task result file

        Raises:
            ValueError: If task_id is invalid or contains unsafe characters
        """
        if not task_id or not isinstance(task_id, str) or len(task_id) < 1:
            raise ValueError("Task ID must be a non-empty string")

        # Sanitize task ID to prevent path injection
        safe_task_id = "".join(c for c in task_id if c.isalnum() or c in ("-", "_"))
        if not safe_task_id:
            raise ValueError("Task ID contains no valid characters")

        return os.path.join(self.base_path, f"{safe_task_id}.json")

    def save_file(self, task_id: str, content: bytes) -> str:
        """
        Save content to file for the given task.

        Uses atomic write operation to prevent file corruption.

        Args:
            task_id: Unique task identifier
            content: Binary content to save

        Returns:
            str: Path to saved file

        Raises:
            PermissionError: If write permissions are insufficient
            OSError: If disk space is insufficient or other IO error occurs
        """
        file_path = self.get_path(task_id)

        try:
            # Atomic write: write to temporary file first
            temp_path = f"{file_path}.tmp"
            with open(temp_path, "wb") as f:
                f.write(content)

            # Rename temporary file to final name
            os.rename(temp_path, file_path)
            logger.info(f"File saved successfully: {file_path}")
            return file_path

        except PermissionError as e:
            raise PermissionError(
                f"Permission denied: Cannot write to '{file_path}'. " f"Please check permissions."
            ) from e
        except OSError as e:
            # Check for disk space issues
            if e.errno == 28:  # No space left on device
                raise OSError(
                    "Insufficient disk space to save file. "
                    f"Required: {len(content)} bytes, "
                    f"Available: {self.get_available_space()} bytes"
                ) from e
            raise

    def read_file(self, task_id: str) -> Optional[bytes]:
        """
        Read file content for the given task.

        Args:
            task_id: Unique task identifier

        Returns:
            Optional[bytes]: File content if exists, None otherwise

        Raises:
            OSError: If file cannot be read
        """
        file_path = self.get_path(task_id)

        if not self.exists(task_id):
            return None

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            logger.info(f"File read successfully: {file_path}")
            return content
        except (OSError, IOError) as e:
            raise OSError(f"Failed to read file '{file_path}': {str(e)}") from e

    def cleanup(self, task_id: str) -> bool:
        """
        Delete file associated with a task.

        Args:
            task_id: Unique task identifier

        Returns:
            bool: True if file was deleted, False if file didn't exist

        Raises:
            PermissionError: If delete permissions are insufficient
            OSError: If file cannot be deleted
        """
        file_path = self.get_path(task_id)

        if not self.exists(task_id):
            return False

        try:
            os.remove(file_path)
            logger.info(f"File cleaned up: {file_path}")
            return True
        except PermissionError as e:
            raise PermissionError(
                f"Permission denied: Cannot delete file '{file_path}'. "
                f"Please check permissions."
            ) from e
        except OSError as e:
            raise OSError(f"Failed to delete file '{file_path}': {str(e)}") from e

    def cleanup_all(self) -> int:
        """
        Delete all storage files.

        Returns:
            int: Number of files deleted
        """
        if not os.path.exists(self.base_path):
            return 0

        count = 0
        try:
            for filename in os.listdir(self.base_path):
                file_path = os.path.join(self.base_path, filename)
                try:
                    if os.path.isfile(file_path) and filename.endswith(".json"):
                        os.remove(file_path)
                        count += 1
                except OSError:
                    # Ignore errors on individual files
                    continue
            logger.info(f"Cleaned up {count} files from storage")
            return count
        except OSError as e:
            raise OSError(f"Failed to clean up storage directory: {str(e)}") from e

    def get_available_space(self) -> int:
        """
        Get available disk space in bytes.

        Returns:
            int: Available space in bytes

        Raises:
            OSError: If disk space cannot be determined
        """
        try:
            stat = os.statvfs(self.base_path)
            return stat.f_bavail * stat.f_frsize
        except OSError as e:
            raise OSError(f"Failed to get available space: {str(e)}") from e

    def get_usage_stats(self) -> dict:
        """
        Get storage usage statistics.

        Returns:
            dict: Storage statistics including total size, file count, and available space
        """
        total_size = 0
        file_count = 0

        if not os.path.exists(self.base_path):
            return {"total_size": 0, "file_count": 0, "available_space": self.get_available_space()}

        try:
            for filename in os.listdir(self.base_path):
                file_path = os.path.join(self.base_path, filename)
                if os.path.isfile(file_path) and filename.endswith(".json"):
                    total_size += os.path.getsize(file_path)
                    file_count += 1

            stats = {
                "total_size": total_size,
                "file_count": file_count,
                "available_space": self.get_available_space(),
            }
            logger.debug(f"Storage stats: {stats}")
            return stats
        except OSError as e:
            raise OSError(f"Failed to get usage statistics: {str(e)}") from e

    def delayed_cleanup(self, task_id: str, delay_seconds: int = 300):
        """
        Clean up file after specified delay.

        Args:
            task_id: Unique task identifier
            delay_seconds: Delay in seconds before cleanup
        """

        def cleanup_after_delay():
            time.sleep(delay_seconds)
            try:
                self.cleanup(task_id)
                logger.info(f"Delayed cleanup completed for task: {task_id}")
            except Exception as e:
                logger.error(f"Delayed cleanup failed for task {task_id}: {e}")

        thread = threading.Thread(target=cleanup_after_delay)
        thread.daemon = True
        thread.start()
        logger.info(f"Scheduled delayed cleanup for task {task_id} in {delay_seconds} seconds")

    def _delete_old_item(self, item_path: str, item_age_seconds: float) -> bool:
        """
        Delete a single old file or directory.

        Args:
            item_path: Path to the item to delete
            item_age_seconds: Age of the item in seconds

        Returns:
            bool: True if deletion was successful, False otherwise
        """
        try:
            age_days = item_age_seconds / 86400
            if os.path.isfile(item_path):
                os.remove(item_path)
                logger.info(f"Deleted old file: {item_path} (age: {age_days:.1f} days)")
                return True
            elif os.path.isdir(item_path):
                import shutil

                shutil.rmtree(item_path)
                logger.info(f"Deleted old directory: {item_path} (age: {age_days:.1f} days)")
                return True
        except OSError as e:
            logger.warning(f"Failed to delete item {item_path}: {e}")
        return False

    def _process_item(self, item: str, max_age_seconds: float, current_time: float) -> bool:
        """
        Process a single item and delete if too old.

        Args:
            item: Name of the item in storage directory
            max_age_seconds: Maximum age in seconds
            current_time: Current timestamp

        Returns:
            bool: True if item was deleted, False otherwise
        """
        item_path = os.path.join(self.base_path, item)
        try:
            item_mtime = os.path.getmtime(item_path)
            item_age_seconds = current_time - item_mtime
            if item_age_seconds > max_age_seconds:
                return self._delete_old_item(item_path, item_age_seconds)
        except OSError as e:
            logger.warning(f"Failed to process item {item_path}: {e}")
        return False

    def cleanup_old_files(self, max_age_days: int) -> int:
        """
        Clean up files and directories older than the specified age.

        Args:
            max_age_days: Maximum age of files in days (0 to skip cleanup)

        Returns:
            int: Number of items (files and directories) deleted
        """
        if max_age_days <= 0:
            logger.debug("Cleanup skipped: MAX_FILE_AGE_DAYS is 0 (unlimited)")
            return 0

        if not os.path.exists(self.base_path):
            logger.debug(f"Cleanup skipped: storage directory does not exist: {self.base_path}")
            return 0

        count = 0
        current_time = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60

        try:
            for item in os.listdir(self.base_path):
                if self._process_item(item, max_age_seconds, current_time):
                    count += 1

            if count > 0:
                logger.info(f"Cleanup completed: removed {count} old items from storage")
            else:
                logger.debug("Cleanup completed: no old items to remove")
            return count
        except OSError as e:
            logger.error(f"Failed to cleanup old files: {str(e)}")
            return count


# Global storage manager instance
storage_manager = StorageServiceManager(config.STORAGE_DIR)
