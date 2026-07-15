#!/usr/bin/env python3
"""Validate the runner configuration loaded from environment variables.

This preflight command uses the same configuration loader and validators as the
runner. It reports every detected error without printing sensitive values, then
returns a process-friendly exit code.

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

RUNNER_ROOT = Path(__file__).resolve().parents[1]
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from app.core._check_output import format_status

ConfigLoadResult = Tuple[Optional[Any], Tuple[str, ...]]


def _configuration_errors(error: Exception) -> Tuple[str, ...]:
    """Return structured validation messages without exposing configuration data."""
    errors = getattr(error, "errors", None)
    if isinstance(errors, (list, tuple)) and errors:
        return tuple(str(item) for item in errors)
    return (f"Unable to load runner configuration: {error}",)


def _load_and_validate_config() -> ConfigLoadResult:
    """Load the effective runner configuration and run its central validator."""
    try:
        config_module = importlib.import_module("app.core.config")
        config = config_module.get_config()
        config.validate_configuration()
    except Exception as error:
        return None, _configuration_errors(error)
    return config, ()


def _port_range(config: Any) -> str:
    """Return the TCP port or inclusive port range used by runner instances."""
    first_port = int(config.RUNNER_BASE_PORT)
    last_port = first_port + int(config.RUNNER_INSTANCES) - 1
    return str(first_port) if first_port == last_port else f"{first_port}-{last_port}"


def _print_summary(config: Any) -> None:
    """Print a concise summary containing only non-sensitive effective values."""
    task_types = ", ".join(sorted(str(item) for item in config.RUNNER_TASK_TYPES))
    print(f"  Instances: {config.RUNNER_INSTANCES}")
    print(f"  Ports: {_port_range(config)}")
    print(f"  Task types: {task_types}")
    print(f"  Encoding mode: {config.ENCODING_TYPE}")
    print(f"  Manager URL: {config.MANAGER_URL}")


def main() -> int:
    """Run the configuration preflight and return its shell exit code."""
    print("=== Runner configuration check ===")
    config, errors = _load_and_validate_config()

    if errors:
        print("\nDetected errors:")
        for error in errors:
            print(format_status(error, level="error"))
        print("\nConclusion:")
        print(format_status("Runner configuration is invalid.", level="error"))
        return 2

    if config is None:  # pragma: no cover - defensive invariant
        print(format_status("Runner configuration could not be loaded.", level="error"))
        return 2

    print(format_status("Runner configuration loaded and validated.", level="info"))
    _print_summary(config)
    print("\nConclusion:")
    print(format_status("Runner configuration is valid.", level="info"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
