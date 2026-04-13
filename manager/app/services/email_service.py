"""Email notifications for manager-side task warnings."""

import asyncio
import smtplib
from email.message import EmailMessage

from app.core.config import config
from app.core.setup_logging import setup_default_logging

logger = setup_default_logging()


def _is_email_configured() -> bool:
    return bool(config.SMTP_SERVER and config.MANAGER_EMAIL)


def _build_sender_address() -> str:
    sender = (config.SMTP_SENDER or "").strip()
    if sender:
        return sender

    manager_email = (config.MANAGER_EMAIL or "").strip()
    if "@" in manager_email:
        domain = manager_email.split("@", 1)[1]
        return f"esup-runner-manager@{domain}"

    host = config.MANAGER_HOST or "localhost"
    return f"esup-runner-manager@{host}"


def _compose_notify_retry_exhausted_email(
    *,
    task_id: str,
    status: str,
    notify_url: str,
    attempts: int,
    error_message: str | None,
) -> EmailMessage:
    subject = f"[esup-runner-manager] Task {task_id} notify callback warning"
    body_lines = [
        f"Task: {task_id}",
        f"Status: {status}",
        f"Notify URL: {notify_url}",
        f"Retry attempts exhausted: {attempts}",
        "",
        "Last known error:",
        error_message or "(no details)",
    ]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _build_sender_address()
    message["To"] = config.MANAGER_EMAIL
    message.set_content("\n".join(body_lines))
    return message


async def send_notify_retry_exhausted_email(
    *,
    task_id: str,
    status: str,
    notify_url: str,
    attempts: int,
    error_message: str | None,
) -> bool:
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

    def _send() -> None:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=10) as smtp:
            if config.SMTP_USE_TLS:
                smtp.starttls()
            if config.SMTP_USERNAME and config.SMTP_PASSWORD:
                smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            smtp.send_message(message)

    try:
        await asyncio.to_thread(_send)
        logger.info("Notify retry exhausted email sent for task %s", task_id)
        return True
    except Exception as exc:
        logger.error("Failed to send notify retry exhausted email for task %s: %s", task_id, exc)
        return False
