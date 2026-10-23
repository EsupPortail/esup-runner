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
        await health_module._monitor_mediamtx()

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
        await health_module._monitor_mediamtx()
    assert sleeps == [43200] * 3
    assert email.await_count == 2


@pytest.mark.asyncio
async def test_cancellation_during_probe_propagates(monkeypatch):
    client = Mock(check_health=AsyncMock(side_effect=asyncio.CancelledError))
    email = AsyncMock()
    monkeypatch.setattr(health_module, "MediaMTXClient", Mock(return_value=client))
    monkeypatch.setattr(health_module, "send_mediamtx_health_warning_email", email)
    with pytest.raises(asyncio.CancelledError):
        await health_module._monitor_mediamtx()
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
    monkeypatch.setattr(email_module.config, "MANAGER_HOST", "media-host.example.org")

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
    assert message["Subject"] == (
        "[esup-runner] MediaMTX warning on media-host.example.org: health check failed"
    )
    assert message["From"] == "service@example.org"
    assert message["To"] == "admins@example.org"
    plain = message.get_body(("plain",)).get_content()
    html = message.get_body(("html",)).get_content()
    for value in (diagnostic, "media-host.example.org", "12 hours", "Manager MediaMTX"):
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
async def test_workers_elect_one_monitor_and_take_over_on_shutdown(monkeypatch, tmp_path):
    from app.core import state

    monkeypatch.setattr(state.persistence, "data_directory", tmp_path)
    entered = asyncio.Queue()

    async def monitor():
        await entered.put(True)
        await asyncio.Future()

    real_sleep = asyncio.sleep
    follower_waiting = asyncio.Event()
    retry = asyncio.Event()

    async def sleep(seconds):
        assert seconds == 60
        follower_waiting.set()
        await retry.wait()
        await real_sleep(0)

    monkeypatch.setattr(health_module, "_monitor_mediamtx", monitor)
    monkeypatch.setattr(health_module.asyncio, "sleep", sleep)
    leader = asyncio.create_task(health_module.mediamtx_health_check_loop())
    await asyncio.wait_for(entered.get(), 1)
    follower = asyncio.create_task(health_module.mediamtx_health_check_loop())
    try:
        await asyncio.wait_for(follower_waiting.wait(), 1)
        assert entered.empty()
        leader.cancel()
        await asyncio.gather(leader, return_exceptions=True)
        retry.set()
        await asyncio.wait_for(entered.get(), 1)
    finally:
        leader.cancel()
        follower.cancel()
        await asyncio.gather(leader, follower, return_exceptions=True)

    # Cancellation must release the lock for a fresh worker as well.
    restarted = asyncio.create_task(health_module.mediamtx_health_check_loop())
    try:
        await asyncio.wait_for(entered.get(), 1)
    finally:
        restarted.cancel()
        await asyncio.gather(restarted, return_exceptions=True)


@pytest.mark.asyncio
async def test_disabled_interval_does_not_start_manager_monitor(monkeypatch):
    from app.services import background_service as services

    monkeypatch.setattr(
        health_module.config,
        "LIVE",
        LiveSettings(enabled=True, mediamtx_health_check_interval_hours=0),
    )

    async def wait_forever():
        await asyncio.Future()

    for name in (
        "check_runners_activity",
        "cleanup_old_tasks",
        "check_task_timeouts",
        "reconcile_running_tasks_with_runners",
    ):
        monkeypatch.setattr(services, name, wait_forever)
    monkeypatch.setattr("app.services.live_service.reconcile_forever", wait_forever)
    manager = services.BackgroundServiceManager()
    await manager.start_all_services()
    try:
        assert "mediamtx_health_check_loop" not in {task.get_name() for task in manager.tasks}
    finally:
        await manager.stop_all_services()
