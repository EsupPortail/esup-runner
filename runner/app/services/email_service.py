# runner/app/services/email_service.py
"""
Email notification service for Runner.
Sends failure notifications to the manager via SMTP.
"""

import asyncio
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import get_runner_id, get_runner_instance_url
from app.services.email_templates import build_branded_email_message

logger = setup_default_logging()

_LOGO_PATH = Path(__file__).resolve().parents[1] / "web" / "static" / "logo.png"


def _build_sender_address() -> str:
    """Build the runner notification sender address."""
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
    """Return True when SMTP settings and a recipient are configured."""
    return bool(config.SMTP_SERVER and config.MANAGER_EMAIL)


def _tone_for_status(status: str) -> str:
    """Map a task status to the branded email tone."""
    normalized = status.lower()
    if normalized in {"failed", "error"}:
        return "danger"
    if normalized in {"timeout", "cancelled", "canceled", "warning"}:
        return "warning"
    return "info"


def _action_url(value: str) -> str | None:
    """Return a safe HTTP(S) action URL, or None when the value is not linkable."""
    return value if value.startswith(("http://", "https://")) else None


def _manager_admin_url() -> str:
    """Return the manager admin URL used in failure notifications."""
    manager_url = (config.MANAGER_URL or "").rstrip("/")
    if manager_url.endswith("/admin"):
        return manager_url
    return f"{manager_url}/admin"


def _compose_failure_email(
    *,
    task_id: str,
    task_type: str,
    status: str,
    error_message: str,
    script_output: Optional[str],
) -> EmailMessage:
    """Compose the runner task failure notification email."""
    runner_id = get_runner_id() or "unknown-runner"
    runner_url = get_runner_instance_url() or "unknown-url"
    manager_admin_url = _manager_admin_url()

    subject = f"[esup-runner] Task {task_id} {status}"

    body_lines = [
        f"Task: {task_id}",
        f"Type: {task_type}",
        f"Status: {status}",
        f"Runner: {runner_id}",
        f"Runner URL: {runner_url}",
        f"Manager admin: {manager_admin_url}",
        "",
        "Error:",
        error_message or "(no details)",
    ]

    if script_output:
        body_lines.extend(["", "Script output:", script_output])

    return build_branded_email_message(
        subject=subject,
        sender=_build_sender_address(),
        recipient=config.MANAGER_EMAIL,
        text_body="\n".join(body_lines),
        product_name="ESUP-Runner",
        eyebrow="Runner failure notification",
        title=f"Task {task_id} {status}",
        summary="A runner task ended in a non-success state and may need attention.",
        status_label=status.upper(),
        tone=_tone_for_status(status),
        details=[
            ("Task", task_id),
            ("Type", task_type),
            ("Status", status),
            ("Runner", runner_id),
            ("Runner URL", runner_url),
            ("Manager admin", manager_admin_url),
        ],
        primary_block_title="Error details",
        primary_block_body=error_message or "(no details)",
        secondary_block_title="Script output" if script_output else None,
        secondary_block_body=script_output,
        action_label="Open manager",
        action_url=_action_url(manager_admin_url),
        footer="This message was generated automatically by the ESUP-Runner runner.",
        logo_path=_LOGO_PATH,
    )


async def send_task_failure_email(
    *,
    task_id: str,
    task_type: str,
    status: str,
    error_message: str,
    script_output: Optional[str] = None,
) -> bool:
    """Send the runner task failure email when SMTP is configured."""
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
        """Send the composed email through the configured SMTP server."""
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
