"""Regression tests for periodic GPU monitoring and administrator warnings."""

import asyncio
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

import app.core.config as config_module
import app.managers.service_manager as service_manager_module
import app.services.email_service as email_service_module
import app.services.gpu_health_service as gpu_health_module


@pytest.mark.parametrize("value,expected", [(None, 12), ("0", 0), ("12", 12), ("24", 24)])
def test_gpu_health_interval_configuration(monkeypatch, value, expected):
    monkeypatch.delenv("GPU_HEALTH_CHECK_INTERVAL_HOURS", raising=False)
    if value is not None:
        monkeypatch.setenv("GPU_HEALTH_CHECK_INTERVAL_HOURS", value)

    cfg = config_module.Config()

    assert cfg.GPU_HEALTH_CHECK_INTERVAL_HOURS == expected
    cfg._validate_numeric_limits()


@pytest.mark.parametrize("value", ["-1", "invalid", "1.5", ""])
def test_gpu_health_interval_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("GPU_HEALTH_CHECK_INTERVAL_HOURS", value)
    cfg = config_module.Config()

    assert cfg.GPU_HEALTH_CHECK_INTERVAL_HOURS == 12
    assert any("GPU_HEALTH_CHECK_INTERVAL_HOURS" in error for error in cfg._configuration_errors)
    with pytest.raises(
        config_module.ConfigValidationError, match="GPU_HEALTH_CHECK_INTERVAL_HOURS"
    ):
        cfg.validate_configuration()


def test_gpu_health_interval_validation_after_loading():
    cfg = config_module.Config()
    cfg.GPU_HEALTH_CHECK_INTERVAL_HOURS = -1

    with pytest.raises(
        config_module.ConfigValidationError, match="GPU_HEALTH_CHECK_INTERVAL_HOURS"
    ):
        cfg._validate_numeric_limits()


@pytest.mark.parametrize(
    "returncode,stdout,stderr,expected",
    [
        (0, "GPU information", "", None),
        (9, "", "NVIDIA-SMI couldn't communicate with the NVIDIA driver.", "NVIDIA driver"),
        (1, "stdout diagnostic", "", "stdout diagnostic"),
        (
            1,
            " stdout diagnostic \n",
            " stderr diagnostic \n",
            "stderr diagnostic\nstdout diagnostic",
        ),
        (1, " \n", "", "(no diagnostic output)"),
        (1, "", "x" * 5000, "x" * 100),
    ],
)
def test_nvidia_smi_check_handles_status_and_diagnostics(
    monkeypatch, returncode, stdout, stderr, expected
):
    run = Mock(return_value=subprocess.CompletedProcess(["nvidia-smi"], returncode, stdout, stderr))
    monkeypatch.setattr(gpu_health_module.subprocess, "run", run)

    diagnostic = gpu_health_module._check_nvidia_smi()

    run.assert_called_once_with(
        ["nvidia-smi"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if expected is None:
        assert diagnostic is None
    else:
        assert expected in diagnostic
        assert f"code {returncode}" in diagnostic
        assert len(diagnostic) <= 4000


@pytest.mark.parametrize(
    "error,expected",
    [
        (FileNotFoundError("nvidia-smi missing"), "Cannot execute nvidia-smi"),
        (PermissionError("permission denied"), "permission denied"),
        (subprocess.TimeoutExpired(["nvidia-smi"], 10), "timed out after 10 seconds"),
    ],
)
def test_nvidia_smi_check_handles_missing_command_permissions_and_timeout(
    monkeypatch, error, expected
):
    monkeypatch.setattr(gpu_health_module.subprocess, "run", Mock(side_effect=error))

    assert expected in gpu_health_module._check_nvidia_smi()


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_hours", [12, 24])
async def test_gpu_health_loop_checks_immediately_repeats_warnings_and_recovers(
    monkeypatch, interval_hours
):
    monkeypatch.setattr(gpu_health_module.config, "GPU_HEALTH_CHECK_INTERVAL_HOURS", interval_hours)
    checks = Mock(side_effect=[None, "driver unavailable", "driver unavailable", None])
    email = AsyncMock(return_value=True)
    sleeps = []

    async def run_inline(func):
        return func()

    async def sleep(seconds):
        sleeps.append(seconds)
        # Each check, including the initial one, must precede its wait.
        assert checks.call_count == len(sleeps)
        if len(sleeps) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(gpu_health_module, "_check_nvidia_smi", checks)
    monkeypatch.setattr(gpu_health_module, "send_gpu_health_warning_email", email)
    monkeypatch.setattr(gpu_health_module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(gpu_health_module.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await gpu_health_module.gpu_health_check_loop()

    assert checks.call_count == 4
    assert sleeps == [interval_hours * 3600] * 4
    assert email.await_count == 2
    email.assert_awaited_with(error_message="driver unavailable")


@pytest.mark.asyncio
async def test_gpu_health_loop_survives_unexpected_errors_and_failed_email(monkeypatch):
    monkeypatch.setattr(gpu_health_module.config, "GPU_HEALTH_CHECK_INTERVAL_HOURS", 12)
    checks = Mock(
        side_effect=[RuntimeError("unexpected probe failure"), "driver down", "driver down"]
    )
    email = AsyncMock(side_effect=[RuntimeError("unexpected SMTP failure"), False])
    sleeps = []

    async def run_inline(func):
        return func()

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(gpu_health_module, "_check_nvidia_smi", checks)
    monkeypatch.setattr(gpu_health_module, "send_gpu_health_warning_email", email)
    monkeypatch.setattr(gpu_health_module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(gpu_health_module.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await gpu_health_module.gpu_health_check_loop()

    assert checks.call_count == 3
    assert email.await_count == 2
    assert sleeps == [43200] * 3


@pytest.mark.asyncio
async def test_gpu_health_loop_propagates_cancellation_during_check(monkeypatch):
    check = AsyncMock(side_effect=asyncio.CancelledError)
    email = AsyncMock()
    monkeypatch.setattr(gpu_health_module.asyncio, "to_thread", check)
    monkeypatch.setattr(gpu_health_module, "send_gpu_health_warning_email", email)

    with pytest.raises(asyncio.CancelledError):
        await gpu_health_module.gpu_health_check_loop()

    email.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "encoding_type,interval_hours,instance_id,enabled",
    [("CPU", 12, 0, False), ("GPU", 0, 0, False), ("GPU", 12, 1, False), ("GPU", 12, 0, True)],
)
async def test_service_manager_starts_monitoring_only_on_enabled_primary_gpu_instance(
    monkeypatch, encoding_type, interval_hours, instance_id, enabled
):
    monkeypatch.setattr(service_manager_module.config.LIVE, "pod_url", "")
    monkeypatch.setattr(service_manager_module.config, "ENCODING_TYPE", encoding_type)
    monkeypatch.setattr(
        service_manager_module.config, "GPU_HEALTH_CHECK_INTERVAL_HOURS", interval_hours
    )
    monkeypatch.setattr(service_manager_module, "get_runner_instance_id", lambda: instance_id)

    async def wait_forever():
        await asyncio.Future()

    for name in (
        "reconnect_loop",
        "heartbeat_loop",
        "storage_cleanup_loop",
        "task_process_check_loop",
        "gpu_health_check_loop",
    ):
        monkeypatch.setattr(service_manager_module, name, wait_forever)

    manager = service_manager_module.BackgroundServiceManager()
    await manager.start_all_services()
    await manager.start_all_services()
    tasks = list(manager.tasks)
    try:
        names = {task.get_name() for task in tasks}
        assert ("gpu_health_check_loop" in names) is enabled
        assert len(tasks) == (5 if enabled else 4)
    finally:
        await manager.stop_all_services()

    assert all(task.cancelled() for task in tasks)
    assert manager.tasks == []


@pytest.mark.asyncio
async def test_service_manager_stops_active_gpu_monitor_and_rechecks_after_restart(monkeypatch):
    monkeypatch.setattr(service_manager_module.config, "ENCODING_TYPE", "GPU")
    monkeypatch.setattr(service_manager_module.config, "GPU_HEALTH_CHECK_INTERVAL_HOURS", 12)
    monkeypatch.setattr(service_manager_module, "get_runner_instance_id", lambda: 0)

    async def wait_forever():
        await asyncio.Future()

    for name in ("reconnect_loop", "heartbeat_loop", "storage_cleanup_loop"):
        monkeypatch.setattr(service_manager_module, name, wait_forever)

    checked = asyncio.Event()
    probe = Mock(return_value="driver unavailable")

    async def run_inline(func):
        return func()

    async def send_email(**_kwargs):
        checked.set()
        return True

    monkeypatch.setattr(gpu_health_module, "_check_nvidia_smi", probe)
    monkeypatch.setattr(gpu_health_module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(gpu_health_module, "send_gpu_health_warning_email", send_email)
    manager = service_manager_module.BackgroundServiceManager()
    for _ in range(2):
        checked.clear()
        await manager.start_all_services()
        try:
            await asyncio.wait_for(checked.wait(), timeout=1)
            gpu_task = next(
                task for task in manager.tasks if task.get_name() == "gpu_health_check_loop"
            )
        finally:
            await manager.stop_all_services()
        assert gpu_task.cancelled()
    assert probe.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["SMTP_SERVER", "MANAGER_EMAIL"])
async def test_gpu_warning_email_skips_incomplete_smtp(monkeypatch, missing):
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "admins@example.org")
    monkeypatch.setattr(email_service_module.config, missing, "")
    smtp = Mock()
    monkeypatch.setattr(email_service_module.smtplib, "SMTP", smtp)

    assert (
        await email_service_module.send_gpu_health_warning_email(error_message="driver down")
        is False
    )
    smtp.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_tls,use_auth", [(False, False), (True, True)])
async def test_gpu_warning_email_delivery_content_and_smtp_failure(monkeypatch, use_tls, use_auth):
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "SMTP_PORT", 2525)
    monkeypatch.setattr(email_service_module.config, "SMTP_USE_TLS", use_tls)
    monkeypatch.setattr(email_service_module.config, "SMTP_USERNAME", "user" if use_auth else "")
    monkeypatch.setattr(email_service_module.config, "SMTP_PASSWORD", "secret" if use_auth else "")
    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "runner@example.org")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "admins@example.org")
    monkeypatch.setattr(email_service_module.config, "RUNNER_HOST", "gpu-host.example.org")
    monkeypatch.setattr(email_service_module.config, "GPU_HEALTH_CHECK_INTERVAL_HOURS", 12)
    monkeypatch.setattr(email_service_module, "get_runner_id", lambda: "gpu-runner-0")
    monkeypatch.setattr(
        email_service_module, "get_runner_instance_url", lambda: "http://gpu-host:8082"
    )

    async def run_inline(func):
        return func()

    smtp = Mock()
    smtp_factory = Mock()
    smtp_factory.return_value.__enter__ = Mock(return_value=smtp)
    smtp_factory.return_value.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(email_service_module.asyncio, "to_thread", run_inline)
    monkeypatch.setattr(email_service_module.smtplib, "SMTP", smtp_factory)
    diagnostic = "NVIDIA-SMI couldn't communicate with the NVIDIA driver. <driver-error>"

    assert (
        await email_service_module.send_gpu_health_warning_email(error_message=diagnostic) is True
    )

    smtp_factory.assert_called_once_with("smtp.example.org", 2525, timeout=10)
    assert smtp.starttls.call_count == int(use_tls)
    assert smtp.login.call_count == int(use_auth)
    if use_auth:
        smtp.login.assert_called_once_with("user", "secret")
    message = smtp.send_message.call_args.args[0]
    assert "GPU warning on gpu-runner-0" in message["Subject"]
    assert message["From"] == "runner@example.org"
    assert message["To"] == "admins@example.org"
    plain_body = message.get_body(("plain",)).get_content()
    html_body = message.get_body(("html",)).get_content()
    for value in (
        diagnostic,
        "gpu-host.example.org",
        "gpu-runner-0",
        "http://gpu-host:8082",
        "12 hours",
    ):
        assert value in plain_body
    assert "GPU health warning" in html_body
    assert "&lt;driver-error&gt;" in html_body
    assert "<driver-error>" not in html_body

    smtp.send_message.side_effect = RuntimeError("SMTP unavailable")
    assert (
        await email_service_module.send_gpu_health_warning_email(error_message=diagnostic) is False
    )
