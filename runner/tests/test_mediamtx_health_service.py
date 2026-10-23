"""Regression tests for periodic MediaMTX monitoring and alert emails."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Config
from app.core.live_config import LiveSettings
from app.services import email_service as email_module
from app.services import mediamtx_health_service as health_module
from app.services.mediamtx import MediaMTXError


@pytest.mark.parametrize("value,expected", [(None, 12), ("0", 0), ("12", 12), ("24", 24)])
def test_health_interval_configuration(monkeypatch, value, expected):
    monkeypatch.delenv("LIVE_MEDIAMTX_HEALTH_CHECK_INTERVAL_HOURS", raising=False)
    if value is not None:
        monkeypatch.setenv("LIVE_MEDIAMTX_HEALTH_CHECK_INTERVAL_HOURS", value)
    assert LiveSettings.from_environment().mediamtx_health_check_interval_hours == expected
    assert Config().LIVE.mediamtx_health_check_interval_hours == expected


@pytest.mark.parametrize("value", ["-1", "1.5", "invalid", "", "nan", "inf"])
def test_invalid_health_interval_is_reported(monkeypatch, value):
    monkeypatch.setenv("LIVE_MEDIAMTX_HEALTH_CHECK_INTERVAL_HOURS", value)
    with pytest.raises(ValueError):
        LiveSettings.from_environment()
    assert "Invalid LIVE configuration" in Config()._configuration_errors


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_hours", [12, 24])
async def test_checks_immediately_repeats_alerts_and_recovers(monkeypatch, interval_hours):
    settings = LiveSettings(
        mediamtx_api_url="http://local-media:9997",
        mediamtx_api_user="control",
        mediamtx_api_password="private-password",
        mediamtx_health_check_interval_hours=interval_hours,
    )
    monkeypatch.setattr(health_module.config, "LIVE", settings)
    check = AsyncMock(
        side_effect=[None, MediaMTXError("unavailable"), MediaMTXError("HTTP 401"), None]
    )
    client = Mock(check_health=check)
    factory = Mock(return_value=client)
    email = AsyncMock(return_value=True)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        assert check.await_count == len(sleeps)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(health_module, "MediaMTXClient", factory)
    monkeypatch.setattr(health_module, "send_mediamtx_health_warning_email", email)
    monkeypatch.setattr(health_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await health_module.mediamtx_health_check_loop()

    factory.assert_called_once_with("http://local-media:9997", "control", "private-password", 5)
    assert sleeps == [interval_hours * 3600] * 4
    assert email.await_count == 2
    assert [call.kwargs["error_message"] for call in email.await_args_list] == [
        "unavailable",
        "HTTP 401",
    ]


@pytest.mark.asyncio
async def test_monitor_survives_unexpected_errors_and_email_failure(monkeypatch):
    monkeypatch.setattr(health_module.config.LIVE, "mediamtx_health_check_interval_hours", 12)
    check = AsyncMock(
        side_effect=[RuntimeError("probe failed"), MediaMTXError("down"), MediaMTXError("down")]
    )
    email = AsyncMock(side_effect=[RuntimeError("SMTP failed"), False])
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        health_module, "MediaMTXClient", Mock(return_value=Mock(check_health=check))
    )
    monkeypatch.setattr(health_module, "send_mediamtx_health_warning_email", email)
    monkeypatch.setattr(health_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await health_module.mediamtx_health_check_loop()
    assert sleeps == [43200] * 3
    assert email.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_during_probe_propagates(monkeypatch):
    client = Mock(check_health=AsyncMock(side_effect=asyncio.CancelledError))
    email = AsyncMock()
    monkeypatch.setattr(health_module, "MediaMTXClient", Mock(return_value=client))
    monkeypatch.setattr(health_module, "send_mediamtx_health_warning_email", email)
    with pytest.raises(asyncio.CancelledError):
        await health_module.mediamtx_health_check_loop()
    email.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["SMTP_SERVER", "MANAGER_EMAIL"])
async def test_health_alert_skips_missing_smtp_configuration(monkeypatch, missing):
    monkeypatch.setattr(email_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_module.config, "MANAGER_EMAIL", "admins@example.org")
    monkeypatch.setattr(email_module.config, missing, "")
    smtp = Mock()
    monkeypatch.setattr(email_module.smtplib, "SMTP", smtp)
    assert await email_module.send_mediamtx_health_warning_email(error_message="down") is False
    smtp.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("secure", [False, True])
async def test_health_alert_format_delivery_and_failure(monkeypatch, secure):
    for name, value in {
        "SMTP_SERVER": "smtp.example.org",
        "SMTP_PORT": 2525,
        "SMTP_SENDER": "service@example.org",
        "SMTP_USE_TLS": secure,
        "SMTP_USERNAME": "user" if secure else "",
        "SMTP_PASSWORD": "smtp-password" if secure else "",
        "MANAGER_EMAIL": "admins@example.org",
        "MANAGER_PUBLIC_URL": "https://manager.example.org/prefix",
    }.items():
        monkeypatch.setattr(email_module.config, name, value)
    monkeypatch.setattr(email_module.config.LIVE, "mediamtx_health_check_interval_hours", 12)
    monkeypatch.setattr(email_module.config.LIVE, "mediamtx_api_password", "private-password")
    monkeypatch.setattr(email_module.config, "RUNNER_HOST", "media-host.example.org")
    monkeypatch.setattr(email_module, "get_runner_id", lambda: "live-runner-1")
    monkeypatch.setattr(email_module, "get_runner_instance_url", lambda: "http://media-host:8083")

    async def run_inline(func):
        return func()

    smtp = Mock()
    smtp_factory = Mock()
    smtp_factory.return_value.__enter__ = Mock(return_value=smtp)
    smtp_factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(email_module.smtplib, "SMTP", smtp_factory)
    monkeypatch.setattr(email_module.asyncio, "to_thread", run_inline)
    diagnostic = "MediaMTX control unavailable <error>"
    assert await email_module.send_mediamtx_health_warning_email(error_message=diagnostic) is True

    smtp_factory.assert_called_once_with("smtp.example.org", 2525, timeout=10)
    assert smtp.starttls.call_count == int(secure)
    assert smtp.login.call_count == int(secure)
    if secure:
        smtp.login.assert_called_once_with("user", "smtp-password")
    message = smtp.send_message.call_args.args[0]
    assert "MediaMTX warning on" in message["Subject"]
    assert message["From"] == "service@example.org"
    assert message["To"] == "admins@example.org"
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    for value in (diagnostic, "media-host.example.org", "12 hours", "Runner MediaMTX"):
        assert value in plain
    assert "MediaMTX health warning" in html
    assert "WARNING" in html
    assert "&lt;error&gt;" in html
    assert "<error>" not in html
    assert "https://manager.example.org/prefix/admin" in html
    assert "cid:esup-runner-logo" in html
    assert "private-password" not in message.as_string()

    smtp.send_message.side_effect = OSError("SMTP unavailable")
    assert await email_module.send_mediamtx_health_warning_email(error_message=diagnostic) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_types,interval,instance,groups,enabled",
    [
        ({"encoding"}, 12, 0, [{"encoding"}, {"live"}], False),
        ({"live"}, 0, 1, [{"encoding"}, {"live"}], False),
        ({"live"}, 12, 1, [{"encoding"}, {"live"}, {"live"}], True),
        ({"live"}, 12, 2, [{"encoding"}, {"live"}, {"live"}], False),
        ({"live"}, 12, 0, [{"live"}, {"live"}], True),
        ({"live"}, 12, 0, [], False),
        ({"live"}, 12, 0, [{"encoding"}], False),
    ],
)
async def test_starts_only_on_first_enabled_live_instance(
    monkeypatch, task_types, interval, instance, groups, enabled
):
    from app.managers import service_manager as services

    monkeypatch.setattr(services.config, "RUNNER_TASK_TYPES", task_types)
    monkeypatch.setattr(services.config, "RUNNER_TASK_TYPES_BY_INSTANCE", groups)
    monkeypatch.setattr(services.config.LIVE, "mediamtx_health_check_interval_hours", interval)
    monkeypatch.setattr(services.config.LIVE, "pod_url", "")
    monkeypatch.setattr(services.config, "ENCODING_TYPE", "CPU")
    monkeypatch.setattr(services, "get_runner_instance_id", lambda: instance)

    async def wait_forever():
        await asyncio.Future()

    for name in (
        "reconnect_loop",
        "heartbeat_loop",
        "storage_cleanup_loop",
        "task_process_check_loop",
        "mediamtx_health_check_loop",
    ):
        monkeypatch.setattr(services, name, wait_forever)
    manager = services.BackgroundServiceManager()
    for _ in range(2):
        await manager.start_all_services()
        await manager.start_all_services()
        tasks = list(manager.tasks)
        assert ("mediamtx_health_check_loop" in {task.get_name() for task in tasks}) is enabled
        await manager.stop_all_services()
        assert all(task.cancelled() for task in tasks)
        assert manager.tasks == []
