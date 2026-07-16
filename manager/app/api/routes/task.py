# manager/app/api/routes/task.py
"""
Tasks routes for Runner Manager.
Utility functions and endpoints responsible for task creation, execution, status updates, and result streaming.
"""

import asyncio
import csv
import os
import sys
from datetime import datetime
from pathlib import Path as PathlibPath
from typing import Any, Dict, List, cast
from urllib.parse import ParseResult

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from filelock import FileLock
from starlette.responses import Response

from app.__version__ import __version__
from app.core.auth import verify_admin, verify_token
from app.core.config import config
from app.core.paths import WEB_TEMPLATES_DIR
from app.core.priorities import would_exceed_other_domain_quota  # noqa: F401
from app.core.setup_logging import setup_default_logging
from app.core.state import delete_task as delete_task_from_state
from app.core.state import get_task as get_task_from_state
from app.core.state import get_tasks_snapshot, runners, save_tasks, tasks  # noqa: F401
from app.models.models import (
    Runner,
    Task,
    TaskCompletionNotification,
    TaskRequest,
    TaskResultManifest,
)
from app.services import task_callback_service, task_dispatch_service, task_result_service
from app.services.email_service import send_notify_retry_exhausted_email  # noqa: F401

logger = setup_default_logging()

router = APIRouter(prefix="/task", tags=["Task"])

templates = Jinja2Templates(directory=WEB_TEMPLATES_DIR)

# ======================================================
# Utility Functions
# ======================================================

_FAILURE_TASK_STATUSES = {"failed", "timeout", "error"}
_NON_DELETABLE_TASK_STATUSES = {"pending", "running"}
_NON_RESTARTABLE_TASK_STATUSES = {"pending", "running"}
_STOPPABLE_TASK_STATUSES = {"running"}
_MAX_BULK_DELETE_TASKS = 200
_MAX_BULK_RESTART_TASKS = 200
_MAX_BULK_STOP_TASKS = 200
_MANIFEST_READ_ATTEMPTS = 5
_MANIFEST_READ_DELAY_SECONDS = 0.2
_TASK_STATS_EXCLUDED_ETAB_NAMES = {"quick manual test"}
_NOTIFY_CALLBACK_READ_TIMEOUT_SECONDS = 15.0


def _is_pytest_run() -> bool:
    """Return True when running under pytest (tests or collection)."""
    return (
        os.getenv("PYTEST_CURRENT_TEST") is not None
        or "pytest" in sys.modules
        or any(PathlibPath(arg).name.startswith("pytest") for arg in sys.argv)
    )


def _is_test_task_for_stats(task: Task) -> bool:
    """Return True when task should be excluded from task_stats.csv."""
    etab_name = str(getattr(task, "etab_name", "") or "").strip().lower()
    return etab_name in _TASK_STATS_EXCLUDED_ETAB_NAMES


def _runner_auth_headers(runner: Runner, accept: str) -> Dict[str, str]:
    """Build auth headers for manager -> runner requests.

    Security note: never send the manager-wide token to arbitrary runner URLs.
    Prefer the per-runner token set at registration.
    """
    token = getattr(runner, "token", None)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Runner authentication token is missing. "
                "Re-register the runner so the manager can call it securely."
            ),
        )
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
    }


def _append_task_stats_csv(task: Task) -> None:
    """Append a single task stats row to data/task_stats.csv."""
    if _is_pytest_run():
        logger.debug("Skipping task_stats.csv append during pytest for task %s", task.task_id)
        return
    if _is_test_task_for_stats(task):
        logger.debug(
            "Skipping task_stats.csv append for test task %s (etab_name=%s)",
            task.task_id,
            task.etab_name,
        )
        return

    data_dir = PathlibPath("data")
    data_dir.mkdir(parents=True, exist_ok=True)
    csv_path = data_dir / "task_stats.csv"
    lock = FileLock(str(csv_path) + ".lock")

    created_at = task.created_at
    date = None
    if created_at:
        try:
            date_value = created_at[:-1] if created_at.endswith("Z") else created_at
            date = datetime.fromisoformat(date_value).date().isoformat()
        except ValueError:
            date = None

    row = {
        "task_id": task.task_id,
        "date": date,
        "task_type": task.task_type,
        "status": task.status,
        "app_name": task.app_name,
        "app_version": task.app_version,
        "etab_name": task.etab_name,
    }

    fieldnames = list(row.keys())

    with lock:
        needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
        with csv_path.open("a", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)


def _host_matches_allowlist(host: str, allowed_hosts: List[str]) -> bool:
    """Return True when host matches an allowed host or one of its subdomains."""
    return task_callback_service.host_matches_allowlist(host, allowed_hosts)


def _is_disallowed_ip(ip: str) -> bool:
    """Return True when an IP must be blocked for outbound callbacks."""
    return task_callback_service.is_disallowed_ip(ip)


async def _resolve_host_ips(host: str) -> List[str]:
    """Resolve all IPs for a hostname using system DNS."""
    return await task_callback_service.resolve_host_ips(host)


def _parse_notify_url(url: str) -> tuple[ParseResult, str]:
    """Parse and syntactically validate a notify URL."""
    return task_callback_service.parse_notify_url(url)


def _validate_notify_url_host(host: str) -> None:
    """Validate notify URL hostname against policy rules."""
    task_callback_service.validate_notify_url_host(sys.modules[__name__], host)


async def _resolve_notify_url_ips(host: str) -> List[str]:
    """Resolve notify URL host and convert DNS failures to HTTP 400."""
    return await task_callback_service.resolve_notify_url_ips(sys.modules[__name__], host)


def _validate_notify_url_public_ips(ips: List[str]) -> None:
    """Reject private, loopback and reserved callback destinations."""
    task_callback_service.validate_notify_url_public_ips(sys.modules[__name__], ips)


async def _validate_notify_url(url: str) -> str:
    """Run full notify URL validation and return the original URL."""
    return await task_callback_service.validate_notify_url(sys.modules[__name__], url)


async def _send_notify_callback(
    task: Task, notification: TaskCompletionNotification
) -> tuple[bool, str | None]:
    """Send a single notify URL callback attempt."""
    return await task_callback_service.send_notify_callback(
        sys.modules[__name__], task, notification
    )


def _set_notify_warning(task_id: str, message: str) -> None:
    """Persist callback warning without losing existing failure diagnostics."""
    task_callback_service.set_notify_warning(sys.modules[__name__], task_id, message)


def _restore_status_after_notify(task_id: str, notification: TaskCompletionNotification) -> None:
    """Restore task status after a successful notify callback."""
    task_callback_service.restore_status_after_notify(sys.modules[__name__], task_id, notification)


def _task_run_matches(task: Task | None, expected_run_id: str | None) -> bool:
    """Return True if the task belongs to the expected execution run."""
    return task_callback_service.task_run_matches(task, expected_run_id)


def _get_retry_notify_task(task_id: str, expected_run_id: str | None) -> Task | None:
    """Get task eligible for notify retry, or None if stale/ineligible."""
    return task_callback_service.get_retry_notify_task(
        sys.modules[__name__], task_id, expected_run_id
    )


async def _run_single_notify_retry_attempt(
    task_id: str,
    notification: TaskCompletionNotification,
    expected_run_id: str | None,
    attempt: int,
    max_retries: int,
) -> bool:
    """Execute one retry attempt and return whether retries must stop."""
    return await task_callback_service.run_single_notify_retry_attempt(
        sys.modules[__name__],
        task_id,
        notification,
        expected_run_id,
        attempt,
        max_retries,
    )


async def _handle_notify_retry_exhausted(
    task_id: str,
    expected_run_id: str | None,
    max_retries: int,
) -> None:
    """Log and optionally email when retries are exhausted."""
    await task_callback_service.handle_notify_retry_exhausted(
        sys.modules[__name__], task_id, expected_run_id, max_retries
    )


async def _retry_notify_callback(
    task_id: str,
    notification: TaskCompletionNotification,
    expected_run_id: str | None = None,
) -> None:
    """Retry notify URL callback with stale-run protection."""
    await task_callback_service.retry_notify_callback(
        sys.modules[__name__], task_id, notification, expected_run_id
    )


async def execute_task_async_background(
    task_id: str, runner: Runner, task_request: TaskRequest
) -> None:
    """Execute a queued task on its reserved runner."""
    await task_dispatch_service.execute_task_background(
        sys.modules[__name__], task_id, runner, task_request
    )


# ======================================================
# Web UI Endpoints
# ======================================================


@router.get(
    "s",  # This makes the full path /tasks
    response_class=HTMLResponse,
    summary="Tasks management page",
    description="Web interface for viewing and searching tasks",
    tags=["Task"],
    dependencies=[Depends(verify_admin)],
)
async def view_tasks(
    request: Request,
    limit: int = Query(100, description="Maximum number of tasks to display", ge=1, le=1000),
    status: List[str] = Query([], description="Filter by task status"),
    task_type: str = Query(None, description="Filter by task type"),
    search: str = Query(None, description="Search term"),
    auto_refresh: int = Query(0, description="Auto-refresh interval in seconds"),
):
    """
    Tasks management page with filtering and search capabilities
    """
    all_tasks_list = list(get_tasks_snapshot().values())

    available_statuses = [
        "pending",
        "running",
        "completed",
        "failed",
        "warning",
        "timeout",
    ]
    available_task_types = list(set(task.task_type for task in all_tasks_list if task.task_type))

    filtered_tasks = all_tasks_list

    if status:
        filtered_tasks = [t for t in filtered_tasks if t.status in status]

    if task_type:
        filtered_tasks = [t for t in filtered_tasks if t.task_type == task_type]

    if search:
        search_lower = search.lower()
        filtered_tasks = [
            t
            for t in filtered_tasks
            if (
                search_lower in str(t.task_id).lower()
                or search_lower in str(t.task_type).lower()
                or (t.source_url and search_lower in t.source_url.lower())
                or (t.etab_name and search_lower in t.etab_name.lower())
                or (
                    (t.parameters or {}).get("video_id")
                    and search_lower in str((t.parameters or {}).get("video_id")).lower()
                )
                or (
                    (t.parameters or {}).get("video_slug")
                    and search_lower in str((t.parameters or {}).get("video_slug")).lower()
                )
                or (
                    (t.parameters or {}).get("video_title")
                    and search_lower in str((t.parameters or {}).get("video_title")).lower()
                )
            )
        ]

    filtered_tasks.sort(key=lambda x: x.updated_at or "", reverse=True)

    display_tasks = filtered_tasks[:limit]

    status_counts = {s: 0 for s in available_statuses}
    for task in all_tasks_list:
        if task.status in status_counts:
            status_counts[task.status] += 1
        else:
            status_counts[task.status] = 1

    dark_mode = request.cookies.get("theme") == "dark"

    context = {
        "request": request,
        "tasks": [
            {
                "id": task.task_id,
                "status": task.status,
                "task_type": task.task_type,
                "source_url": task.source_url,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
            for task in display_tasks
        ],
        "total_tasks": len(all_tasks_list),
        "available_statuses": available_statuses,
        "available_task_types": sorted(available_task_types),
        "status_counts": status_counts,
        "current_filters": {
            "statuses": status,
            "task_type": task_type,
            "search": search,
            "limit": limit,
            "auto_refresh": auto_refresh,
        },
        "now": datetime.now(),
        "version": __version__,
        "dark_mode_enabled": dark_mode,
    }

    return templates.TemplateResponse(request, "tasks.html", context)


@router.get(
    "s/api/{task_id}",  # This makes the full path /tasks/api/{task_id}
    response_model=Task,
    response_model_exclude={"client_token"},
    summary="Get task details API",
    description="API endpoint to get detailed task information",
    tags=["Task"],
    dependencies=[Depends(verify_admin)],
)
async def get_task_details_api(task_id: str = Path(..., description="Task identifier")) -> Task:
    """
    Get detailed task information for the web UI
    """
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return _redact_task_for_api(task)


def _task_to_task_request(task: Task) -> TaskRequest:
    """Build a TaskRequest payload from an existing task."""
    return TaskRequest(
        etab_name=task.etab_name,
        app_name=task.app_name,
        app_version=task.app_version,
        task_type=task.task_type,
        source_url=task.source_url,
        affiliation=task.affiliation,
        parameters=task.parameters,
        notify_url=task.notify_url,
    )


def _redact_task_for_api(task: Task) -> Task:
    """Return a task copy with sensitive fields removed from API payloads."""
    if hasattr(task, "model_copy"):
        return task.model_copy(update={"client_token": None})
    copied = task.copy(deep=True)
    copied.client_token = None
    return copied


def _normalize_task_ids(task_ids: List[str]) -> List[str]:
    """Normalize, deduplicate and sanitize a list of task IDs."""
    unique_task_ids: List[str] = []
    seen_task_ids: set[str] = set()

    for raw_task_id in task_ids:
        if not isinstance(raw_task_id, str):
            continue
        task_id = raw_task_id.strip()
        if not task_id or task_id in seen_task_ids:
            continue
        seen_task_ids.add(task_id)
        unique_task_ids.append(task_id)

    return unique_task_ids


def _http_exception_detail_to_text(detail: object) -> str:
    """Extract a human-readable message from FastAPI HTTPException details."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and "detail" in detail:
        nested = detail.get("detail")
        return nested if isinstance(nested, str) else str(nested)
    return str(detail)


def _runner_response_detail_to_text(response: httpx.Response) -> str:
    """Extract a readable error detail from a runner response."""
    try:
        payload = response.json()
    except Exception:
        payload = None

    if isinstance(payload, dict) and "detail" in payload:
        return _http_exception_detail_to_text(payload.get("detail"))

    text = (response.text or "").strip()
    return text or f"Runner returned status {response.status_code}"


async def _request_runner_task_stop(task_id: str, runner: Runner) -> httpx.Response:
    """Forward a task stop request to the assigned runner."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{runner.url}/task/stop/{task_id}",
                headers=_runner_auth_headers(runner, accept="application/json"),
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Timeout contacting runner")
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Error contacting runner: {str(exc)}")

    if response.status_code == 409:
        raise HTTPException(status_code=409, detail=_runner_response_detail_to_text(response))
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail=_runner_response_detail_to_text(response))
    if response.status_code < 200 or response.status_code >= 300:
        detail = _runner_response_detail_to_text(response)
        raise HTTPException(
            status_code=502,
            detail=f"Runner stop request failed: {response.status_code} - {detail}",
        )

    return response


async def _stop_selected_task(task_id: str) -> tuple[str, dict[str, str | int]]:
    """Stop a single selected task and return outcome bucket with payload."""
    task = get_task_from_state(task_id)
    if task is None:
        return "skipped", {"task_id": task_id, "reason": "Task not found"}

    if task.status not in _STOPPABLE_TASK_STATUSES:
        return (
            "skipped",
            {
                "task_id": task_id,
                "reason": f"Task status '{task.status}' cannot be stopped",
            },
        )

    runner = runners.get(task.runner_id)
    if runner is None:
        return "failed", {"task_id": task_id, "reason": "Runner not found"}

    try:
        response = await _request_runner_task_stop(task_id, runner)
    except HTTPException as exc:
        return "failed", {"task_id": task_id, "reason": _http_exception_detail_to_text(exc.detail)}
    except Exception as exc:
        logger.exception(f"Unexpected error while stopping task {task_id}: {exc}")
        return "failed", {"task_id": task_id, "reason": "Unexpected error while stopping task"}

    return (
        "stopped",
        {
            "task_id": task_id,
            "runner_id": runner.id,
            "runner_status_code": response.status_code,
        },
    )


def _task_request_fingerprint(
    *,
    task_type: str,
    source_url: str,
    parameters: Dict[str, Any] | None,
    notify_url: str | None,
    app_name: str,
    etab_name: str,
) -> str:
    """Build a stable fingerprint used to deduplicate in-flight requests."""
    return task_dispatch_service.task_request_fingerprint(
        task_type=task_type,
        source_url=source_url,
        parameters=parameters,
        notify_url=notify_url,
        app_name=app_name,
        etab_name=etab_name,
    )


def _find_inflight_duplicate_task_id(
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
) -> str | None:
    """Return an in-flight duplicate task ID when one exists."""
    return task_dispatch_service.find_inflight_duplicate_task_id(
        sys.modules[__name__], task_request, tasks_snapshot
    )


def _build_inflight_dedup_response(
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
    *,
    log_message: str,
) -> dict | None:
    """Build a response reusing an in-flight duplicate task."""
    return task_dispatch_service.build_inflight_dedup_response(
        sys.modules[__name__],
        task_request,
        tasks_snapshot,
        log_message=log_message,
    )


def _try_reuse_inflight_duplicate(
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
    *,
    dedup_enabled: bool,
    log_message: str,
) -> dict | None:
    """Return a dedup response when enabled and a match exists."""
    return task_dispatch_service.try_reuse_inflight_duplicate(
        sys.modules[__name__],
        task_request,
        tasks_snapshot,
        dedup_enabled=dedup_enabled,
        log_message=log_message,
    )


def _try_reuse_inflight_duplicate_with_fresh_snapshot(
    task_request: TaskRequest,
    *,
    dedup_enabled: bool,
    log_message: str,
) -> dict | None:
    """Re-check deduplication against a fresh task snapshot."""
    return task_dispatch_service.try_reuse_inflight_duplicate_with_fresh_snapshot(
        sys.modules[__name__],
        task_request,
        dedup_enabled=dedup_enabled,
        log_message=log_message,
    )


def _try_reserve_runner_for_dispatch(runner_id: str) -> Runner | None:
    """Reserve a runner before task creation."""
    return task_dispatch_service.try_reserve_runner_for_dispatch(sys.modules[__name__], runner_id)


def _resolve_runner_for_dispatch(
    runner_id: str,
    runner: Runner,
    *,
    preferred_task_id: str | None,
) -> Runner | None:
    """Return the runner instance to use for dispatch."""
    return task_dispatch_service.resolve_runner_for_dispatch(
        sys.modules[__name__],
        runner_id,
        runner,
        preferred_task_id=preferred_task_id,
    )


async def _queue_task_execution(
    task_request: TaskRequest,
    client_token: str | None,
    *,
    preferred_task_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Queue a task on an available runner and schedule its execution."""
    return await task_dispatch_service.queue_task_execution(
        sys.modules[__name__],
        task_request,
        client_token,
        preferred_task_id=preferred_task_id,
        created_at=created_at,
    )


@router.post(
    "s/delete-selected",  # This makes the full path /tasks/delete-selected
    response_model=dict,
    summary="Delete selected tasks",
    description="Delete selected tasks from the tasks web interface",
    tags=["Task"],
    dependencies=[Depends(verify_admin)],
)
async def delete_selected_tasks(payload: Dict[str, List[str]]) -> dict:
    """Delete selected tasks from the manager state and persistence."""
    raw_task_ids = payload.get("task_ids", [])
    if not isinstance(raw_task_ids, list):
        raise HTTPException(status_code=400, detail="task_ids must be a list")

    task_ids = _normalize_task_ids(raw_task_ids)
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids must contain at least one task ID")
    if len(task_ids) > _MAX_BULK_DELETE_TASKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many task IDs in one request ({len(task_ids)}). "
                f"Maximum allowed: {_MAX_BULK_DELETE_TASKS}"
            ),
        )

    deleted: List[dict[str, str]] = []
    skipped: List[dict[str, str]] = []
    failed: List[dict[str, str]] = []

    for task_id in task_ids:
        original_task = get_task_from_state(task_id)
        if original_task is None:
            skipped.append({"task_id": task_id, "reason": "Task not found"})
            continue

        if original_task.status in _NON_DELETABLE_TASK_STATUSES:
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": f"Task status '{original_task.status}' cannot be deleted",
                }
            )
            continue

        try:
            deleted_ok = delete_task_from_state(task_id)
        except Exception as exc:
            logger.exception(f"Unexpected error while deleting task {task_id}: {exc}")
            failed.append({"task_id": task_id, "reason": "Unexpected error while deleting task"})
            continue

        if not deleted_ok:
            failed.append({"task_id": task_id, "reason": "Task deletion failed"})
            continue

        deleted.append({"task_id": task_id})

    return {
        "requested": len(task_ids),
        "deleted": deleted,
        "skipped": skipped,
        "failed": failed,
    }


@router.post(
    "s/restart-selected",  # This makes the full path /tasks/restart-selected
    response_model=dict,
    summary="Restart selected tasks",
    description="Restart selected tasks from the tasks web interface",
    tags=["Task"],
    dependencies=[Depends(verify_admin)],
)
async def restart_selected_tasks(payload: Dict[str, List[str]]) -> dict:
    """Restart selected tasks in place while preserving each original task ID."""
    raw_task_ids = payload.get("task_ids", [])
    if not isinstance(raw_task_ids, list):
        raise HTTPException(status_code=400, detail="task_ids must be a list")

    task_ids = _normalize_task_ids(raw_task_ids)
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids must contain at least one task ID")
    if len(task_ids) > _MAX_BULK_RESTART_TASKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many task IDs in one request ({len(task_ids)}). "
                f"Maximum allowed: {_MAX_BULK_RESTART_TASKS}"
            ),
        )

    restarted: List[dict[str, str]] = []
    skipped: List[dict[str, str]] = []
    failed: List[dict[str, str]] = []

    for task_id in task_ids:
        original_task = get_task_from_state(task_id)
        if original_task is None:
            skipped.append({"task_id": task_id, "reason": "Task not found"})
            continue

        if original_task.status in _NON_RESTARTABLE_TASK_STATUSES:
            skipped.append(
                {
                    "task_id": task_id,
                    "reason": f"Task status '{original_task.status}' cannot be restarted",
                }
            )
            continue

        task_request = _task_to_task_request(original_task)
        try:
            queued = await _queue_task_execution(
                task_request,
                getattr(original_task, "client_token", None),
                preferred_task_id=task_id,
                created_at=getattr(original_task, "created_at", None),
            )
        except HTTPException as exc:
            failed.append(
                {"task_id": task_id, "reason": _http_exception_detail_to_text(exc.detail)}
            )
            continue
        except Exception as exc:
            logger.exception(f"Unexpected error while restarting task {task_id}: {exc}")
            failed.append({"task_id": task_id, "reason": "Unexpected error while restarting task"})
            continue

        restarted.append({"task_id": queued["task_id"]})

    return {
        "requested": len(task_ids),
        "restarted": restarted,
        "skipped": skipped,
        "failed": failed,
    }


@router.post(
    "s/stop-selected",  # This makes the full path /tasks/stop-selected
    response_model=dict,
    summary="Stop selected running tasks",
    description="Request stop for selected running tasks from the tasks web interface",
    tags=["Task"],
    dependencies=[Depends(verify_admin)],
)
async def stop_selected_tasks(payload: Dict[str, List[str]]) -> dict:
    """Request stop for selected running tasks without mutating their manager status."""
    raw_task_ids = payload.get("task_ids", [])
    if not isinstance(raw_task_ids, list):
        raise HTTPException(status_code=400, detail="task_ids must be a list")

    task_ids = _normalize_task_ids(raw_task_ids)
    if not task_ids:
        raise HTTPException(status_code=400, detail="task_ids must contain at least one task ID")
    if len(task_ids) > _MAX_BULK_STOP_TASKS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Too many task IDs in one request ({len(task_ids)}). "
                f"Maximum allowed: {_MAX_BULK_STOP_TASKS}"
            ),
        )

    stopped: List[dict[str, str | int]] = []
    skipped: List[dict[str, str]] = []
    failed: List[dict[str, str]] = []

    for task_id in task_ids:
        outcome, payload_item = await _stop_selected_task(task_id)
        if outcome == "stopped":
            stopped.append(payload_item)
            continue
        if outcome == "skipped":
            skipped.append(cast(dict[str, str], payload_item))
            continue
        failed.append(cast(dict[str, str], payload_item))

    return {
        "requested": len(task_ids),
        "stopped": stopped,
        "skipped": skipped,
        "failed": failed,
    }


# ======================================================
# API Endpoints
# ======================================================


@router.post(
    "/execute",
    response_model=dict,
    summary="Execute task asynchronously",
    description="Execute a task asynchronously and return immediate task ID",
    tags=["Task"],
)
async def execute_task_async(
    task_request: TaskRequest,
    current_token: str = Depends(verify_token),
) -> dict:
    """
    Execute a task asynchronously and return immediate task ID.

    Args:
        task_request: Task request containing task type

    Returns:
        dict: Task ID and initial status

    Raises:
        HTTPException: If no runners are available
    """
    return await _queue_task_execution(task_request, current_token)


@router.post(
    "/stop/{task_id}",
    response_model=dict,
    summary="Stop a running task",
    description="Request a running task stop through its assigned runner",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
    responses={
        202: {"description": "Task stop request accepted"},
        404: {"description": "Task or runner not found"},
        409: {"description": "Task not running or runner cannot stop it yet"},
        502: {"description": "Runner HTTP error"},
        504: {"description": "Runner timeout"},
    },
)
async def stop_task(
    task_id: str = Path(..., description="Task identifier to stop"),
) -> JSONResponse:
    """Request stop for a running task without changing manager-side status."""
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if task.status not in _STOPPABLE_TASK_STATUSES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Task not running")

    runner = runners.get(task.runner_id)
    if runner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runner not found")

    response = await _request_runner_task_stop(task_id, runner)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "task_id": task_id,
            "status": "stop_requested",
            "runner_id": runner.id,
            "runner_status_code": response.status_code,
        },
    )


@router.get(
    "/status/{task_id}",
    response_model=Task,
    response_model_exclude={"client_token"},
    summary="Get task status",
    description="Retrieve status of a specific task",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
    responses={
        200: {"description": "Task status information"},
        404: {"description": "Task not found"},
    },
)
async def get_task_status(task_id: str = Path(..., description="Task identifier to check")) -> Task:
    """
    Retrieve the status of a specific task.

    Args:
        task_id: Unique identifier of the task

    Returns:
        Task: Task status information

    Raises:
        HTTPException: If task not found
    """
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    return _redact_task_for_api(task)


@router.get(
    "/list",
    response_model=Dict[str, Task],
    response_model_exclude={"__all__": {"client_token"}},
    summary="List all tasks",
    description="Return status of all current tasks",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
)
async def list_tasks() -> Dict[str, Task]:
    """
    List all tasks with their current status.

    Returns:
        Dict[str, Task]: Dictionary of task IDs to task objects
    """
    all_tasks: Dict[str, Task] = get_tasks_snapshot()
    return {task_id: _redact_task_for_api(task) for task_id, task in all_tasks.items()}


@router.get(
    "/result/{task_id}",
    response_model=TaskResultManifest,
    responses={
        200: {"description": "Task result manifest", "content": {"application/json": {}}},
        404: {"description": "Task not found"},
        425: {"description": "Task not completed yet"},
    },
    summary="Get task result",
    description="Return task result manifest from shared storage when enabled, otherwise proxy from runner to client",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
)
async def get_task_result(task_id: str = Path(..., description="Task identifier")) -> Response:
    """
    Return task result manifest.

    Args:
        task_id: Unique identifier of the task

    Returns:
        Response: Manifest response

    Raises:
        HTTPException: If task not found, failed, or not completed
    """
    task = _get_valid_task(task_id)

    if getattr(config, "RUNNERS_STORAGE_ENABLED", False):
        return await asyncio.to_thread(_get_local_manifest, task)

    runner = _get_task_runner(task)
    return await _stream_runner_manifest(task, runner)


@router.get(
    "/result/{task_id}/file/{file_path:path}",
    responses={
        200: {
            "description": "Task result file",
            "content": {"application/octet-stream": {}},
        },
        404: {"description": "Task or file not found"},
        425: {"description": "Task not completed yet"},
    },
    summary="Get task result file",
    description="Stream a single task result file from shared storage when enabled, otherwise proxy-stream from runner to client",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
)
async def get_task_result_file(
    task_id: str = Path(..., description="Task identifier"),
    file_path: str = Path(..., description="Relative path to the result file"),
) -> Response:
    """
    Stream a single task result file.

    Args:
        task_id: Unique identifier of the task
        file_path: Relative file path from the manifest

    Returns:
        Response: Streamed file response

    Raises:
        HTTPException: If task or file not found, failed, or not completed
    """
    task = _get_valid_task(task_id)
    _validate_result_path(file_path)

    if getattr(config, "RUNNERS_STORAGE_ENABLED", False):
        return _stream_local_file(task, file_path)

    runner = _get_task_runner(task)
    return await _stream_runner_file(task, runner, file_path)


def _resolve_shared_storage_base() -> PathlibPath:
    """Resolve and validate the base shared-storage directory."""
    return task_result_service.resolve_shared_storage_base(sys.modules[__name__])


def _mark_warning_as_completed(task_id: str) -> None:
    """Convert warning status to completed after a successful result fetch."""
    task_result_service.mark_warning_as_completed(sys.modules[__name__], task_id)


def _validate_result_path(file_path: str) -> None:
    """Reject absolute paths and traversal in requested result paths."""
    task_result_service.validate_result_path(sys.modules[__name__], file_path)


def _get_local_task_dir(task_id: str) -> PathlibPath:
    """Return a validated task directory below shared storage."""
    return task_result_service.get_local_task_dir(sys.modules[__name__], task_id)


def _get_local_output_dir(task_id: str) -> PathlibPath:
    """Return the validated output directory for a task."""
    return task_result_service.get_local_output_dir(sys.modules[__name__], task_id)


def _resolve_local_manifest_path(task_dir: PathlibPath) -> PathlibPath:
    """Resolve the manifest path from a validated task directory."""
    return task_result_service.resolve_local_manifest_path(sys.modules[__name__], task_dir)


def _read_manifest_with_retry(manifest_resolved: PathlibPath) -> Any:
    """Read manifest JSON with retries to absorb write races."""
    return task_result_service.read_manifest_with_retry(sys.modules[__name__], manifest_resolved)


def _get_local_manifest(task: Task) -> JSONResponse:
    """Return a task manifest directly from shared storage."""
    return task_result_service.get_local_manifest(sys.modules[__name__], task)


def _stream_local_file(task: Task, file_path: str) -> FileResponse:
    """Return one task result file directly from shared storage."""
    return task_result_service.stream_local_file(sys.modules[__name__], task, file_path)


def _get_valid_task(task_id: str) -> Task:
    """Get and validate a result-bearing task."""
    return task_result_service.get_valid_task(sys.modules[__name__], task_id)


def _get_task_runner(task: Task) -> Runner:
    """Get the runner assigned to a task."""
    return task_result_service.get_task_runner(sys.modules[__name__], task)


async def _stream_runner_manifest(task: Task, runner: Runner) -> StreamingResponse:
    """Proxy-stream a manifest from its runner."""
    return await task_result_service.stream_runner_manifest(sys.modules[__name__], task, runner)


async def _stream_runner_file(task: Task, runner: Runner, file_path: str) -> StreamingResponse:
    """Proxy-stream a result file from its runner."""
    return await task_result_service.stream_runner_file(
        sys.modules[__name__], task, runner, file_path
    )


async def _fetch_runner_resource(
    client: httpx.AsyncClient,
    runner: Runner,
    url: str,
    timeout: httpx.Timeout,
    accept: str,
) -> httpx.Response:
    """Retrieve a runner response and reject HTTP errors early."""
    return await task_result_service.fetch_runner_resource(
        sys.modules[__name__], client, runner, url, timeout, accept
    )


def _build_streaming_response(
    task_id: str,
    response: httpx.Response,
    client: httpx.AsyncClient,
    media_type: str | None = None,
    filename: str | None = None,
) -> StreamingResponse:
    """Create a streaming response and close its network resources."""
    return task_result_service.build_streaming_response(
        sys.modules[__name__],
        task_id,
        response,
        client,
        media_type,
        filename,
    )


@router.post(
    "/completion",
    response_model=dict,
    summary="Task completion notification",
    description="Endpoint for runners to notify task completion",
    tags=["Task"],
    dependencies=[Depends(verify_token)],
)
async def task_completion(
    notification: TaskCompletionNotification, current_token: str = Depends(verify_token)
) -> dict:
    """
    Handle task completion notification from runners.

    Args:
        notification: Task completion details
        current_token: Authenticated runner token

    Returns:
        dict: Acknowledgment status

    Raises:
        HTTPException: If task or runner not found, or token invalid
    """
    task = _get_task_or_404(notification.task_id)
    runner = _get_runner_or_404(task.runner_id)
    _verify_runner_token(runner, current_token)

    _apply_task_completion_update(task, notification)
    # Persist terminal status before callback to avoid races with clients that
    # fetch /task/result immediately after receiving notify_url.
    save_tasks()
    await _handle_notify_callback(task, notification)

    runner.availability = "available"
    runners[runner.id] = runner

    logger.info(f"Task {notification.task_id} updated with status: {task.status}")

    try:
        _append_task_stats_csv(task)
    except Exception as e:
        logger.error(f"Failed to append task stats CSV for {notification.task_id}: {e}")

    save_tasks()

    return {"status": "acknowledged"}


def _get_task_or_404(task_id: str) -> Task:
    """Fetch task from state or raise 404."""
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def _get_runner_or_404(runner_id: str) -> Runner:
    """Fetch runner from state or raise 404."""
    if runner_id not in runners:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runner not found")
    return runners[runner_id]


def _verify_runner_token(runner: Runner, current_token: str) -> None:
    """Ensure completion callback token belongs to the assigned runner."""
    if runner.token != current_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token not authorized for this task",
        )


def _apply_task_completion_update(task: Task, notification: TaskCompletionNotification) -> None:
    """Apply completion payload fields onto the in-memory task."""
    task.status = notification.status
    task.updated_at = datetime.now().isoformat()

    if notification.status == "completed":
        task.error = None
    elif notification.error_message:
        task.error = notification.error_message

    if notification.script_output:
        task.script_output = notification.script_output


async def _handle_notify_callback(task: Task, notification: TaskCompletionNotification) -> None:
    """Send notify callback and schedule retries when needed."""
    await task_callback_service.handle_notify_callback(sys.modules[__name__], task, notification)
