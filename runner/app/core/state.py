# runner/app/core/state.py
"""
Global state management for multi-instance Runner.

This module provides centralized state management for runner instances,
storing critical runtime information that needs to be shared across
different modules while avoiding circular import dependencies.

The state includes:
- Runner identification and registration status
- Configuration and operational flags
- Runtime counters and statistics
"""

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# Global state dictionary for consistent state management
_RUNNER_STATE: Dict[str, Any] = {
    "runner_id": None,  # Will be set per instance
    "runner_instance_id": os.getenv("RUNNER_INSTANCE_ID", 0),
    "runner_instance_url": os.getenv("RUNNER_INSTANCE_URL", "http://localhost:8082"),
    "is_registered": False,
    "is_available": True,
    "registration_attempts": 0,
    "last_heartbeat": None,
    "manager_url": None,
    "startup_time": None,
    "task_statuses": {},
}

_TERMINAL_TASK_STATUSES = {"completed", "failed", "timeout"}
_ALLOWED_TASK_STATUSES = {"running", *_TERMINAL_TASK_STATUSES}
_RUNNER_STATE_LOCK = threading.RLock()
_RECOVERABLE_TASK_STATUSES = {"running", "failed", "timeout"}
_PERSISTED_STRING_FIELDS = ("runner_id", "completion_callback", "error_message")


def _normalize_positive_int(value: Any) -> Optional[int]:
    """Return a strictly positive integer when possible."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_non_negative_int(value: Any) -> Optional[int]:
    """Return a non-negative integer when possible."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _normalize_task_request(value: Any) -> Optional[Dict[str, Any]]:
    """Normalize persisted task request payload."""
    if isinstance(value, dict):
        return dict(value)

    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return None
        try:
            decoded_value = json.loads(raw_value)
        except Exception:
            return None
        if isinstance(decoded_value, dict):
            return decoded_value

    return None


def _instance_scoped_status_file(file_path: Path) -> Path:
    """Return a per-instance status file when RUNNER_INSTANCE_ID is set."""
    raw_instance_id = os.getenv("RUNNER_INSTANCE_ID")
    if raw_instance_id is None:
        return file_path

    try:
        instance_id = int(raw_instance_id.strip())
    except (TypeError, ValueError):
        return file_path

    suffix = file_path.suffix
    if suffix:
        scoped_name = f"{file_path.stem}.instance-{instance_id}{suffix}"
    else:
        scoped_name = f"{file_path.name}.instance-{instance_id}"
    return file_path.with_name(scoped_name)


def _sanitize_task_payload_for_persistence(
    task_id: str, task_payload: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Return a compact payload with only recovery-relevant fields."""
    if not isinstance(task_payload, dict):
        return None

    normalized_task_id = (task_id or "").strip()
    if not normalized_task_id:
        return None

    status = str(task_payload.get("status", "")).strip().lower()
    if status not in _RECOVERABLE_TASK_STATUSES:
        return None

    compact_payload: Dict[str, Any] = {
        "task_id": normalized_task_id,
        "status": status,
    }

    for field_name in _PERSISTED_STRING_FIELDS:
        value = task_payload.get(field_name)
        if not isinstance(value, str):
            continue
        normalized_value = value.strip()
        if normalized_value:
            compact_payload[field_name] = normalized_value

    process_pid = _normalize_positive_int(task_payload.get("process_pid"))
    if process_pid is not None:
        compact_payload["process_pid"] = process_pid

    restart_attempts = _normalize_non_negative_int(task_payload.get("recovery_restart_attempts"))
    if restart_attempts:
        compact_payload["recovery_restart_attempts"] = restart_attempts

    task_request = _normalize_task_request(task_payload.get("task_request"))
    if task_request is not None:
        compact_payload["task_request"] = task_request

    return compact_payload


def _resolve_task_status_file() -> Path:
    """Return task status persistence file path."""
    configured_path = (os.getenv("RUNNER_TASK_STATUS_FILE") or "").strip()
    if configured_path:
        return _instance_scoped_status_file(Path(configured_path).expanduser())

    if "pytest" in sys.modules or os.getenv("PYTEST_CURRENT_TEST"):
        return _instance_scoped_status_file(
            Path("/tmp") / f"esup-runner-task-statuses-pytest-{os.getpid()}.json"
        )

    storage_dir = (os.getenv("STORAGE_DIR") or "").strip()

    try:
        # Import lazily to avoid module-level coupling and ensure .env has been loaded.
        from app.core.config import get_config

        runtime_config = get_config()
        config_path = str(getattr(runtime_config, "RUNNER_TASK_STATUS_FILE", "") or "").strip()
        if config_path:
            return _instance_scoped_status_file(Path(config_path).expanduser())

        if not storage_dir:
            storage_dir = str(getattr(runtime_config, "STORAGE_DIR", "") or "").strip()
    except Exception:
        pass

    if not storage_dir:
        storage_dir = "/tmp/esup-runner"

    return _instance_scoped_status_file(
        Path(storage_dir).expanduser() / "runner_task_statuses.json"
    )


def _persist_task_statuses() -> None:
    """Persist runner task statuses atomically to disk."""
    with _RUNNER_STATE_LOCK:
        task_statuses = _RUNNER_STATE.get("task_statuses", {})
        if not isinstance(task_statuses, dict):
            return

        compact_task_statuses: Dict[str, Dict[str, Any]] = {}
        for raw_task_id, raw_task_payload in task_statuses.items():
            normalized_task_id = str(raw_task_id).strip()
            if not normalized_task_id:
                continue

            compact_payload = _sanitize_task_payload_for_persistence(
                normalized_task_id, raw_task_payload
            )
            if compact_payload is None:
                continue
            compact_task_statuses[normalized_task_id] = compact_payload

        file_path = _resolve_task_status_file()

        try:
            if not compact_task_statuses:
                file_path.unlink(missing_ok=True)
                return

            file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = file_path.with_name(f".{file_path.name}.tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(compact_task_statuses, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, file_path)
        except Exception:
            # Best-effort persistence: runtime logic should still work in-memory.
            return


def _load_task_statuses_from_disk() -> None:
    """Load persisted task statuses at startup if available."""
    file_path = _resolve_task_status_file()
    if not file_path.exists():
        return

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_payload = json.load(f)
    except Exception:
        return

    if not isinstance(raw_payload, dict):
        return

    normalized_statuses: Dict[str, Dict[str, Any]] = {}
    needs_rewrite = False
    raw_statuses = raw_payload
    for raw_task_id, raw_task_payload in raw_statuses.items():
        if not isinstance(raw_task_payload, dict):
            needs_rewrite = True
            continue

        normalized_payload = _sanitize_task_payload_for_persistence(
            str(raw_task_id), raw_task_payload
        )
        if normalized_payload is None:
            needs_rewrite = True
            continue

        task_id = normalized_payload["task_id"]
        normalized_statuses[task_id] = normalized_payload
        if normalized_payload != raw_task_payload:
            needs_rewrite = True

    with _RUNNER_STATE_LOCK:
        _RUNNER_STATE["task_statuses"] = normalized_statuses

    if needs_rewrite:
        _persist_task_statuses()


def _get_task_status_store() -> Dict[str, Dict[str, Any]]:
    """Return mutable task status map, reinitializing invalid states if needed."""
    task_statuses = _RUNNER_STATE.get("task_statuses")
    if not isinstance(task_statuses, dict):
        task_statuses = {}
        _RUNNER_STATE["task_statuses"] = task_statuses
    return task_statuses


def set_runner_instance_id(
    runner_instance_id: int,
    runner_base_name: str,
    runner_host: str,
    runner_instance_port: int,
) -> None:
    """
    Set instance-specific runner environnment ID.
    """
    _RUNNER_STATE["runner_instance_id"] = runner_instance_id
    # Create unique runner ID
    _RUNNER_STATE["runner_id"] = f"{runner_base_name}-{runner_host}-{str(runner_instance_port)}"


def get_runner_instance_id() -> int:
    """
    Get the instance-specific runner ID.

    Returns:
        int: Runner instance id
    """
    instance_id: int = _RUNNER_STATE["runner_instance_id"]
    return instance_id


def set_runner_instance_url(runner_instance_url: str) -> None:
    """
    Set instance-specific runner environnment URL.
    """
    _RUNNER_STATE["runner_instance_url"] = runner_instance_url


def get_runner_instance_url() -> str:
    """
    Get the instance-specific runner URL.

    Returns:
        str: Runner instance URL
    """
    url: str = _RUNNER_STATE["runner_instance_url"]
    return url


def get_runner_id() -> str:
    """
    Get instance-specific runner ID.
    """
    runner_id: str = _RUNNER_STATE["runner_id"]
    return runner_id


def is_registered() -> bool:
    """
    Check if the runner is currently registered with the manager.

    Returns:
        bool: True if runner is registered and active, False otherwise
    """
    registered: bool = _RUNNER_STATE["is_registered"]
    return registered


def set_registered(status: bool) -> None:
    """
    Update the runner registration status.

    Args:
        status: New registration status (True for registered, False for unregistered)
    """
    _RUNNER_STATE["is_registered"] = status
    if status:
        _RUNNER_STATE["registration_attempts"] = 0


def is_available() -> bool:
    """
    Check if the runner is currently available.

    Returns:
        bool: True if runner is available, False otherwise
    """
    available: bool = _RUNNER_STATE["is_available"]
    return available


def set_available(status: bool) -> None:
    """
    Update the runner available status.

    Args:
        status: New available status (True for available, False for unavailable)
    """
    _RUNNER_STATE["is_available"] = status


def increment_registration_attempts() -> int:
    """
    Increment the registration attempts counter and return the new value.

    Returns:
        int: Updated number of registration attempts
    """
    _RUNNER_STATE["registration_attempts"] += 1
    attempts: int = _RUNNER_STATE["registration_attempts"]
    return attempts


def get_registration_attempts() -> int:
    """
    Get the current number of registration attempts.

    Returns:
        int: Total number of registration attempts made
    """
    attempts: int = _RUNNER_STATE["registration_attempts"]
    return attempts


def set_manager_url(url: str) -> None:
    """
    Set the manager URL that this runner is registered with.

    Args:
        url: Manager base URL (e.g., "http://manager.example.com:8081")
    """
    _RUNNER_STATE["manager_url"] = url


def get_manager_url() -> Optional[str]:
    """
    Get the manager URL that this runner is registered with.

    Returns:
        Optional[str]: Manager URL if set, None otherwise
    """
    url: Optional[str] = _RUNNER_STATE["manager_url"]
    return url


def update_heartbeat() -> None:
    """
    Update the last heartbeat timestamp to current time.
    """
    import time

    _RUNNER_STATE["last_heartbeat"] = time.time()


def get_last_heartbeat() -> Optional[float]:
    """
    Get the timestamp of the last heartbeat.

    Returns:
        Optional[float]: Unix timestamp of last heartbeat, None if never sent
    """
    heartbeat: Optional[float] = _RUNNER_STATE["last_heartbeat"]
    return heartbeat


def set_startup_time() -> None:
    """
    Set the startup timestamp for this runner instance.
    """
    import time

    _RUNNER_STATE["startup_time"] = time.time()


def get_startup_time() -> Optional[float]:
    """
    Get the startup timestamp for this runner instance.

    Returns:
        Optional[float]: Unix timestamp when runner started, None if not set
    """
    startup: Optional[float] = _RUNNER_STATE["startup_time"]
    return startup


def get_uptime() -> Optional[float]:
    """
    Calculate the current uptime of the runner in seconds.

    Returns:
        Optional[float]: Uptime in seconds, None if startup time not set
    """
    startup_time = get_startup_time()
    if startup_time is None:
        return None
    import time

    return time.time() - startup_time


def set_task_status(
    task_id: str,
    status: str,
    *,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
) -> None:
    """
    Record the latest status of a task handled by this runner.

    The runner keeps this in-memory map so the manager can query task status
    even when completion callbacks were temporarily unavailable.
    """
    normalized_task_id = (task_id or "").strip()
    normalized_status = (status or "").strip().lower()

    if not normalized_task_id:
        return
    if normalized_status not in _ALLOWED_TASK_STATUSES:
        return

    with _RUNNER_STATE_LOCK:
        task_statuses = _get_task_status_store()
        previous_payload = task_statuses.get(normalized_task_id)

        payload: Dict[str, Any] = (
            dict(previous_payload) if isinstance(previous_payload, dict) else {}
        )
        payload["task_id"] = normalized_task_id
        payload["status"] = normalized_status

        if error_message:
            payload["error_message"] = str(error_message)
        elif normalized_status == "completed":
            payload.pop("error_message", None)

        if script_output:
            payload["script_output"] = str(script_output)

        if normalized_status in _TERMINAL_TASK_STATUSES:
            payload.pop("process_pid", None)

        task_statuses[normalized_task_id] = payload

    _persist_task_statuses()


def set_task_metadata(task_id: str, **metadata: Any) -> None:
    """Attach additional metadata to a tracked task and persist it."""
    normalized_task_id = (task_id or "").strip()
    if not normalized_task_id:
        return

    with _RUNNER_STATE_LOCK:
        task_statuses = _get_task_status_store()
        previous_payload = task_statuses.get(normalized_task_id)
        payload: Dict[str, Any] = (
            dict(previous_payload) if isinstance(previous_payload, dict) else {}
        )

        payload.setdefault("task_id", normalized_task_id)
        payload.setdefault("status", "running")

        for key, value in metadata.items():
            normalized_key = str(key).strip()
            if not normalized_key:
                continue
            if value is None:
                payload.pop(normalized_key, None)
            else:
                payload[normalized_key] = value

        task_statuses[normalized_task_id] = payload

    _persist_task_statuses()


def get_task_status(task_id: str) -> Optional[Dict[str, Any]]:
    """
    Return the tracked status payload for a task if available.
    """
    normalized_task_id = (task_id or "").strip()
    if not normalized_task_id:
        return None

    task_statuses = _RUNNER_STATE.get("task_statuses", {})
    payload = task_statuses.get(normalized_task_id)
    if not isinstance(payload, dict):
        return None
    return dict(payload)


def get_running_task_statuses() -> Dict[str, Dict[str, Any]]:
    """Return a copy of tasks currently marked as running."""
    task_statuses = _RUNNER_STATE.get("task_statuses", {})
    if not isinstance(task_statuses, dict):
        return {}

    running_tasks: Dict[str, Dict[str, Any]] = {}
    for task_id, payload in task_statuses.items():
        if not isinstance(payload, dict):
            continue
        if payload.get("status") != "running":
            continue
        running_tasks[str(task_id)] = dict(payload)
    return running_tasks


def clear_task_status(task_id: str) -> None:
    """
    Remove tracked status for a task.
    """
    normalized_task_id = (task_id or "").strip()
    if not normalized_task_id:
        return
    with _RUNNER_STATE_LOCK:
        task_statuses = _RUNNER_STATE.get("task_statuses", {})
        if isinstance(task_statuses, dict):
            task_statuses.pop(normalized_task_id, None)

    _persist_task_statuses()


def get_runner_state() -> Dict[str, Any]:
    """
    Get a complete snapshot of the runner state for monitoring and debugging.

    Returns:
        Dict[str, Any]: Complete runner state dictionary
    """
    return _RUNNER_STATE.copy()  # Return copy to prevent external modification


def reload_task_statuses_from_disk() -> None:
    """Reload persisted task statuses for the current instance context.

    Useful when process-level environment variables (for example
    `RUNNER_INSTANCE_ID`) are set after module import in forked workers.
    """
    with _RUNNER_STATE_LOCK:
        _RUNNER_STATE["task_statuses"] = {}
    _load_task_statuses_from_disk()


# Initialize startup time when module is first imported
set_startup_time()
_load_task_statuses_from_disk()
