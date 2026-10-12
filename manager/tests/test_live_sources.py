"""Automatic discovery, private durable bindings and source authorization boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import test_live_autostart
from fastapi import FastAPI
from test_live_autostart import publication

from app.api.routes.live import router
from app.core import state
from app.core.auth import verify_token
from app.core.config import config
from app.services import live_autostart as auto
from app.services import live_service as live
from app.services import live_sources as sources
from app.services.mediamtx import MediaMTXError

media = test_live_autostart.media
setup_live = test_live_autostart.setup_live


@pytest.mark.asyncio
async def test_unmapped_publications_capacity_stop_restart_and_recovery(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    monkeypatch.setattr(live, "MediaMTXClient", lambda *_: media)
    paths = ["private-key/first", "private-key/second"]
    payloads = {path: {"name": path, **publication()} for path in paths}
    media.list_paths.return_value = list(payloads.values())
    media.get_path.side_effect = lambda path: payloads.get(path)
    await asyncio.gather(*(auto.reconcile_sources() for _ in range(4)))
    (first,) = live.sessions()
    assert first.live.source_id == "first"
    assert len(sources.saved_sources()) == 2
    assert "private-key" not in live.response(first).model_dump_json()
    assert "private-key" not in first.model_dump_json()
    await live.stop(first.task_id, None)
    await auto.reconcile_sources()
    active = [task for task in live.sessions() if task.live.state not in live.TERMINAL]
    assert len(active) == 1 and active[0].live.source_id == sources.generated_id(paths[1])
    await live.stop(active[0].task_id, None)
    state.tasks.clear()
    await auto.reconcile_sources()
    assert len(live.sessions()) == 2  # Same RTMP connections stay explicitly stopped.
    replacement = await live.restart(first.task_id, None)
    assert replacement.live_id != first.task_id
    state.tasks.clear()
    await auto.reconcile_sources()
    assert not live.load(replacement.live_id).live.stop_requested
    assert len(live.sessions()) == 3
    del payloads[paths[0]]
    media.list_paths.return_value = list(payloads.values())
    await auto.reconcile_sources()
    assert live.load(replacement.live_id).live.state == "completed"
    payloads[paths[0]] = {"name": paths[0], **publication()}
    media.list_paths.return_value = list(payloads.values())
    await auto.reconcile_sources()
    assert len(live.sessions()) == 4


@pytest.mark.asyncio
async def test_discovery_ignores_unsafe_paths_non_rtmp_and_bad_observations(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    good = {"name": "live/accepted", **publication()}
    media.list_paths.return_value = [
        {"name": "live/absent", "available": False},
        {"name": "live/rtsp", "available": True, "source": {"type": "rtspSession"}},
        {
            "name": "live/bad",
            "available": True,
            "source": {"type": "rtmpConn", "id": "bad"},
        },
        *(
            {"name": path, **publication()}
            for path in (
                "../escape",
                "a?pass=secret",
                "a#b",
                "/root",
                "rtmp://other/stream",
                "a//b",
                None,
            )
        ),
        good,
    ]
    media.get_path.return_value = good
    await auto.reconcile_sources()
    assert sources.saved_sources() == {sources.generated_id(good["name"]): good["name"]}
    (task,) = live.sessions()
    assert task.live.source_id == "accepted"


@pytest.mark.parametrize(
    "path",
    ["../escape", "live/a?b", "live/_invalid", "live/-invalid", "live/" + "a" * 101],
)
def test_pod_broadcaster_slug_uses_final_path_component_and_rejects_invalid_paths(path):
    assert sources.pod_broadcaster_slug("live/lecture-hall-a") == "lecture-hall-a"
    with pytest.raises(ValueError, match="Invalid Live source path"):
        sources.pod_broadcaster_slug(path)


def test_source_identifier_is_the_full_broadcaster_name():
    """Accept the API length limit and preserve broadcaster names starting with source-."""
    assert sources.generated_id("live/lecture-hall-a") == "lecture-hall-a"
    assert sources.source_id("private/source-lecture-hall-a") == "source-lecture-hall-a"
    assert sources.generated_id("live/" + "a" * 100) == "a" * 100


@pytest.mark.asyncio
async def test_new_publisher_replaces_the_same_broadcaster_after_cleanup(media, monkeypatch):
    """Keep one source row and expose the replacement's session and equipment after restart."""
    from datetime import datetime

    from app.services import live_admin

    monkeypatch.setattr(config.LIVE, "sources", {})
    monkeypatch.setattr(live, "MediaMTXClient", lambda *_: media)
    path = "live/lecture-hall-a"
    initial = {"name": path, **publication()}
    media.list_paths.return_value = [initial]
    media.get_path.return_value = initial
    media.get_rtmp_connection.return_value = {"query": "equipment=first-encoder"}
    await auto.reconcile_sources()
    (first,) = live.sessions()

    replacement = {"name": path, **publication()}
    media.list_paths.return_value = [replacement]
    media.get_path.return_value = replacement
    media.get_rtmp_connection.return_value = {"query": "equipment=second-encoder"}
    await auto.reconcile_sources()
    assert live.load(first.task_id).live.state == "completed"
    assert (await live.source_status("lecture-hall-a"))["live"]["hls_url"] is None
    state.tasks.clear()
    await asyncio.gather(*(auto.reconcile_sources() for _ in range(4)))

    sessions = live.sessions()
    assert len(sessions) == 2
    (second,) = [task for task in sessions if task.live.state not in live.TERMINAL]
    assert second.live.source_id == first.live.source_id == "lecture-hall-a"
    assert second.live.event_id != first.live.event_id
    assert state.runners[second.runner_id].live_task_id == second.task_id
    status = await live.source_status("lecture-hall-a")
    assert status["live"]["live_id"] == second.task_id
    assert status["live"]["equipment"] == "second-encoder"
    overview = live_admin.build_overview(sessions, state.runners.values(), datetime.now())
    (row,) = live_admin.source_rows(overview, {"lecture-hall-a"})
    assert row["id"] == row["broadcaster"] == "lecture-hall-a"
    assert row["latest_session"]["id"] == second.task_id
    assert row["latest_session"]["equipment"] == "second-encoder"
    assert sources.saved_sources() == {"lecture-hall-a": path}


@pytest.mark.asyncio
async def test_listing_failure_never_means_publisher_disconnection(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    payload = {"name": "live/private-key", **publication()}
    media.list_paths.return_value = [payload]
    media.get_path.return_value = payload
    await auto.reconcile_sources()
    (task,) = live.sessions()
    media.list_paths.side_effect = MediaMTXError("unavailable")
    media.get_path.reset_mock()
    await auto.reconcile_sources()
    media.get_path.assert_awaited_once_with(payload["name"])
    assert not live.load(task.task_id).live.stop_requested
    await live.stop(task.task_id, None)
    media.get_path.reset_mock()
    await auto.reconcile_sources()
    media.get_path.assert_not_awaited()  # Historical inactive paths are not polled forever.


@pytest.mark.asyncio
async def test_saved_broadcaster_survives_configuration_removal_and_cannot_be_redirected(
    media, monkeypatch
):
    await sources.register_paths(["secret-key/room"])
    assert sources.saved_sources() == {"room": "secret-key/room"}
    monkeypatch.setattr(config.LIVE, "sources", {})
    assert sources.source_id("secret-key/room") == "room"
    await auto.reconcile_source("room", media)
    (task,) = live.sessions()
    monkeypatch.setattr(config.LIVE, "sources", {"another-name": "secret-key/room"})
    with pytest.raises(ValueError, match="broadcaster"):
        sources.known_sources()
    monkeypatch.setattr(config.LIVE, "sources", {"room": "live/room"})
    with pytest.raises(ValueError, match="conflicts"):
        sources.known_sources()
    await auto.reconcile_sources()
    assert not live.load(task.task_id).live.stop_requested


@pytest.mark.asyncio
async def test_source_listing_requires_authentication_and_never_returns_paths(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    await sources.register_paths(["secret/lecture"])
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as client:
        assert (await client.get("/live/sources")).status_code == 401
        app.dependency_overrides[verify_token] = lambda: "client"
        result = await client.get("/live/sources")
        assert result.status_code == 200
        assert result.json() == {"sources": ["lecture"]}
        assert "secret" not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        "bad-json",
        "[]",
        '{"../escape":"live/key"}',
        '{"room":null}',
        '{"a":"live/key","b":"live/key"}',
        '{"source-0123456789abcdef":"live/lecture"}',
    ],
)
async def test_corrupt_source_registry_fails_closed_without_allocations(media, monkeypatch, data):
    monkeypatch.setattr(config.LIVE, "sources", {})
    sources.registry_path().write_text(data)
    media.list_paths.return_value = [{"name": "live/new", **publication()}]
    await auto.reconcile_sources()
    assert not live.sessions()
    assert sources.registry_path().read_text() == data


@pytest.mark.asyncio
async def test_atomic_registry_write_failure_preserves_bindings_and_cleans_temporary(
    media, monkeypatch
):
    await sources.register_paths(["secret-key/room"])
    original = sources.registry_path().read_bytes()
    replace = Path.replace

    def fail(path, target):
        if target == sources.registry_path():
            raise OSError("unwritable")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        await sources.register_paths(["live/second"])
    assert sources.registry_path().read_bytes() == original
    assert not list(sources.registry_path().parent.glob(".live-sources-*"))
    assert sources.registry_path().stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="Invalid"):
        await sources.register_paths(["../escape"])


@pytest.mark.asyncio
async def test_broadcaster_collision_fails_without_overwriting_binding(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {"new": "private/new"})
    with pytest.raises(ValueError, match="collision"):
        await sources.register_paths(["live/new"])
    assert not sources.saved_sources()


def test_concurrent_registry_writers_retain_every_source(setup_live, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})

    def register(index):
        asyncio.run(sources.register_paths([f"live/key-{index}"]))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(register, range(12)))
    saved = json.loads(sources.registry_path().read_text())
    assert len(saved) == 12
    assert set(saved.values()) == {f"live/key-{index}" for index in range(12)}
