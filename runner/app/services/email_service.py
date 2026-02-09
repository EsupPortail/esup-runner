# runner/app/services/email_service.py
"""
Email notification service for Runner.
Sends failure notifications to the manager via SMTP.
"""

import asyncio
import smtplib
from email.message import EmailMessage
from typing import Optional

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_runner_id, get_runner_instance_url

logger = setup_default_logging()


def _build_sender_address() -> str:
    sender = config.SMTP_SENDER
    if sender:
        return str(sender)

    manager_email = (config.MANAGER_EMAIL or "").strip()
    if "@" in manager_email:
        domain = manager_email.split("@", 1)[1]
        return f"esup-runner@{domain}"

    host = config.RUNNER_HOST or "localhost"
    return f"esup-runner@{host}"


def _is_email_configured() -> bool:
    return bool(config.SMTP_SERVER and config.MANAGER_EMAIL)


def _compose_failure_email(
    *,
    task_id: str,
    task_type: str,
    status: str,
    error_message: str,
    script_output: Optional[str],
) -> EmailMessage:
    runner_id = get_runner_id() or "unknown-runner"
    runner_url = get_runner_instance_url() or "unknown-url"

    subject = f"[esup-runner] Task {task_id} {status}"

    body_lines = [
        f"Task: {task_id}",
        f"Type: {task_type}",
        f"Status: {status}",
        f"Runner: {runner_id}",
        f"Runner URL: {runner_url}",
        "",
        "Error:",
        error_message or "(no details)",
    ]

    if script_output:
        body_lines.extend(["", "Script output:", script_output])

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = _build_sender_address()
    msg["To"] = config.MANAGER_EMAIL
    msg.set_content("\n".join(body_lines))
    return msg


async def send_task_failure_email(
    *,
    task_id: str,
    task_type: str,
    status: str,
    error_message: str,
    script_output: Optional[str] = None,
) -> bool:
    if not _is_email_configured():
        logger.warning("SMTP_SERVER or MANAGER_EMAIL not configured: email notification skipped")
        return False

    message = _compose_failure_email(
        task_id=task_id,
        task_type=task_type,
        status=status,
        error_message=error_message,
        script_output=script_output,
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
        logger.info("Email notification sent for task %s", task_id)
        return True
    except Exception as exc:
        logger.error("Failed to send email for task %s: %s", task_id, exc)
        return False
