"""Runner task recovery workflows.

The route module passes itself as ``runtime`` so its historical monkeypatch
points remain effective while recovery implementation lives in this service.
"""

import json
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional, cast

from fastapi import HTTPException

from app.models.models import TaskRequest

RECOVERY_MONITOR_INTERVAL_SECONDS = 10
MAX_RECOVERY_LOG_CHARS = 100000
RECOVERY_AUTO_RESTART_MAX_ATTEMPTS = 1
RECOVERY_MONITORS: dict[str, object] = {}


def read_text_tail(
    file_path: Path,
    max_chars: int = MAX_RECOVERY_LOG_CHARS,
    *,
    runtime: ModuleType,
) -> str:
    """Read a text file and keep only the tail to cap response size."""
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""

    if len(content) <= max_chars:
        return content
    return content[-max_chars:]


def parse_process_pid(payload: dict, *, runtime: ModuleType) -> int | None:
    """Extract persisted process PID from task payload."""
    value = runtime._parse_positive_int_field(payload, "process_pid")
    return int(value) if value is not None else None


def parse_process_pgid(payload: dict, *, runtime: ModuleType) -> int | None:
    """Extract persisted process group ID from task payload."""
    value = runtime._parse_positive_int_field(payload, "process_pgid")
    return int(value) if value is not None else None


def parse_positive_int_field(
    payload: dict,
    field_name: str,
    *,
    runtime: ModuleType,
) -> int | None:
    """Extract a positive integer field from a task payload."""
    raw_value = payload.get(field_name)
    if raw_value is None:
        return None
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def is_process_alive(pid: int, *, os_module: ModuleType) -> bool:
    """Return True when the process currently exists."""
    try:
        os_module.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_proc_cmdline(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    runtime: ModuleType,
) -> str:
    """Read a process command line from procfs."""
    try:
        raw_cmdline = (proc_root / str(pid) / "cmdline").read_bytes()
    except Exception:
        return ""
    return raw_cmdline.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


def iter_proc_pids(
    *,
    proc_root: Path = Path("/proc"),
    runtime: ModuleType,
) -> list[int]:
    """Return numeric process IDs visible in procfs."""
    try:
        entries = list(proc_root.iterdir())
    except Exception:
        return []

    pids: list[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pids.append(int(entry.name))
        except ValueError:
            continue
    return pids


def find_task_process_pids(
    task_id: str,
    *,
    proc_root: Path = Path("/proc"),
    runtime: ModuleType,
) -> list[int]:
    """Find live processes whose command line references this task workspace."""
    task_root = runtime._resolve_task_root_if_exists(task_id)
    if task_root is None:
        return []

    marker = str(task_root)
    pids: list[int] = []
    for pid in runtime._iter_proc_pids(proc_root=proc_root):
        cmdline = runtime._read_proc_cmdline(pid, proc_root=proc_root)
        if marker in cmdline:
            pids.append(pid)
    return pids


def terminate_process_group(
    pgid: int,
    *,
    kill_signal: int,
    os_module: ModuleType,
    runtime: ModuleType,
) -> bool:
    """Terminate one process group unless it is the current runner group."""
    if pgid <= 0:
        return False
    try:
        if pgid == os_module.getpgrp():
            return False
    except Exception:
        pass

    try:
        os_module.killpg(pgid, kill_signal)
    except ProcessLookupError:
        return False
    except Exception as exc:
        runtime.logger.warning("Failed to terminate process group %s: %s", pgid, exc)
        return False
    return True


def terminate_process_pid(
    pid: int,
    *,
    kill_signal: int,
    os_module: ModuleType,
    runtime: ModuleType,
) -> bool:
    """Terminate one process by PID."""
    if pid <= 0 or pid == os_module.getpid():
        return False
    try:
        os_module.kill(pid, kill_signal)
    except ProcessLookupError:
        return False
    except Exception as exc:
        runtime.logger.warning("Failed to terminate process %s: %s", pid, exc)
        return False
    return True


def terminate_stale_task_processes(
    task_id: str,
    payload: dict,
    *,
    os_module: ModuleType,
    runtime: ModuleType,
) -> bool:
    """Kill orphaned external processes still using a task workspace."""
    matched_pids = runtime._find_task_process_pids(task_id)
    process_groups: set[int] = set()

    payload_pgid = runtime._parse_process_pgid(payload)
    if payload_pgid is not None and matched_pids:
        process_groups.add(payload_pgid)

    for pid in matched_pids:
        try:
            process_groups.add(os_module.getpgid(pid))
        except Exception:
            continue

    terminated = False
    for pgid in sorted(process_groups):
        terminated = runtime._terminate_process_group(pgid) or terminated

    for pid in matched_pids:
        if runtime._is_process_alive(pid):
            terminated = runtime._terminate_process_pid(pid) or terminated

    if terminated:
        runtime.logger.warning("Terminated stale process(es) for recovered task %s", task_id)
    return terminated


def terminate_running_task_processes(
    task_id: str,
    payload: dict,
    *,
    runtime: ModuleType,
) -> tuple[bool, bool]:
    """Try to terminate external processes for one running task."""
    termination_attempted = False
    terminated_any_process = False

    pgid = runtime._parse_process_pgid(payload)
    if pgid is not None:
        termination_attempted = True
        terminated_any_process = runtime._terminate_process_group(pgid) or terminated_any_process

    pid = runtime._parse_process_pid(payload)
    if pid is not None:
        termination_attempted = True
        if runtime._is_process_alive(pid):
            terminated_any_process = runtime._terminate_process_pid(pid) or terminated_any_process

    matched_pids = runtime._find_task_process_pids(task_id)
    if matched_pids:
        termination_attempted = True
        terminated_any_process = (
            runtime._terminate_stale_task_processes(task_id, payload) or terminated_any_process
        )

    return termination_attempted, terminated_any_process


def collect_recovery_script_output(
    task_id: str,
    payload: dict,
    *,
    runtime: ModuleType,
) -> Optional[str]:
    """Collect script stdout/stderr logs for recovery diagnostics."""
    candidate_paths: list[Path] = []

    task_root = runtime._resolve_task_root_if_exists(task_id)
    if task_root is not None:
        candidate_paths.extend(
            [
                task_root / "info_script.log",
                task_root / "error_script.log",
            ]
        )

    for key in ("script_stdout_path", "script_stderr_path"):
        raw_path = payload.get(key)
        if not isinstance(raw_path, str):
            continue
        normalized_path = raw_path.strip()
        if not normalized_path:
            continue
        candidate = runtime.Path(normalized_path)
        if candidate not in candidate_paths:
            candidate_paths.append(candidate)

    chunks: list[str] = []
    for path in candidate_paths:
        text = runtime._read_text_tail(path)
        if not text.strip():
            continue
        chunks.append(f"[{path.name}]\n{text.strip()}")

    if not chunks:
        return None

    merged = "\n\n".join(chunks)
    if len(merged) <= runtime._MAX_RECOVERY_LOG_CHARS:
        return merged
    return merged[-runtime._MAX_RECOVERY_LOG_CHARS :]


def resolve_task_output_dir_if_exists(task_id: str, *, runtime: ModuleType) -> Path | None:
    """Resolve task output directory for recovery workflows."""
    task_root = runtime._resolve_task_root_if_exists(task_id)
    if task_root is None:
        return None

    output_candidate = runtime._find_direct_child_entry(task_root, "output")
    if output_candidate is None:
        return None

    try:
        output_dir = runtime._resolve_within_base(output_candidate, task_root)
    except HTTPException:
        return None

    if not output_dir.is_dir():
        return None
    return Path(output_dir)


def has_useful_output_files(
    output_dir: Path,
    *,
    collect_output_files: Callable[..., list[str]],
) -> bool:
    """Return True when output dir includes at least one deliverable file."""
    ignored_names = {
        "task_metadata.json",
        "info_video.json",
        "encoding.log",
    }
    output_files = collect_output_files(output_dir, ignored_names=ignored_names)
    return bool(output_files)


def read_recovery_task_results(task_id: str, *, runtime: ModuleType) -> Optional[dict]:
    """Read ``results`` from task metadata when present."""
    output_dir = runtime._resolve_task_output_dir_if_exists(task_id)
    if output_dir is None:
        return None

    metadata_candidate = runtime._find_direct_child_entry(output_dir, "task_metadata.json")
    if metadata_candidate is None:
        return None

    try:
        metadata_path = runtime._resolve_within_base(metadata_candidate, output_dir)
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


def ensure_recovery_manifest(
    task_id: str,
    *,
    collect_output_files: Callable[..., list[str]],
    os_module: ModuleType,
    runtime: ModuleType,
) -> bool:
    """Ensure canonical manifest exists using current task output directory."""
    try:
        runtime._resolve_task_manifest_path(task_id)
        return True
    except HTTPException as exc:
        if exc.status_code != 404:
            return False

    output_dir = runtime._resolve_task_output_dir_if_exists(task_id)
    if output_dir is None:
        return False

    if not runtime._has_useful_output_files(output_dir):
        return False

    output_files = collect_output_files(output_dir)
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
            os_module.fsync(manifest_file.fileno())
        temp_manifest_path.replace(manifest_path)
    except Exception:
        return False

    return True


def infer_workspace_terminal_status(
    task_id: str,
    payload: dict,
    *,
    runtime: ModuleType,
) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """Infer terminal status from workspace artifacts after a restart."""
    raw_results = runtime._read_recovery_task_results(task_id)
    script_output = (
        runtime._normalize_script_output(raw_results.get("script_output"))
        if isinstance(raw_results, dict)
        else None
    )
    if not script_output:
        script_output = runtime._collect_recovery_script_output(task_id, payload)

    if isinstance(raw_results, dict) and raw_results.get("success") is True:
        if not runtime._ensure_recovery_manifest(task_id):
            return None
        return ("completed", None, script_output)

    if isinstance(raw_results, dict):
        error_message = str(
            raw_results.get("error")
            or payload.get("error_message")
            or "Task failed before runner restart"
        )
        failure_status = runtime._derive_failure_status(error_message)
        return (failure_status, error_message, script_output)

    if not runtime._ensure_recovery_manifest(task_id):
        return None

    return ("completed", None, script_output)


def refresh_availability_from_recovered_state(*, runtime: ModuleType) -> None:
    """Set runner availability from currently tracked running tasks."""
    has_running_tasks = bool(runtime._get_owned_task_statuses({"running"}))
    runtime.set_available(not has_running_tasks)


def initialize_startup_availability(*, runtime: ModuleType) -> None:
    """Set startup availability before manager registration."""
    if runtime._get_owned_task_statuses({"running", "failed", "timeout"}):
        runtime.set_available(False)


def get_owned_task_statuses(
    statuses: set[str],
    *,
    current_runner_id: str,
    instance_scoped_state: bool,
    runner_state: dict,
) -> dict[str, dict]:
    """Return task statuses owned by this runner for requested status values."""
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


def get_recovery_restart_attempts(payload: dict, *, runtime: ModuleType) -> int:
    """Return normalized startup auto-restart attempts for one task payload."""
    raw_attempts = payload.get("recovery_restart_attempts", 0)
    try:
        attempts = int(raw_attempts)
    except (TypeError, ValueError):
        return 0
    return max(0, attempts)


def load_recovery_task_request(
    task_id: str,
    payload: dict,
    *,
    runtime: ModuleType,
) -> Optional[TaskRequest]:
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
        return cast(TaskRequest, runtime.TaskRequest.model_validate(task_request_payload))
    except Exception:
        return None


def schedule_failed_task_restart(task_id: str, payload: dict, *, runtime: ModuleType) -> bool:
    """Schedule automatic restart of one failed task after startup recovery."""
    restart_attempts = runtime._get_recovery_restart_attempts(payload)
    if restart_attempts >= runtime._RECOVERY_AUTO_RESTART_MAX_ATTEMPTS:
        runtime.logger.warning(
            "Skipping automatic restart for task %s: max attempts reached (%s)",
            task_id,
            runtime._RECOVERY_AUTO_RESTART_MAX_ATTEMPTS,
        )
        return False

    task_request = runtime._load_recovery_task_request(task_id, payload)
    if task_request is None:
        runtime.logger.warning(
            "Skipping automatic restart for task %s: missing or invalid persisted task_request",
            task_id,
        )
        return False

    runtime._terminate_stale_task_processes(task_id, payload)

    runtime.set_task_metadata(
        task_id,
        runner_id=runtime.get_runner_id(),
        completion_callback=task_request.completion_callback,
        task_request=task_request.model_dump(mode="json"),
        recovery_restart_attempts=restart_attempts + 1,
        error_message=None,
        stop_requested=None,
    )
    runtime.set_task_status(task_id, "running")
    runtime.asyncio.create_task(runtime.process_task(task_id, task_request))

    runtime.logger.info(
        "Scheduled automatic restart for task %s after startup recovery (attempt %s/%s)",
        task_id,
        restart_attempts + 1,
        runtime._RECOVERY_AUTO_RESTART_MAX_ATTEMPTS,
    )
    return True


async def finalize_recovered_task(
    task_id: str,
    payload: dict,
    *,
    status: str,
    error_message: Optional[str] = None,
    script_output: Optional[str] = None,
    runtime: ModuleType,
) -> None:
    """Persist terminal status and re-notify manager callback when possible."""
    runtime.set_task_status(
        task_id,
        status,
        error_message=error_message,
        script_output=script_output,
    )

    completion_callback = payload.get("completion_callback")
    if isinstance(completion_callback, str) and completion_callback.strip():
        await runtime.notify_completion(
            completion_callback,
            task_id,
            status,
            error_message,
            script_output,
        )


async def reconcile_recovered_task(
    task_id: str,
    payload: dict,
    *,
    runtime: ModuleType,
) -> str:
    """Reconcile one previously running task after a runner restart."""
    try:
        runtime._resolve_task_manifest_path(task_id)
        script_output = runtime._collect_recovery_script_output(task_id, payload)
        await runtime._finalize_recovered_task(
            task_id,
            payload,
            status="completed",
            script_output=script_output,
        )
        return "completed"
    except HTTPException as exc:
        if exc.status_code != 404:
            raise

    pid = runtime._parse_process_pid(payload)
    if pid is not None and runtime._is_process_alive(pid):
        runtime.set_task_status(task_id, "running")
        return "running"

    runtime._terminate_stale_task_processes(task_id, payload)

    workspace_terminal_status = runtime._infer_workspace_terminal_status(task_id, payload)
    if workspace_terminal_status is not None:
        status, error_message, script_output = workspace_terminal_status
        await runtime._finalize_recovered_task(
            task_id,
            payload,
            status=status,
            error_message=error_message,
            script_output=script_output,
        )
        return str(status)

    script_output = runtime._collect_recovery_script_output(task_id, payload)
    error_message = str(payload.get("error_message") or "Task process is no longer running")
    failure_status = runtime._derive_failure_status(error_message)
    await runtime._finalize_recovered_task(
        task_id,
        payload,
        status=failure_status,
        error_message=error_message,
        script_output=script_output,
    )
    return str(failure_status)


async def monitor_recovered_task(task_id: str, *, runtime: ModuleType) -> None:
    """Background monitor for a recovered in-flight task."""
    try:
        while True:
            await runtime.asyncio.sleep(runtime._RECOVERY_MONITOR_INTERVAL_SECONDS)

            payload = runtime.get_task_status(task_id)
            if not isinstance(payload, dict):
                return
            if payload.get("status") != "running":
                return

            reconciled_status = await runtime._reconcile_recovered_task(task_id, payload)
            if reconciled_status != "running":
                runtime._refresh_availability_from_recovered_state()
                return
    except Exception as exc:
        runtime.logger.error(
            "Recovered task monitor failed for %s: %s", task_id, exc, exc_info=True
        )
    finally:
        runtime._RECOVERY_MONITORS.pop(task_id, None)


def schedule_recovery_monitor(task_id: str, *, runtime: ModuleType) -> None:
    """Schedule background monitoring for one recovered running task."""
    existing = runtime._RECOVERY_MONITORS.get(task_id)
    if existing is not None and not existing.done():
        return
    runtime._RECOVERY_MONITORS[task_id] = runtime.asyncio.create_task(
        runtime._monitor_recovered_task(task_id)
    )


async def recover_owned_running_tasks(
    running_tasks: dict[str, dict],
    *,
    runtime: ModuleType,
) -> None:
    """Recover running tasks tracked for the current runner instance."""
    if not running_tasks:
        return

    runtime.logger.info("Recovering %s running task(s) after restart", len(running_tasks))

    for task_id, payload in running_tasks.items():
        try:
            status = await runtime._reconcile_recovered_task(task_id, payload)
        except Exception as exc:
            runtime.logger.error("Failed to recover task %s: %s", task_id, exc, exc_info=True)
            continue

        if status == "running":
            runtime._schedule_recovery_monitor(task_id)


async def recover_failed_task(task_id: str, payload: dict, *, runtime: ModuleType) -> bool:
    """Recover one failed task. Returns True when a restart is scheduled."""
    if runtime._is_stop_requested_payload(payload):
        runtime.logger.info("Skipping automatic restart for user-stopped task %s", task_id)
        return False

    workspace_terminal_status = runtime._infer_workspace_terminal_status(task_id, payload)
    if workspace_terminal_status is not None:
        status, _error_message, script_output = workspace_terminal_status
        if status == "completed":
            await runtime._finalize_recovered_task(
                task_id,
                payload,
                status="completed",
                script_output=script_output,
            )
            return False

    return bool(runtime._schedule_failed_task_restart(task_id, payload))


async def recover_owned_failed_tasks(
    failed_tasks: dict[str, dict],
    *,
    runtime: ModuleType,
) -> int:
    """Recover failed/timeout tasks and return the number of restarted tasks."""
    if not failed_tasks:
        return 0

    runtime.logger.info(
        "Inspecting %s failed task(s) for startup auto-restart",
        len(failed_tasks),
    )

    restarted_tasks = 0
    for task_id, payload in failed_tasks.items():
        try:
            if await runtime._recover_failed_task(task_id, payload):
                restarted_tasks += 1
        except Exception as exc:
            runtime.logger.error(
                "Failed to recover failed task %s: %s", task_id, exc, exc_info=True
            )

    if restarted_tasks:
        runtime.logger.info("Scheduled automatic restart for %s failed task(s)", restarted_tasks)

    return restarted_tasks


async def recover_running_tasks_after_restart(*, runtime: ModuleType) -> None:
    """Restore running task state after process restart using persisted JSON."""
    running_tasks = runtime._get_owned_task_statuses({"running"})
    if running_tasks:
        await runtime._recover_owned_running_tasks(running_tasks)

    failed_tasks = runtime._get_owned_task_statuses({"failed", "timeout"})
    if not running_tasks and not failed_tasks:
        runtime._refresh_availability_from_recovered_state()
        return

    await runtime._recover_owned_failed_tasks(failed_tasks)
    runtime._refresh_availability_from_recovered_state()


async def stop_recovery_monitors(*, runtime: ModuleType) -> None:
    """Cancel all running recovery monitor tasks."""
    if not runtime._RECOVERY_MONITORS:
        return

    monitors = list(runtime._RECOVERY_MONITORS.values())
    runtime._RECOVERY_MONITORS.clear()

    for monitor_task in monitors:
        monitor_task.cancel()
    await runtime.asyncio.gather(*monitors, return_exceptions=True)
