"""Coverage tests for application bootstrap helpers."""

import pytest
from fastapi import FastAPI

import app.main as main_module


def test_include_api_routers_is_idempotent():
    """Validate API routers are included only once."""
    test_app = FastAPI()

    main_module._include_api_routers(test_app)
    route_count = len(test_app.routes)
    main_module._include_api_routers(test_app)

    assert len(test_app.routes) == route_count


@pytest.mark.asyncio
async def test_lifespan_runs_startup_and_shutdown(monkeypatch):
    """Validate lifespan startup and shutdown orchestration."""
    from app.api.routes import task as task_module

    calls = []

    def _initialize_startup_availability():
        calls.append("initialize")

    async def _register():
        calls.append("register")
        return True

    async def _start_services():
        calls.append("start")

    async def _recover():
        calls.append("recover")

    async def _stop_monitors():
        calls.append("stop-monitors")

    async def _stop_services():
        calls.append("stop-services")

    monkeypatch.setattr(
        task_module,
        "initialize_startup_availability",
        _initialize_startup_availability,
    )
    monkeypatch.setattr(main_module, "register_with_manager", _register)
    monkeypatch.setattr(main_module.background_manager, "start_all_services", _start_services)
    monkeypatch.setattr(task_module, "recover_running_tasks_after_restart", _recover)
    monkeypatch.setattr(task_module, "stop_recovery_monitors", _stop_monitors)
    monkeypatch.setattr(main_module.background_manager, "stop_all_services", _stop_services)

    async with main_module.lifespan(main_module.app):
        calls.append("inside")

    assert calls == [
        "initialize",
        "register",
        "start",
        "recover",
        "inside",
        "stop-monitors",
        "stop-services",
    ]
