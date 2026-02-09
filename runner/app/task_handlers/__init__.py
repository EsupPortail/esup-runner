# runner/app/task_handlers/__init__.py
"""
Task handlers package for specialized task processing.
Dynamically loads and manages different types of task handlers.
"""

import importlib
import pkgutil
from typing import Dict, Optional, Type

from .base_handler import BaseTaskHandler


class TaskHandlerManager:
    """
    Manages registration and discovery of task handlers.

    Automatically discovers and loads task handlers from subdirectories,
    providing a unified interface for task processing.
    """

    def __init__(self):
        self.handlers: Dict[str, Type[BaseTaskHandler]] = {}
        self._discover_handlers()

    def _discover_handlers(self) -> None:
        """
        Automatically discover and register all task handlers.
        """
        package = __import__(__name__, fromlist=[""])

        for _, name, is_pkg in pkgutil.iter_modules(package.__path__):
            if is_pkg and name != "base_handler":
                try:
                    handler_module = importlib.import_module(f".{name}", __name__)
                    if hasattr(handler_module, "get_handler"):
                        handler_class = handler_module.get_handler()
                        if hasattr(handler_class, "task_type"):
                            self.handlers[handler_class.task_type] = handler_class
                except ImportError as e:
                    print(f"Failed to load handler {name}: {e}")

    def get_handler(self, task_type: str) -> Optional[Type[BaseTaskHandler]]:
        """
        Get handler for specific task type.

        Args:
            task_type: Type of task to handle

        Returns:
            Optional handler class, None if not found
        """
        return self.handlers.get(task_type)

    def list_handlers(self) -> Dict[str, str]:
        """
        List all available task handlers.

        Returns:
            Dict mapping task types to handler descriptions
        """
        return {
            task_type: handler.get_description() for task_type, handler in self.handlers.items()
        }


# Global task handler manager
task_handler_manager = TaskHandlerManager()
