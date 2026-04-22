import asyncio

import pytest
from fastapi.testclient import TestClient

import app.services.manager_service as manager_service
from app.core import state
from app.main import app, background_manager


@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch, request):
    async def _fake_register():
        return True

    async def _noop():
        return None

    monkeypatch.setattr(manager_service, "register_with_manager", _fake_register)
    import app.main as main

    monkeypatch.setattr(main, "register_with_manager", _fake_register)
    monkeypatch.setattr(background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_manager, "stop_all_services", _noop)

    recovery_tests = {
        "test_recover_running_tasks_marks_completed_and_notifies",
        "test_recover_running_tasks_keeps_alive_tasks_running",
        "test_recover_running_tasks_restarts_task_transitioned_from_running_to_failed",
        "test_recover_running_tasks_uses_task_metadata_success_without_manifest",
        "test_recover_running_tasks_uses_task_metadata_failure_without_manifest",
        "test_recover_running_tasks_uses_outputs_without_metadata",
        "test_recover_running_tasks_skips_foreign_runner_tasks",
        "test_recover_running_tasks_marks_failed_task_completed_when_manifest_exists",
        "test_recover_running_tasks_restarts_failed_task_with_persisted_request",
        "test_recover_running_tasks_handles_runner_id_drift_with_instance_scoped_state",
        "test_stop_recovery_monitors_cancels_running_tasks",
    }

    if request.node.name not in recovery_tests:
        from app.api.routes import task as task_module

        monkeypatch.setattr(task_module, "recover_running_tasks_after_restart", _noop)
        monkeypatch.setattr(task_module, "stop_recovery_monitors", _noop)


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides.clear()
    app.dependency_overrides = {}
    from app.core.auth import get_current_manager

    app.dependency_overrides[get_current_manager] = lambda: "manager-token"
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_runner_task_statuses(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RUNNER_TASK_STATUS_FILE",
        str(tmp_path / "runner_task_statuses.test.json"),
    )
    snapshot = dict(state._RUNNER_STATE.get("task_statuses", {}))
    state._RUNNER_STATE["task_statuses"] = {}
    yield
    state._RUNNER_STATE["task_statuses"] = snapshot


def test_get_task_result(tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-123"
    manifest_path = tmp_path / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert "manifest.json" in resp.headers.get("content-disposition", "")


def test_get_task_result_does_not_fall_back_to_legacy_manifest(tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-legacy"
    legacy_manifest_path = tmp_path / f"{task_id}.json"
    legacy_manifest_path.write_text("{}", encoding="utf-8")
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}")
        assert resp.status_code == 404


def test_get_task_result_not_found(tmp_path):
    from app.api.routes import task as task_module

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/task/result/missing")
        assert resp.status_code == 404


def test_get_task_status_returns_tracked_status():
    state.set_task_status("task-running", "running")

    with TestClient(app) as client:
        resp = client.get("/task/status/task-running")
        assert resp.status_code == 200
        assert resp.json() == {"task_id": "task-running", "status": "running"}


def test_get_task_status_returns_completed_when_manifest_exists(tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-done"
    manifest_path = tmp_path / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/status/{task_id}")
        assert resp.status_code == 200
        assert resp.json() == {"task_id": task_id, "status": "completed"}


def test_get_task_status_returns_not_found(tmp_path):
    from app.api.routes import task as task_module

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/task/status/unknown-task")
        assert resp.status_code == 200
        assert resp.json() == {"task_id": "unknown-task", "status": "not_found"}


def test_get_task_status_reraises_non_404_manifest_errors(monkeypatch):
    from app.api.routes import task as task_module

    def _raise_non_404(_task_id):
        raise task_module.HTTPException(status_code=500, detail="boom")

    monkeypatch.setattr(task_module, "_resolve_task_manifest_path", _raise_non_404)

    with TestClient(app) as client:
        resp = client.get("/task/status/task-500")
        assert resp.status_code == 500
        assert resp.json()["detail"] == "boom"


def test_normalize_script_output_returns_input_string():
    from app.api.routes import task as task_module

    assert task_module._normalize_script_output("plain-text") == "plain-text"


def test_normalize_script_output_formats_stdout_and_stderr_as_log_sections():
    from app.api.routes import task as task_module

    normalized = task_module._normalize_script_output(
        {
            "success": True,
            "returncode": 0,
            "stdout": "encoding done",
            "stderr": "non-fatal warning",
        }
    )

    assert normalized is not None
    assert "[info_script.log]" in normalized
    assert "encoding done" in normalized
    assert "[error_script.log]" in normalized
    assert "non-fatal warning" in normalized


def test_normalize_script_output_formats_nested_stream_payloads_with_context():
    from app.api.routes import task as task_module

    normalized = task_module._normalize_script_output(
        {
            "studio": {"stdout": "studio ok", "stderr": ""},
            "encoding": {"stdout": "", "stderr": "encoding warning"},
        }
    )

    assert normalized is not None
    assert "[studio/info_script.log]" in normalized
    assert "studio ok" in normalized
    assert "[encoding/error_script.log]" in normalized
    assert "encoding warning" in normalized


def test_normalize_script_output_skips_empty_keys_and_handles_list_contexts():
    from app.api.routes import task as task_module

    normalized = task_module._normalize_script_output(
        {
            "": {"stdout": "ignored"},
            "steps": [
                {"stdout": "step-0 ok"},
                {"stderr": "step-1 warning"},
            ],
        }
    )

    assert normalized is not None
    assert "ignored" not in normalized
    assert "[steps[0]/info_script.log]" in normalized
    assert "step-0 ok" in normalized
    assert "[steps[1]/error_script.log]" in normalized
    assert "step-1 warning" in normalized


def test_normalize_script_output_falls_back_to_str_on_type_error():
    from app.api.routes import task as task_module

    class Unserializable:
        def __str__(self):
            return "custom-object"

    assert task_module._normalize_script_output(Unserializable()) == "custom-object"


def test_get_task_result_file(tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-456"
    filename = "output.txt"
    base_dir = tmp_path / task_id / "output"
    base_dir.mkdir(parents=True)
    file_path = base_dir / filename
    file_path.write_text("hello", encoding="utf-8")

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}/file/{filename}")
        assert resp.status_code == 200
        assert resp.text == "hello"


def test_get_task_result_file_not_found(tmp_path):
    from app.api.routes import task as task_module

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/task/result/none/file/missing")
        assert resp.status_code == 404


def test_get_task_result_file_traversal(tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-evil"
    base_dir = tmp_path / task_id / "output"
    base_dir.mkdir(parents=True)
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}/file/../secret.txt")
        assert resp.status_code == 404


def test_delete_task_result(tmp_path):
    from app.api.routes import task as task_module

    task_id = "abc"
    task_dir = tmp_path / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "manifest.json").write_text("{}", encoding="utf-8")
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.delete(f"/task/delete/{task_id}")
        assert resp.status_code == 200
        assert not task_dir.exists()


@pytest.mark.asyncio
async def test_process_task_success(monkeypatch):
    from app.api.routes import task as task_module
    from app.models.models import TaskRequest

    events = {"avail": [], "notified": None}

    async def _dispatch(task_id, task_request):
        return {"success": True, "script_output": {"ok": True}}

    async def _notify(url, task_id, status, error_message, script_output=None):
        events["notified"] = (url, status, error_message, script_output)

    def _set_available(flag):
        events["avail"].append(flag)

    monkeypatch.setattr(task_module.task_dispatcher, "dispatch_task", _dispatch)
    monkeypatch.setattr(task_module, "notify_completion", _notify)
    monkeypatch.setattr(task_module, "set_available", _set_available)

    req = TaskRequest(
        task_id="tid-1",
        etab_name="UM",
        app_name="Pod",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        parameters={},
        notify_url="http://notify",
        completion_callback="http://cb",
    )

    await task_module.process_task("tid-1", req)

    assert events["avail"] == [False, True]
    assert events["notified"][1] == "completed"


@pytest.mark.asyncio
async def test_process_task_failure(monkeypatch):
    from app.api.routes import task as task_module
    from app.models.models import TaskRequest

    events = {"avail": [], "notified": None, "email": None}

    async def _dispatch(task_id, task_request):
        return {"success": False, "error": "timeout happened", "script_output": {}}

    async def _notify(url, task_id, status, error_message, script_output=None):
        events["notified"] = (url, status, error_message, script_output)

    async def _send_email(**kwargs):
        events["email"] = kwargs
        return True

    def _set_available(flag):
        events["avail"].append(flag)

    monkeypatch.setattr(task_module.task_dispatcher, "dispatch_task", _dispatch)
    monkeypatch.setattr(task_module, "notify_completion", _notify)
    monkeypatch.setattr(task_module, "send_task_failure_email", _send_email)
    monkeypatch.setattr(task_module, "set_available", _set_available)

    req = TaskRequest(
        task_id="tid-2",
        etab_name="UM",
        app_name="Pod",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        parameters={},
        notify_url="http://notify",
        completion_callback="http://cb",
    )

    await task_module.process_task("tid-2", req)

    assert events["avail"] == [False, True]
    assert events["notified"][1] == "timeout"
    assert events["email"]["status"] == "timeout"


@pytest.mark.asyncio
async def test_process_task_exception(monkeypatch):
    from app.api.routes import task as task_module
    from app.models.models import TaskRequest

    events = {"avail": []}

    async def _dispatch(task_id, task_request):
        raise RuntimeError("boom")

    async def _notify(url, task_id, status, error_message, script_output=None):
        events["notified"] = status

    async def _send_email(**kwargs):
        events["email"] = kwargs
        return True

    def _set_available(flag):
        events["avail"].append(flag)

    monkeypatch.setattr(task_module.task_dispatcher, "dispatch_task", _dispatch)
    monkeypatch.setattr(task_module, "notify_completion", _notify)
    monkeypatch.setattr(task_module, "send_task_failure_email", _send_email)
    monkeypatch.setattr(task_module, "set_available", _set_available)

    req = TaskRequest(
        task_id="tid-3",
        etab_name="UM",
        app_name="Pod",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        parameters={},
        notify_url="http://notify",
        completion_callback="http://cb",
    )

    await task_module.process_task("tid-3", req)

    assert events["avail"] == [False, True]
    assert events["email"]["status"] == "failed"


@pytest.mark.asyncio
async def test_process_task_keeps_runner_unavailable_when_other_task_is_running(monkeypatch):
    from app.api.routes import task as task_module
    from app.models.models import TaskRequest

    state.set_available(True)
    state.set_task_status("other-running-task", "running")

    async def _dispatch(task_id, task_request):
        return {"success": True, "script_output": {"ok": True}}

    monkeypatch.setattr(task_module.task_dispatcher, "dispatch_task", _dispatch)

    req = TaskRequest(
        task_id="tid-keep-busy",
        etab_name="UM",
        app_name="Pod",
        task_type="encoding",
        source_url="https://example.org/video.mp4",
        parameters={},
        notify_url="http://notify",
        completion_callback=None,
    )

    await task_module.process_task("tid-keep-busy", req)

    payload = state.get_task_status("tid-keep-busy")
    assert payload is not None
    assert payload["status"] == "completed"
    assert state.is_available() is False


@pytest.mark.asyncio
async def test_notify_completion_success(monkeypatch):
    from app.api.routes import task as task_module

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "ok"

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_, **__):
            return FakeResponse(200)

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 0)

    ok = await task_module.notify_completion("http://cb", "tid", "completed")
    assert ok is True


@pytest.mark.asyncio
async def test_notify_completion_retries_and_fails(monkeypatch):
    from app.api.routes import task as task_module

    statuses = [500, 500]

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = "err"

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_, **__):
            return FakeResponse(statuses.pop(0))

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS", 1)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_BACKOFF_FACTOR", 1.0)

    async def _sleep(*_, **__):
        return None

    monkeypatch.setattr(task_module.asyncio, "sleep", _sleep)

    ok = await task_module.notify_completion("http://cb", "tid", "failed")
    assert ok is False


@pytest.mark.asyncio
async def test_notify_completion_exception(monkeypatch):
    from app.api.routes import task as task_module

    class FakeClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_, **__):
            raise RuntimeError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(task_module.config, "COMPLETION_NOTIFY_MAX_RETRIES", 0)
    ok = await task_module.notify_completion("http://cb", "tid", "failed")
    assert ok is False


@pytest.mark.asyncio
async def test_run_task_background(monkeypatch):
    from app.api.routes import task as task_module

    state.set_registered(True)
    state.set_available(True)

    async def _dispatch(task_id, task_request):
        return {"success": True, "task_id": task_id, "task_type": task_request.task_type}

    monkeypatch.setattr(task_module.task_dispatcher, "dispatch_task", _dispatch)

    with TestClient(app) as client:
        payload = {
            "task_id": "tid-1",
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
        }
        resp = client.post("/task/run", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["task_id"] == "tid-1"


def test_run_task_not_registered(monkeypatch):
    state.set_registered(False)

    with TestClient(app) as client:
        payload = {
            "task_id": "tid-1",
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
        }
        resp = client.post("/task/run", json=payload)
        assert resp.status_code == 503


def test_run_task_busy(monkeypatch):
    state.set_registered(True)
    state.set_available(False)

    with TestClient(app) as client:
        payload = {
            "task_id": "tid-1",
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
        }
        resp = client.post("/task/run", json=payload)
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_recover_running_tasks_marks_completed_and_notifies(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-recover-completed"
    task_module.storage_manager.base_path = str(tmp_path)

    manifest_path = tmp_path / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")

    stdout_path = tmp_path / task_id / "info_script.log"
    stdout_path.write_text("external process finished", encoding="utf-8")

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=1234,
        script_stdout_path=str(stdout_path),
    )

    notifications = {}

    async def _notify(callback_url, task_id, status, error_message=None, script_output=None):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "completed"
    assert notifications["status"] == "completed"
    assert "external process finished" in notifications["script_output"]


def test_initialize_startup_availability_marks_runner_unavailable_when_running_tasks_exist(
    monkeypatch,
):
    from app.api.routes import task as task_module

    state.set_available(True)
    state.set_task_status("task-startup-running", "running")
    state.set_task_metadata(
        "task-startup-running",
        runner_id="runner-a",
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    task_module.initialize_startup_availability()

    assert state.is_available() is False


def test_initialize_startup_availability_does_not_force_available_when_no_running_tasks(
    monkeypatch,
):
    from app.api.routes import task as task_module

    state.set_available(False)
    state.set_task_status("task-startup-failed", "failed", error_message="failed")
    state.set_task_metadata(
        "task-startup-failed",
        runner_id="runner-a",
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    task_module.initialize_startup_availability()

    assert state.is_available() is False


def test_initialize_startup_availability_marks_runner_unavailable_when_failed_tasks_exist(
    monkeypatch,
):
    from app.api.routes import task as task_module

    state.set_available(True)
    state.set_task_status("task-startup-failed-recoverable", "failed", error_message="failed")
    state.set_task_metadata(
        "task-startup-failed-recoverable",
        runner_id="runner-a",
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    task_module.initialize_startup_availability()

    assert state.is_available() is False


@pytest.mark.asyncio
async def test_recover_running_tasks_keeps_alive_tasks_running(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-recover-running"
    task_module.storage_manager.base_path = str(tmp_path)

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=5678,
    )

    scheduled = []

    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: True)
    monkeypatch.setattr(
        task_module,
        "_schedule_recovery_monitor",
        lambda recovered_id: scheduled.append(recovered_id),
    )

    async def _notify(*_args, **_kwargs):
        raise AssertionError("notify_completion must not be called while task is still running")

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "running"
    assert scheduled == [task_id]
    assert state.is_available() is False


@pytest.mark.asyncio
async def test_recover_running_tasks_uses_task_metadata_success_without_manifest(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-metadata-success"
    task_module.storage_manager.base_path = str(tmp_path)

    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "result.vtt").write_text("WEBVTT\n", encoding="utf-8")
    (output_dir / "task_metadata.json").write_text(
        '{"task_id":"task-recover-metadata-success","results":{"success":true,"script_output":{"ok":true}}}',
        encoding="utf-8",
    )

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=9876,
    )

    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "completed"
    assert (tmp_path / task_id / "manifest.json").exists()
    assert notifications["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_running_tasks_uses_task_metadata_failure_without_manifest(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-metadata-failure"
    task_module.storage_manager.base_path = str(tmp_path)

    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "task_metadata.json").write_text(
        '{"task_id":"task-recover-metadata-failure","results":{"success":false,"error":"timeout while processing","script_output":{"step":"whisper"}}}',
        encoding="utf-8",
    )

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=8765,
    )

    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "timeout"
    assert payload["error_message"] == "timeout while processing"
    assert notifications["status"] == "timeout"


@pytest.mark.asyncio
async def test_recover_running_tasks_restarts_task_transitioned_from_running_to_failed(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-running-to-failed-restart"
    task_module.storage_manager.base_path = str(tmp_path)

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=7777,
        task_request={
            "task_id": task_id,
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
            "completion_callback": "http://manager.example.org/task/completion",
        },
    )

    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    scheduled = {}

    def _fake_create_task(coro):
        scheduled["coro"] = coro

        class _DummyTask:
            def done(self):
                return False

        return _DummyTask()

    monkeypatch.setattr(task_module.asyncio, "create_task", _fake_create_task)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "running"
    assert payload["recovery_restart_attempts"] == 1
    assert "coro" in scheduled
    assert notifications["status"] == "failed"

    scheduled["coro"].close()


@pytest.mark.asyncio
async def test_recover_running_tasks_uses_outputs_without_metadata(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-recover-outputs-success"
    task_module.storage_manager.base_path = str(tmp_path)

    output_dir = tmp_path / task_id / "output"
    output_dir.mkdir(parents=True)
    (output_dir / "result.m3u8").write_text("#EXTM3U\n", encoding="utf-8")
    (output_dir / "result.ts").write_text("segment", encoding="utf-8")

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=7654,
        task_type="encoding",
    )

    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "completed"
    assert (tmp_path / task_id / "manifest.json").exists()
    assert notifications["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_running_tasks_skips_foreign_runner_tasks(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-recover-foreign"
    task_module.storage_manager.base_path = str(tmp_path)
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=1111,
        runner_id="runner-b",
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    async def _notify(*_args, **_kwargs):
        raise AssertionError("notify_completion should not be called for foreign runner task")

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "running"
    assert state.is_available() is True


@pytest.mark.asyncio
async def test_recover_running_tasks_handles_runner_id_drift_with_instance_scoped_state(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-runner-id-drift"
    task_module.storage_manager.base_path = str(tmp_path)
    monkeypatch.setenv("RUNNER_INSTANCE_ID", "1")

    state.set_task_status(task_id, "running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        process_pid=17452,
        runner_id="runner-old-id",
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-new-id")
    monkeypatch.setattr(task_module, "_is_process_alive", lambda _pid: False)

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "failed"
    assert payload["error_message"] == "Task process is no longer running"
    assert notifications["status"] == "failed"
    assert notifications["task_id"] == task_id


@pytest.mark.asyncio
async def test_recover_running_tasks_marks_failed_task_completed_when_manifest_exists(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-failed-to-completed"
    task_module.storage_manager.base_path = str(tmp_path)

    manifest_path = tmp_path / task_id / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")

    state.set_task_status(task_id, "failed", error_message="Task process is no longer running")
    state.set_task_metadata(
        task_id,
        completion_callback="http://manager.example.org/task/completion",
        task_request={
            "task_id": task_id,
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
            "completion_callback": "http://manager.example.org/task/completion",
        },
    )

    notifications = {}

    async def _notify(
        callback_url, notified_task_id, status, error_message=None, script_output=None
    ):
        notifications["callback_url"] = callback_url
        notifications["task_id"] = notified_task_id
        notifications["status"] = status
        notifications["error_message"] = error_message
        notifications["script_output"] = script_output
        return True

    monkeypatch.setattr(task_module, "notify_completion", _notify)
    monkeypatch.setattr(
        task_module,
        "_schedule_failed_task_restart",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("failed task with manifest should not be restarted")
        ),
    )

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "completed"
    assert notifications["status"] == "completed"


@pytest.mark.asyncio
async def test_recover_running_tasks_restarts_failed_task_with_persisted_request(
    monkeypatch, tmp_path
):
    from app.api.routes import task as task_module

    task_id = "task-recover-restart-failed"
    task_module.storage_manager.base_path = str(tmp_path)

    state.set_task_status(task_id, "failed", error_message="Task process is no longer running")
    state.set_task_metadata(
        task_id,
        runner_id="runner-a",
        completion_callback="http://manager.example.org/task/completion",
        task_type="encoding",
        task_request={
            "task_id": task_id,
            "etab_name": "UM",
            "app_name": "Pod",
            "task_type": "encoding",
            "source_url": "https://example.org/video.mp4",
            "parameters": {},
            "notify_url": "http://notify",
            "completion_callback": "http://manager.example.org/task/completion",
        },
    )

    monkeypatch.setattr(task_module, "get_runner_id", lambda: "runner-a")

    async def _notify(*_args, **_kwargs):
        raise AssertionError("failed task should be restarted, not notified as failed")

    monkeypatch.setattr(task_module, "notify_completion", _notify)

    scheduled = {}

    def _fake_create_task(coro):
        scheduled["coro"] = coro

        class _DummyTask:
            def done(self):
                return False

        return _DummyTask()

    monkeypatch.setattr(task_module.asyncio, "create_task", _fake_create_task)

    await task_module.recover_running_tasks_after_restart()

    payload = state.get_task_status(task_id)
    assert payload is not None
    assert payload["status"] == "running"
    assert payload["recovery_restart_attempts"] == 1
    assert "error_message" not in payload
    assert "coro" in scheduled

    scheduled["coro"].close()


@pytest.mark.asyncio
async def test_stop_recovery_monitors_cancels_running_tasks():
    from app.api.routes import task as task_module

    gate = asyncio.Event()

    async def _pending_task():
        await gate.wait()

    monitor = asyncio.create_task(_pending_task())
    task_module._RECOVERY_MONITORS["task-monitor"] = monitor

    await task_module.stop_recovery_monitors()

    assert task_module._RECOVERY_MONITORS == {}
    assert monitor.cancelled() or monitor.done()
