"""Load configuration for diagnostics while reporting Live setup issues as warnings."""

import importlib
from contextvars import ContextVar
from types import ModuleType

from app.core._check_output import format_status

# Defer import-time validation only while a diagnostic explicitly loads config.
_DEFER_CONFIG_VALIDATION: ContextVar[bool] = ContextVar("defer_config_validation", default=False)


def load_config_for_checks() -> ModuleType:
    """Return the config module after validation, allowing incomplete Live setup.

    Live validation messages become warnings so resource and storage checks can
    run during installation. Other configuration errors still raise
    ConfigValidationError. The import-time validation override is always reset
    and does not affect normal Runner startup or explicit validation calls.
    """
    token = _DEFER_CONFIG_VALIDATION.set(True)
    try:
        config_module = importlib.import_module("app.core.config")
    finally:
        _DEFER_CONFIG_VALIDATION.reset(token)

    try:
        config_module.get_config().validate_configuration()
    except config_module.ConfigValidationError as error:
        errors = []
        for message in error.errors:
            if message.startswith("LIVE_") or message == "Invalid LIVE configuration":
                print(
                    format_status(
                        f"Live configuration is incomplete or invalid: {message}. "
                        "Complete Live setup before starting the Runner.",
                        level="warning",
                    )
                )
            else:
                errors.append(message)
        if errors:
            raise config_module.ConfigValidationError(errors) from None

    return config_module
