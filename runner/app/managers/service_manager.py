# runner/app/managers/service_manager.py
"""
Background service management and coordination.
"""

import asyncio
from typing import Dict, List

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.managers.storage_manager import storage_manager
from app.services.manager_service import heartbeat_loop, reconnect_loop

logger = setup_default_logging()


async def storage_cleanup_loop() -> None:
    """
    Background service for periodic storage cleanup.
    Removes files and directories older than MAX_FILE_AGE_DAYS.
    """
    cleanup_interval_seconds = config.CLEANUP_INTERVAL_HOURS * 3600
    max_file_age_days = config.MAX_FILE_AGE_DAYS

    if max_file_age_days <= 0:
        logger.info("Storage cleanup service disabled: MAX_FILE_AGE_DAYS is 0 (unlimited)")
        return

    logger.info(
        f"Storage cleanup service started: "
        f"max_age={max_file_age_days} days, interval={config.CLEANUP_INTERVAL_HOURS} hours"
    )

    # Perform initial cleanup immediately
    try:
        logger.debug("Starting initial storage cleanup...")
        deleted_count = storage_manager.cleanup_old_files(max_file_age_days)
        if deleted_count > 0:
            logger.info(f"Initial cleanup completed: removed {deleted_count} old items")
    except Exception as e:
        logger.error(f"Error in initial storage cleanup: {e}", exc_info=True)

    while True:
        try:
            # Wait for the next cleanup interval
            await asyncio.sleep(cleanup_interval_seconds)

            # Perform cleanup
            logger.debug("Starting periodic storage cleanup...")
            deleted_count = storage_manager.cleanup_old_files(max_file_age_days)

            if deleted_count > 0:
                logger.info(f"Periodic cleanup completed: removed {deleted_count} old items")

        except asyncio.CancelledError:
            logger.info("Storage cleanup service stopped")
            break
        except Exception as e:
            logger.error(f"Error in storage cleanup service: {e}", exc_info=True)
            # Continue running despite errors
            await asyncio.sleep(60)  # Wait a minute before retrying


class BackgroundServiceManager:
    """
    Manages background services and their lifecycle.
    """

    def __init__(self):
        self.tasks: List[asyncio.Task] = []
        self.is_running = False

    async def start_all_services(self) -> None:
        """
        Start all background services.
        """
        if self.is_running:
            logger.warning("Background services are already running")
            return

        logger.info("Starting all background services")

        # Start each background service
        services = [
            reconnect_loop(),
            heartbeat_loop(),
            storage_cleanup_loop(),
        ]

        for service in services:
            task = asyncio.create_task(service)
            self.tasks.append(task)

        self.is_running = True
        logger.info(f"Started {len(self.tasks)} background services")

    async def stop_all_services(self) -> None:
        """
        Stop all background services gracefully.
        """
        if not self.is_running:
            logger.warning("Background services are not running")
            return

        logger.info("Stopping all background services")

        # Cancel all tasks
        for task in self.tasks:
            task.cancel()

        # Wait for all tasks to be cancelled
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
            self.tasks.clear()

        self.is_running = False
        logger.info("All background services stopped")

    def get_service_status(self) -> Dict:
        """
        Get status of background services.

        Returns:
            Dict: Service status information
        """
        return {
            "is_running": self.is_running,
            "tasks": len(self.tasks),
            "services": [
                {
                    "name": str(task.get_name()) if hasattr(task, "get_name") else "Unknown",
                    "done": task.done(),
                    "cancelled": task.cancelled(),
                }
                for task in self.tasks
            ],
        }


# Global background service manager instance
background_manager = BackgroundServiceManager()
