# manager/app/core/setup_logging.py
"""
Logging configuration module for runner management API.
Provides flexible logging setup with support for JSON formatting, file rotation, and syslog.
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler, SysLogHandler
from typing import Any, Callable, Dict, Optional

from app.core.config import config


class JSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging.

    Formats log records as JSON objects for better parsing and analysis.
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Format a log record as a JSON string.

        Args:
            record: Log record to format

        Returns:
            str: JSON formatted log entry
        """
        log_record: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add custom fields if they exist
        custom_fields = ["task_id", "runner_id", "component", "operation"]
        for field in custom_fields:
            if hasattr(record, field):
                log_record[field] = getattr(record, field)

        # Add exception information if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
            log_record["stack_trace"] = (
                self.formatStack(record.stack_info) if record.stack_info else None
            )

        return json.dumps(log_record, ensure_ascii=False)


def _coerce_log_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    level_name = level.strip().upper()
    return logging._nameToLevel.get(level_name, logging.INFO)


def setup_logging(
    name: str,
    log_file: Optional[str] = None,
    json_format: bool = False,
    log_level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    max_file_size: int = 10485760,  # 10MB
    backup_count: int = 10,
) -> logging.Logger:
    """
    Configure logging for a given component with flexible options.

    Args:
        name: Name of the logger (typically the component name)
        log_file: Log file name (optional, uses default if not provided)
        json_format: If True, uses JSON formatting for logs
        log_level: Overall log level for the logger
        console_level: Log level for console output
        file_level: Log level for file output
        max_file_size: Maximum size of log file before rotation (in bytes)
        backup_count: Number of backup files to keep

    Returns:
        logging.Logger: Configured logger instance

    Raises:
        OSError: If log directory cannot be created
        PermissionError: If log file cannot be written
    """
    is_test_run = os.getenv("PYTEST_CURRENT_TEST") is not None

    # Use temp directory during tests to avoid permission issues and keep handlers simple
    log_dir = os.getenv(
        "PYTEST_LOG_DIR", config.LOG_DIRECTORY if not is_test_run else "/tmp/esup-runner_logs/"
    )
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError as e:
        raise OSError(f"Failed to create log directory {log_dir}: {e}")

    # Determine log file path
    if log_file is None:
        log_file = f'{name.lower().replace(" ", "_")}.log'
    log_path = os.path.join(log_dir, log_file)

    # Get or create logger
    logger = logging.getLogger(name)

    # Prevent duplicate handlers and propagation to parent loggers
    if logger.handlers:
        logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(log_level)

    # Create formatter based on format preference
    formatter = _create_formatter(json_format)

    # Add console handler for development
    _add_console_handler(logger, formatter, console_level)

    # Skip persistent handlers when running under pytest to avoid resource warnings
    if not is_test_run:
        _add_file_handler(logger, formatter, log_path, file_level, max_file_size, backup_count)
        _add_syslog_handler(logger, formatter)

    logger.info(
        "Logging configured successfully",
        extra={"component": name, "log_level": logging.getLevelName(log_level)},
    )

    return logger


def _create_formatter(json_format: bool) -> logging.Formatter:
    """
    Create appropriate formatter based on format preference.

    Args:
        json_format: Whether to use JSON formatting

    Returns:
        logging.Formatter: Configured formatter instance
    """
    if json_format:
        return JSONFormatter()
    else:
        return logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - [%(module)s:%(funcName)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )


def _add_console_handler(
    logger: logging.Logger, formatter: logging.Formatter, level: int = logging.INFO
) -> None:
    """
    Add console handler to logger for development output.

    Args:
        logger: Logger instance to add handler to
        formatter: Formatter for the handler
        level: Log level for console output
    """
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def _add_file_handler(
    logger: logging.Logger,
    formatter: logging.Formatter,
    log_path: str,
    level: int = logging.DEBUG,
    max_bytes: int = 10485760,
    backup_count: int = 10,
) -> None:
    """
    Add rotating file handler to logger for persistent log storage.

    Args:
        logger: Logger instance to add handler to
        formatter: Formatter for the handler
        log_path: Path to the log file
        level: Log level for file output
        max_bytes: Maximum file size before rotation
        backup_count: Number of backup files to keep

    Raises:
        PermissionError: If log file cannot be written
    """
    try:
        file_handler = RotatingFileHandler(
            filename=log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except PermissionError as e:
        raise PermissionError(f"Cannot write to log file {log_path}: {e}")


def _add_syslog_handler(
    logger: logging.Logger, formatter: logging.Formatter, syslog_address: str = "/dev/log"
) -> None:
    """
    Add syslog handler for system-level logging.

    Args:
        logger: Logger instance to add handler to
        formatter: Formatter for the handler
        syslog_address: Address for syslog (file path or network address)
    """
    try:
        syslog_handler = SysLogHandler(address=syslog_address)
        syslog_handler.setFormatter(formatter)
        logger.addHandler(syslog_handler)
    except (OSError, ConnectionError) as e:
        # Log warning but don't fail if syslog is unavailable
        logger.warning(f"Syslog handler could not be configured: {e}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a configured logger instance for the specified name.

    This is a convenience function that uses default configuration.

    Args:
        name: Name of the logger to retrieve

    Returns:
        logging.Logger: Configured logger instance
    """
    return logging.getLogger(name)


class LogContext:
    """
    Context manager for adding contextual information to logs.

    Example:
        with LogContext(logger, task_id="task-123", runner_id="runner-456"):
            logger.info("Processing task")
    """

    def __init__(self, logger: logging.Logger, **context_fields: Any):
        """
        Initialize log context with additional fields.

        Args:
            logger: Logger instance to use
            **context_fields: Additional fields to include in logs
        """
        self.logger = logger
        self.context_fields = context_fields
        self.old_factory: Optional[Callable[..., logging.LogRecord]] = None

    def __enter__(self) -> "LogContext":
        """
        Enter context and set up custom log record factory.

        Returns:
            LogContext: Self instance
        """
        self.old_factory = logging.getLogRecordFactory()
        old_factory = self.old_factory

        def factory(*args, **kwargs):
            record = old_factory(*args, **kwargs)
            for key, value in self.context_fields.items():
                setattr(record, key, value)
            return record

        logging.setLogRecordFactory(factory)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        Exit context and restore original log record factory.
        """
        if self.old_factory is not None:
            logging.setLogRecordFactory(self.old_factory)


# Default logger configuration for quick setup
def setup_default_logging(
    json_format: bool = False, log_level: int | str = config.LOG_LEVEL
) -> logging.Logger:
    """
    Set up default logging configuration for the application.

    Args:
        json_format: Whether to use JSON formatting
        log_level: Default log level
    """
    resolved_level = _coerce_log_level(log_level)
    return setup_logging(name="manager", json_format=json_format, log_level=resolved_level)


def setup_uvicorn_logging(json_format: bool = False) -> None:
    """
    Configure uvicorn loggers to use our custom logging system.

    This ensures that uvicorn access logs, error logs, and application logs
    all use the same logging configuration.

    Args:
        json_format: Whether to use JSON formatting for uvicorn logs
    """
    # Get uvicorn loggers
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    uvicorn_access_logger = logging.getLogger("uvicorn.access")

    # Remove default uvicorn handlers
    for logger in [uvicorn_logger, uvicorn_error_logger, uvicorn_access_logger]:
        if logger.handlers:
            logger.handlers.clear()

    # Set levels
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_error_logger.setLevel(logging.INFO)
    uvicorn_access_logger.setLevel(logging.INFO)

    # Don't propagate to root logger to avoid duplicate logs
    uvicorn_logger.propagate = False
    uvicorn_error_logger.propagate = False
    uvicorn_access_logger.propagate = False

    # Create formatter
    formatter = _create_formatter(json_format)

    # Add our handlers to uvicorn loggers
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    try:
        file_handler = RotatingFileHandler(
            f"{config.LOG_DIRECTORY}manager_uvicorn.log",
            maxBytes=10485760,  # 10MB
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
    except PermissionError:
        file_handler = None

    # Add handlers to uvicorn loggers
    for logger in [uvicorn_logger, uvicorn_error_logger, uvicorn_access_logger]:
        logger.addHandler(console_handler)
        if file_handler:
            logger.addHandler(file_handler)

    logging.info("Uvicorn logging configured successfully")


def get_uvicorn_log_config(json_format: bool = False) -> dict:
    """
    Get a logging configuration dictionary for uvicorn.

    This can be passed to uvicorn.run(log_config=...)

    Args:
        json_format: Whether to use JSON formatting

    Returns:
        dict: Uvicorn logging configuration
    """
    formatter = "json" if json_format else "default"

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(asctime)s - %(name)s - %(levelname)s - %(client_addr)s - "%(request_line)s" %(status_code)s',
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "json": {
                "()": JSONFormatter,
            },
        },
        "handlers": {
            "default": {
                "formatter": formatter,
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": formatter,
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "file": {
                "formatter": formatter,
                "class": "logging.handlers.RotatingFileHandler",
                "filename": f"{config.LOG_DIRECTORY}manager_uvicorn.log",
                "maxBytes": 10485760,
                "backupCount": 10,
                "encoding": "utf-8",
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default", "file"], "level": "INFO", "propagate": False},
            "uvicorn.error": {
                "handlers": ["default", "file"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["access", "file"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }
