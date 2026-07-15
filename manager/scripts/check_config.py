#!/usr/bin/env python3
"""Validate the manager configuration loaded from environment variables.

This preflight command uses the same configuration loader and validators as the
manager. It reports every detected error without printing sensitive values,
then returns a process-friendly exit code.

Usage:
  uv run scripts/check_config.py

Exit codes:
  0: configuration is valid
  2: configuration could not be loaded or is invalid
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

MANAGER_ROOT = Path(__file__).resolve().parents[1]
if str(MANAGER_ROOT) not in sys.path:
    sys.path.insert(0, str(MANAGER_ROOT))

from app.core._check_output import format_status

ConfigLoadResult = Tuple[Optional[Any], Tuple[str, ...]]


def _configuration_errors(error: Exception) -> Tuple[str, ...]:
    """Return structured validation messages without exposing configuration data."""
    errors = getattr(error, "errors", None)
    if isinstance(errors, (list, tuple)) and errors:
        return tuple(str(item) for item in errors)
    return (f"Unable to load manager configuration: {error}",)


def _load_and_validate_config() -> ConfigLoadResult:
    """Load the effective manager configuration and run its central validator."""
    try:
        config_module = importlib.import_module("app.core.config")
        config = config_module.get_config()
        if not getattr(config, "_configuration_validated", False):
            config.validate_configuration()
    except Exception as error:
        return None, _configuration_errors(error)
    return config, ()


def _print_summary(config: Any) -> None:
    """Print a concise summary containing only non-sensitive effective values."""
    storage_status = "enabled" if config.RUNNERS_STORAGE_ENABLED else "disabled"
    print(f"  Environment: {config.ENVIRONMENT}")
    print(f"  Manager URL: {config.MANAGER_URL}")
    print(f"  Bind address: {config.MANAGER_BIND_HOST}:{config.MANAGER_PORT}")
    print(f"  Uvicorn workers: {config.UVICORN_WORKERS}")
    print(f"  API docs visibility: {config.API_DOCS_VISIBILITY}")
    print(f"  Authorized tokens: {len(config.AUTHORIZED_TOKENS)}")
    print(f"  Admin users: {len(config.ADMIN_USERS)}")
    print(f"  Shared runner storage: {storage_status}")


def main() -> int:
    """Run the configuration preflight and return its shell exit code."""
    print("=== Manager configuration check ===")
    config, errors = _load_and_validate_config()

    if errors:
        print("\nDetected errors:")
        for error in errors:
            print(format_status(error, level="error"))
        print("\nConclusion:")
        print(format_status("Manager configuration is invalid.", level="error"))
        return 2

    if config is None:  # pragma: no cover - defensive invariant
        print(format_status("Manager configuration could not be loaded.", level="error"))
        return 2

    print(format_status("Manager configuration loaded and validated.", level="info"))
    _print_summary(config)
    print("\nConclusion:")
    print(format_status("Manager configuration is valid.", level="info"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
