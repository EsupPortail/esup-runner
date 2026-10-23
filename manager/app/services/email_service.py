"""Email notifications for manager-side task and health warnings."""

import asyncio
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services.email_templates import build_branded_email_message

logger = setup_default_logging()

_LOGO_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logo.png"


def _is_email_configured() -> bool:
    """Return True when SMTP settings and a recipient are configured."""
    return bool(config.SMTP_SERVER and config.MANAGER_EMAIL)


def _build_sender_address() -> str:
    """Build the manager notification sender address."""
    sender = (config.SMTP_SENDER or "").strip()
    if sender:
        return sender

    manager_email = (config.MANAGER_EMAIL or "").strip()
    if "@" in manager_email:
        domain = manager_email.split("@", 1)[1]
        return f"esup-runner-manager@{domain}"

    host = config.MANAGER_HOST or "localhost"
    return f"esup-runner-manager@{host}"


def _tone_for_status(status: str) -> str:
    """Map a task status to the branded email tone."""
    normalized = status.lower()
    if normalized in {"failed", "error"}:
        return "danger"
    if normalized in {"warning", "timeout", "cancelled", "canceled"}:
        return "warning"
    return "info"


def _compose_notify_retry_exhausted_email(
    *,
    task_id: str,
    status: str,
    notify_url: str,
    attempts: int,
    error_message: str | None,
) -> EmailMessage:
    """Compose the notification sent after callback retry exhaustion."""
    subject = f"[esup-runner] Task {task_id} notify callback warning"
    body_lines = [
        f"Task: {task_id}",
        f"Status: {status}",
        f"Notify URL: {notify_url}",
        f"Retry attempts exhausted: {attempts}",
        "",
        "Last known error:",
        error_message or "(no details)",
    ]

    return build_branded_email_message(
        subject=subject,
        sender=_build_sender_address(),
        recipient=config.MANAGER_EMAIL,
        text_body="\n".join(body_lines),
        product_name="ESUP-Runner Manager",
        eyebrow="Manager callback warning",
        title="Notify callback retries exhausted",
        summary="The manager could not deliver a task completion callback after all retries.",
        status_label=status.upper(),
        tone=_tone_for_status(status),
        details=[
            ("Task", task_id),
            ("Status", status),
            ("Notify URL", notify_url),
            ("Retry attempts exhausted", str(attempts)),
        ],
        primary_block_title="Last known error",
        primary_block_body=error_message or "(no details)",
        footer="This message was generated automatically by the ESUP-Runner manager.",
        logo_path=_LOGO_PATH,
    )


async def send_notify_retry_exhausted_email(
    *,
    task_id: str,
    status: str,
    notify_url: str,
    attempts: int,
    error_message: str | None,
) -> bool:
    """Send the manager callback retry-exhausted email when configured."""
    if not _is_email_configured():
        logger.warning("SMTP_SERVER or MANAGER_EMAIL not configured: email notification skipped")
        return False

    message = _compose_notify_retry_exhausted_email(
        task_id=task_id,
        status=status,
        notify_url=notify_url,
        attempts=attempts,
        error_message=error_message,
    )
    return await _send_notification_email(message, f"task {task_id} notify retry exhaustion")


async def send_mediamtx_health_warning_email(*, error_message: str) -> bool:
    """Send a Manager MediaMTX warning using the existing branded email format."""
    if not _is_email_configured():
        logger.warning(
            "SMTP_SERVER or MANAGER_EMAIL not configured: MediaMTX warning email skipped"
        )
        return False

    details = [
        ("Host", config.MANAGER_HOST),
        ("Manager URL", config.MANAGER_PUBLIC_URL),
        ("Service", "MediaMTX"),
        ("Check interval", f"{config.LIVE.mediamtx_health_check_interval_hours} hours"),
    ]
    summary = "The Manager MediaMTX health check failed. Live streaming may be unavailable."
    message = build_branded_email_message(
        subject=f"[esup-runner] MediaMTX warning on {config.MANAGER_HOST}: health check failed",
        sender=_build_sender_address(),
        recipient=config.MANAGER_EMAIL,
        text_body="\n".join(
            [summary, "", *(f"{label}: {value}" for label, value in details), "", error_message]
        ),
        product_name="ESUP-Runner Manager",
        eyebrow="MediaMTX health warning",
        title="Manager MediaMTX unavailable",
        summary=summary,
        status_label="WARNING",
        tone="warning",
        details=details,
        primary_block_title="MediaMTX diagnostics",
        primary_block_body=error_message,
        action_label="Open manager",
        action_url=f"{config.MANAGER_PUBLIC_URL.rstrip('/')}/admin",
        footer="This message was generated automatically by the ESUP-Runner manager.",
        logo_path=_LOGO_PATH,
    )
    return await _send_notification_email(message, f"MediaMTX health on {config.MANAGER_HOST}")


async def _send_notification_email(message: EmailMessage, context: str) -> bool:
    """Deliver a composed notification through SMTP without blocking the event loop."""

    def _send() -> None:
        """Send the composed email through the configured SMTP server."""
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=10) as smtp:
            if config.SMTP_USE_TLS:
                smtp.starttls()
            if config.SMTP_USERNAME and config.SMTP_PASSWORD:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)

    try:
        await asyncio.to_thread(_send)
        logger.info("Email notification sent for %s", context)
        return True
    except Exception as exc:
        logger.error("Failed to send email for %s: %s", context, exc)
        return False
