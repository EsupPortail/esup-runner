# manager/app/api/routes/admin.py
"""
Admin routes for Runner Manager API.
Handles administrative dashboard and monitoring endpoints.
"""

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.__version__ import __version__
from app.core import config as config_module
from app.core.auth import verify_admin
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_task as get_task_from_state
from app.core.state import get_tasks_snapshot, runners

# Configure logging
logger = setup_default_logging()

# Create admin router
router = APIRouter(prefix="/admin", tags=["Admin"], dependencies=[Depends(verify_admin)])

# Rate limiter for admin endpoints (stricter limit to prevent brute-force on Basic Auth)
limiter = Limiter(key_func=get_remote_address)

# Templates configuration
templates = Jinja2Templates(directory="app/web/templates")

# ======================================================
# Endpoints
# ======================================================


@router.get(
    "",
    summary="Admin Dashboard",
    include_in_schema=False,
    description="Main administration dashboard page",
)
@limiter.limit("10/minute")
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
    tasks_snapshot = get_tasks_snapshot()

    # Prepare runner data for dashboard
    runners_data = []
    for runner_id, runner in runners.items():
        status_value = (
            "online" if (datetime.now() - runner.last_heartbeat).total_seconds() < 60 else "offline"
        )

        runners_data.append(
            {
                "id": runner_id,
                "url": runner.url,
                "status": status_value,
                "availability": runner.availability,
                "task_types": runner.task_types,
                "last_heartbeat": runner.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S"),
                "age_seconds": int((datetime.now() - runner.last_heartbeat).total_seconds()),
            }
        )

    tasks_data = []
    for task_id, task in tasks_snapshot.items():
        tasks_data.append(
            {
                "id": task_id,
                "runner_id": task.runner_id,
                "status": task.status,
                "task_type": getattr(task, "task_type", None),
                "created_at": task.created_at,
            }
        )

    # Ordered by created_at
    tasks_data_sorted = sorted(
        tasks_data,
        key=lambda x: str(x["created_at"] or ""),
        reverse=True,
    )

    month_key = datetime.now().strftime("%Y-%m")
    tasks_this_month = 0
    csv_path = Path("data") / "task_stats.csv"
    if csv_path.exists():
        try:
            with csv_path.open("r", encoding="utf-8") as file:
                reader = csv.DictReader(file)
                for row in reader:
                    dae_value = row.get("dae") or row.get("date")
                    if dae_value and dae_value.startswith(month_key):
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
            "admin_count": len(config.ADMIN_USERS),
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    dark_mode = request.cookies.get("theme") == "dark"

    return templates.TemplateResponse(
        request,
        "runner_detail.html",
        {
            "request": request,
            "runner": runner,
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

    # Prepare admin users list (only usernames, not passwords)
    admin_users = list(config.ADMIN_USERS.keys())

    # Prepare authorized tokens list with preview (show first 10 and last 4 chars)
    authorized_tokens = []
    for token_name, token_value in config.AUTHORIZED_TOKENS.items():
        if len(token_value) > 20:
            token_preview = f"{token_value[:10]}...{token_value[-4:]}"
        else:
            token_preview = f"{token_value[:4]}..." if len(token_value) > 4 else "***"
        authorized_tokens.append((token_name, token_preview))

    return templates.TemplateResponse(
        request,
        "credentials.html",
        {
            "request": request,
            "admin_users": admin_users,
            "admin_count": len(admin_users),
            "authorized_tokens": authorized_tokens,
            "tokens_count": len(authorized_tokens),
            "api_docs_visibility": config.API_DOCS_VISIBILITY,
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )


@router.post(
    "/reload-config",
    summary="Reload configuration",
    include_in_schema=False,
    description="Reload .env configuration and return the updated visibility flag",
)
async def reload_config_endpoint(_: bool = Depends(verify_admin)):
    """Reload .env and return key values to confirm refresh."""
    new_config = config_module.reload_config_env()
    return {
        "api_docs_visibility": new_config.API_DOCS_VISIBILITY,
        "authorized_tokens": list(new_config.AUTHORIZED_TOKENS.keys()),
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

    # Get first authorized token to use for protected documentation access
    api_token = None
    if config.API_DOCS_VISIBILITY == "private" and config.AUTHORIZED_TOKENS:
        # Use the first available token
        api_token = next(iter(config.AUTHORIZED_TOKENS.values()))

    return templates.TemplateResponse(
        request,
        "documentation.html",
        {
            "request": request,
            "api_docs_visibility": config.API_DOCS_VISIBILITY,
            "api_token": api_token,
            "dark_mode_enabled": dark_mode,
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": __version__,
        },
    )
