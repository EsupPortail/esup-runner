# manager/app/api/routes/api.py
"""
API routes for Runner Manager.
Handles core API endpoints for runners and tasks.
"""

from datetime import datetime

from fastapi import APIRouter, Depends

from app.__version__ import (
    __author__,
    __description__,
    __email__,
    __license__,
    __version__,
    __version_info__,
)
from app.core.auth import verify_token
from app.core.setup_logging import setup_default_logging
from app.core.state import get_tasks_snapshot, runners

# Configure logging
logger = setup_default_logging()

# Create API router
router = APIRouter(prefix="/api", tags=["API"])

# ======================================================
# Endpoints
# ======================================================


@router.get(
    "/version",
    response_model=dict,
    summary="Get API version",
    description="Returns version information and metadata about the Runner Manager API",
    tags=["API"],
    dependencies=[Depends(verify_token)],
)
async def get_version() -> dict:
    """
    Get version information about the Runner Manager API.

    Returns:
        dict: Version information including version number, author, license, etc.
    """
    return {
        "version": __version__,
        "version_info": {
            "major": __version_info__[0],
            "minor": __version_info__[1],
            "patch": __version_info__[2],
        },
        "description": __description__,
        "author": __author__,
        "email": __email__,
        "license": __license__,
    }


@router.get(
    "/tasks",
    response_model=dict,
    summary="Get tasks status (API)",
    description="API endpoint to get task status for AJAX requests",
    tags=["API"],
    dependencies=[Depends(verify_token)],
)
async def get_tasks_api() -> dict:
    """
    API endpoint to get task status for AJAX requests.

    Returns:
        dict: Task status information
    """
    tasks_data = []
    for task_id, task in get_tasks_snapshot().items():
        tasks_data.append({"id": task_id, "runner_id": task.runner_id, "status": task.status})

    return {"tasks": tasks_data}


@router.get(
    "/runners",
    response_model=dict,
    summary="Get runners status (API)",
    description="API endpoint to get runner status for AJAX requests",
    tags=["API"],
    dependencies=[Depends(verify_token)],
)
async def get_runners_api() -> dict:
    """
    API endpoint to get runner status for AJAX requests.

    Returns:
        dict: Runner status information
    """
    runners_data = []
    for runner_id, runner in runners.items():
        last_heartbeat = runner.last_heartbeat
        status = "online" if (datetime.now() - last_heartbeat).total_seconds() < 60 else "offline"

        runners_data.append(
            {
                "id": runner_id,
                "url": runner.url,
                "status": status,
                "last_heartbeat": last_heartbeat.strftime("%Y-%m-%d %H:%M:%S"),
                "age_seconds": int((datetime.now() - last_heartbeat).total_seconds()),
            }
        )

    return {"runners": runners_data}
