# manager/app/api/routes/manager.py
"""
Manager routes for Runner Manager.
Handles core endpoints for the manager.
"""

from datetime import datetime

from fastapi import APIRouter, Depends

from app.core.auth import verify_token
from app.core.setup_logging import setup_default_logging
from app.core.state import get_tasks_snapshot, runners

# Configure logging
logger = setup_default_logging()

# Create API router
router = APIRouter(prefix="/manager", tags=["Manager"])

# ======================================================
# Endpoints
# ======================================================


@router.get(
    "/health",
    summary="Health endpoint",
    description="Health check endpoint to verify manager is running properly",
    tags=["Manager"],
    dependencies=[Depends(verify_token)],
)
async def health_check() -> dict:
    """
    Health check endpoint to verify manager is running properly.

    Returns:
        dict: Health status and system metrics
    """
    tasks_snapshot = get_tasks_snapshot()
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "runners": len(runners),
        "tasks": len(tasks_snapshot),
    }
