"""Automatic discovery, private durable bindings and source authorization boundaries."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import test_live_autostart
from fastapi import FastAPI
from test_live import request, runner
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


@pytest.mark.asyncio
async def test_inactive_broadcaster_rebinds_across_workers_and_preserves_stop_after_restart(
    media, monkeypatch
):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    payload = {"name": new, **publication()}
    media.list_paths.return_value = [payload]
    media.get_path.side_effect = lambda path: payload if path == new else None
    logger = Mock()
    monkeypatch.setattr(live, "logger", logger)

    await asyncio.gather(*(auto.reconcile_sources() for _ in range(4)))

    (task,) = live.sessions()
    assert task.live.source_id == "room"
    assert sources.saved_sources() == {"room": new}
    assert sources.registry_path().stat().st_mode & 0o777 == 0o600
    assert "SOURCE_REBOUND" in str(logger.info.call_args_list)
    assert "private" not in str(logger.mock_calls)
    await live.stop(task.task_id, None)
    state.tasks.clear()
    await auto.reconcile_sources()
    assert len(live.sessions()) == 1
    assert live.load(task.task_id).live.stop_requested
    assert sources.known_sources() == {"room": new}


@pytest.mark.asyncio
async def test_rebinding_waits_for_automatic_session_cleanup(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    initial = {"name": old, **publication()}
    media.list_paths.return_value = [initial]
    media.get_path.return_value = initial
    await auto.reconcile_sources()
    (first,) = live.sessions()
    replacement = {"name": new, **publication()}
    media.list_paths.return_value = [replacement]
    media.get_path.side_effect = lambda path: replacement if path == new else None

    await auto.reconcile_sources()

    assert sources.saved_sources() == {"room": old}
    assert live.load(first.task_id).live.state == "completed"
    state.tasks.clear()
    await asyncio.gather(*(auto.reconcile_sources() for _ in range(4)))
    sessions = live.sessions()
    assert len(sessions) == 2
    (second,) = [task for task in sessions if task.live.state not in live.TERMINAL]
    assert second.live.source_id == "room"
    assert second.live.publication_id != first.live.publication_id
    assert state.runners[second.runner_id].live_task_id == second.task_id
    assert sources.saved_sources() == {"room": new}


@pytest.mark.asyncio
async def test_rebinding_waits_through_unconfirmed_runner_cleanup(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    initial = {"name": old, **publication()}
    media.list_paths.return_value = [initial]
    media.get_path.return_value = initial
    await auto.reconcile_sources()
    (first,) = live.sessions()
    replacement = {"name": new, **publication()}
    media.list_paths.return_value = [replacement]
    media.get_path.side_effect = lambda path: replacement if path == new else None
    runner_request = live.runner_request

    async def delayed_cleanup(worker, method, path, payload=None):
        if path.startswith("/task/stop/"):
            raise httpx.ConnectError("Runner unavailable")
        return await runner_request(worker, method, path, payload)

    with monkeypatch.context() as patch:
        patch.setattr(live, "runner_request", delayed_cleanup)
        for _ in range(2):
            await auto.reconcile_sources()
            assert sources.saved_sources() == {"room": old}
            assert len(live.sessions()) == 1
            assert live.load(first.task_id).live.state == "stopping"
            assert state.runners[first.runner_id].live_task_id == first.task_id
    await auto.reconcile_sources()
    assert live.load(first.task_id).live.state == "completed"
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": new}
    assert len(live.sessions()) == 2


@pytest.mark.asyncio
async def test_rebinding_serializes_with_api_allocation(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    payload = {"name": new, **publication()}
    media.list_paths.return_value = [payload]
    observing_old = asyncio.Event()
    resume = asyncio.Event()

    async def get_path(path):
        if path == old:
            observing_old.set()
            await resume.wait()
            return None
        return payload

    media.get_path.side_effect = get_path
    dispatch = AsyncMock(wraps=live.runner_request)
    monkeypatch.setattr(live, "runner_request", dispatch)
    discovery = asyncio.create_task(auto.discover_sources(media))
    await observing_old.wait()
    start = asyncio.create_task(live.start(request(), "client"))
    await asyncio.sleep(0)
    assert not live.sessions()
    resume.set()
    discovered, started = await asyncio.gather(discovery, start)
    assert discovered == {"room"}
    assert sources.saved_sources() == {"room": new}
    (task,) = live.sessions()
    assert task.task_id == started.live_id
    (call,) = [call for call in dispatch.await_args_list if call.args[2] == "/task/run"]
    assert call.args[3]["parameters"]["source_path"] == new


@pytest.mark.asyncio
async def test_rebinding_does_not_redirect_or_stop_an_api_owned_session(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    first = await live.start(request(), "client")
    replacement = {"name": new, **publication()}
    media.list_paths.return_value = [replacement]
    media.get_path.side_effect = lambda path: replacement if path == new else None

    await auto.reconcile_sources()

    assert sources.saved_sources() == {"room": old}
    assert not live.load(first.live_id).live.stop_requested
    await live.stop(first.live_id, "client")
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": new}
    assert len(live.sessions()) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("saved", [False, True])
async def test_simultaneous_paths_keep_existing_source_and_do_not_block_other_broadcasters(
    media, monkeypatch, reverse, saved
):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new, other = "old-private/room", "new-private/room", "other-private/other"
    payloads = {path: {"name": path, **publication()} for path in (old, new, other)}
    if saved:
        await sources.register_paths([old])
    media.list_paths.return_value = list(payloads.values())[:: -1 if reverse else 1]
    media.get_path.side_effect = payloads.get
    logger = Mock()
    monkeypatch.setattr(live, "logger", logger)
    state.runners.register(runner("runner-2", 8083))

    await auto.reconcile_sources()

    expected = {"other": other, **({"room": old} if saved else {})}
    assert sources.saved_sources() == expected
    assert {task.live.source_id for task in live.sessions()} == set(expected)
    assert "SOURCE_NAME_CONFLICT" in str(logger.warning.call_args_list)
    assert "SOURCE_DISCOVERY_FAILED" not in str(logger.warning.call_args_list)
    assert "private" not in str(logger.mock_calls)
    del payloads[new]
    media.list_paths.return_value = list(payloads.values())
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": old, "other": other}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "previous",
    [
        publication(),
        {},
        {"available": True, "source": {"type": "rtspSession"}},
        MediaMTXError("unavailable"),
    ],
)
async def test_incomplete_listing_never_redirects_an_active_or_unconfirmed_path(
    media, monkeypatch, previous
):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new, other = "old-private/room", "new-private/room", "other-private/other"
    await sources.register_paths([old])
    payloads = {path: {"name": path, **publication()} for path in (new, other)}
    media.list_paths.return_value = list(payloads.values())

    def get_path(path):
        if path == old:
            if isinstance(previous, Exception):
                raise previous
            return previous
        return payloads.get(path)

    media.get_path.side_effect = get_path
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": old, "other": other}
    (task,) = live.sessions()
    assert task.live.source_id == "other"


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, {"available": False}])
async def test_disconnected_replacement_is_not_registered(media, monkeypatch, replacement):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    media.list_paths.return_value = [{"name": new, **publication()}]
    media.get_path.side_effect = lambda path: replacement if path == new else {"available": False}
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": old}
    assert not live.sessions()


@pytest.mark.asyncio
async def test_changed_binding_during_rebinding_is_retried_later(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    payload = {"name": new, **publication()}
    media.list_paths.return_value = [payload]
    media.get_path.side_effect = lambda path: payload if path == new else None
    monkeypatch.setattr(auto, "rebind_path", AsyncMock(return_value=False))
    await auto.reconcile_sources()
    assert sources.saved_sources() == {"room": old}
    assert not live.sessions()


@pytest.mark.asyncio
async def test_declared_path_conflict_is_reported_without_blocking_discovery(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {"room": "declared-private/room"})
    payloads = {
        path: {"name": path, **publication()} for path in ("other-private/room", "live/other")
    }
    media.list_paths.return_value = list(payloads.values())
    media.get_path.side_effect = payloads.get
    logger = Mock()
    monkeypatch.setattr(live, "logger", logger)

    await auto.reconcile_sources()

    assert sources.known_sources() == {"room": "declared-private/room", "other": "live/other"}
    (task,) = live.sessions()
    assert task.live.source_id == "other"
    assert "SOURCE_NAME_CONFLICT" in str(logger.warning.call_args_list)
    assert "LIVE_SOURCES" in str(logger.warning.call_args_list)
    assert "SOURCE_DISCOVERY_FAILED" not in str(logger.warning.call_args_list)


@pytest.mark.asyncio
async def test_rebinding_compare_and_swap_and_failed_write_preserve_registry(media, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})
    old, new = "old-private/room", "new-private/room"
    await sources.register_paths([old])
    original = sources.registry_path().read_bytes()
    assert not await sources.rebind_path(new, "stale-private/room")
    with pytest.raises(ValueError, match="same broadcaster"):
        await sources.rebind_path("live/other", old)
    with pytest.raises(ValueError, match="Invalid Live source path"):
        await sources.rebind_path("../room", old)
    monkeypatch.setattr(config.LIVE, "sources", {"room": old})
    assert not await sources.rebind_path(new, old)
    monkeypatch.setattr(config.LIVE, "sources", {})
    replace = Path.replace

    def fail(path, target):
        if target == sources.registry_path():
            raise OSError("unwritable")
        return replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="unwritable"):
            await sources.rebind_path(new, old)
    assert sources.registry_path().read_bytes() == original
    assert not list(sources.registry_path().parent.glob(".live-sources-*"))
    assert await sources.rebind_path(new, old)
    assert sources.saved_sources() == {"room": new}


def test_concurrent_registry_writers_retain_every_source(setup_live, monkeypatch):
    monkeypatch.setattr(config.LIVE, "sources", {})

    def register(index):
        asyncio.run(sources.register_paths([f"live/key-{index}"]))

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(register, range(12)))
    saved = json.loads(sources.registry_path().read_text())
    assert len(saved) == 12
    assert set(saved.values()) == {f"live/key-{index}" for index in range(12)}
