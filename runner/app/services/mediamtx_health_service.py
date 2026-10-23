"""Periodic health monitoring for the Runner's shared MediaMTX service."""

import asyncio

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services.email_service import send_mediamtx_health_warning_email
from app.services.mediamtx import MediaMTXClient, MediaMTXError

logger = setup_default_logging()


async def mediamtx_health_check_loop() -> None:
    """Check immediately and periodically, emailing admins on each failure.

    The service manager starts this loop only on the first Live instance.
    HTTP and SMTP work do not block the event loop; cancellation stops monitoring.
    """
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
