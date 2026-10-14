"""Periodically reconcile abandoned VOD executions with their processes and outputs."""

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import HTTPException

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services import task_results
from app.services.result_manifest import collect_manifest_output_files

logger = setup_default_logging()
RECHECK_DELAY_SECONDS = 10
_EXECUTIONS: dict[str, tuple[asyncio.Task, str]] = {}
_WORKERS: set[str] = set()
_WORKER_LOCK = threading.Lock()
_INTERNAL_OUTPUT_NAMES = {"task_metadata.json", "info_video.json", "encoding.log"}


def register_execution(task_id: str, run_id: str | None = None) -> str:
    """Track the current coroutine and return its unique execution identifier."""
    current = asyncio.current_task()
    existing = _EXECUTIONS.get(task_id)
    if (
        existing is not None
        and existing[0] is current
        and (run_id is None or existing[1] == run_id)
    ):
        return existing[1]
    execution_id = run_id or uuid.uuid4().hex
    if current is not None:
        _EXECUTIONS[task_id] = (current, execution_id)
    return execution_id


def unregister_execution(task_id: str, execution_id: str) -> None:
    """Release a coroutine registration without removing a newer execution."""
    existing = _EXECUTIONS.get(task_id)
    if existing is not None and existing[1] == execution_id:
        _EXECUTIONS.pop(task_id, None)


def execute_handler(handler: Any, task_id: str, task_request: Any) -> dict:
    """Track the worker throughout downloads, scripts and result preparation."""
    with _WORKER_LOCK:
        _WORKERS.add(task_id)
    try:
        return dict(handler.execute_task(task_id, task_request))
    finally:
        with _WORKER_LOCK:
            _WORKERS.discard(task_id)


def has_active_execution(task_id: str) -> bool:
    """Keep tasks active while their coroutine or handler thread still owns them."""
    existing = _EXECUTIONS.get(task_id)
    if existing is not None:
        if not existing[0].done():
            return True
        _EXECUTIONS.pop(task_id, None)
    with _WORKER_LOCK:
        return task_id in _WORKERS


def read_process_identity(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, int]:
    """Read Linux process state and birth ticks, raising on unavailable procfs."""
    fields = (proc_root / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return fields[0], int(fields[19])


def has_task_processes(task_id: str, payload: dict, runtime: ModuleType) -> bool:
    """Detect owned processes; unreadable process state is conservatively active."""
    pid = runtime._parse_process_pid(payload)
    if pid is not None and runtime._is_process_alive(pid):
        try:
            process_state, start_ticks = read_process_identity(pid)
        except (OSError, ValueError, IndexError):
            return True
        expected_ticks = payload.get("process_start_ticks")
        if process_state not in {"Z", "X"} and (
            expected_ticks is None or expected_ticks == start_ticks
        ):
            return True

    return has_workspace_processes(task_id, runtime)


def has_workspace_processes(task_id: str, runtime: ModuleType) -> bool:
    """Keep surviving children active by matching their exact workspace arguments."""
    # A child may survive its parent. Match whole workspace arguments, so task-1
    # never matches task-10, and never signal a process from this passive check.
    task_root = runtime._resolve_task_root_if_exists(task_id)
    if task_root is None:
        return False
    marker = str(task_root)
    for candidate_pid in runtime._iter_proc_pids():
        matches = False
        try:
            argv = (Path("/proc") / str(candidate_pid) / "cmdline").read_bytes().split(b"\x00")
            matches = any(
                arg.decode(errors="replace") == marker
                or arg.decode(errors="replace").startswith(marker + os.sep)
                for arg in argv
            )
            if matches and read_process_identity(candidate_pid)[0] not in {"Z", "X"}:
                return True
        except FileNotFoundError:
            continue
        except (OSError, ValueError, IndexError):
            # Only a matching process with unreadable state prevents finalization.
            if matches:
                return True
    return False


def write_result_document(path: Path, document: dict) -> None:
    """Atomically publish JSON using exclusive creation and the deployment's umask."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        try:
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def read_result_document(path: Path) -> dict:
    """Read an object-shaped result document without treating malformed JSON as success."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Task result document is not an object")
    return document


def matches_execution(document: dict, task_id: str, payload: dict) -> bool:
    """Require task and execution identifiers to match the current persisted run."""
    return document.get("task_id") == task_id and document.get("execution_id") == payload.get(
        "execution_id"
    )


def validate_output_files(output_dir: Path, files: object) -> list[str]:
    """Require a nonempty list of contained, nonempty deliverables, excluding metadata."""
    if not isinstance(files, list) or not files:
        raise ValueError("No recorded task output files")
    validated: list[str] = []
    for relative_path in files:
        if not isinstance(relative_path, str):
            raise ValueError("Invalid task output filename")
        parts = task_results.validate_result_relative_path(relative_path)
        path = task_results.resolve_output_file_path(output_dir, parts)
        if path.stat().st_size <= 0:
            raise ValueError("Empty task output file")
        validated.append(relative_path)
    deliverables = set(
        collect_manifest_output_files(output_dir, ignored_names=_INTERNAL_OUTPUT_NAMES)
    )
    if not any(filename in deliverables for filename in validated):
        raise ValueError("No deliverable task output files")
    return validated


def infer_terminal_status(
    task_id: str, payload: dict, runtime: ModuleType
) -> tuple[str, str | None]:
    """Confirm success from current-run evidence, otherwise retain an explicit failure."""
    if runtime._is_stop_requested_payload(payload):
        return "failed", runtime._CANCELLED_BY_USER_ERROR
    output_dir = runtime._resolve_task_output_dir_if_exists(task_id)
    if output_dir is None:
        error = str(
            payload.get("error_message") or "Task execution disappeared without completed outputs"
        )
        return runtime._derive_failure_status(error), error
    metadata_path = task_results.resolve_within_base(output_dir / "task_metadata.json", output_dir)
    metadata = read_result_document(metadata_path) if metadata_path.is_file() else {}
    results = metadata.get("results")
    current_metadata = matches_execution(metadata, task_id, payload)
    if current_metadata and isinstance(results, dict) and results.get("success") is not True:
        error = str(results.get("error") or "Task execution failed")
        return runtime._derive_failure_status(error), error

    manifest_path = output_dir.parent / "manifest.json"
    manifest: dict = {}
    if manifest_path.is_file():
        safe_manifest = task_results.resolve_within_base(manifest_path, output_dir.parent)
        manifest = read_result_document(safe_manifest)
    current_manifest = matches_execution(manifest, task_id, payload)
    if current_metadata and isinstance(results, dict) and results.get("success") is True:
        files = metadata.get("output_files")
        if files is None:
            files = (
                manifest.get("files")
                if current_manifest
                else collect_manifest_output_files(output_dir, ignored_names=_INTERNAL_OUTPUT_NAMES)
            )
        files = validate_output_files(output_dir, files)
        if not current_manifest:
            manifest = {"task_id": task_id, "files": files}
            if payload.get("execution_id"):
                manifest["execution_id"] = payload["execution_id"]
            write_result_document(manifest_path, manifest)
        else:
            validate_output_files(output_dir, manifest.get("files"))
        return "completed", None
    if current_manifest:
        validate_output_files(output_dir, manifest.get("files"))
        return "completed", None
    error = str(
        payload.get("error_message")
        or "Task execution disappeared without confirmed successful outputs"
    )
    return runtime._derive_failure_status(error), error


async def check_task(task_id: str, payload: dict, runtime: ModuleType) -> None:
    """Recheck an abandoned task before persisting a terminal status and callback."""
    if "live" in payload or has_active_execution(task_id):
        return
    if has_task_processes(task_id, payload, runtime):
        return
    await asyncio.sleep(RECHECK_DELAY_SECONDS)
    current = runtime.get_task_status(task_id)
    if current != payload or has_active_execution(task_id):
        return
    if has_task_processes(task_id, current, runtime):
        return
    try:
        status, error = infer_terminal_status(task_id, current, runtime)
    except (OSError, ValueError, HTTPException) as exc:
        status, error = "failed", "Task execution disappeared with invalid or incomplete outputs"
        logger.warning(
            "Cannot confirm completed outputs for task %s: %s", task_id, type(exc).__name__
        )
    script_output = runtime._collect_recovery_script_output(task_id, current)
    # Persist and release availability before awaiting a possibly unavailable Manager.
    runtime.set_task_status(task_id, status, error_message=error, script_output=script_output)
    runtime._refresh_availability_from_recovered_state()
    callback = current.get("completion_callback")
    if callback:
        await runtime.notify_completion(callback, task_id, status, error, script_output)
    logger.info("Reconciled abandoned task %s: status=%s", task_id, status)


async def check_running_tasks_once() -> None:
    """Inspect the current instance's running VOD tasks, isolating individual failures."""
    from app.api.routes import task as runtime

    for task_id, payload in runtime._get_owned_task_statuses({"running"}).items():
        try:
            await check_task(task_id, payload, runtime)
        except Exception:
            logger.exception("Running-task verification failed for task %s", task_id)


async def task_process_check_loop() -> None:
    """Run instance-local checks periodically; zero disables the hourly default."""
    interval = config.TASK_PROCESS_CHECK_INTERVAL_SECONDS
    if interval <= 0:
        return
    while True:
        await asyncio.sleep(interval)
        try:
            await check_running_tasks_once()
        except Exception:
            logger.exception("Running-task verification pass failed")
