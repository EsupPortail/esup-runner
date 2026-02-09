"""Unit coverage for background service manager."""

from __future__ import annotations

import asyncio

import pytest

from app.services import background_service


@pytest.mark.asyncio
async def test_start_and_stop_all_services(monkeypatch):
    started = []

    async def fake_service(name: str):
        started.append(name)
        while True:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(
        background_service, "check_runners_activity", lambda: fake_service("runners")
    )
    monkeypatch.setattr(background_service, "cleanup_old_tasks", lambda: fake_service("cleanup"))
    monkeypatch.setattr(background_service, "check_task_timeouts", lambda: fake_service("timeouts"))

    mgr = background_service.BackgroundServiceManager()
    await mgr.start_all_services()
    await asyncio.sleep(0.01)
    assert mgr.is_running is True
    assert len(mgr.tasks) == 3
    assert set(started) == {"runners", "cleanup", "timeouts"}

    await mgr.stop_all_services()
    assert mgr.is_running is False
    assert mgr.tasks == []


@pytest.mark.asyncio
async def test_start_when_already_running(monkeypatch):
    async def noop():
        await asyncio.sleep(0)

    monkeypatch.setattr(background_service, "check_runners_activity", lambda: noop())
    monkeypatch.setattr(background_service, "cleanup_old_tasks", lambda: noop())
    monkeypatch.setattr(background_service, "check_task_timeouts", lambda: noop())

    mgr = background_service.BackgroundServiceManager()
    await mgr.start_all_services()
    await mgr.start_all_services()
    assert mgr.is_running is True
    await mgr.stop_all_services()


@pytest.mark.asyncio
async def test_stop_when_not_running():
    mgr = background_service.BackgroundServiceManager()
    await mgr.stop_all_services()
    assert mgr.is_running is False


@pytest.mark.asyncio
async def test_get_service_status_reports_tasks():
    mgr = background_service.BackgroundServiceManager()
    task = asyncio.create_task(asyncio.sleep(0))
    mgr.tasks.append(task)
    status = mgr.get_service_status()
    assert status["tasks"] == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
