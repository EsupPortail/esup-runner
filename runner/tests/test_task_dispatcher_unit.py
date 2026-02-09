import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.task_dispatcher import TaskDispatcher


class DummyHandler:
    task_type = "dummy"

    def __init__(self):
        self.called = False

    def validate_parameters(self, parameters):
        return parameters.get("ok", True)

    def execute_task(self, task_id, task_request):
        return {
            "success": True,
            "task_type": self.task_type,
            "input_path": "in",
            "output_dir": str(Path(task_id) / "output"),
            "script_output": {},
        }

    @classmethod
    def get_description(cls):
        return "dummy"


@pytest.fixture
def dispatcher(monkeypatch, tmp_path):
    disp = TaskDispatcher()
    disp.logger.handlers.clear()

    # Patch storage base path
    monkeypatch.setattr("app.services.task_dispatcher.storage_manager.base_path", str(tmp_path))

    # Patch handler manager
    class DummyManager:
        def __init__(self, handler):
            self.handler = handler

        def get_handler(self, task_type):
            return self.handler if task_type == "dummy" else None

        def list_handlers(self):
            return {"dummy": "dummy handler"}

    monkeypatch.setattr(
        "app.services.task_dispatcher.task_handler_manager", DummyManager(DummyHandler)
    )
    return disp


@pytest.mark.asyncio
async def test_dispatch_task_success(dispatcher, tmp_path):
    request = SimpleNamespace(task_id="t1", task_type="dummy", parameters={}, source_url="http://x")

    result = await dispatcher.dispatch_task(task_id="t1", task_request=request)

    assert result["success"] is True
    assert Path(result["result_manifest"]).exists()
    manifest_content = json.loads(Path(result["result_manifest"]).read_text())
    assert manifest_content["task_id"] == "t1"


@pytest.mark.asyncio
async def test_dispatch_task_invalid_params(dispatcher):
    request = SimpleNamespace(
        task_id="t2", task_type="dummy", parameters={"ok": False}, source_url="http://x"
    )

    result = await dispatcher.dispatch_task(task_id="t2", task_request=request)
    assert result["success"] is False
    assert "Invalid parameters" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_task_missing_handler(dispatcher, monkeypatch):
    # Force manager to return None
    class NoneManager:
        def get_handler(self, *_):
            return None

        def list_handlers(self):
            return {}

    monkeypatch.setattr("app.services.task_dispatcher.task_handler_manager", NoneManager())

    request = SimpleNamespace(
        task_id="t3", task_type="unknown", parameters={}, source_url="http://x"
    )
    result = await dispatcher.dispatch_task(task_id="t3", task_request=request)

    assert result["success"] is False
    assert "No handler found" in result["error"]


@pytest.mark.asyncio
async def test_list_handlers(dispatcher):
    handlers = dispatcher.get_available_task_types()
    assert "dummy" in handlers


@pytest.mark.asyncio
async def test_dispatch_task_exception(dispatcher, monkeypatch):
    class ExplodingHandler(DummyHandler):
        def execute_task(self, *_, **__):
            raise RuntimeError("boom")

    class Manager:
        def get_handler(self, *_):
            return ExplodingHandler

        def list_handlers(self):
            return {"dummy": "dummy"}

    monkeypatch.setattr("app.services.task_dispatcher.task_handler_manager", Manager())

    request = SimpleNamespace(task_id="t4", task_type="dummy", parameters={}, source_url="http://x")
    result = await dispatcher.dispatch_task(task_id="t4", task_request=request)

    assert result["success"] is False
    assert "boom" in result["error"]


@pytest.mark.asyncio
async def test_package_results_failure(dispatcher, monkeypatch, tmp_path):
    # Force save_file to raise
    monkeypatch.setattr(
        "app.services.task_dispatcher.storage_manager.save_file",
        lambda *_, **__: (_ for _ in ()).throw(RuntimeError("disk")),
    )

    request = SimpleNamespace(task_id="t5", task_type="dummy", parameters={}, source_url="http://x")

    # Handler returns success to trigger packaging
    class Handler:
        task_type = "dummy"

        def validate_parameters(self, _):
            return True

        def execute_task(self, *_, **__):
            return {
                "success": True,
                "task_type": self.task_type,
                "input_path": "in",
                "output_dir": str(Path(request.task_id) / "output"),
                "script_output": {},
            }

        @classmethod
        def get_description(cls):
            return "dummy"

    class Manager:
        def get_handler(self, *_):
            return Handler

        def list_handlers(self):
            return {"dummy": "dummy"}

    monkeypatch.setattr("app.services.task_dispatcher.task_handler_manager", Manager())
    monkeypatch.setattr("app.services.task_dispatcher.storage_manager.base_path", str(tmp_path))

    result = await dispatcher.dispatch_task(task_id=request.task_id, task_request=request)

    assert result["success"] is False
    assert "Result packaging failed" in result["error"]
