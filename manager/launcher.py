# runner_manager/launcher.py
"""
Start runner management system with Uvicorn (dev) or Gunicorn (prod).
"""

import logging
import os
import signal
import subprocess

from app.core.config import config
from app.core.setup_logging import (
    get_uvicorn_log_config,
    setup_default_logging,
    setup_uvicorn_logging,
)


def reload_config(signum, frame):
    """
    Signal handler for SIGHUP to reload .env configuration dynamically.
    This resets the config module state and reloads environment variables.
    """
    from app.core import config as config_module

    config_module.reload_config_env()

    print("Configuration reloaded after SIGHUP signal.")


def run_dev():
    """
    Run the FastAPI application with Uvicorn in development mode (with reload).
    """
    setup_default_logging(json_format=False, log_level=logging.INFO)
    setup_uvicorn_logging(json_format=False)

    import uvicorn

    log_config = get_uvicorn_log_config(json_format=False)

    manager_host = os.getenv("MANAGER_HOST", "0.0.0.0")
    manager_port = int(os.getenv("MANAGER_PORT", "8000"))

    print(f"[DEV] Starting Runner Manager on {manager_host}:{manager_port}")
    print(f"Admin Dashboard: {config.MANAGER_URL}/admin")
    print(f"API Documentation: {config.MANAGER_URL}/docs")

    uvicorn.run(
        "app.main:app",
        host=manager_host,
        port=manager_port,
        reload=True,
        log_config=log_config,
        access_log=True,
        workers=1,  # In dev, keep it simple
    )


def run_prod():
    """
    Run the FastAPI application with Gunicorn + Uvicorn workers in production mode.
    """
    manager_host = os.getenv("MANAGER_HOST", config.MANAGER_HOST)
    manager_port = int(os.getenv("MANAGER_PORT", config.MANAGER_PORT))
    workers = int(os.getenv("UVICORN_WORKERS", config.UVICORN_WORKERS))

    gunicorn_cmd = [
        "gunicorn",
        "app.main:app",
        "-k",
        "uvicorn.workers.UvicornWorker",
        "-b",
        f"{manager_host}:{manager_port}",
        "--workers",
        str(workers),
        "--access-logfile",
        "-",  # Send access logs to stdout
        "--error-logfile",
        "-",  # Send error logs to stdout
    ]

    print(f"[PROD] Launching Gunicorn with {workers} workers on {manager_host}:{manager_port}")
    subprocess.run(gunicorn_cmd, check=True)


def main():
    """
    Main entry point for running the application.
    Uses Uvicorn in dev, Gunicorn in prod.
    """
    # Register SIGHUP handler for dynamic config reload
    signal.signal(signal.SIGHUP, reload_config)

    env = os.getenv("ENVIRONMENT", config.ENVIRONMENT).lower()

    if env == "production":
        run_prod()
    else:
        run_dev()


if __name__ == "__main__":
    main()
