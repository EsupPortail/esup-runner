"""Live API, durable orchestration and exclusive reservation regressions."""

import asyncio
import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api.routes.live import router
from app.core import state
from app.core.auth import verify_token
from app.core.config import config
from app.core.live_config import LiveSettings
from app.core.persistence import SafeDailyJSONPersistence
from app.core.runner_store import RunnerStore
from app.models.live import LiveStartRequest
from app.models.models import Runner
from app.services import live_service as live
from app.services.mediamtx import MediaMTXError

RUNNER_REQUEST = live.runner_request


@pytest.mark.asyncio
async def test_live_statistics_once_after_stop_and_reconciliation(
    setup_live, monkeypatch, tmp_path
):
    """Stop, duplicate callbacks and concurrent writers count one terminal session."""
    from app.api.routes import task as task_routes

    monkeypatch.setattr(config, "MANAGER_DATA_DIR", str(tmp_path / "stats"))
    monkeypatch.setattr(task_routes, "_is_pytest_run", lambda: False)
    started = await live.start(request(), "client")
    csv_path = tmp_path / "stats" / "task_stats.csv"
    assert not csv_path.exists()
    with pytest.raises(HTTPException) as error:
        await live.stop(started.live_id, "another-client")
    assert error.value.status_code == 403
    ended = await live.stop(started.live_id, None)
    assert ended.state == "completed"
    task = await live.synchronize(started.live_id)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(task_routes._append_task_stats_csv, [task] * 8))
    with csv_path.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["task_type"] == "live" and rows[0]["status"] == "completed"
    assert rows[0]["task_id"] == started.live_id


@pytest.mark.asyncio
async def test_live_stats_failure_does_not_hold_reservation(setup_live, monkeypatch):
    """A statistics disk error leaves media cleanup and Runner release intact."""
    from app.api.routes import task as task_routes

    def fail(_task):
        raise OSError("stats disk unavailable")

    monkeypatch.setattr(task_routes, "_append_task_stats_csv", fail)
    started = await live.start(request(), "client")
    ended = await live.stop(started.live_id, None)
    assert ended.state == "completed"
    assert state.runners[ended.runner].live_task_id is None


def runner(identifier="runner-1", port=8082):
    return Runner(
        id=identifier,
        url=f"http://localhost:{port}",
        task_types=["live"],
        token="runner-secret",
        version="1.9.1",
        last_heartbeat=datetime.now(),
    )


def request(source="room", event="event-1"):
    return LiveStartRequest(
        source_id=source, event_id=event, equipment="SMP351", etab_name="university"
    )


@pytest.fixture(
    params=[["live"], ["encoding", "studio", "transcription", "live"]], ids=["dedicated", "mixed"]
)
def setup_live(tmp_path, monkeypatch, request):
    monkeypatch.setattr(state, "persistence", SafeDailyJSONPersistence(str(tmp_path / "manager")))
    monkeypatch.setattr(state, "tasks", {})
    monkeypatch.setattr(
        state,
        "runners",
        RunnerStore(shared_enabled=True, state_file=str(tmp_path / "runners.json")),
    )
    monkeypatch.setattr(
        config,
        "LIVE",
        LiveSettings(
            enabled=True,
            auto_start=False,
            sources={
                "room": "secret-key/room",
                **{f"room{i}": f"secret-key/room{i}" for i in range(5)},
            },
        ),
    )
    worker = runner()
    worker.task_types = request.param
    state.runners.register(worker)
    remote = {}

    async def call(worker, method, path, payload=None):
        task_id = path.rsplit("/", 1)[-1]
        if path == "/task/run":
            task_id = payload["task_id"]
            remote.setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "status": "running",
                    "live": {
                        "state": "waiting_source",
                        "hls_ready": False,
                        "cleanup_complete": False,
                    },
                },
            )
            return {"status": "started"}
        if path.startswith("/task/stop/"):
            remote[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "live": {"state": "completed", "cleanup_complete": True, "hls_ready": False},
            }
            return {"status": "completed"}
        return remote.get(task_id, {"status": "not_found"})

    monkeypatch.setattr(live, "runner_request", call)
    return remote


@pytest.mark.asyncio
async def test_live_allocation_requires_explicit_declaration(setup_live):
    """A VOD-only registration provides no Live capacity, even with free workers."""
    worker = state.runners["runner-1"]
    worker.task_types = ["encoding", "studio"]
    state.runners[worker.id] = worker
    assert state.runners.reserve_live(worker.id, str(uuid4())) is None
    with pytest.raises(HTTPException) as unavailable:
        await live.start(request(), "pod-token")
    assert unavailable.value.status_code == 503
    assert state.runners[worker.id].availability == "available"
    assert state.runners[worker.id].live_task_id is None


@pytest.mark.asyncio
async def test_api_dynamic_hls_and_idempotent_stop(setup_live):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_token] = lambda: "pod-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as client:
        result = await client.post("/live/start", json=request().model_dump())
        assert result.status_code == 202
        payload = result.json()
        task_id = payload["live_id"]
        assert payload["state"] == "waiting_source" and payload["hls_url"] is None
        assert state.runners["runner-1"].availability == "busy"
        assert "secret-key" not in result.text
        setup_live[task_id]["live"].update(
            state="running",
            hls_ready=True,
            hls_url=f"https://public.example/hls/{task_id}/master.m3u8",
            hls_sessions=3,
        )
        fetched = (await client.get(f"/live/{task_id}")).json()
        assert fetched["state"] == "running"
        assert fetched["hls_sessions"] == 3
        assert fetched["hls_url"].startswith("https://public.example/")
        state.tasks.clear()  # A new worker can recover the URL from persisted task identity.
        assert (await client.get(f"/live/{task_id}")).json()["hls_url"] == fetched["hls_url"]
        await client.post(f"/live/{task_id}/stop")
        ended = (await client.post(f"/live/{task_id}/stop")).json()
        assert ended["state"] == "completed" and ended["hls_url"] is None
        assert state.runners["runner-1"].availability == "available"
        assert (await client.post("/live/start", json=request().model_dump())).json()[
            "live_id"
        ] == task_id


@pytest.mark.asyncio
async def test_four_runners_four_lives_and_no_fifth(setup_live):
    for i in range(2, 5):
        state.runners.register(runner(f"runner-{i}", 8081 + i))
    results = await asyncio.gather(
        *(live.start(request(f"room{i}", f"event{i}"), "pod") for i in range(4))
    )
    assert len({result.runner for result in results}) == 4
    with pytest.raises(HTTPException) as error:
        await live.start(request("room4", "event4"), "pod")
    assert error.value.status_code == 503
    assert all(worker.availability == "busy" for worker in state.runners.values())
    await live.stop(results[0].live_id, "pod")
    next_live = await live.start(request("room4", "event4"), "pod")
    assert next_live.runner == results[0].runner


@pytest.mark.asyncio
async def test_live_spreads_first_five_streams_across_hosts(setup_live):
    """A 2/4/4/4/4 deployment uses all five hosts before allocating a second slot."""
    task_types = state.runners["runner-1"].task_types
    state.runners.clear()
    for host, capacity in enumerate((2, 4, 4, 4, 4)):
        for instance in range(capacity):
            worker = runner(f"worker-{host}-{instance}", 8001 + instance)
            worker.url = f"http://192.0.2.{host + 1}:{8001 + instance}"
            worker.task_types = task_types
            state.runners.register(worker)
    started = await asyncio.gather(
        *(live.start(request(f"room{i}", f"event{i}"), "pod") for i in range(5))
    )
    assert len({state.runners[item.runner].url.rsplit(":", 1)[0] for item in started}) == 5
    assert sum(bool(worker.live_task_id) for worker in state.runners.values()) == 5


@pytest.mark.asyncio
async def test_duplicate_source_and_event_conflicts_and_owner(setup_live):
    first = await live.start(request(), "pod")
    assert (await live.start(request(), "pod")).live_id == first.live_id
    for req in (request(event="different-event"), request("room1")):
        with pytest.raises(HTTPException) as error:
            await live.start(req, "pod")
        assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await live.stop(first.live_id, "another-token")
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_unreachable_runner_withholds_hls_and_keeps_reservation(setup_live, monkeypatch):
    first = await live.start(request(), "pod")
    monkeypatch.setattr(
        live, "runner_request", AsyncMock(side_effect=httpx.ConnectError("secret URL"))
    )
    current = await live.synchronize(first.live_id)
    assert current.live.hls_url is None and current.status == "running"
    assert state.runners[first.runner].live_task_id == first.live_id
    assert "secret" not in current.live.error
    await live.stop(first.live_id, "pod")
    assert live.load(first.live_id).live.state == "stopping"


@pytest.mark.asyncio
async def test_cleanup_must_be_confirmed_and_old_callback_cannot_release_new_live(setup_live):
    first = await live.start(request(), "pod")
    setup_live[first.live_id]["live"].update(state="completed", cleanup_complete=False)
    await live.synchronize(first.live_id)
    assert state.runners[first.runner].live_task_id == first.live_id
    setup_live[first.live_id]["live"]["cleanup_complete"] = True
    await live.synchronize(first.live_id)
    second = await live.start(request(event="second"), "pod")
    await live.synchronize(first.live_id)
    assert state.runners[first.runner].live_task_id == second.live_id


@pytest.mark.asyncio
async def test_dispatch_timeout_is_retried_with_same_live_id(setup_live, monkeypatch):
    original = live.runner_request
    attempts = []

    async def delayed(worker, method, path, payload=None):
        if path == "/task/run":
            attempts.append(payload["task_id"])
            await original(worker, method, path, payload)
            raise httpx.ReadTimeout("accepted but response lost")
        return await original(worker, method, path, payload)

    monkeypatch.setattr(live, "runner_request", delayed)
    first = await live.start(request(), "pod")
    await live.synchronize(first.live_id)
    assert attempts == [first.live_id]
    assert state.runners[first.runner].live_task_id == first.live_id


@pytest.mark.asyncio
async def test_dispatch_forwards_the_pod_broadcaster_slug(setup_live, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_URL", "http://manager.internal:8081/")
    monkeypatch.setattr(config, "MANAGER_PUBLIC_URL", "https://manager.example/manager/")
    task = live._allocate(request(), "pod")
    worker = state.runners[task.runner_id]
    dispatch = AsyncMock(return_value={"status": "started"})
    monkeypatch.setattr(live, "runner_request", dispatch)

    await live._dispatch(task, worker)

    call = dispatch.await_args
    assert call is not None
    payload = call.args[3]
    assert payload["parameters"]["broadcaster_id"] == "room"
    assert payload["parameters"]["source_id"] == "room"
    assert payload["parameters"]["source_path"] == "secret-key/room"
    assert payload["completion_callback"] == "http://manager.internal:8081/task/completion"
    assert (
        payload["completion_callback_public"] == "https://manager.example/manager/task/completion"
    )


def test_reservation_atomic_with_stale_heartbeat_and_registration(tmp_path):
    file = str(tmp_path / "workers.json")
    stores = [RunnerStore(shared_enabled=True, state_file=file) for _ in range(4)]
    stores[0].register(runner())
    stale = stores[1]["runner-1"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda pair: pair[1].reserve_live("runner-1", str(pair[0])), enumerate(stores))
        )
    assert sum(result is not None for result in results) == 1
    reserved = stores[0]["runner-1"].live_task_id
    stale.availability = "available"
    stores[1]["runner-1"] = stale
    stores[2].register(runner())
    assert stores[0]["runner-1"].availability == "busy"
    assert stores[0]["runner-1"].live_task_id == reserved
    assert not stores[0].release_live("runner-1", "stale-live")
    assert stores[0].release_live("runner-1", reserved)


def test_managers_are_isolated(tmp_path):
    first = RunnerStore(shared_enabled=True, state_file=str(tmp_path / "manager-a.json"))
    second = RunnerStore(shared_enabled=True, state_file=str(tmp_path / "manager-b.json"))
    first.register(runner())
    second.register(runner(port=8092))
    first.reserve_live("runner-1", "a-live")
    assert second["runner-1"].availability == "available"
    assert first["runner-1"].url != second["runner-1"].url


@pytest.mark.parametrize("shared", [False, True])
def test_mixed_runner_live_and_vod_reservations_are_mutually_exclusive(tmp_path, shared):
    """Both dispatch paths contend for the same instance reservation."""
    store = RunnerStore(shared_enabled=shared, state_file=str(tmp_path / "runners.json"))
    worker = runner()
    worker.task_types = ["encoding", "studio", "live"]
    store.register(worker)
    with ThreadPoolExecutor(max_workers=2) as pool:
        vod = pool.submit(store.try_reserve, worker.id)
        streaming = pool.submit(store.reserve_live, worker.id, "live-session")
        assert sum(result.result() is not None for result in (vod, streaming)) == 1
    assert store[worker.id].availability == "busy"
    assert store.try_reserve(worker.id) is None
    assert store.reserve_live(worker.id, "another-live") is None
    if store[worker.id].live_task_id:
        stale = worker.model_copy(update={"availability": "available"})
        store[worker.id] = stale
        assert store[worker.id].availability == "busy"
        assert not store.release_live(worker.id, "old-live")
        assert store.release_live(worker.id, "live-session")
        assert store.try_reserve(worker.id) is not None


@pytest.mark.asyncio
async def test_disabled_unknown_source_invalid_id_and_persistence_failure(setup_live, monkeypatch):
    config.LIVE.enabled = False
    with pytest.raises(HTTPException, match="disabled"):
        await live.start(request(), "pod")
    config.LIVE.enabled = True
    with pytest.raises(HTTPException, match="Unknown"):
        await live.start(request("missing"), "pod")
    with pytest.raises(HTTPException):
        live.load("../../secret")
    monkeypatch.setattr(state.persistence, "upsert_tasks", lambda data: False)
    with pytest.raises(HTTPException, match="persisted"):
        await live.start(request(), "pod")
    assert state.runners["runner-1"].availability == "available"


@pytest.mark.asyncio
async def test_stop_before_dispatch_fences_late_request(setup_live, monkeypatch):
    async with live.live_lock("allocation"):
        task = live._allocate(request(), "pod")
    result = await live.stop(task.task_id, "pod")
    assert result.state == "completed"
    assert state.runners[task.runner_id].availability == "available"


@pytest.mark.asyncio
async def test_metrics_and_media_source_status(setup_live, monkeypatch):
    first = await live.start(request(), "pod")
    text = live.metrics()
    assert "live_runners_busy 1" in text and "live_sessions_total 1" in text
    monkeypatch.setattr(
        live.MediaMTXClient,
        "get_path",
        AsyncMock(
            return_value={"available": True, "inboundBytes": 1024, "tracks2": [{"codec": "H264"}]}
        ),
    )
    assert (await live.source_status("room"))["input_bytes"] == 1024
    assert first.source_id == "room"


@pytest.mark.asyncio
async def test_live_load_rejects_a_persisted_non_live_task(setup_live):
    task = await live.start(request(), "pod")
    persisted = live.load(task.live_id)
    persisted.live = None
    state.persistence.upsert_tasks({persisted.task_id: persisted})

    with pytest.raises(HTTPException, match="Live not found"):
        live.load(task.live_id)


@pytest.mark.asyncio
async def test_track_metadata_survives_empty_reports_and_terminal_cleanup(setup_live):
    """Retain observed tracks per session while allowing new tracks and current signal metrics."""
    first = await live.start(request(), "pod")
    remote = setup_live[first.live_id]
    remote["live"].update(
        state="running",
        source_ready=True,
        input_bytes=1024,
        input_bitrate=2_000_000,
        hls_sessions=2,
        codecs=["H264", "MPEG-4 Audio"],
        resolutions=["1920x1080"],
    )
    await live.synchronize(first.live_id)

    remote["live"].update(
        state="waiting_source",
        source_ready=False,
        input_bytes=0,
        input_bitrate=0,
        hls_sessions=0,
        codecs=[],
        resolutions=[],
    )
    await live.synchronize(first.live_id)
    observed = live.load(first.live_id).live
    assert observed.codecs == ["H264", "MPEG-4 Audio"]
    assert observed.resolutions == ["1920x1080"]
    assert not observed.source_ready
    assert observed.input_bytes == observed.input_bitrate == observed.hls_sessions == 0

    remote["live"].update(
        state="running", source_ready=True, codecs=["H264"], resolutions=["1280x720"]
    )
    await live.synchronize(first.live_id)
    refreshed = live.load(first.live_id).live
    assert refreshed.codecs == ["H264"] and refreshed.resolutions == ["1280x720"]

    remote["status"] = "completed"
    remote["live"].update(
        state="completed", cleanup_complete=True, source_ready=False, codecs=[], resolutions=[]
    )
    await live.synchronize(first.live_id)
    completed = live.load(first.live_id).live
    assert completed.state == "completed" and not completed.source_ready
    assert completed.codecs == ["H264"] and completed.resolutions == ["1280x720"]
    assert state.runners[first.runner].availability == "available"

    second = await live.start(request(event="another-event"), "pod")
    setup_live[second.live_id]["live"].update(codecs=[], resolutions=[])
    await live.synchronize(second.live_id)
    unobserved = live.load(second.live_id).live
    assert unobserved.codecs == unobserved.resolutions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["codecs", "resolutions"])
async def test_track_metadata_retention_rejects_invalid_null_reports(setup_live, field):
    """Unknown empty lists are valid; null track metadata must still fail model validation."""
    first = await live.start(request(), "pod")
    task = live.load(first.live_id)
    with pytest.raises(ValueError):
        live._apply_status(
            task,
            {"task_id": first.live_id, "live": {"state": "waiting_source", field: None}},
        )


@pytest.mark.asyncio
async def test_live_sync_rejects_a_reservation_owned_by_another_session(setup_live, monkeypatch):
    task = await live.start(request(), "pod")
    runner_record = state.runners[task.runner]
    runner_record.live_task_id = "another-live"
    monkeypatch.setattr(
        live,
        "runner_request",
        AsyncMock(
            return_value={
                "task_id": task.live_id,
                "status": "running",
                "live": {"state": "running"},
            }
        ),
    )
    monkeypatch.setattr(state.runners, "reserve_live", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="reservation"):
        await live._synchronize_runner(live.load(task.live_id), runner_record)


@pytest.mark.asyncio
async def test_live_status_validation_rejects_missing_and_unknown_status(setup_live):
    task = await live.start(request(), "pod")
    current = live.load(task.live_id)

    with pytest.raises(RuntimeError, match="did not return Live"):
        live._apply_status(current, {"task_id": current.task_id, "live": None})
    with pytest.raises(RuntimeError, match="Invalid Live status"):
        live._apply_status(
            current,
            {"task_id": current.task_id, "live": {"state": "unexpected"}},
        )


@pytest.mark.asyncio
async def test_source_status_hides_mediamtx_control_errors(setup_live, monkeypatch):
    client = AsyncMock()
    client.get_path.side_effect = MediaMTXError("control unavailable")
    monkeypatch.setattr(live, "MediaMTXClient", lambda *args: client)

    with pytest.raises(HTTPException) as error:
        await live.source_status("room")

    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/output",
        "https://user:password@public/hls",
        "https://public/hls?secret=key",
        None,
    ],
)
def test_public_hls_url_never_contains_credentials(url):
    with pytest.raises(RuntimeError):
        live._public_url(url)


def test_invalid_source_configuration():
    settings = LiveSettings(enabled=True, sources={"room": "../../etc/passwd"})
    with pytest.raises(ValueError):
        settings.validate_enabled()


@pytest.mark.asyncio
async def test_real_runner_http_contract_without_network(setup_live, monkeypatch):
    original_client = httpx.AsyncClient
    requests = []
    result = httpx.Response(200, json={"status": "running"})

    def handle(req):
        requests.append(req)
        return result

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert (await RUNNER_REQUEST(runner(), "GET", "/task/status/uuid"))["status"] == "running"
    assert requests[0].headers["authorization"] == "Bearer runner-secret"
    result = httpx.Response(302)
    with pytest.raises(RuntimeError, match="HTTP 302"):
        await RUNNER_REQUEST(runner(), "GET", "/task/status/uuid")
    result = httpx.Response(200, json=[])
    with pytest.raises(RuntimeError, match="Invalid"):
        await RUNNER_REQUEST(runner(), "GET", "/task/status/uuid")


@pytest.mark.asyncio
async def test_stop_waits_for_cleanup_and_recovery_adopts_confirmed_owner(setup_live):
    first = await live.start(request(), "pod")
    task_record = live.load(first.live_id)
    state.runners.release_live(first.runner, first.live_id)
    worker = state.runners[first.runner]
    worker.availability = "busy"
    state.runners[first.runner] = worker
    await live.synchronize(first.live_id)
    assert state.runners[first.runner].live_task_id == first.live_id
    assert task_record.live.source_id == "room"


@pytest.mark.asyncio
async def test_lock_contention_yields_and_times_out(setup_live, monkeypatch):
    from filelock import FileLock

    config.LIVE.runner_timeout = 0.01
    lock = FileLock(state.persistence.data_directory / ".live-held.lock")
    with lock:
        with pytest.raises(HTTPException, match="in progress"):
            async with live.live_lock("held"):
                pytest.fail("Concurrent lock acquired")


@pytest.mark.asyncio
async def test_missing_runner_source_and_invalid_remote_status(setup_live, monkeypatch):
    first = await live.start(request(), "pod")
    worker = state.runners[first.runner]
    del state.runners[first.runner]
    assert (await live.synchronize(first.live_id)).live.hls_url is None
    state.runners.register(worker)
    for payload in ({"status": "running"}, {"live": {"state": "invalid"}}):
        with pytest.raises(RuntimeError):
            live._apply_status(live.load(first.live_id), payload)
    config.LIVE.sources.clear()
    with pytest.raises(RuntimeError, match="configuration"):
        await live._dispatch(live.load(first.live_id), worker)
    with pytest.raises(HTTPException, match="Unknown"):
        await live.source_status("room")
    with pytest.raises(HTTPException, match="not found"):
        live.load(str(uuid4()))


@pytest.mark.asyncio
async def test_reconciliation_keeps_reservations_on_errors(setup_live, monkeypatch):
    await live.start(request(), "pod")
    synchronize = AsyncMock(side_effect=RuntimeError("disk unavailable"))
    monkeypatch.setattr(live, "synchronize", synchronize)
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await live.reconcile_forever()
    synchronize.assert_awaited_once()
    assert state.runners["runner-1"].availability == "busy"


@pytest.mark.asyncio
async def test_sources_and_metrics_routes_are_authenticated(setup_live, monkeypatch):
    app = FastAPI()
    app.include_router(router)
    monkeypatch.setattr(live.MediaMTXClient, "get_path", AsyncMock(return_value=None))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://manager"
    ) as client:
        assert (await client.get("/live/metrics")).status_code == 401
        app.dependency_overrides[verify_token] = lambda: "pod"
        assert (await client.get("/live/metrics")).status_code == 200
        assert not (await client.get("/live/sources/room")).json()["source_ready"]


@pytest.mark.asyncio
async def test_reservation_race_retries_another_runner_and_deduplicates_winner(
    setup_live, monkeypatch
):
    monkeypatch.setattr("app.services.runner_selection.random.shuffle", lambda groups: None)
    state.runners.register(runner("runner-2", 8083))
    reserve = state.runners.reserve_live
    monkeypatch.setattr(
        state.runners,
        "reserve_live",
        lambda key, task_id, **kwargs: (
            None if key == "runner-1" else reserve(key, task_id, **kwargs)
        ),
    )
    result = await live.start(request(), "pod")
    assert result.runner == "runner-2"
    assert (await live.start(request(), "pod")).live_id == result.live_id


@pytest.mark.asyncio
async def test_crash_between_intent_and_reservation_is_recovered(setup_live, monkeypatch):
    reserve = state.runners.reserve_live
    monkeypatch.setattr(
        state.runners,
        "reserve_live",
        lambda *args: (_ for _ in ()).throw(RuntimeError("process died")),
    )
    with pytest.raises(RuntimeError):
        live._allocate(request(), "pod")
    persisted = live.sessions()[0]
    assert state.runners["runner-1"].live_task_id is None
    monkeypatch.setattr(state.runners, "reserve_live", reserve)
    await live.synchronize(persisted.task_id)
    assert state.runners["runner-1"].live_task_id == persisted.task_id


@pytest.mark.asyncio
async def test_inactive_live_runner_is_retained_and_heartbeat_restores_online(
    setup_live, monkeypatch
):
    from datetime import timedelta

    from app.api.routes import runner as routes
    from app.services import runner_service

    first = await live.start(request(), "pod")
    worker = state.runners[first.runner]
    worker.last_heartbeat = datetime.now() - timedelta(minutes=2)
    state.runners[first.runner] = worker
    monkeypatch.setattr(runner_service, "runners", state.runners)
    monkeypatch.setattr(routes, "runners", state.runners)
    stopped = asyncio.Event()

    async def tick(delay):
        stopped.set()

    monkeypatch.setattr(runner_service.asyncio, "sleep", tick)
    await runner_service.check_runners_activity(stop_event=stopped)
    assert state.runners[first.runner].status == "offline"
    assert state.runners[first.runner].live_task_id == first.live_id
    state.runners.release_live(first.runner, first.live_id)
    await routes.runner_heartbeat(
        first.runner,
        routes.RunnerHeartbeatPayload(availability="available"),
        "runner-secret",
        "1.9.1",
    )
    assert state.runners[first.runner].status == "online"


@pytest.mark.asyncio
async def test_old_terminal_status_cannot_disturb_new_reservation(setup_live):
    first = await live.start(request(), "pod")
    state.runners.release_live(first.runner, first.live_id)
    second = await live.start(request("room1", "second"), "pod")
    setup_live[first.live_id].update(
        status="completed", live={"state": "completed", "cleanup_complete": True}
    )
    assert (await live.synchronize(first.live_id)).status == "completed"
    assert state.runners[first.runner].live_task_id == second.live_id
