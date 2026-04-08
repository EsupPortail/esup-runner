import asyncio
import os
import signal
import sys
import types

import pytest

import app.managers.process_manager as process_manager_module
import app.managers.service_manager as service_manager_module
import app.services.email_service as email_service_module


@pytest.fixture(autouse=True)
def cleanup_runner_process_env():
    yield
    for key in (
        "RUNNER_INSTANCE_ID",
        "RUNNER_PORT",
        "RUNNER_INSTANCE_URL",
        "GPU_CUDA_VISIBLE_DEVICES",
        "CUDA_VISIBLE_DEVICES",
        "GPU_HWACCEL_DEVICE",
    ):
        os.environ.pop(key, None)


class FakeProcess:
    next_pid = 1000

    def __init__(self, target=None, args=(), name="", daemon=False):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.pid = None
        self._alive = False
        self.join_calls = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.stubborn = False

    def start(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self._alive = True

    def is_alive(self):
        return self._alive

    def terminate(self):
        self.terminate_calls += 1
        if not self.stubborn:
            self._alive = False

    def kill(self):
        self.kill_calls += 1
        self._alive = False

    def join(self, timeout=None):
        self.join_calls.append(timeout)


def test_select_gpu_for_instance_handles_empty_and_round_robin():
    assert process_manager_module._select_gpu_for_instance(1, "") == ""
    assert process_manager_module._select_gpu_for_instance(3, "0,1") == "1"


def test_run_uvicorn_instance_in_gpu_mode_sets_environment(monkeypatch):
    captured = {}

    fake_config = types.SimpleNamespace(
        ENCODING_TYPE="GPU",
        GPU_CUDA_VISIBLE_DEVICES="0,1",
        GPU_HWACCEL_DEVICE=7,
        RUNNER_PROTOCOL="http",
        RUNNER_HOST="runner.example.org",
    )
    fake_uvicorn = types.ModuleType("uvicorn")

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    fake_uvicorn.run = fake_run

    monkeypatch.setattr(process_manager_module, "reload_config_from_env", lambda: fake_config)
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    import app.core.setup_logging as setup_logging_module

    monkeypatch.setattr(
        setup_logging_module,
        "get_uvicorn_log_config",
        lambda runner_instance_id, json_format=False: {
            "runner_instance_id": runner_instance_id,
            "json_format": json_format,
        },
    )

    process_manager_module.run_uvicorn_instance(3, 9103)

    assert os.environ["RUNNER_INSTANCE_ID"] == "3"
    assert os.environ["RUNNER_PORT"] == "9103"
    assert os.environ["RUNNER_INSTANCE_URL"] == "http://runner.example.org:9103"
    assert os.environ["GPU_CUDA_VISIBLE_DEVICES"] == "1"
    assert os.environ["CUDA_VISIBLE_DEVICES"] == "1"
    assert os.environ["GPU_HWACCEL_DEVICE"] == "0"
    assert fake_config.GPU_HWACCEL_DEVICE == 0
    assert fake_config.GPU_CUDA_VISIBLE_DEVICES == "1"
    assert captured["kwargs"]["host"] == "runner.example.org"
    assert captured["kwargs"]["port"] == 9103


def test_find_available_port_skips_busy_ports(monkeypatch):
    attempts = {"count": 0}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def bind(self, addr):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("busy")

    monkeypatch.setattr(process_manager_module.socket, "socket", lambda *_args: FakeSocket())
    manager = process_manager_module.UvicornProcessManager(base_port=9200, instances=1)
    assert manager._find_available_port(9200) == 9201


def test_process_manager_lifecycle_methods(monkeypatch):
    monkeypatch.setattr(process_manager_module, "Process", FakeProcess)
    monkeypatch.setattr(process_manager_module.time, "sleep", lambda *_args, **_kwargs: None)

    manager = process_manager_module.UvicornProcessManager(base_port=9300, instances=2)
    monkeypatch.setattr(
        manager, "_find_available_port", lambda port: port + 1 if port == 9300 else port
    )

    created = manager._create_uvicorn_process(9301, 0)
    assert isinstance(created, FakeProcess)
    assert created.target is process_manager_module.run_uvicorn_instance
    assert created.args == (0, 9301)
    assert created.daemon is False

    manager.start_all_instances()
    assert manager.ports == [9301, 9301]
    assert len(manager.processes) == 2
    assert all(proc.pid is not None for proc in manager.processes)

    manager.processes[0].stubborn = True
    manager.stop_all_instances()
    assert manager.processes == []


def test_restart_instance_status_monitor_and_wait(monkeypatch):
    monkeypatch.setattr(process_manager_module, "Process", FakeProcess)
    manager = process_manager_module.UvicornProcessManager(base_port=9400, instances=2)
    manager.processes = [FakeProcess(), FakeProcess()]
    manager.processes[0].start()
    manager.processes[1].start()
    manager.ports = [9400, 9401]

    assert manager.restart_instance(5) is False

    old_process = manager.processes[1]
    restarted = manager.restart_instance(1)
    assert restarted is True
    assert manager.processes[1] is not old_process
    assert manager.processes[1].pid is not None

    manager.processes[0]._alive = False
    manager.processes[0].exitcode = 2
    status = manager.get_instance_status()
    assert status[0]["alive"] is False
    assert status[0]["exitcode"] == 2
    assert status[1]["alive"] is True

    restart_calls = []
    manager.processes[0]._alive = False
    monkeypatch.setattr(manager, "restart_instance", lambda idx: restart_calls.append(idx) or True)

    sleep_calls = {"count": 0}

    def fake_sleep(_interval):
        sleep_calls["count"] += 1
        if sleep_calls["count"] > 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(process_manager_module.time, "sleep", fake_sleep)
    manager.monitor_instances(check_interval=1)
    assert restart_calls == [0]

    captured_handlers = {}
    join_calls = []

    class JoinableProcess:
        def join(self):
            join_calls.append("joined")

    manager.processes = [JoinableProcess()]
    monkeypatch.setattr(
        process_manager_module.signal,
        "signal",
        lambda signum, handler: captured_handlers.__setitem__(signum, handler),
    )
    manager.wait_for_termination()
    assert join_calls == ["joined"]
    assert signal.SIGINT in captured_handlers
    assert signal.SIGTERM in captured_handlers

    stopped = {"called": False}
    monkeypatch.setattr(manager, "stop_all_instances", lambda: stopped.__setitem__("called", True))
    monkeypatch.setattr(
        process_manager_module.sys, "exit", lambda code: (_ for _ in ()).throw(SystemExit(code))
    )
    with pytest.raises(SystemExit):
        captured_handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stopped["called"] is True


def test_wait_for_termination_handles_keyboard_interrupt(monkeypatch):
    manager = process_manager_module.UvicornProcessManager(base_port=9500, instances=1)

    class InterruptingProcess:
        def join(self):
            raise KeyboardInterrupt

    stopped = {"called": False}
    manager.processes = [InterruptingProcess()]
    monkeypatch.setattr(manager, "stop_all_instances", lambda: stopped.__setitem__("called", True))
    monkeypatch.setattr(process_manager_module.signal, "signal", lambda *_args, **_kwargs: None)

    manager.wait_for_termination()
    assert stopped["called"] is True


@pytest.mark.asyncio
async def test_storage_cleanup_loop_disabled(monkeypatch):
    monkeypatch.setattr(service_manager_module.config, "MAX_FILE_AGE_DAYS", 0)
    monkeypatch.setattr(service_manager_module.config, "CLEANUP_INTERVAL_HOURS", 1)

    await service_manager_module.storage_cleanup_loop()


@pytest.mark.asyncio
async def test_storage_cleanup_loop_runs_initial_periodic_and_cancel(monkeypatch):
    monkeypatch.setattr(service_manager_module.config, "MAX_FILE_AGE_DAYS", 3)
    monkeypatch.setattr(service_manager_module.config, "CLEANUP_INTERVAL_HOURS", 1)

    cleanup_calls = []

    def fake_cleanup(max_age_days):
        cleanup_calls.append(max_age_days)
        return 2 if len(cleanup_calls) == 1 else 1

    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    monkeypatch.setattr(service_manager_module.storage_manager, "cleanup_old_files", fake_cleanup)
    monkeypatch.setattr(service_manager_module.asyncio, "sleep", fake_sleep)

    await service_manager_module.storage_cleanup_loop()
    assert cleanup_calls == [3, 3]


@pytest.mark.asyncio
async def test_storage_cleanup_loop_handles_initial_and_periodic_errors(monkeypatch):
    monkeypatch.setattr(service_manager_module.config, "MAX_FILE_AGE_DAYS", 5)
    monkeypatch.setattr(service_manager_module.config, "CLEANUP_INTERVAL_HOURS", 1)

    cleanup_calls = {"count": 0}

    def fake_cleanup(_max_age_days):
        cleanup_calls["count"] += 1
        raise RuntimeError(f"boom-{cleanup_calls['count']}")

    sleep_calls = {"count": 0}

    async def fake_sleep(_seconds):
        sleep_calls["count"] += 1
        if sleep_calls["count"] == 1:
            return None
        raise asyncio.CancelledError

    monkeypatch.setattr(service_manager_module.storage_manager, "cleanup_old_files", fake_cleanup)
    monkeypatch.setattr(service_manager_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await service_manager_module.storage_cleanup_loop()


@pytest.mark.asyncio
async def test_background_service_manager_start_stop_and_status(monkeypatch):
    async def sleeper():
        await asyncio.sleep(10)

    monkeypatch.setattr(service_manager_module, "reconnect_loop", sleeper)
    monkeypatch.setattr(service_manager_module, "heartbeat_loop", sleeper)
    monkeypatch.setattr(service_manager_module, "storage_cleanup_loop", sleeper)

    manager = service_manager_module.BackgroundServiceManager()
    await manager.start_all_services()
    await manager.start_all_services()

    status = manager.get_service_status()
    assert status["is_running"] is True
    assert status["tasks"] == 3
    assert len(status["services"]) == 3

    await manager.stop_all_services()
    await manager.stop_all_services()
    assert manager.is_running is False
    assert manager.tasks == []


def test_background_service_manager_status_handles_objects_without_get_name():
    manager = service_manager_module.BackgroundServiceManager()
    manager.tasks = [types.SimpleNamespace(done=lambda: False, cancelled=lambda: False)]
    status = manager.get_service_status()
    assert status["services"][0]["name"] == "Unknown"


def test_email_helpers_and_composition(monkeypatch):
    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "sender@example.org")
    assert email_service_module._build_sender_address() == "sender@example.org"

    monkeypatch.setattr(email_service_module.config, "SMTP_SENDER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "manager@example.org")
    assert email_service_module._build_sender_address() == "esup-runner@example.org"

    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "invalid-email")
    monkeypatch.setattr(email_service_module.config, "RUNNER_HOST", "runner.example.org")
    assert email_service_module._build_sender_address() == "esup-runner@runner.example.org"

    monkeypatch.setattr(email_service_module, "get_runner_id", lambda: None)
    monkeypatch.setattr(email_service_module, "get_runner_instance_url", lambda: None)
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "manager@example.org")

    message = email_service_module._compose_failure_email(
        task_id="task-1",
        task_type="encoding",
        status="failed",
        error_message="",
        script_output="stdout",
    )

    assert message["Subject"] == "[esup-runner] Task task-1 failed"
    assert "(no details)" in message.get_content()
    assert "Script output:" in message.get_content()


@pytest.mark.asyncio
async def test_send_task_failure_email_skip_success_and_failure(monkeypatch):
    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "")
    assert (
        await email_service_module.send_task_failure_email(
            task_id="task-1",
            task_type="encoding",
            status="failed",
            error_message="boom",
        )
        is False
    )

    sent = {}

    class FakeSMTP:
        def __init__(self, server, port, timeout):
            sent["init"] = (server, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, username, password):
            sent["login"] = (username, password)

        def send_message(self, message):
            sent["subject"] = message["Subject"]

    async def fake_to_thread(func):
        func()

    monkeypatch.setattr(email_service_module.config, "SMTP_SERVER", "smtp.example.org")
    monkeypatch.setattr(email_service_module.config, "SMTP_PORT", 2525)
    monkeypatch.setattr(email_service_module.config, "SMTP_USE_TLS", True)
    monkeypatch.setattr(email_service_module.config, "SMTP_USERNAME", "user")
    monkeypatch.setattr(email_service_module.config, "SMTP_PASSWORD", "secret")
    monkeypatch.setattr(email_service_module.config, "MANAGER_EMAIL", "manager@example.org")
    monkeypatch.setattr(email_service_module.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(email_service_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(email_service_module, "get_runner_id", lambda: "runner-1")
    monkeypatch.setattr(email_service_module, "get_runner_instance_url", lambda: "http://runner")

    assert (
        await email_service_module.send_task_failure_email(
            task_id="task-2",
            task_type="encoding",
            status="failed",
            error_message="boom",
        )
        is True
    )
    assert sent["init"] == ("smtp.example.org", 2525, 10)
    assert sent["tls"] is True
    assert sent["login"] == ("user", "secret")

    async def failing_to_thread(_func):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(email_service_module.asyncio, "to_thread", failing_to_thread)
    assert (
        await email_service_module.send_task_failure_email(
            task_id="task-3",
            task_type="encoding",
            status="failed",
            error_message="boom",
        )
        is False
    )
