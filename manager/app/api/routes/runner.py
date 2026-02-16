# manager/app/api/routes/runner.py
"""
Runners routes for Runner Manager.
Handles endpoints for runners.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.core.auth import verify_runner_version, verify_token
from app.core.setup_logging import setup_default_logging
from app.core.state import runners
from app.models.models import Runner

# Configure logging
logger = setup_default_logging()

# Create API router
router = APIRouter(prefix="/runner", tags=["Runner"])

# ======================================================
# Utility Functions
# ======================================================


def verify_runner_token(runner_id: str, token: str) -> bool:
    """
    Verify that a token is valid for a specific runner.

    Args:
        runner_id: Unique identifier of the runner
        token: Authentication token to verify

    Returns:
        bool: True if token is valid for the runner, False otherwise
    """
    if runner_id not in runners:
        return False

    runner = runners[runner_id]
    is_valid: bool = runner.token == token
    return is_valid


# ======================================================
# Endpoints
# ======================================================


@router.post(
    "/register",
    response_model=dict,
    summary="Register a runner",
    description="Register a new runner with the manager",
    tags=["Runner"],
    dependencies=[Depends(verify_token), Depends(verify_runner_version)],
    responses={
        200: {"description": "Runner registered successfully"},
        403: {"description": "Token not authorized to register runners"},
    },
)
async def register_runner(
    runner: Runner,
    current_token: str = Depends(verify_token),
    current_version: str = Depends(verify_runner_version),
) -> dict:
    """
    Register a new runner with the manager.

    Args:
        runner: Runner instance to register
        current_token: Authenticated runner token
        current_version: Verified runner version

    Returns:
        dict: Registration status

    Raises:
        HTTPException: If token is not authorized
    """
    runner.last_heartbeat = datetime.now()
    runner.token = current_token
    runner.version = current_version
    runners[runner.id] = runner

    logger.info(f"Runner v{runner.version} registered: {runner.id} - {runner.url}")
    return {"status": "registered"}


@router.post(
    "/heartbeat/{runner_id}",
    response_model=dict,
    summary="Send heartbeat",
    description="Endpoint for runners to signal they are still active",
    tags=["Runner"],
    dependencies=[Depends(verify_token), Depends(verify_runner_version)],
)
async def runner_heartbeat(
    runner_id: str = Path(..., description="Runner identifier"),
    current_token: str = Depends(verify_token),
    current_version: str = Depends(verify_runner_version),
) -> dict:
    """
    Update runner heartbeat to indicate it's still active.

    Args:
        runner_id: Unique identifier of the runner
        current_token: Authenticated runner token

    Returns:
        dict: Success status

    Raises:
        HTTPException: If runner not found or token invalid
    """
    runner = runners.get(runner_id)
    if runner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runner not found")

    if runner.token != current_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token not authorized for this runner"
        )

    runner.last_heartbeat = datetime.now()
    runners[runner_id] = runner
    return {"status": "ok"}
