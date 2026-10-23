"""Periodic health monitoring for the Manager's MediaMTX service."""

import asyncio

from filelock import FileLock, Timeout

from app.core import state
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services.email_service import send_mediamtx_health_warning_email
from app.services.mediamtx import MediaMTXClient, MediaMTXError

logger = setup_default_logging()


async def mediamtx_health_check_loop() -> None:
    """Elect one worker to monitor MediaMTX, releasing ownership on shutdown.

    Other workers retry the nonblocking shared lock every minute so monitoring
    resumes if the owning worker stops. HTTP and SMTP work remain asynchronous.
    """
    lock = FileLock(state.persistence.data_directory / ".mediamtx-health.lock")
    while True:
        try:
            lock.acquire(timeout=0)
        except Timeout:
            await asyncio.sleep(60)
            continue
        try:
            await _monitor_mediamtx()
        finally:
            lock.release()


async def _monitor_mediamtx() -> None:
    """Check immediately and periodically, emailing admins on each failure."""
    settings = config.LIVE
    client = MediaMTXClient(
        settings.mediamtx_api_url,
        settings.mediamtx_api_user,
        settings.mediamtx_api_password,
        settings.mediamtx_timeout,
    )
    interval_seconds = settings.mediamtx_health_check_interval_hours * 3600
    logger.info(
        "MediaMTX health monitoring started: interval=%s hours",
        settings.mediamtx_health_check_interval_hours,
    )
    while True:
        try:
            try:
                await client.check_health()
            except MediaMTXError as exc:
                error_message = str(exc)
                logger.warning("MediaMTX health check failed: %s", error_message)
                await send_mediamtx_health_warning_email(error_message=error_message)
            else:
                logger.info("MediaMTX health check succeeded: Control API is available")
        except Exception:
            logger.exception("Error in MediaMTX health monitoring")

        await asyncio.sleep(interval_seconds)
