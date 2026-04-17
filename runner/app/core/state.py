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

import os
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


def set_runner_instance_id(
    runner_instance_id: int, runner_base_name: str, runner_host: str, runner_instance_port: int
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

    payload: Dict[str, Any] = {
        "task_id": normalized_task_id,
        "status": normalized_status,
    }
    if error_message:
        payload["error_message"] = str(error_message)
    if script_output:
        payload["script_output"] = str(script_output)

    task_statuses = _RUNNER_STATE.setdefault("task_statuses", {})
    task_statuses[normalized_task_id] = payload


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


def clear_task_status(task_id: str) -> None:
    """
    Remove tracked status for a task.
    """
    normalized_task_id = (task_id or "").strip()
    if not normalized_task_id:
        return
    task_statuses = _RUNNER_STATE.get("task_statuses", {})
    if isinstance(task_statuses, dict):
        task_statuses.pop(normalized_task_id, None)


def get_runner_state() -> Dict[str, Any]:
    """
    Get a complete snapshot of the runner state for monitoring and debugging.

    Returns:
        Dict[str, Any]: Complete runner state dictionary
    """
    return _RUNNER_STATE.copy()  # Return copy to prevent external modification


# Initialize startup time when module is first imported
set_startup_time()
