"""Runtime filesystem usage diagnostics for runner status."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

GREEN_USED_PERCENT_LIMIT = 75.0
RED_USED_PERCENT_LIMIT = 90.0


def _humanize_bytes(value: int) -> str:
    """Return a compact df-like byte value."""
    units = ("B", "K", "M", "G", "T", "P")
    amount = float(max(int(value), 0))
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)}B"
            return f"{amount:.1f}{unit}"
        amount /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover


def _find_existing_parent(path: Path) -> Optional[Path]:
    """Return nearest existing parent path."""
    current = path
    while True:
        if current.exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def _status_for_used_percent(used_percent: Optional[float]) -> str:
    """Return green/orange/red/unknown status for a used percentage."""
    if used_percent is None:
        return "unknown"
    if used_percent >= RED_USED_PERCENT_LIMIT:
        return "red"
    if used_percent >= GREEN_USED_PERCENT_LIMIT:
        return "orange"
    return "green"


def _worst_status(statuses: list[str]) -> str:
    """Return the most severe status from a list of status labels."""
    for status in ("red", "orange", "unknown"):
        if status in statuses:
            return status
    return "green"


def _configured_directories(cfg: Any) -> dict[str, dict[str, str]]:
    """Return runtime directories worth exposing in runner status."""
    storage_dir = str(Path(str(getattr(cfg, "STORAGE_DIR", "/tmp/esup-runner"))).expanduser())
    cache_dir = Path(
        str(getattr(cfg, "CACHE_DIR", "/home/esup-runner/.cache/esup-runner"))
    ).expanduser()
    return {
        "STORAGE_DIR": {
            "path": storage_dir,
            "description": "Runner storage and task output root",
        },
        "CACHE_DIR": {
            "path": str(cache_dir),
            "description": "Shared runner cache root",
        },
        "WHISPER_MODELS_DIR": {
            "path": str(
                Path(
                    str(getattr(cfg, "WHISPER_MODELS_DIR", str(cache_dir / "whisper-models")))
                ).expanduser()
            ),
            "description": "Whisper models cache",
        },
        "HUGGINGFACE_MODELS_DIR": {
            "path": str(
                Path(
                    str(getattr(cfg, "HUGGINGFACE_MODELS_DIR", str(cache_dir / "huggingface")))
                ).expanduser()
            ),
            "description": "Hugging Face models cache",
        },
        "UV_CACHE_DIR": {
            "path": str(
                Path(str(getattr(cfg, "UV_CACHE_DIR", str(cache_dir / "uv")))).expanduser()
            ),
            "description": "uv package cache",
        },
        "LOG_DIR": {
            "path": str(Path(str(getattr(cfg, "LOG_DIR", "/var/log/esup-runner"))).expanduser()),
            "description": "Runner log directory",
        },
    }


def _usage_for_path(path: str, description: str) -> dict[str, Any]:
    """Return df-like usage for the filesystem containing path."""
    configured_path = Path(path)
    target_path = (
        configured_path if configured_path.exists() else _find_existing_parent(configured_path)
    )
    if target_path is None:
        return {
            "path": path,
            "target_path": "",
            "description": description,
            "exists": False,
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "total_human": "0B",
            "used_human": "0B",
            "free_human": "0B",
            "used_percent": None,
            "used_percent_display": "n/a",
            "status": "unknown",
            "error": "No existing parent path found.",
        }

    try:
        usage = shutil.disk_usage(target_path)
    except OSError as exc:
        return {
            "path": path,
            "target_path": str(target_path),
            "description": description,
            "exists": configured_path.exists(),
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "total_human": "0B",
            "used_human": "0B",
            "free_human": "0B",
            "used_percent": None,
            "used_percent_display": "n/a",
            "status": "unknown",
            "error": str(exc),
        }

    used_percent = round((usage.used / usage.total) * 100.0, 1) if usage.total > 0 else None
    return {
        "path": path,
        "target_path": str(target_path),
        "description": description,
        "exists": configured_path.exists(),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "total_human": _humanize_bytes(usage.total),
        "used_human": _humanize_bytes(usage.used),
        "free_human": _humanize_bytes(usage.free),
        "used_percent": used_percent,
        "used_percent_display": f"{used_percent:.1f}%" if used_percent is not None else "n/a",
        "status": _status_for_used_percent(used_percent),
        "error": "",
    }


def collect_disk_usage(cfg: Any) -> dict[str, Any]:
    """Return df-like filesystem usage for runner runtime directories."""
    directories = {
        key: _usage_for_path(item["path"], item["description"])
        for key, item in _configured_directories(cfg).items()
    }
    storage_dir = str(Path(str(getattr(cfg, "STORAGE_DIR", "/tmp/esup-runner"))).expanduser())
    overall_status = _worst_status([item["status"] for item in directories.values()])
    return {
        "checked_at": datetime.now().isoformat(),
        "status": overall_status,
        "ok": overall_status != "red",
        "thresholds": {
            "green_below_used_percent": GREEN_USED_PERCENT_LIMIT,
            "orange_from_used_percent": GREEN_USED_PERCENT_LIMIT,
            "red_from_used_percent": RED_USED_PERCENT_LIMIT,
        },
        "output_dir_pattern": str(Path(storage_dir) / "<task_id>" / "output"),
        "directories": directories,
    }
