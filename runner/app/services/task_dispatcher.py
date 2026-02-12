# runner/app/services/task_dispatcher.py
"""
Task dispatching service for routing tasks to appropriate handlers.
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict

from app.core.setup_logging import setup_default_logging
from app.managers.storage_manager import storage_manager
from app.models.models import TaskRequest
from app.task_handlers import task_handler_manager

logger = setup_default_logging()


class TaskDispatcher:
    """
    Dispatches tasks to appropriate handlers based on task type.

    Manages task execution, result collection, and error handling
    for different types of processing tasks.
    """

    def __init__(self):
        """Initialize task dispatcher."""
        self.logger = setup_default_logging()

    async def dispatch_task(self, task_id: str, task_request: TaskRequest) -> Dict[str, Any]:
        """
        Dispatch task to appropriate handler for execution.

        Args:
            task_id: Unique task identifier
            task_request: Task execution request details

        Returns:
            Dict containing task execution results
        """
        self.logger.info(f"Dispatching task {task_id} of type {task_request.task_type}")

        # Get appropriate handler
        handler_class = task_handler_manager.get_handler(task_request.task_type)
        if not handler_class:
            return {
                "success": False,
                "error": f"No handler found for task type: {task_request.task_type}",
            }

        try:
            # Create handler instance
            handler = handler_class()

            # Validate parameters
            if not handler.validate_parameters(task_request.parameters):
                return {
                    "success": False,
                    "error": f"Invalid parameters for task type: {task_request.task_type}",
                }

            # Create workspace for this task
            workspace = Path(storage_manager.base_path) / task_id
            workspace.mkdir(parents=True, exist_ok=True)
            # Output directory inside workspace (already created in prepare_workspace)
            work_dir = "output"
            output_dir = workspace / work_dir

            # Execute task (run in thread pool since it might be CPU-intensive)
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, handler.execute_task, task_id, task_request)

            # Package results if task was successful
            if results.get("success"):
                results = await self._package_task_results(task_id, output_dir, results)

            result: Dict[str, Any] = results
            return result

        except Exception as e:
            self.logger.error(f"Task dispatch failed for {task_id}: {e}")
            return {"success": False, "error": str(e), "task_type": task_request.task_type}

    async def _package_task_results(
        self, task_id: str, output_dir: Path, results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Package task results and create downloadable manifest.

        Args:
            task_id: Task identifier
            output_dir: Directory containing task outputs
            results: Task execution results

        Returns:
            Dict containing packaged results
        """
        try:
            output_files = [
                str(p.relative_to(output_dir)) for p in output_dir.rglob("*") if p.is_file()
            ]
            manifest = {
                "task_id": task_id,
                "files": output_files,
            }

            manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )

            # Canonical manifest path: <storage>/<task_id>/manifest.json
            canonical_manifest_path = output_dir.parent / "manifest.json"
            canonical_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            temp_manifest_path = canonical_manifest_path.with_name(".manifest.json.tmp")
            with open(temp_manifest_path, "wb") as f:
                f.write(manifest_bytes)
                f.flush()
                os.fsync(f.fileno())
            temp_manifest_path.replace(canonical_manifest_path)

            # Update results with manifest information
            results["result_manifest"] = str(canonical_manifest_path)
            results["output_files"] = output_files

            # Clean up temporary files
            # import shutil
            # shutil.rmtree(output_dir)
            # Path(manifest_path).unlink()

            self.logger.info(f"Packaged results for task {task_id}")
            return results

        except Exception as e:
            self.logger.error(f"Failed to package results for {task_id}: {e}")
            results["success"] = False
            results["error"] = f"Result packaging failed: {e}"
            return results

    def get_available_task_types(self) -> Dict[str, str]:
        """
        Get list of available task types and their descriptions.

        Returns:
            Dict mapping task types to descriptions
        """
        handlers: Dict[str, str] = task_handler_manager.list_handlers()
        return handlers


# Global task dispatcher instance
task_dispatcher = TaskDispatcher()
