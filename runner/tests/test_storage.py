"""
Storage tests for the Runner (file operations, cleanup, statistics).
All comments are in English for clarity.
"""

import os
import tempfile
import time

import pytest

from app.managers.storage_manager import StorageServiceManager


def test_storage_save_and_read():
    """
    Test saving and reading files from storage.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Test data
        test_content = b"Hello, World!"
        task_id = "test_task_123"

        # Save file
        saved_path = storage.save_file(task_id, test_content)
        assert os.path.exists(saved_path), "Saved file should exist on disk."

        # Check existence
        exists = storage.exists(task_id)
        assert exists is True, "Storage should confirm file exists."

        # Read file
        content = storage.read_file(task_id)
        assert content == test_content, "Read content should match saved content."


def test_storage_cleanup():
    """
    Test cleanup of individual task files.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        task_id = "test_task_456"
        test_content = b"Test cleanup content"

        # Save file
        storage.save_file(task_id, test_content)
        assert storage.exists(task_id) is True, "File should exist before cleanup."

        # Cleanup file
        cleaned = storage.cleanup(task_id)
        assert cleaned is True, "Cleanup should return True for existing file."

        # Verify file is deleted
        exists_after = storage.exists(task_id)
        assert exists_after is False, "File should not exist after cleanup."


def test_storage_statistics():
    """
    Test storage usage statistics.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Save multiple files
        storage.save_file("task_1", b"Data 1")
        storage.save_file("task_2", b"Data 2")
        storage.save_file("task_3", b"Data 3")

        # Get statistics
        stats = storage.get_usage_stats()

        assert "total_size" in stats, "Stats should include total_size."
        assert "file_count" in stats, "Stats should include file_count."
        assert "available_space" in stats, "Stats should include available_space."
        assert stats["file_count"] == 3, "Should have 3 files in storage."
        assert stats["total_size"] > 0, "Total size should be greater than 0."


def test_storage_cleanup_all():
    """
    Test cleanup of all files in storage.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Save multiple files
        storage.save_file("task_a", b"Content A")
        storage.save_file("task_b", b"Content B")
        storage.save_file("task_c", b"Content C")

        # Cleanup all files
        count = storage.cleanup_all()
        assert count == 3, "Should delete 3 files."

        # Verify all files are deleted
        stats = storage.get_usage_stats()
        assert stats["file_count"] == 0, "No files should remain after cleanup_all."


def test_storage_old_files_cleanup():
    """
    Test cleanup of old files based on age threshold.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Create recent file
        recent_task = "recent_task"
        storage.save_file(recent_task, b"Recent content")

        # Create old file (simulate by modifying mtime)
        old_task = "old_task"
        old_path = storage.save_file(old_task, b"Old content")
        old_time = time.time() - (10 * 24 * 60 * 60)  # 10 days ago
        os.utime(old_path, (old_time, old_time))

        # Cleanup files older than 7 days
        deleted_count = storage.cleanup_old_files(max_age_days=7)
        assert deleted_count == 1, "Should delete 1 old file."

        # Verify recent file still exists
        assert storage.exists(recent_task) is True, "Recent file should still exist."
        assert storage.exists(old_task) is False, "Old file should be deleted."


def test_storage_path_sanitization():
    """
    Test path sanitization to prevent directory traversal attacks.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Valid task ID
        valid_path = storage.get_path("valid_task_123")
        assert temp_dir in valid_path, "Path should be within base directory."

        # Invalid task ID (empty string)
        with pytest.raises(ValueError):
            storage.get_path("")

        # Task ID with special characters should be sanitized
        sanitized_path = storage.get_path("task../../../etc/passwd")
        assert (
            temp_dir in sanitized_path
        ), "Path should be within base directory after sanitization."
        assert ".." not in sanitized_path, "Path should not contain directory traversal."


def test_storage_nonexistent_file():
    """
    Test reading and deleting nonexistent files.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        # Read nonexistent file
        content = storage.read_file("nonexistent_task")
        assert content is None, "Reading nonexistent file should return None."

        # Delete nonexistent file
        deleted = storage.cleanup("nonexistent_task")
        assert deleted is False, "Deleting nonexistent file should return False."
