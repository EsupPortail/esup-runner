from __future__ import annotations

import pytest

import app.services.email_service as email_service_module


def test_email_helper_configuration_and_sender(monkeypatch):
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
    body = message.get_content()
    assert "Task: task-1" in body
    assert "Status: warning" in body
    assert "Retry attempts exhausted: 5" in body
    assert "(no details)" in body


@pytest.mark.asyncio
async def test_send_notify_retry_exhausted_email_skips_when_not_configured(monkeypatch):
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


@pytest.mark.asyncio
async def test_send_notify_retry_exhausted_email_returns_false_on_send_error(monkeypatch):
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
