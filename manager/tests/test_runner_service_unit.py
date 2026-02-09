"""Unit coverage for app.services.runner_service."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from app.core.state import runners
from app.models.models import Runner
from app.services import runner_service


@pytest.fixture
def clean_runners():
    original = dict(runners)
    runners.clear()
    yield
    runners.clear()
    runners.update(original)


def _runner(runner_id: str, *, last_heartbeat: datetime) -> Runner:
    return Runner(
        id=runner_id,
        url="http://runner.example",
        task_types=["encoding"],
        status="online",
        availability="available",
        last_heartbeat=last_heartbeat,
        token="tok",
        version="1.0.0",
    )


@pytest.mark.asyncio
async def test_check_runners_activity_removes_inactive_and_stops(clean_runners):
    runners["old"] = _runner("old", last_heartbeat=datetime.now() - timedelta(minutes=2))
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        runner_service.check_runners_activity(poll_interval=0, stop_event=stop_event)
    )
    await asyncio.sleep(0)
    stop_event.set()
    await asyncio.wait_for(task, timeout=0.1)

    assert "old" not in runners


def test_get_online_runners_filters_by_heartbeat(clean_runners):
    now = datetime.now()
    runners["on"] = _runner("on", last_heartbeat=now)
    runners["off"] = _runner("off", last_heartbeat=now - timedelta(minutes=2))

    online = runner_service.get_online_runners()
    assert [r["id"] for r in online] == ["on"]


def test_verify_runner_token_and_update_heartbeat(clean_runners):
    now = datetime.now() - timedelta(minutes=1)
    runners["r1"] = _runner("r1", last_heartbeat=now)

    assert runner_service.verify_runner_tokenINUTILE("r1", "tok") is True
    assert runner_service.verify_runner_tokenINUTILE("r1", "wrong") is False
    assert runner_service.verify_runner_tokenINUTILE("missing", "tok") is False

    assert runner_service.update_runner_heartbeat("r1") is True
    assert runner_service.update_runner_heartbeat("missing") is False
    assert runners["r1"].last_heartbeat > now
