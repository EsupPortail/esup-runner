"""Idempotent Live restart while the same RTMP publication remains connected."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import test_live
from fastapi import FastAPI, HTTPException

from app.api.routes.live import router
from app.core import state
from app.core.auth import verify_token
from app.core.config import config
from app.services import live_autostart as auto
from app.services import live_service as live
from app.services.mediamtx import MediaMTXError

setup_live = test_live.setup_live


@pytest.fixture
def media(setup_live, monkeypatch):
    client = AsyncMock()
    client.get_path.return_value = {
        "available": True,
        "source": {"type": "rtmpConn", "id": str(uuid4())},
    }
    monkeypatch.setattr(live, "MediaMTXClient", lambda *args: client)
    return client


@pytest.mark.asyncio
async def test_restart_same_publication_concurrently_and_after_manager_restart(media):
    await auto.reconcile_source("room", media)
    (old,) = live.sessions()
    await live.stop(old.task_id, None)
    results = await asyncio.gather(*(live.restart(old.task_id, None) for _ in range(4)))
    assert len({result.live_id for result in results}) == 1
    current = results[0]
    assert current.live_id != old.task_id and current.automatic
    assert current.publication_id == old.live.event_id
    assert current.event_id == f"restart:{old.task_id}"
    assert not current.stop_requested
    state.tasks.clear()
    await auto.reconcile_source("room", media)
    assert not live.load(current.live_id).live.stop_requested
    assert len(live.sessions()) == 2
    assert (await live.restart(old.task_id, None)).live_id == current.live_id
    await live.stop(current.live_id, None)
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 2
    assert auto.read_fence("room") == current.publication_id
    next_session = await live.restart(current.live_id, None)
    assert next_session.live_id not in {old.task_id, current.live_id}
    media.get_path.return_value = None
    await auto.reconcile_source("room", media)
    assert live.load(next_session.live_id).live.state == "completed"
    assert (await live.restart(current.live_id, None)).live_id == next_session.live_id


@pytest.mark.asyncio
async def test_api_restart_preserves_owner_and_requires_authentication(media):
    old = await live.start(test_live.request(), "owner")
    await live.stop(old.live_id, "owner")
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as client:
        url = f"/live/{old.live_id}/restart"
        assert (await client.post(url)).status_code == 401
        app.dependency_overrides[verify_token] = lambda: "other"
        assert (await client.post(url)).status_code == 403
        app.dependency_overrides[verify_token] = lambda: "owner"
        response = await client.post(url)
        assert response.status_code == 202
        new = response.json()
        assert new["live_id"] != old.live_id and not new["automatic"]
        assert live.load(new["live_id"], "owner").client_token == "owner"
        assert len(live.sessions()) == 2


@pytest.mark.asyncio
async def test_restart_requires_terminal_cleanup(media):
    old = await live.start(test_live.request(), "owner")
    with pytest.raises(HTTPException) as error:
        await live.restart(old.live_id, "owner")
    assert error.value.status_code == 409 and len(live.sessions()) == 1
    media.get_path.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "condition,status",
    [
        ("disabled", 503),
        ("removed", 404),
        ("absent", 409),
        ("media_error", 503),
        ("no_capacity", 503),
        ("storage_error", 503),
    ],
)
async def test_restart_failure_does_not_resurrect_old_task(media, monkeypatch, condition, status):
    await auto.reconcile_source("room", media)
    (old,) = live.sessions()
    await live.stop(old.task_id, None)
    if condition == "disabled":
        monkeypatch.setattr(config.LIVE, "enabled", False)
    elif condition == "removed":
        monkeypatch.setattr(config.LIVE, "sources", {})
    elif condition == "absent":
        media.get_path.return_value = None
    elif condition == "media_error":
        media.get_path.side_effect = MediaMTXError("secret must not be exposed")
    elif condition == "no_capacity":
        await live.start(test_live.request("room0"), "owner")
    else:
        monkeypatch.setattr(state.persistence, "upsert_tasks", lambda _: False)
    with pytest.raises(HTTPException) as error:
        await live.restart(old.task_id, None)
    assert error.value.status_code == status
    assert "secret" not in str(error.value.detail)
    assert live.load(old.task_id).live.state == "completed"
    assert not any(task.live.event_id == f"restart:{old.task_id}" for task in live.sessions())


@pytest.mark.asyncio
async def test_restart_refuses_another_session_on_the_source(media):
    old = await live.start(test_live.request(), "owner")
    await live.stop(old.live_id, "owner")
    await live.start(test_live.request(event="other"), "owner")
    with pytest.raises(HTTPException) as error:
        await live.restart(old.live_id, None)
    assert error.value.status_code == 409
    assert len(live.sessions()) == 2
