# runner/app/api/routes/task.py
"""
Task management routes for Runner.
Handles task execution, status tracking, and result streaming endpoints.
"""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from app.core.auth import get_current_manager
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import (
    get_runner_id,
    get_runner_state,
    get_task_status,
    is_available,
    is_registered,
    set_available,
    set_task_metadata,
    set_task_status,
)
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest, TaskResultResponse
from app.services.email_service import send_task_failure_email
from app.services.task_dispatcher import task_dispatcher

# Configure logging
logger = setup_default_logging()

# Create API router for task-related endpoints
router = APIRouter(prefix="/task", tags=["Task"])

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RESULT_PATH_PART_RE = re.compile(r"^[A-Za-z0-9._ -]+$")
_RECOVERY_MONITOR_INTERVAL_SECONDS = 10
_MAX_RECOVERY_LOG_CHARS = 100000
_RECOVERY_AUTO_RESTART_MAX_ATTEMPTS = 1
_RECOVERY_MONITORS: dict[str, asyncio.Task] = {}

# ======================================================
# Utility Functions
# ======================================================


def _validate_task_id(task_id: str) -> str:
    """Validate a task identifier before using it in filesystem paths."""
    safe_task_id = (task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(safe_task_id):
        raise HTTPException(status_code=404, detail="File not found")
    return safe_task_id


def _validate_result_relative_path(file_path: str) -> tuple[str, ...]:
    """Validate a relative file path under a task output directory."""
    raw_path = (file_path or "").strip().replace("\\", "/")
    if not raw_path:
        raise HTTPException(status_code=404, detail="File not found")

    relative_path = Path(raw_path)
    if relative_path.is_absolute():
        raise HTTPException(status_code=404, detail="File not found")

    safe_parts = []
    for part in relative_path.parts:
        if part in {"", ".", ".."}:
            raise HTTPException(status_code=404, detail="File not found")
        if not _RESULT_PATH_PART_RE.fullmatch(part):
            raise HTTPException(status_code=404, detail="File not found")
        safe_parts.append(part)

    if not safe_parts:
        raise HTTPException(status_code=404, detail="File not found")

    return tuple(safe_parts)


def _resolve_storage_base_path() -> Path:
    """Resolve storage base path."""
    return Path(storage_manager.base_path).resolve()


def _find_direct_child_entry(directory: Path, entry_name: str) -> Path | None:
    """Find a direct child entry by name without composing a user-controlled path."""
    try:
        for candidate in directory.iterdir():
            if candidate.name == entry_name:
                return candidate
    except OSError:
        return None
    return None


def _resolve_within_base(candidate: Path, base_path: Path) -> Path:
    """Resolve a candidate path and enforce that it stays under base_path."""
    resolved = candidate.resolve(strict=False)
    if resolved != base_path and base_path not in resolved.parents:
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


def _resolve_output_file_path(output_dir: Path, relative_parts: tuple[str, ...]) -> Path:
    """Resolve a relative output path by traversing directory entries safely."""
    current_path = output_dir

    for index, part in enumerate(relative_parts):
        next_candidate = _find_direct_child_entry(current_path, part)
        if next_candidate is None:
            raise HTTPException(status_code=404, detail="File not found")

        next_path = _resolve_within_base(next_candidate, output_dir)
        if index < len(relative_parts) - 1 and not next_path.is_dir():
            raise HTTPException(status_code=404, detail="File not found")

        current_path = next_path

    return current_path


def _derive_failure_status(error_message: str) -> str:
    """Derive a failure status from the error message.

    Returns "timeout" when the message indicates a timeout; otherwise "failed".
    """
    if "timeout" in (error_message or "").lower():
        return "timeout"
    return "failed"


def _collect_script_stream_chunks(
    script_output: object,
    *,
    context: Optional[str] = None,
) -> list[str]:
    """Collect `[info_script.log]` / `[error_script.log]`-style chunks from nested payloads."""
    if isinstance(script_output, dict):
        chunks: list[str] = []

        has_stream_keys = "stdout" in script_output or "stderr" in script_output
        if has_stream_keys:
            label_prefix = f"{context}/" if context else ""
            stdout_text = str(script_output.get("stdout") or "").strip()
            stderr_text = str(script_output.get("stderr") or "").strip()
            if stdout_text:
                chunks.append(f"[{label_prefix}info_script.log]\n{stdout_text}")
            if stderr_text:
                chunks.append(f"[{label_prefix}error_script.log]\n{stderr_text}")

        for key, value in script_output.items():
            if key in {"stdout", "stderr"}:
                continue
            key_text = str(key).strip()
            if not key_text:
                continue
            child_context = key_text if context is None else f"{context}.{key_text}"
            chunks.extend(_collect_script_stream_chunks(value, context=child_context))
        return chunks

    if isinstance(script_output, list):
        list_chunks: list[str] = []
        for index, value in enumerate(script_output):
            child_context = str(index) if context is None else f"{context}[{index}]"
            list_chunks.extend(_collect_script_stream_chunks(value, context=child_context))
        return list_chunks

    return []


def _normalize_script_output(script_output: object) -> Optional[str]:
    """Normalize script output into a serializable string for status payloads."""
    if script_output is None:
        return None
    if isinstance(script_output, str):
        return script_output

    stream_chunks = _collect_script_stream_chunks(script_output)
    if stream_chunks:
        return "\n\n".join(stream_chunks)

    try:
        return json.dumps(script_output, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(script_output)


def _resolve_task_manifest_path(task_id: str) -> Path:
    """Resolve canonical manifest path (<base>/<task_id>/manifest.json)."""
    task_root = _resolve_task_root(task_id)
    manifest_candidate = _find_direct_child_entry(task_root, "manifest.json")
    if manifest_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    manifest_path = _resolve_within_base(manifest_candidate, task_root)
    if manifest_path.parent != task_root:
        raise HTTPException(status_code=404, detail="File not found")
    if not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return manifest_path


def _resolve_task_root(task_id: str) -> Path:
    """Resolve task root directory and reject path traversal."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    task_candidate = _find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    task_root = _resolve_within_base(task_candidate, base_path)
    if not task_root.is_dir():
        raise HTTPException(status_code=404, detail="File not found")

    return task_root


def _resolve_task_root_if_exists(task_id: str) -> Path | None:
    """Resolve task root for delete flows (None when missing/invalid)."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    task_candidate = _find_direct_child_entry(base_path, safe_task_id)
    if task_candidate is None:
        return None

    try:
        task_root = _resolve_within_base(task_candidate, base_path)
    except HTTPException:
        return None

    if not task_root.is_dir():
        return None

    return task_root


def _resolve_legacy_manifest_if_exists(task_id: str) -> Path | None:
    """Resolve legacy flat manifest path (<base>/<task_id>.json) when present."""
    safe_task_id = _validate_task_id(task_id)
    base_path = _resolve_storage_base_path()
    legacy_name = f"{safe_task_id}.json"

    candidate = _find_direct_child_entry(base_path, legacy_name)
    if candidate is None:
        return None

    try:
        resolved = _resolve_within_base(candidate, base_path)
    except HTTPException:
        return None

    if resolved.parent != base_path:
        return None
    if not resolved.is_file():
        return None

    return resolved


def _read_text_tail(file_path: Path, max_chars: int = _MAX_RECOVERY_LOG_CHARS) -> str:
    """Read a text file and keep only the tail to cap response size."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def _parse_process_pid(payload: dict) -> int | None:
    """Extract persisted process PID from task payload."""
    raw_pid = payload.get("process_pid")
    if raw_pid is None:
        return None
    try:
        pid = int(raw_pid)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _is_process_alive(pid: int) -> bool:
    """Return True when the process currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _collect_recovery_script_output(task_id: str, payload: dict) -> Optional[str]:
    """Collect script stdout/stderr logs for recovery diagnostics."""
    candidate_paths: list[Path] = []

    task_root = _resolve_task_root_if_exists(task_id)
    if task_root is not None:
        candidate_paths.extend(
            [
                task_root / "info_script.log",
                task_root / "error_script.log",
            ]
        )

    # Keep compatibility with previously persisted absolute paths.
    for key in ("script_stdout_path", "script_stderr_path"):
        raw_path = payload.get(key)
        if not isinstance(raw_path, str):
            continue
        normalized_path = raw_path.strip()
        if not normalized_path:
            continue
        candidate = Path(normalized_path)
        if candidate not in candidate_paths:
            candidate_paths.append(candidate)

    chunks: list[str] = []
    for path in candidate_paths:
        text = _read_text_tail(path)
        if not text.strip():
            continue
        chunks.append(f"[{path.name}]\n{text.strip()}")

    if not chunks:
        return None

    merged = "\n\n".join(chunks)
    if len(merged) <= _MAX_RECOVERY_LOG_CHARS:
        return merged
    return merged[-_MAX_RECOVERY_LOG_CHARS:]


def _resolve_task_output_dir_if_exists(task_id: str) -> Path | None:
    """Resolve task output directory for recovery workflows."""
    task_root = _resolve_task_root_if_exists(task_id)
    if task_root is None:
        return None

    output_candidate = _find_direct_child_entry(task_root, "output")
    if output_candidate is None:
        return None

    try:
        output_dir = _resolve_within_base(output_candidate, task_root)
    except HTTPException:
        return None

    if not output_dir.is_dir():
        return None
    return output_dir


def _has_useful_output_files(output_dir: Path) -> bool:
    """Return True when output dir includes at least one deliverable file."""
    ignored_names = {
        "task_metadata.json",
        "info_video.json",
        "encoding.log",
    }
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name.lower() in ignored_names:
            continue
        return True
    return False


def _read_recovery_task_results(task_id: str) -> Optional[dict]:
    """Read `results` from task metadata when present."""
    output_dir = _resolve_task_output_dir_if_exists(task_id)
    if output_dir is None:
        return None

    metadata_candidate = _find_direct_child_entry(output_dir, "task_metadata.json")
    if metadata_candidate is None:
        return None

    try:
        metadata_path = _resolve_within_base(metadata_candidate, output_dir)
    except HTTPException:
        return None

    if not metadata_path.is_file():
        return None

    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None

    if not isinstance(payload, dict):
        return None

    raw_results = payload.get("results")
    if not isinstance(raw_results, dict):
        return None

    return dict(raw_results)


def _ensure_recovery_manifest(task_id: str) -> bool:
    """Ensure canonical manifest exists using current task output directory."""
    try:
        _resolve_task_manifest_path(task_id)
        return True
    except HTTPException as exc:
        if exc.status_code != 404:
            return False

    output_dir = _resolve_task_output_dir_if_exists(task_id)
    if output_dir is None:
        return False

    if not _has_useful_output_files(output_dir):
        return False

    output_files = [
        str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()
    ]
    manifest = {
        "task_id": task_id,
        "files": output_files,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    manifest_path = output_dir.parent / "manifest.json"
    temp_manifest_path = manifest_path.with_name(".manifest.json.tmp")

    try:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp_manifest_path, "wb") as manifest_file:
            manifest_file.write(manifest_bytes)
            manifest_file.flush()
            os.fsync(manifest_file.fileno())
        temp_manifest_path.replace(manifest_path)
    except Exception:
        return False

    return True


def _infer_workspace_terminal_status(
    task_id: str, payload: dict
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Infer terminal status from workspace artifacts after a restart."""
    raw_results = _read_recovery_task_results(task_id)
    script_output = (
        _normalize_script_output(raw_results.get("script_output"))
        if isinstance(raw_results, dict)
        else None
    )
    if not script_output:
        script_output = _collect_recovery_script_output(task_id, payload)

    if isinstance(raw_results, dict) and raw_results.get("success") is True:
        if not _ensure_recovery_manifest(task_id):
            return None
        return ("completed", None, script_output)

    if isinstance(raw_results, dict):
        error_message = str(
            raw_results.get("error")
            or payload.get("error_message")
            or "Task failed before runner restart"
        )
        failure_status = _derive_failure_status(error_message)
        return (failure_status, error_message, script_output)

    if not _ensure_recovery_manifest(task_id):
        return None

    return ("completed", None, script_output)


def _refresh_availability_from_recovered_state() -> None:
    """Set runner availability from currently tracked running tasks."""
    has_running_tasks = bool(_get_owned_task_statuses({"running"}))
    set_available(not has_running_tasks)


def initialize_startup_availability() -> None:
    """Set startup availability before manager registration.

    When persisted recoverable tasks exist for this runner instance, mark the
    runner unavailable immediately so the manager does not dispatch new tasks
    before reconciliation/restart flows complete.
    """
    if _get_owned_task_statuses({"running", "failed", "timeout"}):
        set_available(False)


def _get_owned_task_statuses(statuses: set[str]) -> dict[str, dict]:
    """Return task statuses owned by this runner for requested status values."""
    current_runner_id = str(get_runner_id() or "").strip()
    instance_scoped_state = bool((os.getenv("RUNNER_INSTANCE_ID") or "").strip())
    runner_state = get_runner_state()
    task_statuses = runner_state.get("task_statuses", {})

    if not isinstance(task_statuses, dict):
        return {}

    owned_statuses: dict[str, dict] = {}
    for task_id, payload in task_statuses.items():
        if not isinstance(payload, dict):
            continue

        status = str(payload.get("status") or "").strip().lower()
        if status not in statuses:
            continue

        payload_runner_id = str(payload.get("runner_id") or "").strip()
        # When task-status storage is scoped per instance, runner_id can drift across
        # restarts (host/port/env changes) without implying a foreign owner.
        if (
            payload_runner_id
            and current_runner_id
            and payload_runner_id != current_runner_id
            and not instance_scoped_state
        ):
            continue

        normalized_task_id = str(task_id).strip()
        if not normalized_task_id:
            continue
        owned_statuses[normalized_task_id] = dict(payload)

    return owned_statuses


def _get_recovery_restart_attempts(payload: dict) -> int:
    """Return normalized startup auto-restart attempts for one task payload."""
    raw_attempts = payload.get("recovery_restart_attempts", 0)
    try:
        attempts = int(raw_attempts)
    except (TypeError, ValueError):
        return 0
    return max(0, attempts)


def _load_recovery_task_request(task_id: str, payload: dict) -> Optional[TaskRequest]:
    """Load persisted TaskRequest payload used for startup task auto-restart."""
    raw_task_request = payload.get("task_request")

    if isinstance(raw_task_request, str):
        try:
            raw_task_request = json.loads(raw_task_request)
        except Exception:
            return None

    if not isinstance(raw_task_request, dict):
        return None

    task_request_payload = dict(raw_task_request)
    task_request_payload["task_id"] = task_id

    completion_callback = payload.get("completion_callback")
    if (
        isinstance(completion_callback, str)
        and completion_callback.strip()
        and not task_request_payload.get("completion_callback")
    ):
        task_request_payload["completion_callback"] = completion_callback

    try:
        return TaskRequest.model_validate(task_request_payload)
    except Exception:
        return None


def _schedule_failed_task_restart(task_id: str, payload: dict) -> bool:
    """Schedule automatic restart of one failed task after startup recovery."""
    restart_attempts = _get_recovery_restart_attempts(payload)
    if restart_attempts >= _RECOVERY_AUTO_RESTART_MAX_ATTEMPTS:
        logger.warning(
            "Skipping automatic restart for task %s: max attempts reached (%s)",
            task_id,
            _RECOVERY_AUTO_RESTART_MAX_ATTEMPTS,
        )
        return False

    task_request = _load_recovery_task_request(task_id, payload)
    if task_request is None:
        logger.warning(
            "Skipping automatic restart for task %s: missing or invalid persisted task_request",
            task_id,
        )
        return False

    set_task_metadata(
        task_id,
        runner_id=get_runner_id(),
        completion_callback=task_request.completion_callback,
        task_request=task_request.model_dump(mode="json"),
        recovery_restart_attempts=restart_attempts + 1,
        error_message=None,
    )
    set_task_status(task_id, "running")
    asyncio.create_task(process_task(task_id, task_request))

    logger.info(
        "Scheduled automatic restart for task %s after startup recovery (attempt %s/%s)",
        task_id,
        restart_attempts + 1,
        _RECOVERY_AUTO_RESTART_MAX_ATTEMPTS,
    )
    return True


async def _finalize_recovered_task(
    task_id: str,
    payload: dict,
    *,
    status: str,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
) -> None:
    """Persist terminal status and re-notify manager callback when possible."""
    set_task_status(
        task_id,
        status,
        error_message=error_message,
        script_output=script_output,
    )

    completion_callback = payload.get("completion_callback")
    if isinstance(completion_callback, str) and completion_callback.strip():
        await notify_completion(
            completion_callback,
            task_id,
            status,
            error_message,
            script_output,
        )


async def _reconcile_recovered_task(task_id: str, payload: dict) -> str:
    """Reconcile one previously running task after a runner restart."""
    try:
        _resolve_task_manifest_path(task_id)
        script_output = _collect_recovery_script_output(task_id, payload)
        await _finalize_recovered_task(
            task_id,
            payload,
            status="completed",
            script_output=script_output,
        )
        return "completed"
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    pid = _parse_process_pid(payload)
    if pid is not None and _is_process_alive(pid):
        set_task_status(task_id, "running")
        return "running"

    workspace_terminal_status = _infer_workspace_terminal_status(task_id, payload)
    if workspace_terminal_status is not None:
        status, error_message, script_output = workspace_terminal_status
        await _finalize_recovered_task(
            task_id,
            payload,
            status=status,
            error_message=error_message,
            script_output=script_output,
        )
        return status

    script_output = _collect_recovery_script_output(task_id, payload)
    error_message = str(payload.get("error_message") or "Task process is no longer running")
    failure_status = _derive_failure_status(error_message)
    await _finalize_recovered_task(
        task_id,
        payload,
        status=failure_status,
        error_message=error_message,
        script_output=script_output,
    )
    return failure_status


async def _monitor_recovered_task(task_id: str) -> None:
    """Background monitor for a recovered in-flight task."""
    try:
        while True:
            await asyncio.sleep(_RECOVERY_MONITOR_INTERVAL_SECONDS)

            payload = get_task_status(task_id)
            if not isinstance(payload, dict):
                return
            if payload.get("status") != "running":
                return

            reconciled_status = await _reconcile_recovered_task(task_id, payload)
            if reconciled_status != "running":
                _refresh_availability_from_recovered_state()
                return
    except Exception as exc:
        logger.error("Recovered task monitor failed for %s: %s", task_id, exc, exc_info=True)
    finally:
        _RECOVERY_MONITORS.pop(task_id, None)


def _schedule_recovery_monitor(task_id: str) -> None:
    """Schedule background monitoring for one recovered running task."""
    existing = _RECOVERY_MONITORS.get(task_id)
    if existing is not None and not existing.done():
        return
    _RECOVERY_MONITORS[task_id] = asyncio.create_task(_monitor_recovered_task(task_id))


async def _recover_owned_running_tasks(running_tasks: dict[str, dict]) -> None:
    """Recover running tasks tracked for the current runner instance."""
    if not running_tasks:
        return

    logger.info("Recovering %s running task(s) after restart", len(running_tasks))

    for task_id, payload in running_tasks.items():
        try:
            status = await _reconcile_recovered_task(task_id, payload)
        except Exception as exc:
            logger.error("Failed to recover task %s: %s", task_id, exc, exc_info=True)
            continue

        if status == "running":
            _schedule_recovery_monitor(task_id)


async def _recover_failed_task(task_id: str, payload: dict) -> bool:
    """Recover one failed task. Returns True when a restart is scheduled."""
    workspace_terminal_status = _infer_workspace_terminal_status(task_id, payload)
    if workspace_terminal_status is not None:
        status, _error_message, script_output = workspace_terminal_status
        if status == "completed":
            await _finalize_recovered_task(
                task_id,
                payload,
                status="completed",
                script_output=script_output,
            )
            return False

    return _schedule_failed_task_restart(task_id, payload)


async def _recover_owned_failed_tasks(failed_tasks: dict[str, dict]) -> int:
    """Recover failed/timeout tasks and return the number of restarted tasks."""
    if not failed_tasks:
        return 0

    logger.info(
        "Inspecting %s failed task(s) for startup auto-restart",
        len(failed_tasks),
    )

    restarted_tasks = 0
    for task_id, payload in failed_tasks.items():
        try:
            if await _recover_failed_task(task_id, payload):
                restarted_tasks += 1
        except Exception as exc:
            logger.error("Failed to recover failed task %s: %s", task_id, exc, exc_info=True)

    if restarted_tasks:
        logger.info("Scheduled automatic restart for %s failed task(s)", restarted_tasks)

    return restarted_tasks


async def recover_running_tasks_after_restart() -> None:
    """Restore running task state after process restart using persisted JSON."""
    running_tasks = _get_owned_task_statuses({"running"})
    if running_tasks:
        await _recover_owned_running_tasks(running_tasks)

    # Re-read failed/timeout after running-task reconciliation so tasks that
    # just transitioned from running -> failed/timeout are restarted now.
    failed_tasks = _get_owned_task_statuses({"failed", "timeout"})
    if not running_tasks and not failed_tasks:
        _refresh_availability_from_recovered_state()
        return

    await _recover_owned_failed_tasks(failed_tasks)

    _refresh_availability_from_recovered_state()


async def stop_recovery_monitors() -> None:
    """Cancel all running recovery monitor tasks."""
    if not _RECOVERY_MONITORS:
        return

    monitors = list(_RECOVERY_MONITORS.values())
    _RECOVERY_MONITORS.clear()

    for monitor_task in monitors:
        monitor_task.cancel()
    await asyncio.gather(*monitors, return_exceptions=True)


async def process_task(task_id: str, task_request: TaskRequest):
    """
    Process task using task dispatcher.
    """
    completion_callback = task_request.completion_callback
    try:
        set_task_metadata(
            task_id,
            runner_id=get_runner_id(),
            completion_callback=completion_callback,
            task_request=task_request.model_dump(mode="json"),
        )

        # Mark runner as busy
        set_available(False)
        set_task_status(task_id, "running")

        logger.info(
            f"Starting task {task_id} of type {task_request.task_type} with parameters: {task_request.parameters}"
        )

        # Dispatch task to appropriate handler
        results = await task_dispatcher.dispatch_task(task_id=task_id, task_request=task_request)

        # Notify manager of task completion if callback provided
        if results.get("success"):
            script_output_text = _normalize_script_output(results.get("script_output"))
            set_task_status(task_id, "completed", script_output=script_output_text)
            logger.info(f"Task {task_id} completed successfully")
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    "completed",
                    None,
                    script_output_text,
                )
        else:
            error_msg = results.get("error", "Unknown error")
            failure_status = _derive_failure_status(error_msg)
            # Detailed error from script output if available
            script_output_text = _normalize_script_output(results.get("script_output"))
            set_task_status(
                task_id,
                failure_status,
                error_message=error_msg,
                script_output=script_output_text,
            )
            logger.error(f"Task {task_id} failed: {error_msg}")
            logger.info(f"Sending failure email for task {task_id}")
            await send_task_failure_email(
                task_id=task_id,
                task_type=task_request.task_type,
                status=failure_status,
                error_message=error_msg,
                script_output=script_output_text,
            )
            if completion_callback:
                await notify_completion(
                    completion_callback,
                    task_id,
                    failure_status,
                    error_msg,
                    script_output_text,
                )

    except Exception as e:
        error_msg = str(e)
        failure_status = _derive_failure_status(error_msg)
        set_task_status(task_id, failure_status, error_message=error_msg)
        logger.error(f"Error processing task {task_id}: {error_msg}")
        logger.info(f"Sending failure email for task {task_id}")
        await send_task_failure_email(
            task_id=task_id,
            task_type=task_request.task_type,
            status=failure_status,
            error_message=error_msg,
        )
        if completion_callback:
            await notify_completion(completion_callback, task_id, failure_status, error_msg)
    finally:
        # Recompute availability from tracked running tasks to avoid
        # advertising availability while other tasks are still running.
        _refresh_availability_from_recovered_state()


@router.get(
    "/status/{task_id}",
    response_model=dict,
    summary="Get task execution status",
    description="Return the runner-side status for a specific task",
    tags=["Task"],
)
async def get_task_status_endpoint(
    task_id: str,
    current_manager: str = Depends(get_current_manager),
) -> dict:
    """
    Return the current task status known by the runner.

    Status source priority:
    1) In-memory task status tracked during execution.
    2) Result manifest presence on disk (completed).
    3) Unknown task (not_found).
    """
    safe_task_id = _validate_task_id(task_id)

    tracked_status = get_task_status(safe_task_id)
    if tracked_status is not None:
        return tracked_status

    try:
        _resolve_task_manifest_path(safe_task_id)
        return {"task_id": safe_task_id, "status": "completed"}
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    return {"task_id": safe_task_id, "status": "not_found"}


async def notify_completion(
    callback_url: str,
    task_id: str,
    status: str,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
):
    """
    Notify manager about task completion status.

    Args:
        callback_url: Manager endpoint to receive notification
        task_id: Task identifier being reported
        status: Completion status ('completed' or 'failed')
        error_message: Optional error details for failed tasks
        script_output: Optional script output for debugging
    """
    max_retries = max(0, int(config.COMPLETION_NOTIFY_MAX_RETRIES))
    base_delay = max(0, int(config.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS))
    backoff_factor = max(1.0, float(config.COMPLETION_NOTIFY_BACKOFF_FACTOR))
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)

    attempt = 0
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    callback_url,
                    json={
                        "task_id": task_id,
                        "status": status,
                        "error_message": error_message,
                        "script_output": script_output,
                    },
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    },
                )

                if response.status_code == 200:
                    if attempt > 1:
                        logger.info(
                            f"Completion notification sent for task {task_id} after {attempt} attempts"
                        )
                    else:
                        logger.info(f"Completion notification sent for task {task_id}")
                    return True

                logger.warning(
                    "Completion notification failed (attempt %s/%s): %s - %s",
                    attempt,
                    max_retries + 1,
                    response.status_code,
                    response.text,
                )

        except Exception as e:
            logger.error(
                "Error sending completion notification to %s (attempt %s/%s): %s",
                callback_url,
                attempt,
                max_retries + 1,
                str(e),
            )

        if attempt > max_retries:
            return False

        delay = int(base_delay * (backoff_factor ** (attempt - 1)))
        if delay > 0:  # pragma: no cover - timing branch covered by config
            logger.warning(
                "Retrying completion notification for task %s in %s seconds (attempt %s/%s)",
                task_id,
                delay,
                attempt + 1,
                max_retries + 1,
            )
            await asyncio.sleep(delay)


# ======================================================
# API Endpoints
# ======================================================


@router.get(
    "/result/{task_id}",
    responses={
        200: {"description": "Task result manifest", "content": {"application/json": {}}},
        404: {"description": "Result file not found"},
    },
    summary="Get task result",
    description="Stream task result directly from runner storage",
    tags=["Task"],
)
async def get_task_result(
    task_id: str,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Stream task result directly from runner storage.

    Args:
        task_id: Unique identifier of the task
        background_tasks: FastAPI background tasks for cleanup
        current_manager: Authenticated manager instance

    Returns:
        FileResponse: Streamed JSON manifest containing task results
    """
    logger.info(f"Retrieving task result: {task_id}")

    file_path = _resolve_task_manifest_path(task_id)

    # Schedule cleanup after file delivery if necessary
    # background_tasks.add_task(storage_manager.cleanup, task_id)

    # Return file with streaming support
    return FileResponse(
        str(file_path),
        media_type="application/json",
        filename="manifest.json",
    )


@router.get(
    "/result/{task_id}/file/{filename}",
    responses={
        200: {
            "description": "Task result file",
            "content": {"application/octet-stream": {}},
        },
        404: {"description": "Result file not found"},
    },
    summary="Get task result file",
    description="Stream a single task output file from runner storage",
    tags=["Task"],
)
async def get_task_result_file(
    task_id: str,
    filename: str,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Stream a single task output file from runner storage.

    Args:
        task_id: Unique identifier of the task
        filename: Relative file path from the task output directory
        background_tasks: FastAPI background tasks for cleanup
        current_manager: Authenticated manager instance

    Returns:
        FileResponse: Streamed file content
    """
    logger.info(f"Retrieving task result file: {task_id}/{filename}")

    task_root = _resolve_task_root(task_id)
    output_candidate = _find_direct_child_entry(task_root, "output")
    if output_candidate is None:
        raise HTTPException(status_code=404, detail="File not found")

    output_dir = _resolve_within_base(output_candidate, task_root)
    if not output_dir.is_dir():
        raise HTTPException(status_code=404, detail="File not found")

    safe_relative_parts = _validate_result_relative_path(filename)
    file_path = _resolve_output_file_path(output_dir, safe_relative_parts)

    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        str(file_path),
        media_type="application/octet-stream",
        filename=file_path.name,
    )


@router.delete(
    "/delete/{task_id}",
    response_model=TaskResultResponse,
    summary="Delete task result",
    description="Remove result files for a specific task",
    tags=["Storage"],
)
async def delete_task_result(task_id: str, current_manager: str = Depends(get_current_manager)):
    """
    Delete task result files and free up storage.

    Args:
        task_id: Unique identifier of the task to delete
        current_manager: Authenticated manager instance

    Returns:
        dict: Deletion status confirmation
    """
    task_root = _resolve_task_root_if_exists(task_id)
    if task_root is not None:
        shutil.rmtree(task_root)

    # Backward compatibility cleanup for legacy flat manifest files.
    legacy_manifest = _resolve_legacy_manifest_if_exists(task_id)
    if legacy_manifest is not None:
        legacy_manifest.unlink()

    # Recompute availability from tracked running tasks.
    _refresh_availability_from_recovered_state()
    return {"status": "deleted"}


@router.post(
    "/run",
    response_model=dict,
    summary="Execute task",
    description="Start task execution on the runner",
    tags=["Task"],
)
async def run_task(
    task_request: TaskRequest,
    background_tasks: BackgroundTasks,
    current_manager: str = Depends(get_current_manager),
):
    """
    Initiate task execution on the runner.

    Args:
        task_request: Task execution request details
        background_tasks: FastAPI background tasks for async processing
        current_manager: Authenticated manager instance

    Returns:
        dict: Task execution status and identifier
    """
    logger.info(f"Received task execution request, registered: {is_registered()}")

    # Verify runner is registered with manager
    if not is_registered():
        raise HTTPException(status_code=503, detail="Runner not registered with manager")

    if not is_available():
        raise HTTPException(status_code=400, detail="Runner is busy")

    # Mark runner as busy
    set_available(False)
    set_task_status(task_request.task_id, "running")
    set_task_metadata(
        task_request.task_id,
        runner_id=get_runner_id(),
        completion_callback=task_request.completion_callback,
        task_request=task_request.model_dump(mode="json"),
    )

    # Start task processing in background
    background_tasks.add_task(
        process_task, task_id=task_request.task_id, task_request=task_request
    )  # pragma: no cover (background scheduling side-effect)

    return {"status": "started", "task_id": task_request.task_id}  # pragma: no cover
