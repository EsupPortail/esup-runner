# manager/app/services/runner_service.py
"""
Service for managing runners and their availability.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from app.core.setup_logging import setup_default_logging
from app.core.state import runners

logger = setup_default_logging()


async def check_runners_activity(
    poll_interval: float = 30.0, stop_event: Optional[asyncio.Event] = None
) -> None:
    """
    Periodically check runner activity and remove inactive ones.

    Runs every 30 seconds and removes runners that haven't sent
    heartbeat in over 1 minute.
    """
    logger.info("Starting runner activity monitoring")
    while True:
        if stop_event and stop_event.is_set():
            logger.info("Stopping runner activity monitoring")
            break

        await asyncio.sleep(poll_interval)
        now = datetime.now()
        runners_to_remove = []

        for runner_id, runner in runners.items():
            # Remove runners without heartbeat for 1+ minutes
            if now - runner.last_heartbeat > timedelta(minutes=1):
                runners_to_remove.append(runner_id)

        for runner_id in runners_to_remove:
            del runners[runner_id]
            logger.info(f"Runner {runner_id} removed due to inactivity")


def get_online_runners() -> List[Dict]:
    """
    Get list of currently online runners.

    Returns:
        List[Dict]: Online runners with their status information
    """
    online_runners = []
    now = datetime.now()

    for runner_id, runner in runners.items():
        last_heartbeat = runner.last_heartbeat
        if (now - last_heartbeat).total_seconds() < 60:  # Online if heartbeat < 60s
            online_runners.append(
                {
                    "id": runner_id,
                    "url": runner.url,
                    "last_heartbeat": last_heartbeat,
                    "has_token": runner.token is not None and runner.token != "",
                }
            )

    return online_runners


def verify_runner_tokenINUTILE(runner_id: str, token: str) -> bool:
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


def update_runner_heartbeat(runner_id: str) -> bool:
    """
    Update the heartbeat timestamp for a runner.

    Args:
        runner_id: Unique identifier of the runner

    Returns:
        bool: True if runner exists and heartbeat was updated
    """
    if runner_id in runners:
        runners[runner_id].last_heartbeat = datetime.now()
        return True
    return False
