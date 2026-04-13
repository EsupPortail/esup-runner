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

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

# Conservative free-space bounds (GB)
LOG_DIR_MIN_FREE_GB = 0.5
STORAGE_DIR_MIN_FREE_GB = 15.0
HUGGINGFACE_MODELS_DIR_MIN_FREE_GB = 2.0
WHISPER_MODELS_DIR_MIN_FREE_GB = 3.0
UV_CACHE_DIR_MIN_FREE_GB = 5.0

# Approximate required free space by logical Whisper model (GB).
# These values are intentionally conservative to leave room for cache/temp files.
WHISPER_MODEL_MIN_FREE_GB = {
    "tiny": 0.5,
    "base": 1.0,
    "small": 1.5,
    "medium": 3.0,
    "large": 5.0,
    "large-v3": 5.0,
    "turbo": 3.0,
}


@dataclass
class DirectoryRule:
    env_key: str
    path: str
    min_free_gb: float
    description: str
    note: str = ""
    must_exist: bool = True
    aggregate_paths: Tuple[str, ...] = ()


@dataclass
class DirectoryStatus:
    rule: DirectoryRule
    exists: bool
    is_dir: bool
    writable: bool
    total_gb: float
    used_gb: float
    free_gb: float
    ok: bool
    detail: str


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


def _colorize(text: str, *, level: str) -> str:
    """Colorize output based on severity level."""
    colors = {
        "info": "\033[32m",
        "warning": "\033[33m",
        "error": "\033[31m",
    }
    reset = "\033[0m"
    color = colors.get(level, "")
    if not color:
        return text
    return f"{color}{text}{reset}"


def _directory_size_bytes(path: Path) -> int:
    """Return directory size in bytes (best effort)."""
    if not path.exists() or not path.is_dir():
        return 0

    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                fp = Path(root) / name
                try:
                    total += fp.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _find_existing_parent(path: Path) -> Optional[Path]:
    """Return nearest existing parent path."""
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _disk_usage_for_path(path: Path) -> Tuple[float, float, float]:
    """Return (total_gb, used_gb, free_gb) for filesystem containing path."""
    target = path if path.exists() else _find_existing_parent(path)
    if target is None:
        return 0.0, 0.0, 0.0

    try:
        usage = shutil.disk_usage(target)
    except OSError:
        return 0.0, 0.0, 0.0

    gb = 1024.0 * 1024.0 * 1024.0
    return usage.total / gb, usage.used / gb, usage.free / gb


def _resolve_whisper_min_free_gb(model_name: str) -> Tuple[float, str]:
    """Resolve conservative minimum free space for Whisper cache directory."""
    normalized = (model_name or "").strip().lower()
    if normalized in WHISPER_MODEL_MIN_FREE_GB:
        return WHISPER_MODEL_MIN_FREE_GB[normalized], normalized

    if normalized.startswith("large"):
        return WHISPER_MODEL_MIN_FREE_GB["large"], "large"

    # Unknown model: keep a conservative default.
    return 3.0, "unknown"


def _resolve_uv_cache_dir(cache_dir: str) -> str:
    """Return the uv cache directory, defaulting to CACHE_DIR/uv."""
    uv_cache_dir = os.getenv("UV_CACHE_DIR")
    if uv_cache_dir:
        return str(Path(uv_cache_dir).expanduser())
    return str(Path(cache_dir).expanduser() / "uv")


def _is_within_path(path: Path, parent: Path) -> bool:
    """Return whether path is equal to or inside parent."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _build_rules(cfg) -> Dict[str, DirectoryRule]:
    """Build directory rules from config and environment variables."""
    max_file_age_days = int(getattr(cfg, "MAX_FILE_AGE_DAYS", 0))
    whisper_model = str(getattr(cfg, "WHISPER_MODEL", "small"))
    cache_dir = str(getattr(cfg, "CACHE_DIR", "/home/esup-runner/.cache/esup-runner"))
    whisper_min_free_gb, whisper_ref = _resolve_whisper_min_free_gb(whisper_model)
    cache_path = Path(cache_dir).expanduser()
    huggingface_path = Path(
        str(
            getattr(
                cfg,
                "HUGGINGFACE_MODELS_DIR",
                str(cache_path / "huggingface"),
            )
        )
    ).expanduser()
    whisper_path = Path(
        str(
            getattr(
                cfg,
                "WHISPER_MODELS_DIR",
                str(cache_path / "whisper-models"),
            )
        )
    ).expanduser()
    uv_cache_path = Path(str(getattr(cfg, "UV_CACHE_DIR", str(cache_path / "uv")))).expanduser()
    grouped_cache_dirs = all(
        _is_within_path(path, cache_path)
        for path in (huggingface_path, whisper_path, uv_cache_path)
    )

    storage_note = (
        "Contains generated videos and temporary outputs. "
        "You can reduce retained files with MAX_FILE_AGE_DAYS."
    )
    if max_file_age_days > 0:
        storage_note += f" Current MAX_FILE_AGE_DAYS={max_file_age_days}."
    else:
        storage_note += " Current MAX_FILE_AGE_DAYS=0 (keep files indefinitely)."

    whisper_note = (
        "Approximate requirement based on WHISPER_MODEL "
        f"('{whisper_model}', reference='{whisper_ref}')."
    )

    rules = {
        "LOG_DIR": DirectoryRule(
            env_key="LOG_DIR",
            path=str(getattr(cfg, "LOG_DIR", "/var/log/esup-runner")),
            min_free_gb=LOG_DIR_MIN_FREE_GB,
            description="Log output directory",
            note="Low space is usually acceptable for logs, but keep a small safety margin.",
        ),
        "STORAGE_DIR": DirectoryRule(
            env_key="STORAGE_DIR",
            path=str(getattr(cfg, "STORAGE_DIR", "/tmp/esup-runner/storage")),
            min_free_gb=STORAGE_DIR_MIN_FREE_GB,
            description="Generated media workspace",
            note=storage_note,
        ),
    }

    if grouped_cache_dirs:
        rules["CACHE_DIR"] = DirectoryRule(
            env_key="CACHE_DIR",
            path=str(cache_path),
            min_free_gb=whisper_min_free_gb
            + HUGGINGFACE_MODELS_DIR_MIN_FREE_GB
            + UV_CACHE_DIR_MIN_FREE_GB,
            description="Shared cache root for Whisper/Hugging Face/uv",
            note=(
                "Aggregated check for WHISPER_MODELS_DIR, HUGGINGFACE_MODELS_DIR, and UV_CACHE_DIR. "
                f"Whisper reference='{whisper_ref}'."
            ),
            aggregate_paths=(str(whisper_path), str(huggingface_path), str(uv_cache_path)),
        )
    else:
        rules["HUGGINGFACE_MODELS_DIR"] = DirectoryRule(
            env_key="HUGGINGFACE_MODELS_DIR",
            path=str(huggingface_path),
            min_free_gb=HUGGINGFACE_MODELS_DIR_MIN_FREE_GB,
            description="Hugging Face translation models cache",
            note="Reserve at least ~2 GB for local FR/EN translation models.",
        )
        rules["WHISPER_MODELS_DIR"] = DirectoryRule(
            env_key="WHISPER_MODELS_DIR",
            path=str(whisper_path),
            min_free_gb=whisper_min_free_gb,
            description="Whisper models cache",
            note=whisper_note,
        )
        rules["UV_CACHE_DIR"] = DirectoryRule(
            env_key="UV_CACHE_DIR",
            path=str(uv_cache_path),
            min_free_gb=UV_CACHE_DIR_MIN_FREE_GB,
            description="uv package cache",
            note=(
                "Used by uv for wheel downloads and extraction during sync/upgrade operations. "
                "If UV_CACHE_DIR is not set, this defaults to CACHE_DIR/uv."
            ),
            must_exist=False,
        )

    return rules


def _evaluate_rule(rule: DirectoryRule) -> DirectoryStatus:
    """Evaluate one directory against existence, permission, and free-space checks."""
    path = Path(rule.path)
    exists = path.exists()
    is_dir = path.is_dir()
    writable = exists and is_dir and os.access(path, os.W_OK | os.X_OK)

    total_gb, _fs_used_gb, free_gb = _disk_usage_for_path(path)
    local_used_gb = _directory_size_bytes(path) / (1024.0 * 1024.0 * 1024.0)

    if not exists:
        if not rule.must_exist:
            parent = _find_existing_parent(path)
            parent_writable = parent is not None and os.access(parent, os.W_OK | os.X_OK)
            enough_free = free_gb >= rule.min_free_gb
            ok = parent_writable and enough_free
            detail = (
                "Directory does not exist yet; uv will create it on demand."
                if ok
                else "Directory does not exist yet and parent path is not writable or lacks free space."
            )
            return DirectoryStatus(
                rule=rule,
                exists=False,
                is_dir=False,
                writable=parent_writable,
                total_gb=total_gb,
                used_gb=0.0,
                free_gb=free_gb,
                ok=ok,
                detail=detail,
            )
        return DirectoryStatus(
            rule=rule,
            exists=False,
            is_dir=False,
            writable=False,
            total_gb=total_gb,
            used_gb=local_used_gb,
            free_gb=free_gb,
            ok=False,
            detail="Directory does not exist (run 'sudo make init' to create it).",
        )

    if not is_dir:
        return DirectoryStatus(
            rule=rule,
            exists=True,
            is_dir=False,
            writable=False,
            total_gb=total_gb,
            used_gb=local_used_gb,
            free_gb=free_gb,
            ok=False,
            detail="Path exists but is not a directory.",
        )

    if not writable:
        return DirectoryStatus(
            rule=rule,
            exists=True,
            is_dir=True,
            writable=False,
            total_gb=total_gb,
            used_gb=local_used_gb,
            free_gb=free_gb,
            ok=False,
            detail="Directory is not writable by current user.",
        )

    if rule.env_key == "CACHE_DIR" and rule.aggregate_paths:
        aggregate_used_gb = sum(
            _directory_size_bytes(Path(sub_path)) / (1024.0 * 1024.0 * 1024.0)
            for sub_path in rule.aggregate_paths
        )
        required_additional_free_gb = max(rule.min_free_gb - aggregate_used_gb, 0.0)
        enough_free = free_gb >= required_additional_free_gb
        detail = "OK" if enough_free else "Insufficient free space for aggregated cache threshold."
        return DirectoryStatus(
            rule=rule,
            exists=True,
            is_dir=True,
            writable=True,
            total_gb=total_gb,
            used_gb=aggregate_used_gb,
            free_gb=free_gb,
            ok=enough_free,
            detail=detail,
        )

    # For storage and model caches, account for already-used space in the directory.
    # Requested rule: (required free space - directory used) < filesystem free.
    if rule.env_key in {"STORAGE_DIR", "HUGGINGFACE_MODELS_DIR", "WHISPER_MODELS_DIR"}:
        required_additional_free_gb = max(rule.min_free_gb - local_used_gb, 0.0)
        enough_free = free_gb >= required_additional_free_gb
    else:
        enough_free = free_gb >= rule.min_free_gb

    if enough_free:
        if rule.env_key == "STORAGE_DIR":
            detail = (
                "Storage that can be customized to your needs "
                "(this space stores the generated video files)"
            )
        else:
            detail = "OK"
    else:
        detail = "Insufficient free space for recommended threshold."
    return DirectoryStatus(
        rule=rule,
        exists=True,
        is_dir=True,
        writable=True,
        total_gb=total_gb,
        used_gb=local_used_gb,
        free_gb=free_gb,
        ok=enough_free,
        detail=detail,
    )


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
        status_label = (
            _colorize("OK", level="info") if status.ok else _colorize("NOT OK", level="error")
        )
        print(f"\n[{key}]")
        print(f"  Path: {rule.path}")
        print(f"  Purpose: {rule.description}")
        print(f"  Required free space: {rule.min_free_gb:.1f} GB")
        if (
            key in {"STORAGE_DIR", "HUGGINGFACE_MODELS_DIR", "WHISPER_MODELS_DIR", "CACHE_DIR"}
            and not status.ok
        ):
            required_additional_free_gb = max(
                rule.min_free_gb - status.used_gb - status.free_gb,
                0.0,
            )
            print(
                _colorize(
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
        print(_colorize("INFO: Storage configuration is adequate.", level="info"))
    else:
        print(
            _colorize(
                "ERROR: Storage configuration is NOT adequate. Adjust disk space, permissions, or cleanup policy.",
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
