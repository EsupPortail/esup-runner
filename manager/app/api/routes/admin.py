# manager/app/api/routes/admin.py
"""
Admin routes for Runner Manager API.
Handles administrative dashboard and monitoring endpoints.
"""

import csv
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.__version__ import __version__
from app.api.routes.task import (
    _NON_DELETABLE_TASK_STATUSES,
    _NON_RESTARTABLE_TASK_STATUSES,
    _STOPPABLE_TASK_STATUSES,
)
from app.core import config as config_module
from app.core.auth import OPENAPI_TOKEN_COOKIE_NAME, build_openapi_cookie_value, verify_admin
from app.core.config import config
from app.core.passwords import BcryptPasswordContext
from app.core.setup_logging import setup_default_logging
from app.core.state import get_task as get_task_from_state
from app.core.state import get_tasks_snapshot, runners

# Configure logging
logger = setup_default_logging()

# Create admin router
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(verify_admin)])

# Rate limiter for admin endpoints. The dashboard has built-in auto-refresh, so
# keep this above normal browsing cadence while the global limiter still applies.
limiter = Limiter(key_func=get_remote_address)

# Templates configuration
templates = Jinja2Templates(directory="app/web/templates")

_ATTENTION_TASK_STATUSES = ("failed", "warning", "timeout")
_ATTENTION_ITEMS_LIMIT = 5
_ATTENTION_ERROR_LABEL_LIMIT = 160
_STALE_RUNNING_TASK_THRESHOLD_MINUTES = 300
_TASK_AGE_WARNING_THRESHOLD_MINUTES = 240
_TASK_AGE_CREATED_AT_LABELS = {
    "pending": "Waiting",
    "running": "Started",
}
_TASK_AGE_UPDATED_AT_LABELS = {
    "completed": "Completed",
    "failed": "Failed",
    "timeout": "Timed out",
    "warning": "Warning",
}
_ADMIN_DASHBOARD_RATE_LIMIT = "60/minute"
_RUNNER_STATUS_TIMEOUT = httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=2.0)
_RUNNER_ONLINE_HEARTBEAT_SECONDS = 60
_TOKEN_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_ADMIN_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]+$")
_TOKEN_ENV_PREFIX = "AUTHORIZED_TOKENS__"
_ADMIN_ENV_PREFIX = "ADMIN_USERS__"
_DEFAULT_GENERATED_TOKEN_LENGTH = 32
_MIN_GENERATED_TOKEN_LENGTH = 16
_MAX_GENERATED_TOKEN_LENGTH = 128
_PASSWORD_CONTEXT = BcryptPasswordContext()

# ======================================================
# Endpoints
# ======================================================


def _format_datetime_without_milliseconds(value: str | None) -> str:
    """Format ISO datetime values to second precision for compact UI display."""
    if not value:
        return ""

    raw = str(value).strip()
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(normalized).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        # Fallback for non-ISO inputs: strip fractional part when present.
        return raw.split(".", 1)[0].replace("T", " ")


def _format_attention_error_label(value: Any, limit: int = _ATTENTION_ERROR_LABEL_LIMIT) -> str:
    """Return a compact one-line error label for the dashboard."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    for line in raw.splitlines():
        label = " ".join(line.split())
        if label:
            break

    if len(label) <= limit:
        return label
    return label[: limit - 3].rstrip() + "..."


def _format_secret_preview(value: Any) -> str:
    """Return a non-sensitive preview for configured secrets."""
    raw = str(value or "").strip()
    if not raw:
        return "not configured"
    if len(raw) > 20:
        return f"{raw[:10]}...{raw[-4:]}"
    if len(raw) > 4:
        return f"{raw[:4]}..."
    return "***"


def _is_valid_token_label(label: str) -> bool:
    """Return True when token label can be used in .env key names."""
    return bool(_TOKEN_LABEL_PATTERN.fullmatch(label))


def _is_valid_admin_label(label: str) -> bool:
    """Return True when admin label can be used in .env key names."""
    return bool(_ADMIN_LABEL_PATTERN.fullmatch(label))


def _generate_authorized_token(length: int = _DEFAULT_GENERATED_TOKEN_LENGTH) -> str:
    """Generate a secure token using the same strategy as scripts/generate_token.py."""
    bounded_length = max(_MIN_GENERATED_TOKEN_LENGTH, min(_MAX_GENERATED_TOKEN_LENGTH, length))
    return secrets.token_urlsafe(bounded_length)


def _hash_admin_password(password: str) -> str:
    """Hash an admin password using the same policy as scripts/generate_password.py."""
    return _PASSWORD_CONTEXT.hash(password)


def _read_env_lines(env_path: Path) -> list[str]:
    """Read .env lines, returning an empty list when the file does not yet exist."""
    if not env_path.exists():
        return []
    return env_path.read_text(encoding="utf-8").splitlines()


def _write_env_lines(env_path: Path, lines: list[str]) -> None:
    """Write .env lines with a trailing newline for POSIX-friendly formatting."""
    env_path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if content:
        content += "\n"
    env_path.write_text(content, encoding="utf-8")


def _upsert_authorized_token_in_env(token_name: str, token_value: str) -> None:
    """Insert or update one AUTHORIZED_TOKENS entry in .env."""
    env_path = config_module.get_env_file_path()
    lines = _read_env_lines(env_path)
    token_prefix = f"{_TOKEN_ENV_PREFIX}{token_name}="

    for index, line in enumerate(lines):
        if line.startswith(token_prefix):
            lines[index] = f"{token_prefix}{token_value}"
            _write_env_lines(env_path, lines)
            return

    insert_index = len(lines)
    for index, line in enumerate(lines):
        if line.startswith(_TOKEN_ENV_PREFIX):
            insert_index = index + 1

    lines.insert(insert_index, f"{token_prefix}{token_value}")
    _write_env_lines(env_path, lines)


def _delete_authorized_token_from_env(token_name: str) -> bool:
    """Delete one AUTHORIZED_TOKENS entry from .env."""
    env_path = config_module.get_env_file_path()
    if not env_path.exists():
        return False

    lines = _read_env_lines(env_path)
    token_prefix = f"{_TOKEN_ENV_PREFIX}{token_name}="
    filtered_lines = [line for line in lines if not line.startswith(token_prefix)]
    if len(filtered_lines) == len(lines):
        return False

    _write_env_lines(env_path, filtered_lines)
    return True


def _upsert_admin_user_in_env(admin_name: str, hashed_password: str) -> None:
    """Insert or update one ADMIN_USERS entry in .env."""
    env_path = config_module.get_env_file_path()
    lines = _read_env_lines(env_path)
    admin_prefix = f"{_ADMIN_ENV_PREFIX}{admin_name}="
    admin_line = f'{admin_prefix}"{hashed_password}"'

    for index, line in enumerate(lines):
        if line.startswith(admin_prefix):
            lines[index] = admin_line
            _write_env_lines(env_path, lines)
            return

    insert_index = len(lines)
    for index, line in enumerate(lines):
        if line.startswith(_ADMIN_ENV_PREFIX):
            insert_index = index + 1

    lines.insert(insert_index, admin_line)
    _write_env_lines(env_path, lines)


def _delete_admin_user_from_env(admin_name: str) -> bool:
    """Delete one ADMIN_USERS entry from .env."""
    env_path = config_module.get_env_file_path()
    if not env_path.exists():
        return False

    lines = _read_env_lines(env_path)
    admin_prefix = f"{_ADMIN_ENV_PREFIX}{admin_name}="
    filtered_lines = [line for line in lines if not line.startswith(admin_prefix)]
    if len(filtered_lines) == len(lines):
        return False

    _write_env_lines(env_path, filtered_lines)
    return True


def _credentials_redirect(
    feedback: str,
    token_name: str = "",
    admin_name: str = "",
) -> RedirectResponse:
    """Build a redirect response to the credentials page with UI feedback hints."""
    query_params = {"feedback": feedback}
    if token_name:
        query_params["token_name"] = token_name
    if admin_name:
        query_params["admin_name"] = admin_name
    query = urlencode(query_params)
    return RedirectResponse(
        url=f"/admin/credentials?{query}", status_code=status.HTTP_303_SEE_OTHER
    )


def _build_credentials_feedback(request: Request) -> dict[str, str]:
    """Build user-facing feedback messages from query parameters."""
    feedback = str(request.query_params.get("feedback", "") or "").strip()
    token_name = str(request.query_params.get("token_name", "") or "").strip()
    admin_name = str(request.query_params.get("admin_name", "") or "").strip()
    safe_name = token_name if _is_valid_token_label(token_name) else "token"
    safe_admin = admin_name if _is_valid_admin_label(admin_name) else "administrator"

    token_feedback_messages = {
        "token_created": {
            "level": "success",
            "message": f"Token '{safe_name}' created. .env updated and manager config reloaded.",
        },
        "token_deleted": {
            "level": "success",
            "message": f"Token '{safe_name}' deleted. .env updated and manager config reloaded.",
        },
        "token_exists": {
            "level": "warning",
            "message": f"Token label '{safe_name}' already exists.",
        },
        "token_missing": {
            "level": "warning",
            "message": f"Token label '{safe_name}' was not found in .env.",
        },
        "token_invalid": {
            "level": "danger",
            "message": "Invalid token label. Use only letters, numbers, and underscores.",
        },
        "token_write_failed": {
            "level": "danger",
            "message": "Unable to write token changes to .env. Check file permissions.",
        },
    }

    admin_feedback_messages = {
        "admin_created": {
            "level": "success",
            "message": (
                f"Administrator '{safe_admin}' created. .env updated and manager config reloaded."
            ),
        },
        "admin_deleted": {
            "level": "success",
            "message": (
                f"Administrator '{safe_admin}' deleted. .env updated and manager config reloaded."
            ),
        },
        "admin_exists": {
            "level": "warning",
            "message": f"Administrator label '{safe_admin}' already exists.",
        },
        "admin_missing": {
            "level": "warning",
            "message": f"Administrator label '{safe_admin}' was not found in .env.",
        },
        "admin_invalid": {
            "level": "danger",
            "message": "Invalid administrator label. Use only letters, numbers, underscores, ., -, and @.",
        },
        "admin_password_empty": {
            "level": "danger",
            "message": "Password cannot be empty.",
        },
        "admin_password_mismatch": {
            "level": "danger",
            "message": "Passwords do not match.",
        },
        "admin_write_failed": {
            "level": "danger",
            "message": "Unable to write administrator changes to .env. Check file permissions.",
        },
    }

    if feedback in token_feedback_messages:
        return token_feedback_messages[feedback]
    if feedback in admin_feedback_messages:
        return admin_feedback_messages[feedback]

    return {"level": "", "message": ""}


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO datetime value into a local naive datetime for dashboard comparisons."""
    raw = str(value or "").strip()
    if not raw:
        return None

    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _build_runner_heartbeat_metadata(runner: Any, now: datetime | None = None) -> dict[str, Any]:
    """Build heartbeat-derived runner status metadata for admin pages."""
    raw_heartbeat = getattr(runner, "last_heartbeat", None)
    heartbeat: datetime | None
    if isinstance(raw_heartbeat, datetime):
        heartbeat = (
            raw_heartbeat.astimezone().replace(tzinfo=None)
            if raw_heartbeat.tzinfo is not None
            else raw_heartbeat
        )
    else:
        heartbeat = _parse_datetime(raw_heartbeat)

    if heartbeat is None:
        return {
            "status": "offline",
            "last_heartbeat": _format_datetime_without_milliseconds(str(raw_heartbeat or "")),
            "age_seconds": 0,
        }

    reference_time = now or datetime.now()
    age_seconds = max(0, int((reference_time - heartbeat).total_seconds()))
    return {
        "status": "online" if age_seconds < _RUNNER_ONLINE_HEARTBEAT_SECONDS else "offline",
        "last_heartbeat": heartbeat.strftime("%Y-%m-%d %H:%M:%S"),
        "age_seconds": age_seconds,
    }


def _format_duration_label(total_seconds: int) -> str:
    """Format a duration for compact dashboard labels."""
    minutes = max(1, int(total_seconds // 60))
    days, remaining_minutes_after_days = divmod(minutes, 24 * 60)
    hours, remaining_minutes = divmod(remaining_minutes_after_days, 60)

    if days > 0:
        if hours == 0:
            return f"{days}d"
        return f"{days}d {hours}h"
    if hours <= 0:
        return f"{minutes}m"
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def _build_task_age_metadata(
    status: Any,
    created_at: Any,
    updated_at: Any,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build compact task age metadata for dashboard display."""
    normalized_status = str(status or "").lower()
    label_prefix = _TASK_AGE_CREATED_AT_LABELS.get(normalized_status)
    timestamp_value = created_at
    uses_created_at = label_prefix is not None

    if label_prefix is None:
        label_prefix = _TASK_AGE_UPDATED_AT_LABELS.get(normalized_status)
        timestamp_value = updated_at

    if label_prefix is None:
        return {"label": "", "is_warning": False}

    timestamp = _parse_datetime(timestamp_value)
    if timestamp is None:
        return {"label": "", "is_warning": False}

    reference_time = now or datetime.now()
    age_seconds = max(0, int((reference_time - timestamp).total_seconds()))
    duration = _format_duration_label(age_seconds)
    suffix = "" if normalized_status == "pending" else " ago"
    is_warning = uses_created_at and (age_seconds >= _TASK_AGE_WARNING_THRESHOLD_MINUTES * 60)

    return {
        "label": f"{label_prefix} {duration}{suffix}",
        "is_warning": is_warning,
    }


def _build_task_detail_actions(task: Any) -> dict[str, Any]:
    """Build task detail action state while mirroring task API constraints."""
    status_value = str(getattr(task, "status", "") or "").lower()
    can_delete = status_value not in _NON_DELETABLE_TASK_STATUSES
    can_restart = status_value not in _NON_RESTARTABLE_TASK_STATUSES
    can_stop = status_value in _STOPPABLE_TASK_STATUSES

    return {
        "can_delete": can_delete,
        "can_restart": can_restart,
        "can_stop": can_stop,
        "delete_disabled_reason": (
            "" if can_delete else f"Task status '{status_value}' cannot be deleted"
        ),
        "restart_disabled_reason": (
            "" if can_restart else f"Task status '{status_value}' cannot be restarted"
        ),
        "stop_disabled_reason": (
            "" if can_stop else f"Task status '{status_value}' cannot be stopped"
        ),
    }


def _runner_status_headers(runner: Any) -> dict[str, str]:
    """Return optional auth headers for live runner status requests."""
    token = str(getattr(runner, "token", "") or "").strip()
    return {"X-API-Token": token} if token else {}


async def _fetch_runner_live_status(runner: Any) -> dict[str, Any]:
    """Fetch live status from a runner for the admin detail page."""
    url = f"{str(getattr(runner, 'url', '')).rstrip('/')}/runner/status"
    try:
        async with httpx.AsyncClient(timeout=_RUNNER_STATUS_TIMEOUT) as client:
            response = await client.get(url, headers=_runner_status_headers(runner))
    except (httpx.RequestError, httpx.TimeoutException) as exc:
        logger.warning(f"Unable to fetch runner status from {url}: {exc}")
        return {
            "available": False,
            "url": url,
            "error": "Runner status request failed.",
            "disk_usage": None,
        }

    if response.status_code != 200:
        return {
            "available": False,
            "url": url,
            "error": f"Runner status returned HTTP {response.status_code}.",
            "disk_usage": None,
        }

    try:
        payload = response.json()
    except ValueError:
        return {
            "available": False,
            "url": url,
            "error": "Runner status returned invalid JSON.",
            "disk_usage": None,
        }

    if not isinstance(payload, dict):
        return {
            "available": False,
            "url": url,
            "error": "Runner status returned an unexpected payload.",
            "disk_usage": None,
        }

    disk_usage = payload.get("disk_usage")
    return {
        "available": True,
        "url": url,
        "error": "",
        "payload": payload,
        "disk_usage": disk_usage if isinstance(disk_usage, dict) else None,
    }


def _build_attention_summary(
    runners_data: list[dict[str, Any]],
    tasks_data_sorted: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build compact dashboard data for runners and tasks needing attention."""
    reference_time = now or datetime.now()
    offline_runners = sorted(
        [runner for runner in runners_data if runner["status"] == "offline"],
        key=lambda runner: runner["age_seconds"],
        reverse=True,
    )
    attention_task_status_counts = {
        status: len([task for task in tasks_data_sorted if task["status"] == status])
        for status in _ATTENTION_TASK_STATUSES
    }
    attention_tasks = [
        task for task in tasks_data_sorted if task["status"] in _ATTENTION_TASK_STATUSES
    ]
    stale_running_tasks: list[dict[str, Any]] = []
    stale_threshold_seconds = _STALE_RUNNING_TASK_THRESHOLD_MINUTES * 60
    for task in tasks_data_sorted:
        if task["status"] != "running":
            continue

        updated_at = _parse_datetime(task.get("updated_at"))
        if updated_at is None:
            continue

        age_seconds = int((reference_time - updated_at).total_seconds())
        if age_seconds < stale_threshold_seconds:
            continue

        stale_task = dict(task)
        stale_task["stale_running_label"] = (
            f"Running without update for {_format_duration_label(age_seconds)}."
        )
        stale_running_tasks.append(stale_task)

    attention_tasks.extend(stale_running_tasks)

    return {
        "attention_count": len(offline_runners) + len(attention_tasks),
        "attention_task_status_counts": attention_task_status_counts,
        "stale_running_tasks_count": len(stale_running_tasks),
        "attention_tasks_count": len(attention_tasks),
        "attention_tasks": attention_tasks[:_ATTENTION_ITEMS_LIMIT],
        "offline_runners": offline_runners[:_ATTENTION_ITEMS_LIMIT],
        "offline_runners_count": len(offline_runners),
    }


@router.get(
    "",
    summary="Admin Dashboard",
    include_in_schema=False,
    description="Main administration dashboard page",
)
@limiter.limit(_ADMIN_DASHBOARD_RATE_LIMIT)
async def admin_dashboard(request: Request):
    """
    Main administration dashboard page.

    Note: include_in_schema=False removes this from OpenAPI docs
    as it's a HTML page, not an API endpoint.

    Args:
        request: FastAPI request object

    Returns:
        TemplateResponse: Rendered admin dashboard
    """
    dark_mode = request.cookies.get("theme") == "dark"
    now = datetime.now()
    tasks_snapshot = get_tasks_snapshot()

    # Prepare runner data for dashboard
    runners_data = []
    for runner_id, runner in runners.items():
        heartbeat_metadata = _build_runner_heartbeat_metadata(runner, now=now)

        runners_data.append(
            {
                "id": runner_id,
                "url": runner.url,
                "status": heartbeat_metadata["status"],
                "availability": runner.availability,
                "task_types": runner.task_types,
                "last_heartbeat": heartbeat_metadata["last_heartbeat"],
                "age_seconds": heartbeat_metadata["age_seconds"],
            }
        )

    tasks_data = []
    for task_id, task in tasks_snapshot.items():
        params = getattr(task, "parameters", {}) or {}
        age_metadata = _build_task_age_metadata(
            task.status,
            task.created_at,
            task.updated_at,
            now=now,
        )
        tasks_data.append(
            {
                "id": task_id,
                "runner_id": task.runner_id,
                "status": task.status,
                "task_type": getattr(task, "task_type", None),
                "created_at": task.created_at,
                "created_at_display": _format_datetime_without_milliseconds(task.created_at),
                "updated_at": task.updated_at,
                "updated_at_display": _format_datetime_without_milliseconds(task.updated_at),
                "error_label": _format_attention_error_label(getattr(task, "error", None)),
                "age_label": age_metadata["label"],
                "age_is_warning": age_metadata["is_warning"],
                "video_id": params.get("video_id"),
            }
        )

    # Ordered by created_at
    tasks_data_sorted = sorted(
        tasks_data,
        key=lambda x: str(x["created_at"] or ""),
        reverse=True,
    )
    attention_summary = _build_attention_summary(runners_data, tasks_data_sorted, now=now)

    month_key = now.strftime("%Y-%m")
    tasks_this_month = 0
    csv_path = Path("data") / "task_stats.csv"
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                for row in reader:
                    date_value = row.get("date") or row.get("dae")
                    if date_value and date_value.startswith(month_key):
                        tasks_this_month += 1
        except Exception as exc:
            logger.error(f"Failed to read task stats CSV: {exc}")
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "runners": runners_data,
            "tasks": tasks_data_sorted,
            "total_runners": len(runners_data),
            "online_runners": len([r for r in runners_data if r["status"] == "online"]),
            "total_tasks": len(tasks_data_sorted),
            "tasks_this_month": tasks_this_month,
            **attention_summary,
            "admin_count": len(config.ADMIN_USERS),
            "dark_mode_enabled": dark_mode,
            "last_update": now.strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )


@router.get(
    "/task/{task_id}",
    summary="Task detail",
    include_in_schema=False,
    description="Task detail page",
)
async def get_task_detail(request: Request, task_id: str):
    """
    Task detail page.

    Args:
        request: FastAPI request object
        task_id: task id

    Returns:
        TemplateResponse: Rendered task detail page
    """
    # Check if runner exists
    task = get_task_from_state(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Task with ID '{task_id}' not found"
        )

    dark_mode = request.cookies.get("theme") == "dark"

    return templates.TemplateResponse(
        request,
        "task_detail.html",
        {
            "request": request,
            "task": task,
            "task_actions": _build_task_detail_actions(task),
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )


@router.get(
    "/runner/{runner_id}",
    summary="Runner detail",
    include_in_schema=False,
    description="Runner detail page",
)
async def get_runner_detail(request: Request, runner_id: str):
    """
    Runner detail page.

    Args:
        request: FastAPI request object
        runner_id: runner id

    Returns:
        TemplateResponse: Rendered runner detail page
    """
    # Check if runner exists
    if runner_id not in runners:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Runner with ID '{runner_id}' not found"
        )

    runner = runners[runner_id]
    runner_live_status = await _fetch_runner_live_status(runner)
    heartbeat_metadata = _build_runner_heartbeat_metadata(runner)

    dark_mode = request.cookies.get("theme") == "dark"

    return templates.TemplateResponse(
        request,
        "runner_detail.html",
        {
            "request": request,
            "runner": runner,
            "runner_status": heartbeat_metadata["status"],
            "runner_last_heartbeat": heartbeat_metadata["last_heartbeat"],
            "runner_age_seconds": heartbeat_metadata["age_seconds"],
            "runner_token_preview": _format_secret_preview(getattr(runner, "token", "")),
            "runner_live_status": runner_live_status,
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )


@router.get(
    "/tasks",
    summary="Tasks Management",
    include_in_schema=False,
    description="Task supervision and management page",
)
async def admin_tasks(request: Request) -> Any:
    """
    Task supervision and management page.

    Note: include_in_schema=False removes this from OpenAPI docs
    as it's a HTML page, not an API endpoint.

    Args:
        request: FastAPI request object

    Returns:
        TemplateResponse: Rendered tasks dashboard
    """
    dark_mode = request.cookies.get("theme") == "dark"

    all_tasks_list = list(get_tasks_snapshot().values())
    available_statuses = ["pending", "running", "completed", "failed", "warning", "timeout"]
    available_task_types = sorted(
        {t.task_type for t in all_tasks_list if getattr(t, "task_type", None)}
    )

    status_counts = {s: 0 for s in available_statuses}
    for task in all_tasks_list:
        status_counts[task.status] = status_counts.get(task.status, 0) + 1

    display_tasks = [
        {
            "id": task.task_id,
            "status": task.status,
            "task_type": task.task_type,
            "source_url": task.source_url,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        for task in all_tasks_list
    ]
    display_tasks.sort(key=lambda x: x["updated_at"] or "", reverse=True)

    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "request": request,
            "tasks": display_tasks,
            "total_tasks": len(all_tasks_list),
            "available_statuses": available_statuses,
            "available_task_types": available_task_types,
            "status_counts": status_counts,
            "current_filters": {
                "statuses": [],
                "task_type": None,
                "search": None,
                "limit": 1000,
                "auto_refresh": 0,
            },
            "now": datetime.now(),
            "version": __version__,
            "dark_mode_enabled": dark_mode,
        },
    )


@router.post(
    "/toggle-theme",
    summary="Toggle theme",
    include_in_schema=False,
    description="Toggle light/dark theme",
)
def toggle_theme(request: Request):
    """Toggle the admin UI theme cookie and redirect to the dashboard."""
    current = request.cookies.get("theme")
    new_theme = "light" if current == "dark" else "dark"
    resp = RedirectResponse(url="/admin", status_code=303)
    # 30 days
    resp.set_cookie(key="theme", value=new_theme, max_age=60 * 60 * 24 * 30)
    return resp


@router.get(
    "/credentials",
    summary="Credentials Configuration",
    include_in_schema=False,
    description="Display administrators and authorized tokens configured in .env file",
)
async def credentials_page(request: Request):
    """
    Display credentials configuration page.

    Shows:
    - Admin users configured in .env (ADMIN_USERS__*)
    - Authorized tokens for API access (AUTHORIZED_TOKENS__*)

    Note: include_in_schema=False removes this from OpenAPI docs
    as it's a HTML page, not an API endpoint.

    Args:
        request: FastAPI request object

    Returns:
        TemplateResponse: Rendered credentials configuration page
    """
    dark_mode = request.cookies.get("theme") == "dark"
    feedback = _build_credentials_feedback(request)

    # Prepare admin users list with preview and value (value is only used for clipboard copy).
    admin_users = []
    for admin_name, hashed_password in sorted(config.ADMIN_USERS.items()):
        admin_users.append(
            {
                "name": admin_name,
                "preview": _format_secret_preview(hashed_password),
                "value": hashed_password,
            }
        )

    # Prepare authorized tokens list with preview and value (value is only used for clipboard copy).
    authorized_tokens = []
    for token_name, token_value in sorted(config.AUTHORIZED_TOKENS.items()):
        authorized_tokens.append(
            {
                "name": token_name,
                "preview": _format_secret_preview(token_value),
                "value": token_value,
            }
        )

    return templates.TemplateResponse(
        request,
        "credentials.html",
        {
            "request": request,
            "admin_users": admin_users,
            "admin_count": len(admin_users),
            "authorized_tokens": authorized_tokens,
            "feedback_level": feedback["level"],
            "feedback_message": feedback["message"],
            "tokens_count": len(authorized_tokens),
            "api_docs_visibility": config.API_DOCS_VISIBILITY,
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )


@router.post(
    "/credentials/admins",
    summary="Generate admin password",
    include_in_schema=False,
    description="Hash admin password, persist a new ADMIN_USERS entry in .env, and reload config",
)
async def create_admin_user(
    admin_name: str = Form(...),
    admin_password: str = Form(""),
    admin_password_confirm: str = Form(""),
    _: bool = Depends(verify_admin),
):
    """Create or update one admin user from the credentials page."""
    normalized_name = str(admin_name or "").strip()
    password = str(admin_password or "")
    password_confirm = str(admin_password_confirm or "")

    if not _is_valid_admin_label(normalized_name):
        return _credentials_redirect("admin_invalid")

    if not password.strip():
        return _credentials_redirect("admin_password_empty", admin_name=normalized_name)

    if password != password_confirm:
        return _credentials_redirect("admin_password_mismatch", admin_name=normalized_name)

    if normalized_name in config.ADMIN_USERS:
        return _credentials_redirect("admin_exists", admin_name=normalized_name)

    hashed_password = _hash_admin_password(password)
    try:
        _upsert_admin_user_in_env(normalized_name, hashed_password)
    except OSError as exc:
        logger.error(f"Failed to write generated admin '{normalized_name}' to .env: {exc}")
        return _credentials_redirect("admin_write_failed", admin_name=normalized_name)

    config_module.reload_config_env()
    config_module.publish_config_reload_event()
    return _credentials_redirect("admin_created", admin_name=normalized_name)


@router.post(
    "/credentials/admins/{admin_name}/delete",
    summary="Delete administrator",
    include_in_schema=False,
    description="Delete one ADMIN_USERS entry from .env and reload config",
)
async def delete_admin_user(admin_name: str, _: bool = Depends(verify_admin)):
    """Delete one admin user from .env from the credentials page."""
    normalized_name = str(admin_name or "").strip()
    if not _is_valid_admin_label(normalized_name):
        return _credentials_redirect("admin_invalid")

    try:
        admin_deleted = _delete_admin_user_from_env(normalized_name)
    except OSError as exc:
        logger.error(f"Failed to delete admin '{normalized_name}' from .env: {exc}")
        return _credentials_redirect("admin_write_failed", admin_name=normalized_name)

    if not admin_deleted:
        return _credentials_redirect("admin_missing", admin_name=normalized_name)

    config_module.reload_config_env()
    config_module.publish_config_reload_event()
    return _credentials_redirect("admin_deleted", admin_name=normalized_name)


@router.post(
    "/credentials/tokens",
    summary="Generate authorized token",
    include_in_schema=False,
    description="Generate a new AUTHORIZED_TOKENS entry, persist it in .env, and reload config",
)
async def create_authorized_token(
    token_name: str = Form(...),
    token_length: int = Form(_DEFAULT_GENERATED_TOKEN_LENGTH),
    _: bool = Depends(verify_admin),
):
    """Generate and persist one API token in .env from the admin credentials page."""
    normalized_name = str(token_name or "").strip()
    if not _is_valid_token_label(normalized_name):
        return _credentials_redirect("token_invalid")

    if normalized_name in config.AUTHORIZED_TOKENS:
        return _credentials_redirect("token_exists", normalized_name)

    generated_token = _generate_authorized_token(token_length)
    try:
        _upsert_authorized_token_in_env(normalized_name, generated_token)
    except OSError as exc:
        logger.error(f"Failed to write generated token '{normalized_name}' to .env: {exc}")
        return _credentials_redirect("token_write_failed", normalized_name)

    config_module.reload_config_env()
    config_module.publish_config_reload_event()
    return _credentials_redirect("token_created", normalized_name)


@router.post(
    "/credentials/tokens/{token_name}/delete",
    summary="Delete authorized token",
    include_in_schema=False,
    description="Delete one AUTHORIZED_TOKENS entry from .env and reload config",
)
async def delete_authorized_token(token_name: str, _: bool = Depends(verify_admin)):
    """Delete one API token from .env from the admin credentials page."""
    normalized_name = str(token_name or "").strip()
    if not _is_valid_token_label(normalized_name):
        return _credentials_redirect("token_invalid")

    try:
        token_deleted = _delete_authorized_token_from_env(normalized_name)
    except OSError as exc:
        logger.error(f"Failed to delete token '{normalized_name}' from .env: {exc}")
        return _credentials_redirect("token_write_failed", normalized_name)

    if not token_deleted:
        return _credentials_redirect("token_missing", normalized_name)

    config_module.reload_config_env()
    config_module.publish_config_reload_event()
    return _credentials_redirect("token_deleted", normalized_name)


@router.post(
    "/reload-config",
    summary="Reload configuration",
    include_in_schema=False,
    description="Reload .env configuration and return the updated visibility flag",
)
async def reload_config_endpoint(_: bool = Depends(verify_admin)):
    """Reload .env and return key values to confirm refresh."""
    new_config = config_module.reload_config_env()
    config_module.publish_config_reload_event()
    return {
        "api_docs_visibility": new_config.API_DOCS_VISIBILITY,
        "authorized_tokens": list(new_config.AUTHORIZED_TOKENS.keys()),
        "authorized_tokens_count": len(new_config.AUTHORIZED_TOKENS),
        "admin_users": list(new_config.ADMIN_USERS.keys()),
        "admin_users_count": len(new_config.ADMIN_USERS),
    }


@router.get(
    "/docs",
    summary="API Documentation",
    include_in_schema=False,
    description="API documentation page with links to all available documentation formats",
)
async def documentation_page(request: Request):
    """
    API documentation page.

    Displays links to:
    - OpenAPI JSON specification
    - Swagger UI interactive documentation
    - ReDoc documentation
    - API root information endpoint

    Note: include_in_schema=False removes this from OpenAPI docs
    as it's a HTML page, not an API endpoint.

    Args:
        request: FastAPI request object

    Returns:
        TemplateResponse: Rendered documentation page
    """
    dark_mode = request.cookies.get("theme") == "dark"

    # Get first authorized token to bootstrap OpenAPI docs auth via cookie.
    api_token = None
    if config.API_DOCS_VISIBILITY == "private" and config.AUTHORIZED_TOKENS:
        # Use the first available token.
        api_token = next(iter(config.AUTHORIZED_TOKENS.values()))

    response = templates.TemplateResponse(
        request,
        "documentation.html",
        {
            "request": request,
            "api_docs_visibility": config.API_DOCS_VISIBILITY,
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )
    if api_token:
        cookie_value = build_openapi_cookie_value(api_token)
        if cookie_value:
            response.set_cookie(
                key=OPENAPI_TOKEN_COOKIE_NAME,
                value=cookie_value,
                max_age=config.OPENAPI_COOKIE_MAX_AGE_SECONDS,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="lax",
                path="/",
            )
        else:
            response.delete_cookie(key=OPENAPI_TOKEN_COOKIE_NAME, path="/")
    else:
        response.delete_cookie(key=OPENAPI_TOKEN_COOKIE_NAME, path="/")
    return response
