"""
Test script for automatic storage cleanup functionality.
"""

import os
import tempfile
import time

from app.managers.storage_manager import StorageServiceManager


def test_cleanup_old_files():
    """Verify that cleanup removes only items older than the max age."""

    with tempfile.TemporaryDirectory() as temp_dir:
        storage = StorageServiceManager(base_path=temp_dir)

        recent_file = os.path.join(temp_dir, "recent_file.json")
        with open(recent_file, "w", encoding="utf-8") as handle:
            handle.write("Recent content")

        recent_dir = os.path.join(temp_dir, "recent_dir")
        os.makedirs(recent_dir)
        with open(os.path.join(recent_dir, "data.txt"), "w", encoding="utf-8") as handle:
            handle.write("Recent directory content")

        old_file = os.path.join(temp_dir, "old_file.json")
        with open(old_file, "w", encoding="utf-8") as handle:
            handle.write("Old content")

        old_dir = os.path.join(temp_dir, "old_dir")
        os.makedirs(old_dir)
        with open(os.path.join(old_dir, "data.txt"), "w", encoding="utf-8") as handle:
            handle.write("Old directory content")

        old_time = time.time() - (10 * 24 * 60 * 60)
        os.utime(old_file, (old_time, old_time))
        os.utime(old_dir, (old_time, old_time))

        # No deletion when unlimited age
        assert storage.cleanup_old_files(max_age_days=0) == 0
        assert set(os.listdir(temp_dir)) == {
            "recent_file.json",
            "recent_dir",
            "old_file.json",
            "old_dir",
        }

        # Old items should be removed when max age is 7 days
        deleted = storage.cleanup_old_files(max_age_days=7)
        assert deleted == 2

        remaining_items = set(os.listdir(temp_dir))
        assert remaining_items == {"recent_file.json", "recent_dir"}
        assert os.path.exists(recent_file)
        assert os.path.isdir(recent_dir)
