#!/usr/bin/env python3
"""Check runner storage directories against conservative free-space bounds.

This script validates storage availability for directories configured in .env
and for cache subdirectories derived from CACHE_DIR:
- LOG_DIR_MIN_FREE_GB
- STORAGE_DIR_MIN_FREE_GB
- HUGGINGFACE_MODELS_DIR_MIN_FREE_GB
- WHISPER_MODELS_DIR_MIN_FREE_GB
- UV_CACHE_DIR_MIN_FREE_GB

It prints current usage/free space per directory and a recommendation summary.

Usage:
  uv run scripts/check_runner_storage.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Tuple

RUNNER_ROOT = Path(__file__).resolve().parents[1]
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from app.core import storage_checks as _storage_checks
from app.core._check_output import colorize, format_prefix, format_status

os = _storage_checks.os
LOG_DIR_MIN_FREE_GB = _storage_checks.LOG_DIR_MIN_FREE_GB
STORAGE_DIR_MIN_FREE_GB = _storage_checks.STORAGE_DIR_MIN_FREE_GB
HUGGINGFACE_MODELS_DIR_MIN_FREE_GB = _storage_checks.HUGGINGFACE_MODELS_DIR_MIN_FREE_GB
WHISPER_MODELS_DIR_MIN_FREE_GB = _storage_checks.WHISPER_MODELS_DIR_MIN_FREE_GB
UV_CACHE_DIR_MIN_FREE_GB = _storage_checks.UV_CACHE_DIR_MIN_FREE_GB
WHISPER_MODEL_MIN_FREE_GB = _storage_checks.WHISPER_MODEL_MIN_FREE_GB
DirectoryRule = _storage_checks.DirectoryRule
DirectoryStatus = _storage_checks.DirectoryStatus
_CORE_DIRECTORY_SIZE_BYTES = _storage_checks._directory_size_bytes
_CORE_FIND_EXISTING_PARENT = _storage_checks._find_existing_parent
_CORE_DISK_USAGE_FOR_PATH = _storage_checks._disk_usage_for_path


def _repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    """Ensure the repository root is on sys.path."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_config():
    """Load config from app.core.config."""
    _ensure_import_path()
    from app.core.config import get_config  # type: ignore

    return get_config()


def _directory_size_bytes(path: Path) -> int:
    """Return directory size in bytes (best effort)."""
    return _CORE_DIRECTORY_SIZE_BYTES(path)


def _find_existing_parent(path: Path):
    """Return nearest existing parent path."""
    return _CORE_FIND_EXISTING_PARENT(path)


def _disk_usage_for_path(path: Path):
    """Return (total_gb, used_gb, free_gb) for filesystem containing path."""
    return _CORE_DISK_USAGE_FOR_PATH(path)


def _resolve_whisper_min_free_gb(model_name: str) -> Tuple[float, str]:
    """Resolve conservative minimum free space for Whisper cache directory."""
    return _storage_checks._resolve_whisper_min_free_gb(model_name)


def _resolve_uv_cache_dir(cache_dir: str) -> str:
    """Return the uv cache directory, defaulting to CACHE_DIR/uv."""
    return _storage_checks._resolve_uv_cache_dir(cache_dir)


def _is_within_path(path: Path, parent: Path) -> bool:
    """Return whether path is equal to or inside parent."""
    return _storage_checks._is_within_path(path, parent)


def _build_rules(cfg) -> Dict[str, DirectoryRule]:
    """Build directory rules from config and environment variables."""
    return _storage_checks._build_rules(cfg)


def _evaluate_rule(rule: DirectoryRule) -> DirectoryStatus:
    """Evaluate one directory against existence, permission, and free-space checks."""
    original_disk_usage = _storage_checks._disk_usage_for_path
    original_directory_size = _storage_checks._directory_size_bytes
    original_find_parent = _storage_checks._find_existing_parent
    try:
        _storage_checks._disk_usage_for_path = _disk_usage_for_path
        _storage_checks._directory_size_bytes = _directory_size_bytes
        _storage_checks._find_existing_parent = _find_existing_parent
        return _storage_checks._evaluate_rule(rule)
    finally:
        _storage_checks._disk_usage_for_path = original_disk_usage
        _storage_checks._directory_size_bytes = original_directory_size
        _storage_checks._find_existing_parent = original_find_parent


def _print_report(statuses: Dict[str, DirectoryStatus]) -> None:
    """Print directory checks and recommendations."""
    print("=== Storage check for runner directories ===")
    checked = ["LOG_DIR", "STORAGE_DIR"]
    if "CACHE_DIR" in statuses:
        checked.append("CACHE_DIR")
    else:
        checked.extend(["HUGGINGFACE_MODELS_DIR", "WHISPER_MODELS_DIR", "UV_CACHE_DIR"])
    print(f"Checked directories: {', '.join(checked)}")

    for key in checked:
        status = statuses[key]
        rule = status.rule
        status_label = format_prefix(level="info" if status.ok else "error")
        print(f"\n[{key}]")
        print(f"  Path: {rule.path}")
        print(f"  Purpose: {rule.description}")
        print(f"  Required free space: {rule.min_free_gb:.1f} GB")
        if (
            key
            in {
                "STORAGE_DIR",
                "HUGGINGFACE_MODELS_DIR",
                "WHISPER_MODELS_DIR",
                "CACHE_DIR",
            }
            and not status.ok
        ):
            required_additional_free_gb = max(
                rule.min_free_gb - status.used_gb - status.free_gb,
                0.0,
            )
            print(
                colorize(
                    f"  Required additional free: {required_additional_free_gb:.1f} GB",
                    level="error",
                )
            )
        print(f"  Filesystem total: {status.total_gb:.1f} GB")
        print(f"  Directory used: {status.used_gb:.1f} GB")
        print(f"  Filesystem free: {status.free_gb:.1f} GB")
        print(f"  Status: {status_label}")
        print(f"  Detail: {status.detail}")
        if rule.note:
            print(f"  Note: {rule.note}")

    all_ok = all(s.ok for s in statuses.values())
    print("\nConclusion:")
    if all_ok:
        print(format_status("Storage configuration is adequate.", level="info"))
    else:
        print(
            format_status(
                "Storage configuration is NOT adequate. Adjust disk space, permissions, or cleanup policy.",
                level="error",
            )
        )


def main() -> int:
    """Entry point for storage checks."""
    cfg = _load_config()
    rules = _build_rules(cfg)
    statuses = {key: _evaluate_rule(rule) for key, rule in rules.items()}
    _print_report(statuses)
    return 0 if all(s.ok for s in statuses.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
