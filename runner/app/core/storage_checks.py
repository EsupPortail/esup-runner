"""Shared storage and cache disk usage checks for the runner."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


def _configured_cache_paths(cfg: Any) -> dict[str, str]:
    """Return configured cache paths derived from runner config."""
    cache_dir = str(getattr(cfg, "CACHE_DIR", "/home/esup-runner/.cache/esup-runner"))
    cache_path = Path(cache_dir).expanduser()
    return {
        "cache_dir": str(cache_path),
        "whisper_models_dir": str(
            Path(
                str(getattr(cfg, "WHISPER_MODELS_DIR", str(cache_path / "whisper-models")))
            ).expanduser()
        ),
        "huggingface_models_dir": str(
            Path(
                str(getattr(cfg, "HUGGINGFACE_MODELS_DIR", str(cache_path / "huggingface")))
            ).expanduser()
        ),
        "uv_cache_dir": str(
            Path(str(getattr(cfg, "UV_CACHE_DIR", str(cache_path / "uv")))).expanduser()
        ),
    }


def _configured_paths(cfg: Any) -> dict[str, str]:
    """Return configured storage/cache paths for API display."""
    storage_dir = str(Path(str(getattr(cfg, "STORAGE_DIR", "/tmp/esup-runner"))).expanduser())
    return {
        "storage_dir": storage_dir,
        "output_dir_pattern": str(Path(storage_dir) / "<task_id>" / "output"),
        **_configured_cache_paths(cfg),
    }


def _build_rules(cfg: Any) -> Dict[str, DirectoryRule]:
    """Build directory rules from config and environment variables."""
    max_file_age_days = int(getattr(cfg, "MAX_FILE_AGE_DAYS", 0))
    whisper_model = str(getattr(cfg, "WHISPER_MODEL", "small"))
    cache_paths = _configured_cache_paths(cfg)
    whisper_min_free_gb, whisper_ref = _resolve_whisper_min_free_gb(whisper_model)
    cache_path = Path(cache_paths["cache_dir"]).expanduser()
    huggingface_path = Path(cache_paths["huggingface_models_dir"]).expanduser()
    whisper_path = Path(cache_paths["whisper_models_dir"]).expanduser()
    uv_cache_path = Path(cache_paths["uv_cache_dir"]).expanduser()
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
            path=str(getattr(cfg, "STORAGE_DIR", "/tmp/esup-runner")),
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
            aggregate_paths=(
                str(whisper_path),
                str(huggingface_path),
                str(uv_cache_path),
            ),
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


def _round_gb(value: float) -> float:
    """Return a compact, API-friendly GB value."""
    return round(float(value), 2)


def _status_payload(status: DirectoryStatus) -> dict[str, Any]:
    """Serialize a directory status for API responses."""
    rule = status.rule
    return {
        "path": rule.path,
        "description": rule.description,
        "min_free_gb": _round_gb(rule.min_free_gb),
        "exists": status.exists,
        "is_dir": status.is_dir,
        "writable": status.writable,
        "total_gb": _round_gb(status.total_gb),
        "used_gb": _round_gb(status.used_gb),
        "free_gb": _round_gb(status.free_gb),
        "ok": status.ok,
        "detail": status.detail,
        "note": rule.note,
        "aggregate_paths": list(rule.aggregate_paths),
    }


def collect_disk_usage(cfg: Any) -> dict[str, Any]:
    """Return storage/cache disk usage diagnostics for the runner status API."""
    rules = _build_rules(cfg)
    statuses = {key: _evaluate_rule(rule) for key, rule in rules.items()}
    paths = _configured_paths(cfg)
    return {
        "ok": all(status.ok for status in statuses.values()),
        "checked_at": datetime.now().isoformat(),
        "paths": paths,
        "output_dir_pattern": paths["output_dir_pattern"],
        "directories": {key: _status_payload(status) for key, status in statuses.items()},
    }
