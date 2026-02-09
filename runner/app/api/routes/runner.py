# runner/app/api/routes/runner.py
"""
API routes for Runner management.
Handles core endpoints for runner health, status, and operational checks.
"""

import os
from datetime import datetime

from fastapi import APIRouter

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_runner_id, get_runner_instance_id, is_available, is_registered
from app.managers.storage_manager import storage_manager

# Configure logging for this module
logger = setup_default_logging()

# Create API router with prefix and tags for OpenAPI documentation
router = APIRouter(prefix="/runner", tags=["Runner"])

# ======================================================
# Health & Status Endpoints
# ======================================================


@router.get(
    "/health",
    response_model=dict,
    summary="Check runner health",
    description="Comprehensive health check endpoint to verify runner operational status",
    tags=["Runner"],
)
async def health_check() -> dict:
    """
    Health check endpoint to verify manager is running properly.

    Returns:
        dict: Health status and system metrics
    """
    return {
        "status": "healthy",
        "runner_instance_id": get_runner_instance_id(),
        "runner_id": get_runner_id(),
        "registered": is_registered(),
        "timestamp": datetime.now().isoformat(),
    }


@router.get(
    "/ping",
    response_model=dict,
    summary="Check runner availability",
    description="Endpoint to verify runner is available and ready to execute tasks",
    tags=["Runner"],
)
async def ping() -> dict:
    """
    Availability check endpoint for task distribution system.

    Used by the manager to determine if this runner can accept new tasks.
    Returns current availability status and registration state.

    Returns:
        dict: Availability status containing
    """
    return {
        "runner_instance_id": get_runner_instance_id(),
        "runner_id": get_runner_id(),
        "available": is_available(),
        "registered": is_registered(),
        "task_types": list(config.RUNNER_TASK_TYPES),
    }


@router.get(
    "/status",
    response_model=dict,
    summary="Get runner status",
    description="Returns detailed information about runner state and configuration",
    tags=["Runner"],
)
async def status() -> dict:
    """
    Detailed status endpoint for runner monitoring and debugging.

    Provides comprehensive information about runner configuration,
    registration status, and operational parameters. Useful for
    administrative dashboards and system monitoring.

    Returns:
        dict: Detailed status information containing
    """
    return {
        "runner_instance_id": get_runner_instance_id(),
        "runner_id": get_runner_id(),
        "available": is_available(),
        "registered": is_registered(),
        "task_types": list(config.RUNNER_TASK_TYPES),
        "manager_url": config.MANAGER_URL,
        "port": os.getenv("RUNNER_PORT"),
        "storage_stats": (
            storage_manager.get_usage_stats() if hasattr(storage_manager, "get_usage_stats") else {}
        ),
    }
