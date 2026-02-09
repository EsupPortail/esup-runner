# runner/app/services/manager_service.py
"""
Manager communication service for Runner.
Handles registration, heartbeats, and connection management with the central manager.
"""

import asyncio

import httpx

from app.__version__ import __version__
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_runner_id, get_runner_instance_url, is_registered, set_registered

logger = setup_default_logging()


async def register_with_manager():
    """
    Register this runner with the central manager.

    Attempts to register the runner with the manager using the configured
    URL and authentication token.

    Returns:
        bool: True if registration was successful, False otherwise
    """
    logger.info("Starting manager registration")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{config.MANAGER_URL}/runner/register",
                json={
                    "id": get_runner_id(),
                    "url": get_runner_instance_url(),
                    "task_types": list(config.RUNNER_TASK_TYPES),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    "X-Runner-Version": __version__,
                },
            )

            logger.info(
                f"Manager registration response {get_runner_id()} {response.status_code}: {get_runner_instance_url()} {is_registered()}"
            )

            if response.status_code == 200:
                logger.info(f"Successfully registered with manager: {get_runner_instance_url()}")
                set_registered(True)
                return True
            else:
                logger.error(f"Registration failed: {response.text}")
                set_registered(False)
                return False

    except Exception as e:
        logger.error(f"Unable to contact manager: {e}")
        set_registered(False)
        return False


async def send_heartbeat():
    """
    Send heartbeat signal to manager to maintain registration.

    Sends periodic heartbeat to inform manager that this runner is still
    active and available for task execution.

    Returns:
        bool: True if heartbeat was successful, False otherwise
    """
    if not is_registered():
        return False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{config.MANAGER_URL}/runner/heartbeat/{get_runner_id()}",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    "X-Runner-Version": __version__,
                },
            )

            if response.status_code == 200:
                return True
            else:
                logger.warning(f"Heartbeat failed: {response.text}")
                set_registered(False)
                return False

    except Exception as e:
        logger.error(f"Error sending heartbeat: {e}")
        set_registered(False)
        return False


async def heartbeat_loop():  # pragma: no cover
    """
    Continuous loop for sending heartbeats to manager.

    Runs indefinitely, sending heartbeats at regular intervals to
    maintain runner registration and availability status.
    """
    heartbeat_interval = 15  # Seconds between heartbeats

    while True:
        if is_registered():
            await send_heartbeat()
        await asyncio.sleep(heartbeat_interval)


async def reconnect_loop():  # pragma: no cover
    """
    Automatic reconnection loop with exponential backoff.

    Monitors registration status and attempts to reconnect to manager
    when connection is lost. Uses exponential backoff to avoid overwhelming
    the manager during network issues.
    """
    reconnect_attempts = 0
    max_reconnect_interval = 300  # 5 minutes maximum
    base_reconnect_interval = 15  # Base seconds between reconnection attempts

    while True:
        if not is_registered():
            # Check manager health before attempting reconnection
            if await check_manager_health():
                logger.info(
                    f"Healthy manager detected, attempting reconnection (attempt {reconnect_attempts + 1})..."
                )
            else:
                logger.warning(
                    "Manager unhealthy, or token problem detected, waiting before retry..."
                )
                await asyncio.sleep(base_reconnect_interval)
                continue

            # Calculate current interval with exponential backoff
            current_interval = min(
                base_reconnect_interval * (2**reconnect_attempts), max_reconnect_interval
            )

            success = await register_with_manager()

            if success:
                logger.info("Reconnection successful")
                reconnect_attempts = 0  # Reset attempt counter
            else:
                reconnect_attempts += 1
                logger.warning(f"Reconnection failed, next attempt in {current_interval} seconds")
                await asyncio.sleep(current_interval)
                continue

        await asyncio.sleep(base_reconnect_interval)


async def check_manager_health():
    """
    Check manager health status before attempting reconnection.

    Verifies that the manager is healthy and responsive before
    attempting registration or reconnection.

    Returns:
        bool: True if manager is healthy and responding, False otherwise
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{config.MANAGER_URL}/manager/health",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {config.RUNNER_TOKEN}",
                    "X-Runner-Version": __version__,
                },
            )

            if response.status_code == 200:
                health_data = response.json()
                return health_data.get("status") == "healthy"
            return False

    except Exception:
        return False
