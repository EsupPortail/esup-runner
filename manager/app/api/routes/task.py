# manager/app/api/routes/task.py
"""
Tasks routes for Runner Manager.
Utility functions and endpoints responsible for task creation, execution, status updates, and result streaming.
"""

import asyncio
import csv
import ipaddress
import json
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path as PathlibPath
from typing import Dict, List
from urllib.parse import ParseResult, quote, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from filelock import FileLock
from starlette.responses import Response

from app.__version__ import __version__
from app.core.auth import verify_admin, verify_token
from app.core.config import config
from app.core.priorities import would_exceed_other_domain_quota
from app.core.setup_logging import setup_default_logging
from app.core.state import get_task as get_task_from_state
from app.core.state import get_tasks_snapshot
from app.core.state import runners, save_tasks, tasks
from app.models.models import (
    Runner,
    Task,
    TaskCompletionNotification,
    TaskRequest,
    TaskResultManifest,
)

# Configure logging
logger = setup_default_logging()

# Create API router
router = APIRouter(prefix="/task", tags=["Task"])

# Templates configuration
templates = Jinja2Templates(directory="app/web/templates")

# ======================================================
# Utility Functions
# ======================================================

_FAILURE_TASK_STATUSES = {"failed", "timeout", "error"}
_MANIFEST_READ_ATTEMPTS = 5
_MANIFEST_READ_DELAY_SECONDS = 0.2


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
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in allowed_hosts:
        a = (allowed or "").strip().lower().rstrip(".")
        if not a:
            continue
        if host == a or host.endswith("." + a):
            return True
    return False


def _is_disallowed_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


async def _resolve_host_ips(host: str) -> List[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: List[str] = sorted({info[4][0] for info in infos if info and info[4]})
    return ips


def _parse_notify_url(url: str) -> tuple[ParseResult, str]:
    if not url:
        raise HTTPException(status_code=400, detail="notify_url is empty")

    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="notify_url must use http or https")

    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="notify_url is missing host")

    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="notify_url must not include userinfo")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail="notify_url has invalid host")

    return parsed, host


def _validate_notify_url_host(host: str) -> None:
    if config.NOTIFY_URL_ALLOWED_HOSTS and not _host_matches_allowlist(
        host, config.NOTIFY_URL_ALLOWED_HOSTS
    ):
        raise HTTPException(status_code=400, detail="notify_url host not allowed")

    # Block localhost-style hosts even before DNS resolution.
    if host in {"localhost"}:
        raise HTTPException(status_code=400, detail="notify_url host not allowed")


async def _resolve_notify_url_ips(host: str) -> List[str]:
    try:
        ips = await _resolve_host_ips(host)
    except Exception:
        raise HTTPException(status_code=400, detail="notify_url host cannot be resolved")

    if not ips:
        raise HTTPException(status_code=400, detail="notify_url host cannot be resolved")

    return ips


def _validate_notify_url_public_ips(ips: List[str]) -> None:
    for ip in ips:
        if _is_disallowed_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="notify_url resolves to a private/loopback/link-local address",
            )


async def _validate_notify_url(url: str) -> str:
    _, host = _parse_notify_url(url)
    _validate_notify_url_host(host)
    ips = await _resolve_notify_url_ips(host)

    if not config.NOTIFY_URL_ALLOW_PRIVATE_NETWORKS:
        _validate_notify_url_public_ips(ips)

    return url


async def _send_notify_callback(
    task: Task, notification: TaskCompletionNotification
) -> tuple[bool, str | None]:
    """Send a single notify_url callback attempt."""
    if not task.notify_url:
        return False, "notify_url is empty"

    # Validate notify_url before attempting outbound request.
    await _validate_notify_url(task.notify_url)

    logger.info(f"Sending notify URL callback to {task.notify_url} for task {notification.task_id}")
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)

    payload = {
        "task_id": notification.task_id,
        "status": notification.status,
        # Keep callback payload robust for lightweight test doubles/mocks.
        "error_message": getattr(notification, "error_message", None),
        "script_output": notification.script_output,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    headers: Dict[str, str] = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    client_token = getattr(task, "client_token", None)
    if client_token:
        headers["Authorization"] = f"Bearer {client_token}"

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            task.notify_url,
            content=body,
            headers=headers,
        )

    if response.status_code == 200:
        logger.info(
            f"Notify URL callback {task.notify_url} successful for task {notification.task_id}"
        )
        return True, None

    error_message = (
        f"Notify URL callback {task.notify_url} failed: "
        f"{response.status_code} - {response.text}"
    )
    logger.warning(error_message)
    return False, error_message


def _set_notify_warning(task_id: str, message: str) -> None:
    task = tasks[task_id]

    if task.status in _FAILURE_TASK_STATUSES:
        # Preserve terminal failure status and attach notify callback diagnostics.
        if task.error and message not in task.error:
            task.error = f"{task.error}\n\nNotify callback warning: {message}"
        elif not task.error:
            task.error = message
    else:
        task.status = "warning"
        task.error = message

    task.updated_at = datetime.now().isoformat()
    save_tasks()


def _restore_status_after_notify(task_id: str, notification: TaskCompletionNotification) -> None:
    task = tasks[task_id]
    task.status = notification.status
    task.updated_at = datetime.now().isoformat()

    if notification.status == "completed":
        task.error = None
    elif notification.error_message:
        task.error = notification.error_message

    save_tasks()


async def _retry_notify_callback(task_id: str, notification: TaskCompletionNotification) -> None:
    task = get_task_from_state(task_id)
    if task is None:
        return
    if not task.notify_url:
        return

    max_retries = config.COMPLETION_NOTIFY_MAX_RETRIES
    delay_seconds = config.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS
    backoff_factor = config.COMPLETION_NOTIFY_BACKOFF_FACTOR

    for attempt in range(1, max_retries + 1):
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        try:
            notify_ok, _ = await _send_notify_callback(task, notification)
            if notify_ok:
                _restore_status_after_notify(task_id, notification)
                logger.info(
                    "Notify URL callback succeeded after retry "
                    f"{attempt}/{max_retries} for task {task_id}"
                )
                return
        except Exception as e:
            logger.error(f"Error during notify URL retry to {task.notify_url}: {str(e)}")

        delay_seconds = int(delay_seconds * backoff_factor)

    logger.warning(
        f"Notify URL callback retries exhausted for task {task_id} after {max_retries} attempts"
    )


async def execute_task_async_background(
    task_id: str, runner: Runner, task_request: TaskRequest
) -> None:
    """
    Execute a task in the background on a specific runner.

    Args:
        task_id: Unique task identifier
        runner: Runner instance to execute the task
        task_type: Type of task to execute
    """
    try:
        logger.info(f"Starting background task {task_id} on runner {runner.id}")

        # Update task status to running
        tasks[task_id].status = "running"
        tasks[task_id].updated_at = datetime.now().isoformat()

        # Execute task on runner
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{runner.url}/task/run",
                json={
                    "task_id": task_id,
                    "etab_name": task_request.etab_name,
                    "app_name": task_request.app_name,
                    "app_version": task_request.app_version,
                    "task_type": task_request.task_type,
                    "source_url": task_request.source_url,
                    "affiliation": task_request.affiliation,
                    "parameters": task_request.parameters,
                    "notify_url": task_request.notify_url,
                    "completion_callback": f"{config.MANAGER_URL}/task/completion",
                },
                headers=_runner_auth_headers(runner, accept="application/json"),
            )

            if response.status_code == 200:
                runner.availability = "busy"
                runners[runner.id] = runner
            else:
                tasks[task_id].status = "failed"
                tasks[task_id].error = (
                    f"Runner returned status {response.status_code}: {response.text}"
                )
                tasks[task_id].updated_at = datetime.now().isoformat()
                runner.availability = "available"
                runners[runner.id] = runner
                logger.error(f"Task {task_id} failed with status {response.status_code}")

            # Save tasks state
            save_tasks()

    except Exception as e:
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = datetime.now().isoformat()
        logger.error(f"Error executing task {task_id}: {e}")

        # Save tasks state
        save_tasks()


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
    # Get all tasks
    all_tasks_list = list(get_tasks_snapshot().values())

    # Available statuses and task types
    available_statuses = ["pending", "running", "completed", "failed", "warning"]
    available_task_types = list(set(task.task_type for task in all_tasks_list if task.task_type))

    # Apply filters
    filtered_tasks = all_tasks_list

    # Filter by status
    if status:
        filtered_tasks = [t for t in filtered_tasks if t.status in status]

    # Filter by task type
    if task_type:
        filtered_tasks = [t for t in filtered_tasks if t.task_type == task_type]

    # Apply search
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
            )
        ]

    # Sort by updated_at (most recent first)
    filtered_tasks.sort(key=lambda x: x.updated_at or "", reverse=True)

    # Limit results
    display_tasks = filtered_tasks[:limit]

    # Calculate statistics
    status_counts = {s: 0 for s in available_statuses}
    for task in all_tasks_list:
        if task.status in status_counts:
            status_counts[task.status] += 1
        else:
            status_counts[task.status] = 1

    dark_mode = request.cookies.get("theme") == "dark"

    # Prepare template context
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

    return task


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
    logger.info("Starting async task execution")
    tasks_snapshot = get_tasks_snapshot()

    if task_request.notify_url:
        await _validate_notify_url(task_request.notify_url)

    if config.PRIORITIES_ENABLED:
        if would_exceed_other_domain_quota(
            request_notify_url=task_request.notify_url,
            tasks=tasks_snapshot,
            runner_capacity=len(runners),
            priority_domain=config.PRIORITY_DOMAIN,
            max_other_percent=config.MAX_OTHER_DOMAIN_TASK_PERCENT,
        ):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("Non-priority domain rejected: maximum other-domain task quota reached"),
            )

    # Find available runner
    for runner_id, runner in runners.items():
        logger.info(f"Checking runner: {runner_id}")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                logger.info(f"Pinging runner at: {runner.url}/runner/ping")
                response = await client.get(f"{runner.url}/runner/ping")
                logger.info("Runner ping response received")

                # Runner well registered with manager, available and with the good task type
                if (
                    response.json().get("available")
                    and response.json().get("registered")
                    and task_request.task_type in response.json().get("task_types")
                ):
                    # Generate unique task ID
                    task_id = str(uuid.uuid4())

                    # Store initial task status
                    tasks[task_id] = Task(
                        task_id=task_id,
                        runner_id=runner_id,
                        status="running",
                        etab_name=task_request.etab_name,
                        app_name=task_request.app_name,
                        app_version=task_request.app_version,
                        task_type=task_request.task_type,
                        source_url=task_request.source_url,
                        affiliation=task_request.affiliation,
                        parameters=task_request.parameters,
                        notify_url=task_request.notify_url,
                        client_token=current_token,
                        completion_callback=None,
                        created_at=datetime.now().isoformat(),
                        updated_at=datetime.now().isoformat(),
                        error=None,
                        script_output=None,
                    )

                    # Save tasks state
                    save_tasks()

                    # Start task execution in background
                    asyncio.create_task(
                        execute_task_async_background(task_id, runner, task_request)
                    )

                    return {"task_id": task_id, "status": "running"}
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.warning(f"Runner {runner_id} unavailable: {e}")
            continue

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No runners available"
    )


@router.get(
    "/status/{task_id}",
    response_model=Task,
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

    return task


@router.get(
    "/list",
    response_model=Dict[str, Task],
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
    return all_tasks


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
    # Validate and get resources
    task = _get_valid_task(task_id)

    # If manager has direct access to runner storage, return the manifest from disk
    if getattr(config, "RUNNERS_STORAGE_ENABLED", False):
        return _get_local_manifest(task)

    runner = _get_task_runner(task)
    return await _stream_runner_manifest(task, runner)


@router.get(
    "/result/{task_id}/file/{file_path:path}",
    responses={
        200: {"description": "Task result file", "content": {"application/octet-stream": {}}},
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
    base_dir = PathlibPath(getattr(config, "RUNNERS_STORAGE_PATH", "/tmp/esup-runner")).expanduser()
    try:
        base_resolved = base_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid RUNNERS_STORAGE_PATH")

    if not base_resolved.exists() or not base_resolved.is_dir():
        raise HTTPException(500, "RUNNERS_STORAGE_PATH does not exist or is not a directory")

    return base_resolved


def _mark_warning_as_completed(task_id: str) -> None:
    if tasks[task_id].status == "warning":
        tasks[task_id].status = "completed"
        tasks[task_id].error = None
        save_tasks()


def _validate_result_path(file_path: str) -> None:
    path = PathlibPath(file_path)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(400, "Invalid result file path")


def _get_local_task_dir(task_id: str) -> PathlibPath:
    base_resolved = _resolve_shared_storage_base()
    task_dir = base_resolved / task_id
    try:
        task_resolved = task_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid task result path")

    if task_resolved != base_resolved and base_resolved not in task_resolved.parents:
        raise HTTPException(500, "Resolved result path is outside RUNNERS_STORAGE_PATH")

    if not task_resolved.exists() or not task_resolved.is_dir():
        raise HTTPException(404, "Result directory not found in shared storage")

    return task_resolved


def _get_local_output_dir(task_id: str) -> PathlibPath:
    task_dir = _get_local_task_dir(task_id)
    output_dir = task_dir / "output"

    try:
        output_resolved = output_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid task output path")

    if output_resolved != task_dir and task_dir not in output_resolved.parents:
        raise HTTPException(500, "Resolved output path is outside task directory")

    if not output_resolved.exists() or not output_resolved.is_dir():
        raise HTTPException(404, "Result output directory not found in shared storage")

    return output_resolved


def _resolve_local_manifest_path(task_dir: PathlibPath) -> PathlibPath:
    manifest_path = task_dir / "manifest.json"
    try:
        return manifest_path.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid manifest path")


def _read_manifest_with_retry(manifest_resolved: PathlibPath):
    manifest_data = None
    last_json_error = False

    for attempt in range(1, _MANIFEST_READ_ATTEMPTS + 1):
        if manifest_resolved.exists() and manifest_resolved.is_file():
            try:
                manifest_data = json.loads(manifest_resolved.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                last_json_error = True

        if attempt < _MANIFEST_READ_ATTEMPTS:
            time.sleep(_MANIFEST_READ_DELAY_SECONDS)

    if manifest_data is not None:
        return manifest_data

    if last_json_error:
        raise HTTPException(500, "Invalid manifest JSON")

    raise HTTPException(404, "Manifest not found in shared storage")


def _get_local_manifest(task: Task) -> JSONResponse:
    task_id = task.task_id
    task_dir = _get_local_task_dir(task_id)
    manifest_resolved = _resolve_local_manifest_path(task_dir)
    manifest_data = _read_manifest_with_retry(manifest_resolved)

    if isinstance(manifest_data, dict):
        manifest_data.setdefault("task_id", task_id)

    _mark_warning_as_completed(task_id)
    return JSONResponse(content=manifest_data, headers={"X-Task-ID": task_id})


def _stream_local_file(task: Task, file_path: str) -> FileResponse:
    task_id = task.task_id
    output_dir = _get_local_output_dir(task_id)
    full_path = output_dir / file_path

    try:
        file_resolved = full_path.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid result file path")

    if file_resolved != output_dir and output_dir not in file_resolved.parents:
        raise HTTPException(400, "Invalid result file path")

    if not file_resolved.exists() or not file_resolved.is_file():
        raise HTTPException(404, "Result file not found in shared storage")

    _mark_warning_as_completed(task_id)
    return FileResponse(
        path=str(file_resolved),
        media_type="application/octet-stream",
        filename=file_resolved.name,
        headers={
            "X-Task-ID": task_id,
        },
    )


def _get_valid_task(task_id: str) -> Task:
    """Get and validate task."""
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    if task.status == "failed":
        raise HTTPException(400, f"Task failed: {task.error}")
    elif task.status != "completed" and task.status != "warning":
        # 425: Too Early
        raise HTTPException(425, f"Task not completed. Status: {task.status}")

    return task


def _get_task_runner(task: Task) -> Runner:
    """Get runner for task."""
    if task.runner_id not in runners:
        raise HTTPException(500, "Runner not available")
    return runners[task.runner_id]


async def _stream_runner_manifest(task: Task, runner: Runner) -> StreamingResponse:
    """Stream manifest from runner to client without loading it entirely into memory."""
    task_id = task.task_id
    client = httpx.AsyncClient()
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)

    try:
        response = await _fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task_id}",
            timeout,
            accept="application/json",
        )
        return _build_streaming_response(task_id, response, client, media_type="application/json")
    except HTTPException:
        await client.aclose()
        raise
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as e:
        await client.aclose()
        raise HTTPException(502, f"Error contacting runner: {str(e)}")
    except Exception as e:
        await client.aclose()
        raise HTTPException(500, f"Unexpected error: {str(e)}")


async def _stream_runner_file(task: Task, runner: Runner, file_path: str) -> StreamingResponse:
    """Stream a single result file from runner to client."""
    task_id = task.task_id
    client = httpx.AsyncClient()
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)
    encoded_path = quote(file_path, safe="/")

    try:
        response = await _fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task_id}/file/{encoded_path}",
            timeout,
            accept="application/octet-stream",
        )
        return _build_streaming_response(
            task_id,
            response,
            client,
            media_type=response.headers.get("content-type", "application/octet-stream"),
            filename=PathlibPath(file_path).name,
        )
    except HTTPException:
        await client.aclose()
        raise
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as e:
        await client.aclose()
        raise HTTPException(502, f"Error contacting runner: {str(e)}")
    except Exception as e:
        await client.aclose()
        raise HTTPException(500, f"Unexpected error: {str(e)}")


async def _fetch_runner_resource(
    client: httpx.AsyncClient,
    runner: Runner,
    url: str,
    timeout: httpx.Timeout,
    accept: str,
) -> httpx.Response:
    """Retrieve the runner response and raise HTTP errors early."""
    response = await client.get(
        url,
        headers=_runner_auth_headers(runner, accept=accept),
        timeout=timeout,
    )

    if response.status_code != 200:
        logger.error(f"Error fetching result from runner: {response.status_code}")
        await response.aclose()
        raise HTTPException(response.status_code, "Error fetching result from runner")

    return response


def _build_streaming_response(
    task_id: str,
    response: httpx.Response,
    client: httpx.AsyncClient,
    media_type: str | None = None,
    filename: str | None = None,
) -> StreamingResponse:
    """Create the streaming response and ensure resources are released."""

    async def content_generator():
        try:
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    _mark_warning_as_completed(task_id)

    headers: Dict[str, str] = {
        "X-Task-ID": task_id,
    }

    if filename:
        headers["Content-Disposition"] = f"attachment; filename={filename}"
    elif response.headers.get("content-disposition"):
        headers["Content-Disposition"] = response.headers.get("content-disposition")

    return StreamingResponse(
        content_generator(),
        media_type=media_type or response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
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
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def _get_runner_or_404(runner_id: str) -> Runner:
    if runner_id not in runners:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runner not found")
    return runners[runner_id]


def _verify_runner_token(runner: Runner, current_token: str) -> None:
    if runner.token != current_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token not authorized for this task"
        )


def _apply_task_completion_update(task: Task, notification: TaskCompletionNotification) -> None:
    task.status = notification.status
    task.updated_at = datetime.now().isoformat()

    if notification.status == "completed":
        task.error = None
    elif notification.error_message:
        task.error = notification.error_message

    if notification.script_output:
        task.script_output = notification.script_output


async def _handle_notify_callback(task: Task, notification: TaskCompletionNotification) -> None:
    if not task.notify_url:
        return

    try:
        notify_ok, notify_error = await _send_notify_callback(task, notification)
        if notify_ok:
            _restore_status_after_notify(notification.task_id, notification)
            return

        _set_notify_warning(
            notification.task_id,
            notify_error or "Notify URL callback failed: non-200 response",
        )
        asyncio.create_task(_retry_notify_callback(notification.task_id, notification))
    except Exception as e:
        _set_notify_warning(
            notification.task_id,
            f"Notify URL callback {task.notify_url} failed: server error - {str(e)}",
        )
        logger.error(f"Error during notify URL callback to {task.notify_url}: {str(e)}")
        asyncio.create_task(_retry_notify_callback(notification.task_id, notification))
