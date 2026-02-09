#!/usr/bin/env python3
# runner/scripts/manual_cleanup.py
"""
Utility script to run storage cleanup manually.
Useful for testing or forcing a cleanup without waiting for the scheduled interval.

Usage:
    uv run scripts/manual_cleanup.py
"""

import os
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.managers.storage_manager import storage_manager

logger = setup_default_logging()


def print_config():
    print("=" * 70)
    print("MANUAL STORAGE CLEANUP")
    print("=" * 70)
    print("\nCurrent configuration:")
    print(f"  - STORAGE_DIR: {config.STORAGE_DIR}")
    print(f"  - MAX_FILE_AGE_DAYS: {config.MAX_FILE_AGE_DAYS} days")
    print(f"  - CLEANUP_INTERVAL_HOURS: {config.CLEANUP_INTERVAL_HOURS} hours")


def print_items(items, title, *, max_age_seconds=None, show_status=True):
    if items:
        print(f"\n   {title}")

        for item in items:
            age_days = item["age_seconds"] / 86400
            item_type = "DIR " if item["is_dir"] else "FILE"
            should_delete = False
            if show_status and max_age_seconds is not None:
                should_delete = item["age_seconds"] > max_age_seconds
            status = "🗑️" if should_delete else "✅"
            print(f"   {status} [{item_type}] {item['name']} (age: {age_days:.2f} days)")


def collect_items(storage_dir, items, current_time):
    collected = []
    for item in items:
        item_path = os.path.join(storage_dir, item)
        try:
            item_mtime = os.path.getmtime(item_path)
        except OSError:
            continue
        collected.append(
            {
                "name": item,
                "path": item_path,
                "age_seconds": current_time - item_mtime,
                "is_dir": os.path.isdir(item_path),
            }
        )
    return collected


def confirm_cleanup():
    print(f"\n🗑️  Ready to delete items older than {config.MAX_FILE_AGE_DAYS} days")
    response = input("   Continue? (y/N): ")
    return response.lower() in ["y", "yes", "o", "oui"]


def main():
    print_config()

    if config.MAX_FILE_AGE_DAYS <= 0:
        print("\n⚠️  WARNING: MAX_FILE_AGE_DAYS=0 (unlimited storage)")
        print("   No cleanup will be performed.")
        return

    print(f"\n🔎 Scanning directory {config.STORAGE_DIR}...")
    if not os.path.exists(config.STORAGE_DIR):
        print(f"❌ Directory does not exist: {config.STORAGE_DIR}")
        return

    try:
        items_before = os.listdir(config.STORAGE_DIR)
        print(f"   Items found: {len(items_before)}")

        current_time = time.time()
        max_age_seconds = config.MAX_FILE_AGE_DAYS * 24 * 60 * 60
        collected_items = collect_items(config.STORAGE_DIR, items_before, current_time)
        print_items(
            collected_items,
            "List of items:",
            max_age_seconds=max_age_seconds,
        )

        items_to_delete = [
            item for item in collected_items if item["age_seconds"] > max_age_seconds
        ]
        print(
            f"\n   Items to delete (older than {config.MAX_FILE_AGE_DAYS} days): {len(items_to_delete)}"
        )
    except Exception as e:
        print(f"❌ Error reading directory: {e}")
        return

    if not confirm_cleanup():
        print("❌ Cleanup cancelled.")
        return

    print("\n🔧 Running cleanup...")
    try:
        deleted_count = storage_manager.cleanup_old_files(config.MAX_FILE_AGE_DAYS)
        print("\n✅ Cleanup complete!")
        print(f"   Items deleted: {deleted_count}")
        items_after = os.listdir(config.STORAGE_DIR)
        print(f"   Items remaining: {len(items_after)}")
        current_time = time.time()
        kept_items = collect_items(config.STORAGE_DIR, items_after, current_time)
        print_items(kept_items, "Items kept:", show_status=False)
    except Exception as e:
        print(f"❌ Error during cleanup: {e}")
        import traceback

        traceback.print_exc()
        return
    print("\n" + "=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n❌ Operation cancelled by the user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)
