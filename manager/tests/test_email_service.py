"""Validates email configuration, sender address building, and notify retry message composition."""

from __future__ import annotations

from email.message import EmailMessage

import pytest

import app.services.email_service as email_service_module
import app.services.email_templates as email_templates_module


def _message_body(message, content_type):
    body = message.get_body((content_type,))
    assert body is not None
    return body.get_content()


def _message_content_ids(message):
    return {part.get("Content-ID") for part in message.walk() if part.get("Content-ID")}


def test_email_helper_configuration_and_sender(monkeypatch):
    """Validate Email helper configuration and sender."""
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "")
    assert email_service_module._is_email_configured() is False

    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "ops@example.org")
    assert email_service_module._is_email_configured() is True

    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", " sender@example.org ")
    assert email_service_module._build_sender_address() == "sender@example.org"

    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "ops@example.org")
    assert email_service_module._build_sender_address() == "esup-runner-manager@example.org"

    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "invalid-email")
    monkeypatch.setattr(email_service_module.config, "MANAGER_HOST", "manager.local")
    assert email_service_module._build_sender_address() == "esup-runner-manager@manager.local"


def test_compose_notify_retry_exhausted_email(monkeypatch):
    """Validate Compose notify retry exhausted email."""
    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "ops@example.org")

    message = email_service_module._compose_notify_retry_exhausted_email(
        task_id="task-1",
        status="warning",
        notify_url="https://example.org/notify",
        attempts=5,
        error_message=None,
    )

    assert message["Subject"] == "[esup-runner-manager] Task task-1 notify callback warning"
    assert message["From"] == "esup-runner-manager@example.org"
    assert message["To"] == "ops@example.org"
    assert message.is_multipart()
    body = _message_body(message, "plain")
    assert "Task: task-1" in body
    assert "Status: warning" in body
    assert "Retry attempts exhausted: 5" in body
    assert "(no details)" in body
    html_body = _message_body(message, "html")
    assert "Notify callback retries exhausted" in html_body
    assert "Manager callback warning" in html_body
    assert "cid:esup-runner-logo" in html_body
    assert "<esup-runner-logo>" in _message_content_ids(message)


def test_email_status_tones_and_template_optional_branches():
    """Validate Email status tones and template optional branches."""
    assert email_service_module._tone_for_status("failed") == "danger"
    assert email_service_module._tone_for_status("completed") == "info"

    html = email_templates_module.render_html_email(
        product_name="ESUP-Runner Manager",
        eyebrow="Preview",
        title="Email preview",
        summary="HTML rendering preview",
        status_label="TEST",
        tone="unknown-tone",
        details=[("Task", "task-1"), ("Empty", None)],
        primary_block_title="No block",
        primary_block_body=None,
        secondary_block_title="Diagnostics",
        secondary_block_body="secondary details",
        action_label="Open manager",
        action_url="https://manager.example.org/admin",
        footer="Footer",
        logo_cid=None,
    )

    assert "ESUP-Runner Manager" in html
    assert "secondary details" in html
    assert "Open manager" in html
    assert "https://manager.example.org/admin" in html
    assert "(none)" in html


def test_attach_inline_logo_ignores_missing_logo_and_plain_messages(tmp_path):
    """Validate Attach inline logo ignores missing logo and plain messages."""
    html_message = EmailMessage()
    html_message.set_content("plain")
    html_message.add_alternative("<p>html</p>", subtype="html")

    email_templates_module.attach_inline_logo(
        html_message,
        tmp_path / "missing-logo.png",
        "missing-logo",
    )
    assert "missing-logo" not in _message_content_ids(html_message)

    plain_message = EmailMessage()
    plain_message.set_content("plain only")
    logo_path = tmp_path / "logo.png"
    logo_path.write_bytes(b"png")

    email_templates_module.attach_inline_logo(plain_message, logo_path, "plain-logo")
    assert "plain-logo" not in _message_content_ids(plain_message)


@pytest.mark.asyncio
async def test_send_notify_retry_exhausted_email_skips_when_not_configured(monkeypatch):
    """Validate Send notify retry exhausted email skips when not configured."""
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "")

    sent = await email_service_module.send_notify_retry_exhausted_email(
        task_id="task-1",
        status="warning",
        notify_url="https://example.org/notify",
        attempts=5,
        error_message="boom",
    )
    assert sent is False


@pytest.mark.asyncio
async def test_send_notify_retry_exhausted_email_success(monkeypatch):
    """Validate Send notify retry exhausted email success."""
    captured = {}

    class FakeSMTP:
        def __init__(self, server, port, timeout):
            captured["init"] = (server, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            captured["tls"] = True

        def login(self, username, password):
            captured["login"] = (username, password)

        def send_message(self, message):
            captured["subject"] = message["Subject"]
            captured["to"] = message["To"]
            captured["message"] = message

    async def fake_to_thread(func):
        func()

    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "SMTP_PORT", 2525)
    monkeypatch.setattr(email_service_module.config, "SMTP_USE_TLS", True)
    monkeypatch.setattr(email_service_module.config, "SMTP_USERNAME", "user")
    monkeypatch.setattr(email_service_module.config, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "manager@example.org")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "ops@example.org")
    monkeypatch.setattr(email_service_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_service_module.asyncio, "to_thread", fake_to_thread)

    sent = await email_service_module.send_notify_retry_exhausted_email(
        task_id="task-2",
        status="warning",
        notify_url="https://example.org/notify",
        attempts=3,
        error_message="callback timeout",
    )

    assert sent is True
    assert captured["init"] == ("smtp.example.org", 2525, 10)
    assert captured["tls"] is True
    assert captured["login"] == ("user", "secret")
    assert "task-2" in captured["subject"]
    assert captured["to"] == "ops@example.org"
    assert "callback timeout" in _message_body(captured["message"], "html")


@pytest.mark.asyncio
async def test_send_notify_retry_exhausted_email_returns_false_on_send_error(monkeypatch):
    """Validate Send notify retry exhausted email returns false on send error."""
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "ops@example.org")

    async def failing_to_thread(_func):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_service_module.asyncio, "to_thread", failing_to_thread)

    sent = await email_service_module.send_notify_retry_exhausted_email(
        task_id="task-3",
        status="warning",
        notify_url="https://example.org/notify",
        attempts=2,
        error_message="boom",
    )
    assert sent is False
