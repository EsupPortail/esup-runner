"""Optional branded email reports for the standalone MediaMTX installer."""

import os
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlsplit

from app.services.email_templates import build_branded_email_message

SETTING_KEYS = (
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USE_TLS",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_SENDER",
    "MANAGER_EMAIL",
    "MANAGER_PUBLIC_URL",
    "MANAGER_URL",
)


def smtp_settings(root: Path) -> dict[str, str]:
    """Read one SMTP configuration without evaluating or interpolating secrets.

    Exported variables override the component's dotenv values. A Manager without
    complete settings can reuse runner/.env, as the stack updater does.
    """
    paths = [root / ".env"]
    if root.name == "manager":
        paths.append(root.parent / "runner" / ".env")
    for path in paths:
        values: dict[str, str | None] = {}
        if path.is_file():
            from dotenv import dotenv_values

            values = dict(dotenv_values(path, interpolate=False))
        settings = {key: os.environ.get(key, values.get(key) or "") for key in SETTING_KEYS}
        if settings["SMTP_SERVER"].strip() and settings["MANAGER_EMAIL"].strip():
            return settings
    return {}


def manager_admin_url(settings: dict[str, str]) -> str:
    """Return a linkable Manager URL without credentials, queries or fragments."""
    value = (settings["MANAGER_PUBLIC_URL"] or settings["MANAGER_URL"]).strip().rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return value if parsed.path.endswith("/admin") else f"{value}/admin"


def compose_install_report(
    *,
    settings: dict[str, str],
    root: Path,
    command: str,
    previous_version: str | None,
    installed_version: str | None,
    changed: bool,
    failed: bool,
) -> EmailMessage:
    """Compose the branded outcome report without including private diagnostics."""
    recipient = settings["MANAGER_EMAIL"].strip()
    sender = settings["SMTP_SENDER"].strip() or (
        "esup-runner@" + (recipient.split("@", 1)[1] if "@" in recipient else "localhost")
    )
    status = "FAILED" if failed else "SUCCESS"
    version = "not verified" if failed else installed_version or "unknown"
    subject = f"[esup-runner] {status} - MediaMTX {version}"
    if failed:
        title = "MediaMTX operation failed"
        summary = (
            "The MediaMTX command failed. Review its console output for diagnostics "
            "and rollback instructions."
        )
    elif not changed:
        subject += " - No updates"
        title = "MediaMTX already installed"
        summary = (
            "No updates were applied to the MediaMTX executable. "
            f"Version {version} is already installed."
        )
    elif previous_version:
        title = "MediaMTX update completed"
        summary = f"MediaMTX was updated from {previous_version} to {version}."
    else:
        title = "MediaMTX installation completed"
        summary = f"MediaMTX {version} was installed."
    admin_url = manager_admin_url(settings)
    details = [
        ("Workflow result", status),
        ("Component", root.name),
        ("Command", command),
        ("Executable updated", "not verified" if failed else "yes" if changed else "none"),
        ("Previous version", previous_version or "(not installed)"),
        ("Installed version", version),
        ("Host", socket.getfqdn()),
        ("Date", datetime.now().astimezone().isoformat(timespec="seconds")),
    ]
    if admin_url:
        details.append(("Manager admin", admin_url))
    footer = "This message was generated automatically by ESUP-Runner install_mediamtx.py."
    return build_branded_email_message(
        subject=subject,
        sender=sender,
        recipient=recipient,
        text_body="\n".join(
            [summary, "", *(f"{label}: {value}" for label, value in details), "", footer]
        ),
        product_name="ESUP-Runner",
        eyebrow="MediaMTX installation report",
        title=title,
        summary=summary,
        status_label=status,
        tone="danger" if failed else "success",
        details=details,
        primary_block_title="",
        primary_block_body=None,
        action_label="Open manager",
        action_url=admin_url,
        footer=footer,
    )


def send_install_report(
    *,
    root: Path,
    command: str,
    previous_version: str | None,
    installed_version: str | None,
    changed: bool,
    failed: bool = False,
) -> str:
    """Send one result report, returning sent, skipped or failed.

    SMTP errors never undo installation work. Failure reports omit raw command
    diagnostics and do not claim a final version or a successful rollback.
    """
    try:
        settings = smtp_settings(root)
        if not settings:
            return "skipped"
        message = compose_install_report(
            settings=settings,
            root=root,
            command=command,
            previous_version=previous_version,
            installed_version=installed_version,
            changed=changed,
            failed=failed,
        )
        port = int(settings["SMTP_PORT"] or "25")
        if not 1 <= port <= 65535:
            raise ValueError("Invalid SMTP port")
        with smtplib.SMTP(settings["SMTP_SERVER"].strip(), port, timeout=10) as smtp:
            if settings["SMTP_USE_TLS"].strip().lower() in {"1", "true", "t", "yes", "y", "on"}:
                smtp.starttls(context=ssl.create_default_context())
            if settings["SMTP_USERNAME"] and settings["SMTP_PASSWORD"]:
                smtp.login(settings["SMTP_USERNAME"], settings["SMTP_PASSWORD"])
            smtp.send_message(message)
        return "sent"
    except Exception:
        # SMTP replies, invalid settings and parser errors can contain credentials.
        return "failed"
