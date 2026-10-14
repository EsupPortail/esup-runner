"""Pod retry durability, media-independent cleanup and stale-session protection."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import test_live

from app.core import state
from app.core.config import config
from app.services import live_runtime as live
from app.services import pod_sync
from app.services.pod_broadcaster import PodBroadcasterError

live_env = test_live.live_env


@pytest.fixture
def configured(live_env, monkeypatch):
    live_env.parameters["broadcaster_id"] = "pod-source"
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"
    test_live.tracked(live_env)
    monkeypatch.setattr(pod_sync, "_sync_lock", asyncio.Lock())
    client = AsyncMock()
    monkeypatch.setattr(pod_sync, "PodBroadcasterClient", lambda *args: client)
    return live_env, client


@pytest.mark.asyncio
@pytest.mark.parametrize("desired", [True, False])
async def test_failed_intent_survives_restart_and_retries(configured, monkeypatch, desired):
    request, client = configured
    client.set_status.side_effect = [PodBroadcasterError("offline"), None]
    with pytest.raises(PodBroadcasterError):
        await pod_sync.set_status(request, desired, client)
    if not desired:
        live.update(request.task_id, state="completed", cleanup_complete=True)
        state.set_task_status(request.task_id, "completed")
        state.set_available(True)
    state.reload_task_statuses_from_disk()
    assert pod_sync.pending(request.task_id) is desired
    monkeypatch.setattr(
        pod_sync, "_manager_status", AsyncMock(return_value=(desired, request.task_id))
    )
    await pod_sync.retry(request)
    assert client.set_status.await_count == 2
    assert pod_sync.pending(request.task_id) is None
    assert state.is_available() is (not desired)


@pytest.mark.asyncio
async def test_retry_failure_keeps_terminal_intent_without_reservation(configured, monkeypatch):
    request, client = configured
    live.update(request.task_id, state="completed", pod_status_pending=False)
    state.set_task_status(request.task_id, "completed")
    state.set_available(True)
    client.set_status.side_effect = PodBroadcasterError("offline")
    monkeypatch.setattr(
        pod_sync, "_manager_status", AsyncMock(return_value=(False, request.task_id))
    )
    with pytest.raises(PodBroadcasterError):
        await pod_sync.retry(request)
    assert pod_sync.pending(request.task_id) is False
    assert state.is_available()


@pytest.mark.asyncio
async def test_old_stop_cannot_disable_replacement(configured, monkeypatch):
    request, client = configured
    live.update(request.task_id, pod_status_pending=False)
    replacement = str(uuid4())
    monkeypatch.setattr(pod_sync, "_manager_status", AsyncMock(return_value=(True, replacement)))
    await pod_sync.retry(request)
    client.set_status.assert_awaited_once_with("pod-source", True)
    assert pod_sync.pending(request.task_id) is None


@pytest.mark.asyncio
async def test_replacement_during_patch_is_compensated(configured, monkeypatch):
    request, client = configured
    live.update(request.task_id, pod_status_pending=False)
    replacement = str(uuid4())
    monkeypatch.setattr(
        pod_sync,
        "_manager_status",
        AsyncMock(side_effect=[(False, request.task_id), (True, replacement), (True, replacement)]),
    )
    await pod_sync.retry(request)
    assert [call.args for call in client.set_status.await_args_list] == [
        ("pod-source", False),
        ("pod-source", True),
    ]
    assert pod_sync.pending(request.task_id) is None


@pytest.mark.asyncio
async def test_changing_manager_state_bounds_each_retry_cycle(configured, monkeypatch):
    request, client = configured
    live.update(request.task_id, pod_status_pending=False)
    replacement = str(uuid4())
    monkeypatch.setattr(
        pod_sync,
        "_manager_status",
        AsyncMock(
            side_effect=[(False, request.task_id), (True, replacement), (False, replacement)]
        ),
    )
    await pod_sync.retry(request)
    assert client.set_status.await_count == 2
    assert pod_sync.pending(request.task_id) is False


@pytest.mark.asyncio
async def test_delayed_activation_cannot_acknowledge_new_stop(configured):
    request, client = configured

    async def newer_stop(*args):
        live.update(request.task_id, pod_status_pending=False)

    client.set_status.side_effect = newer_stop
    await pod_sync.set_status(request, True, client)
    assert pod_sync.pending(request.task_id) is False


@pytest.mark.asyncio
async def test_activation_and_cleanup_are_serialized(configured):
    request, client = configured
    entered = asyncio.Event()
    release = asyncio.Event()

    async def patch(source, desired):
        if desired:
            entered.set()
            await release.wait()

    client.set_status.side_effect = patch
    activating = asyncio.create_task(pod_sync.set_status(request, True, client))
    await entered.wait()
    stopping = asyncio.create_task(pod_sync.set_status(request, False, client))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(activating, stopping)
    assert [call.args[1] for call in client.set_status.await_args_list] == [True, False]
    assert pod_sync.pending(request.task_id) is None


@pytest.mark.asyncio
async def test_queued_activation_is_skipped_when_stop_supersedes_it(configured):
    request, client = configured
    async with pod_sync._sync_lock:
        activating = asyncio.create_task(pod_sync.set_status(request, True, client))
        await asyncio.sleep(0)
        stopping = asyncio.create_task(pod_sync.set_status(request, False, client))
        await asyncio.sleep(0)
    await asyncio.gather(activating, stopping)
    client.set_status.assert_awaited_once_with("pod-source", False)
    assert pod_sync.pending(request.task_id) is None


@pytest.mark.asyncio
async def test_retry_is_skipped_when_disabled_or_already_synchronized(configured):
    request, client = configured
    await pod_sync.retry(request)
    live.update(request.task_id, pod_status_pending=True)
    config.LIVE.pod_establishment_name = "another-establishment"
    await pod_sync.retry(request)
    client.set_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["owned", "foreign", "malformed", "mismatch", "checkpoint"])
async def test_retry_loop_isolates_records_and_write_failures(configured, monkeypatch, kind):
    request, client = configured
    live.update(request.task_id, pod_status_pending=False)
    if kind == "foreign":
        state.set_task_metadata(request.task_id, runner_id="another-runner")
    elif kind == "malformed":
        state.set_task_metadata(request.task_id, task_request={})
    elif kind == "mismatch":
        state.set_task_metadata(
            request.task_id, task_request={**request.model_dump(), "task_id": str(uuid4())}
        )
    worker = AsyncMock()
    if kind == "checkpoint":
        from fastapi import HTTPException

        worker.side_effect = HTTPException(503, "checkpoint unavailable")
    monkeypatch.setattr(pod_sync, "retry", worker)
    monkeypatch.setattr(pod_sync.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await pod_sync.retry_loop()
    assert worker.await_count == (1 if kind in ("owned", "checkpoint") else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("observation", ["active", "stop", "replacement", "other_etab", "absent"])
async def test_manager_observation_uses_latest_scoped_session(configured, monkeypatch, observation):
    request, client = configured
    live.update(request.task_id, pod_status_pending=observation != "stop")
    replacement = str(uuid4())
    is_new = observation in ("replacement", "other_etab")
    config.LIVE.pod_establishment_name = request.etab_name
    requests = []

    def reply(req):
        requests.append(req)
        if "/task/status/" in req.url.path:
            return httpx.Response(
                200,
                json={"etab_name": "another" if observation == "other_etab" else request.etab_name},
            )
        return httpx.Response(
            200,
            json={
                "source_ready": True,
                "live": (
                    None
                    if observation == "absent"
                    else {"live_id": replacement if is_new else request.task_id, "state": "running"}
                ),
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        pod_sync.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(reply), **kwargs),
    )
    expected = observation in ("active", "replacement")
    assert await pod_sync._manager_status(request) == (
        expected,
        None if observation == "absent" else replacement if is_new else request.task_id,
    )
    assert all(req.headers["Authorization"] == f"Bearer {config.RUNNER_TOKEN}" for req in requests)
    assert all(str(req.url).startswith(config.MANAGER_URL.rstrip("/")) for req in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        {"live": []},
        {"live": {"state": "invalid"}},
        {"live": {"state": "running", "live_id": None}},
        {"live": {"state": "running", "live_id": "invalid"}},
    ],
)
async def test_uncertain_manager_status_defers_retry(configured, monkeypatch, payload):
    request, client = configured
    original = httpx.AsyncClient
    monkeypatch.setattr(
        pod_sync.httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(503 if payload is None else 200, json=payload)
            ),
            **kwargs,
        ),
    )
    with pytest.raises(PodBroadcasterError, match="Manager Live status unavailable"):
        await pod_sync._manager_status(request)
    client.set_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "types,pod_url,enabled",
    [
        (set(["live"]), "https://pod.example", True),
        (set(["encoding", "live"]), "https://pod.example", True),
        (set(["encoding"]), "https://pod.example", False),
        (set(["live"]), "", False),
    ],
)
async def test_background_retry_service_is_scoped_to_live_instances(
    monkeypatch, types, pod_url, enabled
):
    from app.managers import service_manager

    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", types)
    monkeypatch.setattr(config, "ENCODING_TYPE", "CPU")
    monkeypatch.setattr(config.LIVE, "pod_url", pod_url)

    async def sleeper():
        await asyncio.Event().wait()

    for name in (
        "reconnect_loop",
        "heartbeat_loop",
        "storage_cleanup_loop",
        "task_process_check_loop",
        "pod_sync_loop",
    ):
        monkeypatch.setattr(service_manager, name, sleeper)
    manager = service_manager.BackgroundServiceManager()
    await manager.start_all_services()
    assert {task.get_name() for task in manager.tasks} == {
        "reconnect_loop",
        "heartbeat_loop",
        "storage_cleanup_loop",
        "task_process_check_loop",
    } | ({"pod_sync_loop"} if enabled else set())
    await manager.stop_all_services()
