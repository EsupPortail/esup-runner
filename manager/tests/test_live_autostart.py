"""RTMP-triggered allocation, durable publication identity and encoder lifecycle."""

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import test_live
from fastapi import FastAPI, HTTPException
from test_live import request, runner

from app.api.routes.live import router
from app.core import state
from app.core.auth import verify_token
from app.core.config import config
from app.core.live_config import LiveSettings
from app.services import live_autostart as auto
from app.services import live_service as live
from app.services.mediamtx import MediaMTXError

setup_live = test_live.setup_live


def publication():
    """Model the RTMP connection identity exposed by MediaMTX 1.21.1."""
    return {"available": True, "source": {"type": "rtmpConn", "id": str(uuid4())}}


@pytest.fixture
def media(setup_live, monkeypatch):
    monkeypatch.setattr(config.LIVE, "auto_start", True)
    client = AsyncMock()
    client.get_path.return_value = publication()
    client.get_rtmp_connection.return_value = None
    client.list_paths.return_value = []
    monkeypatch.setattr(auto, "MediaMTXClient", lambda *args: client)
    return client


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"available": False},
        {"available": True, "source": {"type": "rtspSession"}},
    ],
)
def test_absent_or_non_rtmp_publication(payload):
    assert auto.publication_event("room", payload) is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"available": True},
        {"available": True, "source": {"type": "rtmpConn", "id": "invalid"}},
    ],
)
def test_invalid_observation_is_not_an_absent_publication(payload):
    with pytest.raises(MediaMTXError):
        auto.publication_event("room", payload)


@pytest.mark.parametrize(
    "query,expected",
    [
        (None, None),
        ("user=publisher&pass=secret", None),
        ("user=publisher&equipment=SMP351-01&pass=secret", "SMP351-01"),
        ("equipment=SMP351%2D01", "SMP351-01"),
        ("equipment=invalid%2Fvalue", None),
        ("equipment=" + "x" * 201, None),
        ("equipment=one&equipment=two", None),
    ],
)
def test_equipment_is_extracted_without_accepting_ambiguous_or_unsafe_values(query, expected):
    assert auto.extract_equipment(query) == expected


def test_equipment_rejects_too_many_query_fields():
    query = "&".join(f"field{index}=value" for index in range(65))

    assert auto.extract_equipment(query) is None


@pytest.mark.parametrize(
    "query,expected",
    [
        (None, None),
        ("user=publisher&pass=secret", None),
        (
            "etablishment=Universit%C3%A9%20de%20Montpellier",
            "Université de Montpellier",
        ),
        ("etablishment=   Universit%C3%A9+de+Lorraine  ", "Université de Lorraine"),
        ("etablishment=", None),
        ("etablishment=" + "x" * 201, None),
        ("etablishment=one&etablishment=two", None),
    ],
)
def test_etablishment_is_decoded_and_rejects_ambiguous_or_empty_values(query, expected):
    assert auto.extract_etablishment(query) == expected


def test_etablishment_rejects_too_many_query_fields():
    query = "&".join(f"field{index}=value" for index in range(65))

    assert auto.extract_etablishment(query) is None


@pytest.mark.asyncio
async def test_automatic_publication_query_sets_equipment_and_path_slug(media):
    payload = publication()
    media.get_path.return_value = payload
    media.get_rtmp_connection.return_value = {
        "query": (
            "user=publisher&pass=secret&equipment=SMP351-01&"
            "etablishment=Universit%C3%A9%20de%20Montpellier"
        )
    }

    await auto.reconcile_source("room", media)

    (task,) = live.sessions()
    assert task.app_version == "1.21.1"
    assert task.live.equipment == "SMP351-01"
    assert task.etab_name == "Université de Montpellier"
    assert "secret" not in json.dumps(task.live.model_dump())
    media.get_rtmp_connection.assert_awaited_once_with(payload["source"]["id"], secure=False)


@pytest.mark.asyncio
async def test_concurrent_publication_allocates_once_and_survives_cache_restart(media):
    state.runners.register(runner("runner-2", 8083))
    await asyncio.gather(*(auto.reconcile_source("room", media) for _ in range(8)))
    (task,) = live.sessions()
    assert task.live.automatic and task.client_token is None
    assert task.etab_name == "unknown" and task.app_name == "MediaMTX"
    assert task.live.state == "waiting_source"
    assert state.runners[task.runner_id].live_task_id == task.task_id
    assert auto.read_fence("room") == task.live.event_id
    state.tasks.clear()
    await auto.reconcile_source("room", media)
    assert [item.task_id for item in live.sessions()] == [task.task_id]


@pytest.mark.asyncio
async def test_absence_releases_runner_and_republication_gets_new_session(media):
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    media.get_path.return_value = None
    await auto.reconcile_source("room", media)
    assert live.load(first.task_id).live.state == "completed"
    assert state.runners[first.runner_id].live_task_id is None
    media.get_path.return_value = publication()
    await auto.reconcile_source("room", media)
    active = [task for task in live.sessions() if task.live.state not in live.TERMINAL]
    assert len(active) == 1 and active[0].task_id != first.task_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shutdown_error",
    ["Runner stopped", "Runner shutting down", "Runner restarted; session stopped safely"],
)
async def test_runner_shutdown_restarts_active_automatic_publication(media, shutdown_error):
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    first.live = first.live.model_copy(update={"state": "failed", "error": shutdown_error})
    first.status = "failed"
    live.persist(first)
    state.runners.release_live(first.runner_id, first.task_id)

    await auto.reconcile_source("room", media)

    sessions = live.sessions()
    assert len(sessions) == 2
    replacement = next(task for task in sessions if task.task_id != first.task_id)
    assert replacement.live.automatic
    assert replacement.live.publication_id == first.live.event_id
    assert replacement.live.event_id == f"restart:{first.task_id}"
    assert live.load(first.task_id).live.error == shutdown_error

    replacement.live = replacement.live.model_copy(
        update={"state": "failed", "error": "Runner stopped"}
    )
    replacement.status = "failed"
    live.persist(replacement)
    state.runners.release_live(replacement.runner_id, replacement.task_id)
    await auto.reconcile_source("room", media)

    sessions = live.sessions()
    assert len(sessions) == 3
    second_replacement = max(sessions, key=lambda task: task.created_at)
    assert second_replacement.live.publication_id == first.live.event_id
    assert second_replacement.live.event_id == f"restart:{replacement.task_id}"

    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 3

    second_replacement.live = second_replacement.live.model_copy(
        update={"state": "failed", "error": "FFmpeg failed"}
    )
    second_replacement.status = "failed"
    live.persist(second_replacement)
    state.runners.release_live(second_replacement.runner_id, second_replacement.task_id)
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 3


@pytest.mark.asyncio
async def test_forced_runner_restart_waits_for_cleanup_and_allocates_once(media, setup_live):
    """Recover one publication after persisted cleanup, across competing scans."""
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    setup_live[first.task_id]["live"].update(
        state="stopping", cleanup_complete=False, error="Live cleanup pending; runner remains busy"
    )
    await live.synchronize(first.task_id)
    await asyncio.gather(*(auto.reconcile_source("room", media) for _ in range(8)))
    assert len(live.sessions()) == 1
    assert state.runners[first.runner_id].live_task_id == first.task_id

    setup_live[first.task_id].update(status="failed")
    setup_live[first.task_id]["live"].update(
        state="failed", cleanup_complete=True, error="Runner restarted; session stopped safely"
    )
    await live.synchronize(first.task_id)
    state.tasks.clear()
    await asyncio.gather(*(auto.reconcile_source("room", media) for _ in range(8)))

    replacements = [task for task in live.sessions() if task.task_id != first.task_id]
    assert len(replacements) == 1
    replacement = replacements[0]
    assert replacement.live.publication_id == first.live.publication_id
    assert replacement.live.event_id == f"restart:{first.task_id}"
    assert state.runners[replacement.runner_id].live_task_id == replacement.task_id
    state.tasks.clear()
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 2


@pytest.mark.asyncio
async def test_explicit_stop_fences_forced_runner_restart(media):
    """A recovered Runner must never replace an operator-stopped publication."""
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    first.live = first.live.model_copy(
        update={
            "state": "failed",
            "error": "Runner restarted; session stopped safely",
            "stop_requested": True,
        }
    )
    first.status = "failed"
    live.persist(first)
    state.runners.release_live(first.runner_id, first.task_id)
    state.tasks.clear()

    await asyncio.gather(*(auto.reconcile_source("room", media) for _ in range(8)))

    assert len(live.sessions()) == 1
    assert state.runners[first.runner_id].live_task_id is None


@pytest.mark.asyncio
async def test_other_terminal_failure_does_not_restart_automatic_publication(media):
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    first.live = first.live.model_copy(update={"state": "failed", "error": "FFmpeg failed"})
    first.status = "failed"
    live.persist(first)
    state.runners.release_live(first.runner_id, first.task_id)

    await auto.reconcile_source("room", media)

    assert len(live.sessions()) == 1


@pytest.mark.asyncio
async def test_fast_republication_waits_for_old_cleanup(media, monkeypatch):
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    media.get_path.return_value = publication()
    control = live.runner_request
    monkeypatch.setattr(live, "runner_request", AsyncMock(side_effect=RuntimeError("offline")))
    await auto.reconcile_source("room", media)
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 1
    assert live.load(first.task_id).live.stop_requested
    assert state.runners[first.runner_id].live_task_id == first.task_id
    monkeypatch.setattr(live, "runner_request", control)
    await auto.reconcile_source("room", media)
    assert live.load(first.task_id).live.state == "completed"
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 2


@pytest.mark.asyncio
async def test_media_outage_does_not_stop_session_or_block_other_sources(media, monkeypatch):
    await auto.reconcile_source("room", media)
    (first,) = live.sessions()
    state.runners.register(runner("runner-2", 8083))
    next_publication = publication()

    async def observe(path):
        if path == config.LIVE.sources["room"]:
            raise MediaMTXError("unavailable")
        return next_publication if path == config.LIVE.sources["room0"] else None

    media.get_path.side_effect = observe
    await auto.reconcile_sources()
    assert not live.load(first.task_id).live.stop_requested
    assert {task.live.source_id for task in live.sessions()} == {"room", "room0"}


@pytest.mark.asyncio
async def test_capacity_is_retried_without_creating_pending_tasks(media):
    manual = await live.start(request("room0"), "owner")
    await auto.reconcile_sources()
    assert len(live.sessions()) == 1
    assert auto.read_fence("room") is None
    await live.stop(manual.live_id, "owner")
    await auto.reconcile_sources()
    automatic = [task for task in live.sessions() if task.live.automatic]
    assert len(automatic) == 1 and automatic[0].live.source_id == "room"


@pytest.mark.asyncio
async def test_existing_api_session_retains_lifecycle_and_ownership(media):
    manual = await live.start(request(), "owner")
    await auto.reconcile_source("room", media)
    media.get_path.return_value = None
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 1
    assert not live.load(manual.live_id).live.stop_requested
    with pytest.raises(HTTPException) as error:
        live.load(manual.live_id, "another-client")
    assert error.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed"])
async def test_terminal_publication_is_fenced_even_after_history_retention(
    media, setup_live, terminal
):
    await auto.reconcile_source("room", media)
    (task,) = live.sessions()
    if terminal == "completed":
        await live.stop(task.task_id, None)
    else:
        setup_live[task.task_id]["live"].update(state="failed", cleanup_complete=True)
        await live.synchronize(task.task_id)
    # Recover the checkpoint if shutdown interrupted its first write.
    auto.fence_path("room").unlink()
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 1
    assert auto.read_fence("room") == task.live.event_id
    assert state.persistence.delete_task(task.task_id)
    state.tasks.clear()
    assert not live.sessions()
    await auto.reconcile_source("room", media)
    assert not live.sessions()
    media.get_path.return_value = publication()
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 1


@pytest.mark.asyncio
async def test_failed_reservation_does_not_consume_publication(media, monkeypatch):
    reserve = state.runners.reserve_live
    monkeypatch.setattr(state.runners, "reserve_live", lambda *_: None)
    with pytest.raises(HTTPException):
        await auto.reconcile_source("room", media)
    assert auto.read_fence("room") is None
    monkeypatch.setattr(state.runners, "reserve_live", reserve)
    await auto.reconcile_source("room", media)
    active = [task for task in live.sessions() if task.live.state not in live.TERMINAL]
    assert len(active) == 1


@pytest.mark.asyncio
async def test_checkpoint_write_failure_recovers_without_duplicate(media, monkeypatch):
    remember = auto.remember_publication
    monkeypatch.setattr(auto, "remember_publication", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        await auto.reconcile_source("room", media)
    (task,) = live.sessions()
    assert state.runners[task.runner_id].live_task_id == task.task_id
    monkeypatch.setattr(auto, "remember_publication", remember)
    await live.synchronize(task.task_id)
    await auto.reconcile_source("room", media)
    assert len(live.sessions()) == 1
    assert auto.read_fence("room") == task.live.event_id


@pytest.mark.asyncio
@pytest.mark.parametrize("contents", ["{bad-json", "null", '"wrong-source"'])
async def test_corrupt_checkpoint_fails_closed(media, contents):
    auto.fence_path("room").write_text(contents)
    with pytest.raises(ValueError):
        await auto.reconcile_source("room", media)
    assert not live.sessions()
    with pytest.raises(ValueError, match="Unknown Live source"):
        auto.fence_path("../invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,automatic", [(False, True), (True, False)])
async def test_disabled_watcher_makes_no_media_calls(media, monkeypatch, enabled, automatic):
    monkeypatch.setattr(config.LIVE, "enabled", enabled)
    monkeypatch.setattr(config.LIVE, "auto_start", automatic)
    await auto.reconcile_sources()
    media.get_path.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_discovery_and_automatic_control_require_authenticated_client(
    media, monkeypatch
):
    await auto.reconcile_source("room", media)
    (task,) = live.sessions()
    monkeypatch.setattr(live, "MediaMTXClient", lambda *args: media)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as client:
        assert (await client.get("/live/sources/room")).status_code == 401
        assert (await client.get(f"/live/{task.task_id}")).status_code == 401
        assert (await client.post(f"/live/{task.task_id}/stop")).status_code == 401
        app.dependency_overrides[verify_token] = lambda: "authorized-client"
        data = (await client.get("/live/sources/room")).json()
        assert data["live"]["live_id"] == task.task_id and data["live"]["automatic"]
        assert "secret-key" not in json.dumps(data)
        assert (await client.get(f"/live/{task.task_id}")).status_code == 200
        assert (await client.post(f"/live/{task.task_id}/stop")).json()["state"] == "completed"
        await live.start(request("room0"), "private-owner")
        assert (await client.get("/live/sources/room0")).json()["live"] is None


def test_automatic_configuration_and_unique_paths(monkeypatch):
    monkeypatch.setenv("LIVE_AUTO_START", "false")
    monkeypatch.setenv("LIVE_AUTO_ETAB_NAME", "University")
    settings = LiveSettings.from_environment()
    assert not settings.auto_start and settings.auto_etab_name == "University"
    settings = LiveSettings(enabled=True, sources={"a": "live/key", "b": "live/key"})
    with pytest.raises(ValueError, match="unique"):
        settings.validate_enabled()
    settings.auto_start = False
    with pytest.raises(ValueError, match="broadcaster"):
        settings.validate_enabled()
