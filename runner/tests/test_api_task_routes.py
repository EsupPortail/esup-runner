import pytest
from fastapi.testclient import TestClient

import app.services.manager_service as manager_service
from app.core import state
from app.main import app, background_manager


@pytest.fixture(autouse=True)
def stub_lifespan(monkeypatch):
    async def _fake_register():
        return True

    async def _noop():
        return None

    monkeypatch.setattr(manager_service, "register_with_manager", _fake_register)
    import app.main as main

    monkeypatch.setattr(main, "register_with_manager", _fake_register)
    monkeypatch.setattr(background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_manager, "stop_all_services", _noop)


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides.clear()
    app.dependency_overrides = {}
    from app.core.auth import get_current_manager

    app.dependency_overrides[get_current_manager] = lambda: "manager-token"
    yield
    app.dependency_overrides.clear()


def test_get_task_result(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-123"
    result_path = tmp_path / f"{task_id}.json"
    result_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(task_module.storage_manager, "get_path", lambda tid: str(result_path))
    monkeypatch.setattr(task_module.storage_manager, "exists", lambda tid: True)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")


def test_get_task_result_not_found(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/task/result/missing")
        assert resp.status_code == 404


def test_get_task_result_file(monkeypatch, tmp_path):
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


def test_get_task_result_file_not_found(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get("/task/result/none/file/missing")
        assert resp.status_code == 404


def test_get_task_result_file_traversal(monkeypatch, tmp_path):
    from app.api.routes import task as task_module

    task_id = "task-evil"
    base_dir = tmp_path / task_id / "output"
    base_dir.mkdir(parents=True)
    task_module.storage_manager.base_path = str(tmp_path)

    with TestClient(app) as client:
        resp = client.get(f"/task/result/{task_id}/file/../secret.txt")
        assert resp.status_code == 404


def test_delete_task_result(monkeypatch):
    from app.api.routes import task as task_module

    monkeypatch.setattr(task_module.storage_manager, "cleanup", lambda tid: True)
    called = {}

    def _set_available(flag):
        called["flag"] = flag

    monkeypatch.setattr(task_module, "set_available", _set_available)

    with TestClient(app) as client:
        resp = client.delete("/task/delete/abc")
        assert resp.status_code == 200
        assert called["flag"] is True


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
