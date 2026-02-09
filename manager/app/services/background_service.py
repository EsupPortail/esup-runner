# manager/app/services/background_service.py
"""
Background service management and coordination.
"""

import asyncio
from typing import Dict, List

from app.core.setup_logging import setup_default_logging
from app.services.runner_service import check_runners_activity
from app.services.task_service import check_task_timeouts, cleanup_old_tasks

logger = setup_default_logging()


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
            check_runners_activity(),
            cleanup_old_tasks(),
            check_task_timeouts(),
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
