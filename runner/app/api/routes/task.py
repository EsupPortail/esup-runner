# runner/app/api/routes/task.py
"""
Task management routes for Runner.
Handles task execution, status tracking, and result streaming endpoints.
"""

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from app.core.auth import get_current_manager
from app.core.config import config
from app.core.encoding_diagnostics import INCOMPLETE_OUTPUT_PREFIX, prepend_encoding_warning
from app.core.setup_logging import setup_default_logging
from app.core.state import clear_task_status  # noqa: F401 - recovery runtime hook
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
from app.services import task_monitor, task_recovery, task_results
from app.services.email_service import send_task_failure_email
from app.services.manager_service import manager_task_exists  # noqa: F401 - recovery runtime hook
from app.services.result_manifest import collect_manifest_output_files
from app.services.task_dispatcher import task_dispatcher

logger = setup_default_logging()

router = APIRouter(prefix="/task", tags=["Task"])

_TASK_ID_RE = task_results.TASK_ID_RE
_RESULT_PATH_PART_RE = task_results.RESULT_PATH_PART_RE
_RECOVERY_MONITOR_INTERVAL_SECONDS = task_recovery.RECOVERY_MONITOR_INTERVAL_SECONDS
_MAX_RECOVERY_LOG_CHARS = task_recovery.MAX_RECOVERY_LOG_CHARS
_RECOVERY_AUTO_RESTART_MAX_ATTEMPTS = task_recovery.RECOVERY_AUTO_RESTART_MAX_ATTEMPTS
_RECOVERY_MONITORS = task_recovery.RECOVERY_MONITORS
_STOP_REQUESTED_METADATA_VALUE = "true"
_CANCELLED_BY_USER_ERROR = "Cancelled by user."
_COMPLETION_NOTIFY_READ_TIMEOUT_SECONDS = 30.0


def _task_recovery_runtime() -> ModuleType:
    """Expose route-level compatibility hooks to the recovery service."""
    return sys.modules[__name__]


# ======================================================
# Utility Functions
# ======================================================


def _validate_task_id(task_id: str) -> str:
    """Validate a task identifier before using it in filesystem paths."""
    return task_results.validate_task_id(task_id)


def _validate_result_relative_path(file_path: str) -> tuple[str, ...]:
    """Validate a relative file path under a task output directory."""
    return task_results.validate_result_relative_path(file_path)


def _resolve_storage_base_path() -> Path:
    """Resolve storage base path."""
    return task_results.resolve_storage_base_path(storage_manager.base_path)


def _find_direct_child_entry(directory: Path, entry_name: str) -> Path | None:
    """Find a direct child entry by name without composing a user-controlled path."""
    return task_results.find_direct_child_entry(directory, entry_name)


def _resolve_within_base(candidate: Path, base_path: Path) -> Path:
    """Resolve a candidate path and enforce that it stays under base_path."""
    return task_results.resolve_within_base(candidate, base_path)


def _resolve_output_file_path(output_dir: Path, relative_parts: tuple[str, ...]) -> Path:
    """Resolve a relative output path by traversing directory entries safely."""
    return task_results.resolve_output_file_path(output_dir, relative_parts)


def _derive_failure_status(error_message: str) -> str:
    """Derive a failure status from the error message.

    Returns "timeout" when the message indicates a timeout; otherwise "failed".
    """
    if (error_message or "").startswith(INCOMPLETE_OUTPUT_PREFIX):
        return "failed"
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
    return task_results.resolve_task_manifest_path(task_id, storage_manager.base_path)


def _resolve_task_root(task_id: str) -> Path:
    """Resolve task root directory and reject path traversal."""
    return task_results.resolve_task_root(task_id, storage_manager.base_path)


def _resolve_task_root_if_exists(task_id: str) -> Path | None:
    """Resolve task root for delete flows (None when missing/invalid)."""
    return task_results.resolve_task_root_if_exists(task_id, storage_manager.base_path)


def _resolve_legacy_manifest_if_exists(task_id: str) -> Path | None:
    """Resolve legacy flat manifest path (<base>/<task_id>.json) when present."""
    return task_results.resolve_legacy_manifest_if_exists(task_id, storage_manager.base_path)


def _resolve_task_result_file_path(task_id: str, file_path: str) -> Path:
    """Resolve one task result file through the dedicated result service."""
    return task_results.resolve_task_result_file_path(
        task_id,
        file_path,
        storage_manager.base_path,
    )


def _delete_task_results(task_id: str) -> None:
    """Delete task result files through the dedicated result service."""
    task_results.delete_task_results(task_id, storage_manager.base_path)


def _read_text_tail(file_path: Path, max_chars: int = _MAX_RECOVERY_LOG_CHARS) -> str:
    """Read a text file and keep only the tail to cap response size."""
    return task_recovery.read_text_tail(
        file_path,
        max_chars=max_chars,
        runtime=_task_recovery_runtime(),
    )


def _parse_process_pid(payload: dict) -> int | None:
    """Extract persisted process PID from task payload."""
    return task_recovery.parse_process_pid(payload, runtime=_task_recovery_runtime())


def _parse_process_pgid(payload: dict) -> int | None:
    """Extract persisted process group ID from task payload."""
    return task_recovery.parse_process_pgid(payload, runtime=_task_recovery_runtime())


def _parse_positive_int_field(payload: dict, field_name: str) -> int | None:
    """Extract a positive integer field from a task payload."""
    return task_recovery.parse_positive_int_field(
        payload,
        field_name,
        runtime=_task_recovery_runtime(),
    )


def _is_process_alive(pid: int) -> bool:
    """Return True when the process currently exists."""
    return task_recovery.is_process_alive(pid, os_module=os)


def _read_proc_cmdline(pid: int, *, proc_root: Path = Path("/proc")) -> str:
    """Read a process command line from procfs."""
    return task_recovery.read_proc_cmdline(
        pid,
        proc_root=proc_root,
        runtime=_task_recovery_runtime(),
    )


def _iter_proc_pids(*, proc_root: Path = Path("/proc")) -> list[int]:
    """Return numeric process IDs visible in procfs."""
    return task_recovery.iter_proc_pids(
        proc_root=proc_root,
        runtime=_task_recovery_runtime(),
    )


def _find_task_process_pids(task_id: str, *, proc_root: Path = Path("/proc")) -> list[int]:
    """Find live processes whose command line references this task workspace."""
    return task_recovery.find_task_process_pids(
        task_id,
        proc_root=proc_root,
        runtime=_task_recovery_runtime(),
    )


def _terminate_process_group(pgid: int) -> bool:
    """Terminate one process group unless it is the current runner group."""
    return task_recovery.terminate_process_group(
        pgid,
        kill_signal=signal.SIGKILL,
        os_module=os,
        runtime=_task_recovery_runtime(),
    )


def _terminate_process_pid(pid: int) -> bool:
    """Terminate one process by PID."""
    return task_recovery.terminate_process_pid(
        pid,
        kill_signal=signal.SIGKILL,
        os_module=os,
        runtime=_task_recovery_runtime(),
    )


def _terminate_stale_task_processes(task_id: str, payload: dict) -> bool:
    """Kill orphaned external processes still using a task workspace."""
    return task_recovery.terminate_stale_task_processes(
        task_id,
        payload,
        os_module=os,
        runtime=_task_recovery_runtime(),
    )


def _is_stop_requested_payload(payload: dict | None) -> bool:
    """Return True when a task payload carries a user stop marker."""
    if not isinstance(payload, dict):
        return False

    marker = str(payload.get("stop_requested") or "").strip().lower()
    if marker in {"1", "true", "yes"}:
        return True

    error_message = str(payload.get("error_message") or "").strip()
    return error_message == _CANCELLED_BY_USER_ERROR


def _is_task_stop_requested(task_id: str) -> bool:
    """Return True when the tracked task has been stopped by a user."""
    return _is_stop_requested_payload(get_task_status(task_id))


def _terminate_running_task_processes(task_id: str, payload: dict) -> tuple[bool, bool]:
    """Try to terminate external processes for one running task."""
    return task_recovery.terminate_running_task_processes(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


def _collect_recovery_script_output(task_id: str, payload: dict) -> Optional[str]:
    """Collect script stdout/stderr logs for recovery diagnostics."""
    return task_recovery.collect_recovery_script_output(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


def _resolve_task_output_dir_if_exists(task_id: str) -> Path | None:
    """Resolve task output directory for recovery workflows."""
    return task_recovery.resolve_task_output_dir_if_exists(
        task_id,
        runtime=_task_recovery_runtime(),
    )


def _has_useful_output_files(output_dir: Path) -> bool:
    """Return True when output dir includes at least one deliverable file."""
    return task_recovery.has_useful_output_files(
        output_dir,
        collect_output_files=collect_manifest_output_files,
    )


def _read_recovery_task_results(task_id: str) -> Optional[dict]:
    """Read `results` from task metadata when present."""
    return task_recovery.read_recovery_task_results(
        task_id,
        runtime=_task_recovery_runtime(),
    )


def _ensure_recovery_manifest(task_id: str) -> bool:
    """Ensure canonical manifest exists using current task output directory."""
    return task_recovery.ensure_recovery_manifest(
        task_id,
        collect_output_files=collect_manifest_output_files,
        os_module=os,
        runtime=_task_recovery_runtime(),
    )


def _infer_workspace_terminal_status(
    task_id: str, payload: dict
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Infer terminal status from workspace artifacts after a restart."""
    return task_recovery.infer_workspace_terminal_status(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


def _refresh_availability_from_recovered_state() -> None:
    """Set runner availability from currently tracked running tasks."""
    task_recovery.refresh_availability_from_recovered_state(runtime=_task_recovery_runtime())


def initialize_startup_availability() -> None:
    """Set startup availability before manager registration.

    When persisted recoverable tasks exist for this runner instance, mark the
    runner unavailable immediately so the manager does not dispatch new tasks
    before reconciliation/restart flows complete.
    """
    task_recovery.initialize_startup_availability(runtime=_task_recovery_runtime())


def _get_owned_task_statuses(statuses: set[str]) -> dict[str, dict]:
    """Return task statuses owned by this runner for requested status values."""
    return task_recovery.get_owned_task_statuses(
        statuses,
        current_runner_id=str(get_runner_id() or "").strip(),
        instance_scoped_state=bool((os.getenv("RUNNER_INSTANCE_ID") or "").strip()),
        runner_state=get_runner_state(),
    )


def _get_recovery_restart_attempts(payload: dict) -> int:
    """Return normalized startup auto-restart attempts for one task payload."""
    return task_recovery.get_recovery_restart_attempts(
        payload,
        runtime=_task_recovery_runtime(),
    )


def _load_recovery_task_request(task_id: str, payload: dict) -> Optional[TaskRequest]:
    """Load persisted TaskRequest payload used for startup task auto-restart."""
    return task_recovery.load_recovery_task_request(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


def _schedule_failed_task_restart(task_id: str, payload: dict) -> bool:
    """Schedule automatic restart of one failed task after startup recovery."""
    return task_recovery.schedule_failed_task_restart(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


async def _finalize_recovered_task(
    task_id: str,
    payload: dict,
    *,
    status: str,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
) -> None:
    """Persist terminal status and re-notify manager callback when possible."""
    await task_recovery.finalize_recovered_task(
        task_id,
        payload,
        status=status,
        error_message=error_message,
        script_output=script_output,
        runtime=_task_recovery_runtime(),
    )


async def _reconcile_recovered_task(task_id: str, payload: dict) -> str:
    """Reconcile one previously running task after a runner restart."""
    return await task_recovery.reconcile_recovered_task(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


async def _monitor_recovered_task(task_id: str) -> None:
    """Background monitor for a recovered in-flight task."""
    await task_recovery.monitor_recovered_task(
        task_id,
        runtime=_task_recovery_runtime(),
    )


def _schedule_recovery_monitor(task_id: str) -> None:
    """Schedule background monitoring for one recovered running task."""
    task_recovery.schedule_recovery_monitor(
        task_id,
        runtime=_task_recovery_runtime(),
    )


async def _recover_owned_running_tasks(running_tasks: dict[str, dict]) -> None:
    """Recover running tasks tracked for the current runner instance."""
    await task_recovery.recover_owned_running_tasks(
        running_tasks,
        runtime=_task_recovery_runtime(),
    )


async def _recover_failed_task(task_id: str, payload: dict) -> bool:
    """Recover one failed task. Returns True when a restart is scheduled."""
    return await task_recovery.recover_failed_task(
        task_id,
        payload,
        runtime=_task_recovery_runtime(),
    )


async def _recover_owned_failed_tasks(failed_tasks: dict[str, dict]) -> int:
    """Recover failed/timeout tasks and return the number of restarted tasks."""
    return await task_recovery.recover_owned_failed_tasks(
        failed_tasks,
        runtime=_task_recovery_runtime(),
    )


async def recover_running_tasks_after_restart() -> None:
    """Restore running task state after process restart using persisted JSON."""
    from app.services import live_runtime

    await live_runtime.recover()
    await task_recovery.recover_running_tasks_after_restart(runtime=_task_recovery_runtime())


async def stop_recovery_monitors() -> None:
    """Cancel all running recovery monitor tasks."""
    await task_recovery.stop_recovery_monitors(runtime=_task_recovery_runtime())


async def process_task(task_id: str, task_request: TaskRequest):
    """
    Process a task while tracking coroutine ownership for periodic verification.
    """
    completion_callback = task_request.completion_callback
    execution_id = task_monitor.register_execution(task_id, task_request.run_id)
    try:
        set_task_metadata(
            task_id,
            runner_id=get_runner_id(),
            completion_callback=completion_callback,
            task_request=task_request.model_dump(mode="json"),
            error_message=None,
            stop_requested=None,
            execution_id=execution_id,
            process_pid=None,
            process_pgid=None,
            process_start_ticks=None,
        )

        set_available(False)
        set_task_status(task_id, "running")

        logger.info(
            f"Starting task {task_id} of type {task_request.task_type} with parameters: {task_request.parameters}"
        )

        results = await task_dispatcher.dispatch_task(task_id=task_id, task_request=task_request)

        if results.get("success") and _is_task_stop_requested(task_id):
            results = dict(results)
            results["success"] = False
            results["error"] = _CANCELLED_BY_USER_ERROR

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
            if _is_task_stop_requested(task_id):
                error_msg = _CANCELLED_BY_USER_ERROR
            failure_status = _derive_failure_status(error_msg)
            script_output_text = _normalize_script_output(results.get("script_output"))
            script_output_text = prepend_encoding_warning(error_msg, script_output_text)
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
                video_id=task_request.parameters.get("video_id"),
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
        if _is_task_stop_requested(task_id):
            error_msg = _CANCELLED_BY_USER_ERROR
        failure_status = _derive_failure_status(error_msg)
        set_task_status(task_id, failure_status, error_message=error_msg)
        logger.error(f"Error processing task {task_id}: {error_msg}")
        logger.info(f"Sending failure email for task {task_id}")
        await send_task_failure_email(
            task_id=task_id,
            task_type=task_request.task_type,
            status=failure_status,
            error_message=error_msg,
            video_id=task_request.parameters.get("video_id"),
        )
        if completion_callback:
            await notify_completion(completion_callback, task_id, failure_status, error_msg)
    finally:
        task_monitor.unregister_execution(task_id, execution_id)
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
        manifest_path = _resolve_task_manifest_path(safe_task_id)
        manifest = task_monitor.read_result_document(manifest_path)
        payload = {"task_id": safe_task_id, "status": "completed"}
        if manifest.get("execution_id"):
            if manifest.get("task_id") != safe_task_id:
                raise ValueError("Task manifest identity does not match")
            output_dir = _resolve_task_output_dir_if_exists(safe_task_id)
            if output_dir is None:
                raise ValueError("Task output directory is missing")
            task_monitor.validate_output_files(output_dir, manifest.get("files"))
            payload["execution_id"] = manifest["execution_id"]
        return payload
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    except (OSError, ValueError):
        pass

    return {"task_id": safe_task_id, "status": "not_found"}


def _describe_completion_callback(callback_url: Optional[str]) -> str:
    """Describe an endpoint without exposing URL credentials, queries or fragments."""
    if not callback_url:
        return "not provided"
    try:
        url = httpx.URL(callback_url)
    except httpx.InvalidURL:
        return "invalid URL"
    return f"scheme={url.scheme!r}, host={url.host!r}, port={url.port!r}, path={url.path!r}"


def _validate_completion_callback(
    callback_url: str, public_callback_url: Optional[str] = None
) -> str:
    """Return the configured endpoint if a Manager address matches, else raise HTTP 422.

    Ignore trailing slashes on Manager bases and completion paths. Advertised
    addresses only establish a match: the returned destination always comes
    from this Runner's MANAGER_URL. Rejections include a redacted configuration
    diagnostic suitable for task errors and logs.
    """
    manager_base_url = config.MANAGER_URL.rstrip("/")
    completion_path = "/task/completion"
    manager_callback_url = f"{manager_base_url}{completion_path}"
    for advertised_url in (callback_url, public_callback_url):
        if not advertised_url:
            continue
        candidate = advertised_url.rstrip("/")
        if (
            candidate.endswith(completion_path)
            and candidate.removesuffix(completion_path).rstrip("/") == manager_base_url
        ):
            return manager_callback_url

    raise HTTPException(
        status_code=422,
        detail=(
            "Completion callback rejected: neither Manager address matches this Runner's "
            f"MANAGER_URL. Runner expects ({_describe_completion_callback(manager_callback_url)}); "
            f"Manager private callback ({_describe_completion_callback(callback_url)}); "
            f"Manager public callback ({_describe_completion_callback(public_callback_url)}). "
            "Align runner/.env MANAGER_URL with manager/.env MANAGER_PROTOCOL, MANAGER_HOST "
            "and MANAGER_PORT, or with MANAGER_PUBLIC_URL. Trailing slashes are ignored; "
            "credentials, query strings and fragments are not accepted."
        ),
    )


def _prepare_task_completion_callback(task_request: TaskRequest) -> None:
    """Canonicalize an advertised callback before admission, logging HTTP 422 rejections."""
    if not (task_request.completion_callback or task_request.completion_callback_public):
        return
    try:
        task_request.completion_callback = _validate_completion_callback(
            task_request.completion_callback or "", task_request.completion_callback_public
        )
    except HTTPException as exc:
        logger.error(
            "Rejected completion callback for task %s: %s", task_request.task_id, exc.detail
        )
        raise


def _completion_notification_payload(
    task_id: str,
    status: str,
    error_message: Optional[str],
    script_output: Optional[str],
) -> dict:
    """Snapshot terminal fields and the Manager run_id before delivery retries."""
    tracked_payload = get_task_status(task_id) or {}
    persisted_request = tracked_payload.get("task_request")
    run_id = persisted_request.get("run_id") if isinstance(persisted_request, dict) else None
    notification = {
        "task_id": task_id,
        "status": status,
        "error_message": error_message,
        "script_output": script_output,
    }
    if run_id:
        notification["run_id"] = run_id
    return notification


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
        callback_url: Must match the configured Manager completion endpoint, ignoring trailing slashes
        task_id: Task identifier being reported
        status: Terminal completion status ('completed', 'failed' or 'timeout')
        error_message: Optional error details for failed tasks
        script_output: Optional script output for debugging

    Only the endpoint derived from MANAGER_URL receives the runner token. Invalid
    callbacks return False without a request, including during persisted task recovery.
    Trailing slashes on the Manager base or completion path are ignored; the
    request always uses the configured canonical endpoint. Redirects are disabled.
    All request phases use finite timeouts and any successful 2xx response is accepted.
    The Manager run_id is captured before delivery and preserved across retries.
    """
    try:
        manager_callback_url = _validate_completion_callback(callback_url)
    except HTTPException as exc:
        logger.error(
            "Rejected completion callback for task %s: %s",
            task_id,
            exc.detail,
        )
        return False

    max_retries = max(0, int(config.COMPLETION_NOTIFY_MAX_RETRIES))
    base_delay = max(0, int(config.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS))
    backoff_factor = max(1.0, float(config.COMPLETION_NOTIFY_BACKOFF_FACTOR))
    timeout = httpx.Timeout(
        connect=5.0,
        read=_COMPLETION_NOTIFY_READ_TIMEOUT_SECONDS,
        write=5.0,
        pool=5.0,
    )

    attempt = 0
    notification = _completion_notification_payload(task_id, status, error_message, script_output)
    while True:
        attempt += 1
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                response = await client.post(
                    manager_callback_url,
                    json=notification,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    },
                )

                if 200 <= response.status_code < 300:
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
                manager_callback_url,
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

    file_path = _resolve_task_result_file_path(task_id, filename)

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
    _delete_task_results(task_id)

    # Recompute availability from tracked running tasks.
    _refresh_availability_from_recovered_state()
    return {"status": "deleted"}


@router.post(
    "/stop/{task_id}",
    response_model=dict,
    summary="Stop a running task",
    description="Request termination of the external process for a running task",
    tags=["Task"],
)
async def stop_task(task_id: str, current_manager: str = Depends(get_current_manager)):
    """Stop a task using its persisted type, or fence a not-yet-dispatched Live UUID."""
    safe_task_id = (task_id or "").strip()
    payload = get_task_status(safe_task_id)
    if "live" in config.RUNNER_TASK_TYPES and (payload is None or payload.get("live")):
        from app.services.live_runtime import request_stop

        return request_stop(safe_task_id)
    try:
        if not isinstance(payload, dict):
            raise HTTPException(status_code=404, detail="Task not found")

        current_status = str(payload.get("status") or "").strip().lower()
        if current_status != "running":
            return {
                "task_id": safe_task_id,
                "status": "already_terminal",
                "current_status": current_status or "unknown",
            }

        termination_attempted, terminated_any_process = _terminate_running_task_processes(
            safe_task_id, payload
        )
        if not terminated_any_process:
            raise HTTPException(
                status_code=409,
                detail="Task is running but no killable external process found yet",
            )

        set_task_metadata(
            safe_task_id,
            stop_requested=_STOP_REQUESTED_METADATA_VALUE,
            error_message=_CANCELLED_BY_USER_ERROR,
        )
        logger.warning("Stop requested for running task %s", safe_task_id)
        return JSONResponse(
            status_code=202,
            content={
                "task_id": safe_task_id,
                "status": "stop_requested",
                "termination_attempted": termination_attempted,
                "terminated_any_process": terminated_any_process,
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Internal stop error for task %s: %s", safe_task_id, exc)
        raise HTTPException(status_code=500, detail="Internal stop error")


@router.post(
    "/run",
    response_model=dict,
    summary="Execute task",
    description="Start task execution on the runner",
    tags=["Task"],
    responses={422: {"description": "Invalid task request or Manager completion address"}},
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

    Raises:
        HTTPException: If neither advertised Manager endpoint matches MANAGER_URL,
            or the Runner cannot accept the task. Callback validation happens
            before workspace creation, task state changes or background execution.
    """
    logger.info(f"Received task execution request, registered: {is_registered()}")
    _prepare_task_completion_callback(task_request)

    if task_request.task_type == "live":
        from app.services import live_runtime

        live_runtime.validate_request(task_request)
        existing = get_task_status(task_request.task_id)
        if existing is not None:
            if not existing.get("live"):
                raise HTTPException(409, "Task identifier already belongs to another task type")
            return {"task_id": task_request.task_id, "status": existing["status"]}
    elif config.RUNNER_TASK_TYPES == {"live"}:
        raise HTTPException(400, "Dedicated live runner")

    if not is_registered():
        raise HTTPException(status_code=503, detail="Runner not registered with manager")

    if not is_available():
        raise HTTPException(status_code=400, detail="Runner is busy")

    try:
        task_results.resolve_task_workspace(task_request.task_id, storage_manager.base_path)
    except (HTTPException, OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid task workspace")

    set_available(False)
    set_task_status(task_request.task_id, "running")
    set_task_metadata(
        task_request.task_id,
        runner_id=get_runner_id(),
        completion_callback=task_request.completion_callback,
        task_request=task_request.model_dump(mode="json"),
        error_message=None,
        stop_requested=None,
    )

    if task_request.task_type == "live":
        from app.services import live_runtime

        live_runtime.accept(task_request)
        return {"status": "started", "task_id": task_request.task_id}

    # Register the accepting request too: response transmission may yield before
    # Starlette starts process_task, and the watchdog must keep this task active.
    execution_id = task_monitor.register_execution(task_request.task_id, task_request.run_id)
    set_task_metadata(
        task_request.task_id,
        execution_id=execution_id,
        process_pid=None,
        process_pgid=None,
        process_start_ticks=None,
    )
    background_tasks.add_task(
        process_task, task_id=task_request.task_id, task_request=task_request
    )  # pragma: no cover (background scheduling side-effect)

    return {"status": "started", "task_id": task_request.task_id}  # pragma: no cover
