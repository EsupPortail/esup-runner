"""Periodic NVIDIA driver health monitoring, independent of media tasks."""

import asyncio
import subprocess

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services.email_service import send_gpu_health_warning_email

logger = setup_default_logging()

_CHECK_TIMEOUT_SECONDS = 10
_MAX_DIAGNOSTIC_LENGTH = 4000


def _check_nvidia_smi() -> str | None:
    """Return a bounded failure diagnostic, or None when nvidia-smi succeeds.

    Run without a shell and kill/reap the subprocess if its timeout expires.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"nvidia-smi timed out after {_CHECK_TIMEOUT_SECONDS} seconds."
    except OSError as exc:
        return f"Cannot execute nvidia-smi: {exc}"[:_MAX_DIAGNOSTIC_LENGTH]

    if result.returncode == 0:
        return None

    diagnostic = "\n".join(part.strip() for part in (result.stderr, result.stdout) if part.strip())
    return (
        f"nvidia-smi exited with code {result.returncode}:\n"
        f"{diagnostic or '(no diagnostic output)'}"
    )[:_MAX_DIAGNOSTIC_LENGTH]


async def gpu_health_check_loop() -> None:
    """Check immediately and periodically, emailing admins on each failure.

    Subprocess and SMTP work run in threads to keep the event loop responsive.
    Cancellation propagates to the background service manager on shutdown.
    """
    interval_seconds = config.GPU_HEALTH_CHECK_INTERVAL_HOURS * 3600
    logger.info(
        "GPU health monitoring started: interval=%s hours",
        config.GPU_HEALTH_CHECK_INTERVAL_HOURS,
    )
    while True:
        try:
            error_message = await asyncio.to_thread(_check_nvidia_smi)
            if error_message is None:
                logger.info("GPU health check succeeded: nvidia-smi is available")
            else:
                logger.warning("GPU health check failed: %s", error_message)
                await send_gpu_health_warning_email(error_message=error_message)
        except Exception:
            logger.exception("Error in GPU health monitoring")

        await asyncio.sleep(interval_seconds)
