"""Live orchestration using existing tasks, persistence, runners and authentication."""

import asyncio
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from filelock import FileLock, Timeout

from app.core import state
from app.core.config import config
from app.core.live_config import MEDIAMTX_VERSION
from app.core.setup_logging import setup_default_logging
from app.models.live import LiveInfo, LiveResponse, LiveStartRequest
from app.models.models import Runner, Task
from app.services.live_sources import known_sources, pod_broadcaster_slug
from app.services.mediamtx import MediaMTXClient, MediaMTXError, path_metrics
from app.services.runner_selection import rank_runners

logger = setup_default_logging()
TERMINAL = {"completed", "failed"}


@asynccontextmanager
async def live_lock(name: str) -> AsyncIterator[None]:
    """Serialize Live transitions across workers without blocking the event loop."""
    lock = FileLock(state.persistence.data_directory / f".live-{name}.lock")
    deadline = time.monotonic() + config.LIVE.runner_timeout * 3
    while True:
        try:
            lock.acquire(timeout=0)
            break
        except Timeout:
            if time.monotonic() >= deadline:
                raise HTTPException(503, "Live operation in progress; retry") from None
            await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def persist(task: Task) -> None:
    """Persist a timestamped task before refreshing the local task cache.

    Raise HTTPException with status 503 if storage does not acknowledge the write.
    """
    task.updated_at = datetime.now().isoformat()
    if not state.persistence.upsert_tasks({task.task_id: task}):
        raise HTTPException(503, "Live state could not be persisted")
    state.tasks[task.task_id] = task


def load(live_id: str, token: str | None = None) -> Task:
    """Load a persisted Live task and optionally verify its client token.

    Invalid or unknown identifiers return HTTP 404. API-created sessions require
    their owning client token. Manager-owned automatic sessions are shared by
    authenticated API clients; callers must verify tokens before calling here.
    Omitting the token is for internal or independently authenticated admin calls.
    """
    try:
        uuid.UUID(live_id)
    except ValueError:
        raise HTTPException(404, "Live not found") from None
    payload = state.persistence.load_task(live_id)
    if payload is None:
        raise HTTPException(404, "Live not found")
    task = Task.model_validate(payload)
    if task.live is None:
        raise HTTPException(404, "Live not found")
    if (
        token is not None
        and not task.live.automatic
        and not hmac.compare_digest(task.client_token or "", token)
    ):
        raise HTTPException(403, "Token not authorized for this Live")
    return task


def response(task: Task) -> LiveResponse:
    """Build the public Live response from a task with Live metadata."""
    assert task.live is not None
    return LiveResponse(
        **task.live.model_dump(),
        live_id=task.task_id,
        runner=task.runner_id,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def sessions() -> list[Task]:
    """Load all persisted Live tasks, including retained terminal sessions."""
    return [
        task
        for payload in state.persistence.load_tasks().values()
        if (task := Task.model_validate(payload)).live is not None
    ]


async def start(request: LiveStartRequest, token: str) -> LiveResponse:
    """Allocate or retrieve a client's event session and synchronize its Runner.

    Allocation is serialized across Manager workers. Source conflicts return
    HTTP 409; disabled Live or exhausted Runner capacity returns HTTP 503.
    """
    if not config.LIVE.enabled:
        raise HTTPException(503, "Live is disabled")
    if request.source_id not in known_sources():
        raise HTTPException(404, "Unknown Live source")
    async with live_lock("allocation"):
        task = _allocate(request, token)
    await synchronize(task.task_id)
    return response(load(task.task_id, token))


def _allocate(
    request: LiveStartRequest, token: str | None, *, publication_id: str | None = None
) -> Task:
    """Persist intent before reservation; None identifies Manager-owned RTMP sessions."""
    for existing in sessions():
        assert existing.live is not None
        if existing.live.error == "Runner reservation lost":
            continue
        same_event = existing.client_token == token and existing.live.event_id == request.event_id
        if same_event:
            if existing.live.source_id != request.source_id:
                raise HTTPException(409, "Event already belongs to another source")
            return existing
        if existing.live.source_id == request.source_id and existing.live.state not in TERMINAL:
            raise HTTPException(409, "Source already has an active Live")
    live_id = str(uuid.uuid4())
    for runner in rank_runners(state.runners.values()):
        if (
            "live" not in runner.task_types
            or runner.availability != "available"
            or runner.status != "online"
        ):
            continue
        now = datetime.now().isoformat()
        task = Task(
            task_id=live_id,
            runner_id=runner.id,
            status="running",
            task_type="live",
            source_url="",
            parameters={},
            notify_url="",
            client_token=token,
            run_id=live_id,
            created_at=now,
            updated_at=now,
            etab_name=request.etab_name,
            app_name=request.app_name,
            app_version=MEDIAMTX_VERSION if request.app_name == "MediaMTX" else None,
            affiliation=None,
            completion_callback=None,
            error=None,
            script_output=None,
            live=LiveInfo(
                **request.model_dump(include={"source_id", "event_id", "equipment"}),
                automatic=token is None,
                publication_id=publication_id,
            ),
        )
        persist(task)
        reserved = state.runners.reserve_live(runner.id, live_id)
        if reserved is not None:
            logger.info(
                "[LIVE] [ALLOCATED] Runner reserved live_id=%s stream_id=%s runner_id=%s",
                live_id,
                request.source_id,
                runner.id,
            )
            return task
        # The candidate was concurrently reserved. Keep a terminal audit record.
        assert task.live is not None
        task.live = task.live.model_copy(
            update={"state": "failed", "error": "Runner reservation lost"}
        )
        task.status = "failed"
        persist(task)
        live_id = str(uuid.uuid4())
    raise HTTPException(503, "No live runners available")


async def runner_request(
    runner: Runner, method: str, path: str, payload: dict | None = None
) -> dict:
    """Send an authenticated control request and return a JSON object.

    Redirects and environment proxies are disabled. HTTP failures and non-object
    responses raise RuntimeError without including remote response bodies;
    transport and JSON decoding errors propagate to the caller.
    """
    async with httpx.AsyncClient(
        timeout=config.LIVE.runner_timeout, follow_redirects=False, trust_env=False
    ) as client:
        result = await client.request(
            method,
            f"{runner.url}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {runner.token}", "Accept": "application/json"},
        )
    if not result.is_success:
        raise RuntimeError(f"Runner control returned HTTP {result.status_code}")
    data = result.json()
    if not isinstance(data, dict):
        raise RuntimeError("Invalid runner response")
    return data


async def _dispatch(task: Task, runner: Runner) -> None:
    """Submit the private configured/discovered path through the Runner's task API."""
    assert task.live is not None
    source_path = known_sources().get(task.live.source_id)
    if source_path is None:
        raise RuntimeError("Live source configuration missing")
    broadcaster_id = pod_broadcaster_slug(source_path)
    await runner_request(
        runner,
        "POST",
        "/task/run",
        {
            "task_id": task.task_id,
            "task_type": "live",
            "source_url": "",
            "etab_name": task.etab_name,
            "app_name": task.app_name,
            "notify_url": "",
            "parameters": {
                "source_id": task.live.source_id,
                "source_path": source_path,
                "broadcaster_id": broadcaster_id,
            },
            "completion_callback": f"{config.MANAGER_URL.rstrip('/')}/task/completion",
            "completion_callback_public": f"{config.MANAGER_PUBLIC_URL.rstrip('/')}/task/completion",
        },
    )


async def synchronize(live_id: str) -> Task:
    """Reconcile and persist a session while holding its shared Live lock.

    Unavailable control hides HLS readiness and retains the reservation.
    Release only the matching reservation after a terminal state is persisted.
    """
    async with live_lock(live_id):
        task = load(live_id)
        assert task.live is not None
        if task.live.state in TERMINAL:
            _record_statistics(task)
            state.runners.release_live(task.runner_id, task.task_id)
            return task
        runner = state.runners.get(task.runner_id)
        if runner is None:
            return _unavailable(task, "Runner unavailable; reservation retained")
        previous = (task.live.state, task.live.error)
        try:
            await _synchronize_runner(task, runner)
        except (httpx.HTTPError, RuntimeError, ValueError):
            return _unavailable(task, "Runner control unavailable; reservation retained")
        persist(task)
        if previous != (task.live.state, task.live.error):
            report = (
                logger.warning if task.live.error or task.live.state == "failed" else logger.info
            )
            report(
                "[LIVE] [SESSION_STATE] live_id=%s source_id=%s runner_id=%s state=%s error_present=%s; see Live session details",
                task.task_id,
                task.live.source_id,
                task.runner_id,
                task.live.state,
                bool(task.live.error),
            )
        if task.live.state in TERMINAL:
            _record_statistics(task)
            state.runners.release_live(task.runner_id, task.task_id)
            logger.info(
                "[LIVE] [RELEASED] Runner released live_id=%s stream_id=%s runner_id=%s",
                task.task_id,
                task.live.source_id,
                task.runner_id,
            )
        return task


def _record_statistics(task: Task) -> None:
    """Record a terminal Live once without blocking cleanup on a statistics failure."""
    from app.api.routes.task import _append_task_stats_csv

    try:
        _append_task_stats_csv(task)
    except Exception:
        logger.warning("Live statistics could not be recorded live_id=%s", task.task_id)


def _unavailable(task: Task, error: str) -> Task:
    """Persist unavailable HLS status and a safe diagnostic without releasing the Runner."""
    assert task.live is not None
    if task.live.error != error:
        logger.warning(
            "[LIVE] [RUNNER_CONTROL_UNAVAILABLE] live_id=%s runner_id=%s; reservation retained",
            task.task_id,
            task.runner_id,
        )
    task.live.hls_ready = False
    task.live.hls_url = None
    task.live.state = "stopping" if task.live.stop_requested else "starting"
    task.live.error = error
    persist(task)
    return task


async def _synchronize_runner(task: Task, runner: Runner) -> None:
    """Reconcile ownership, dispatch or stop intent, and the Runner's reported state.

    The caller holds the session lock. This mutates the task in memory; the
    caller persists it and releases any completed reservation.
    """
    assert task.live is not None
    payload = await runner_request(runner, "GET", f"/task/status/{task.task_id}")
    if payload.get("status") in TERMINAL:
        _apply_status(task, payload)
        return
    if runner.live_task_id != task.task_id:
        confirmed = (
            payload.get("task_id") == task.task_id
            and payload.get("status") == "running"
            and isinstance(payload.get("live"), dict)
        )
        if state.runners.reserve_live(runner.id, task.task_id, confirmed_running=confirmed) is None:
            raise RuntimeError("Live reservation not owned by this session")
    if payload.get("status") == "not_found":
        if task.live.stop_requested:
            # A persisted stop tombstone on the runner fences any delayed dispatch.
            await runner_request(runner, "POST", f"/task/stop/{task.task_id}")
        else:
            await _dispatch(task, runner)
        payload = await runner_request(runner, "GET", f"/task/status/{task.task_id}")
    elif task.live.stop_requested and payload.get("status") == "running":
        await runner_request(runner, "POST", f"/task/stop/{task.task_id}")
        payload = await runner_request(runner, "GET", f"/task/status/{task.task_id}")
    _apply_status(task, payload)


def _apply_status(task: Task, payload: dict) -> None:
    """Validate and apply the Runner payload to the task without persisting it.

    Reject mismatched identifiers, invalid states and unconfirmed terminal
    cleanup. A pending stop always suppresses the public HLS URL.
    Keep the last non-empty codec and resolution lists for this session when a
    relay disappears or a report lacks tracks. Other metrics remain current.
    """
    assert task.live is not None
    if payload.get("task_id") != task.task_id:
        raise RuntimeError("Runner returned a different Live identifier")
    live = payload.get("live")
    if not isinstance(live, dict):
        raise RuntimeError("Runner did not return Live status")
    reported = str(live.get("state"))
    if reported not in {"waiting_source", "starting", "running", "stopping", *TERMINAL}:
        raise RuntimeError("Invalid Live status")
    if reported in TERMINAL and not live.get("cleanup_complete"):
        raise RuntimeError("Live cleanup not confirmed")
    task.live.state = "stopping" if task.live.stop_requested and reported not in TERMINAL else reported  # type: ignore[assignment]
    task.live.hls_ready = (
        reported == "running" and not task.live.stop_requested and bool(live.get("hls_ready"))
    )
    task.live.hls_url = _public_url(live.get("hls_url")) if task.live.hls_ready else None
    task.live.error = str(live["error"]) if live.get("error") else None
    task.live.last_seen = datetime.now().isoformat()
    for name in (
        "source_ready",
        "input_bytes",
        "forward_bytes",
        "hls_sessions",
        "input_bitrate",
        "codecs",
        "resolutions",
        "transcode_errors",
    ):
        if name in live:
            if name in {"codecs", "resolutions"} and live[name] == []:
                continue
            setattr(task.live, name, live[name])
    task.live = LiveInfo.model_validate(task.live.model_dump())
    if reported in TERMINAL:
        task.status = reported
        task.error = task.live.error


def _public_url(value: object) -> str:
    """Return a public HTTP(S) HLS URL or raise RuntimeError for an unsafe value."""
    if not isinstance(value, str):
        raise RuntimeError("Missing HLS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Invalid public HLS URL")
    return value


async def stop(live_id: str, token: str | None) -> LiveResponse:
    """Persist an authorized stop intent and return the reconciled session state.

    Repeated stops are safe, and HLS readiness is cleared before contacting
    the Runner. The reservation remains until terminal cleanup is confirmed.
    None bypasses client ownership for an independently authenticated admin.
    """
    async with live_lock(live_id):
        task = load(live_id, token)
        assert task.live is not None
        if task.live.state not in TERMINAL:
            task.live.stop_requested = True
            task.live.state = "stopping"
            task.live.hls_ready = False
            task.live.hls_url = None
            persist(task)
            logger.info(
                "[LIVE] [STOP_REQUESTED] live_id=%s source_id=%s", task.task_id, task.live.source_id
            )
    return response(await synchronize(live_id))


async def restart(live_id: str, token: str | None) -> LiveResponse:
    """Create one replacement for a terminal session while its encoder keeps publishing.

    Preserve client ownership and automatic lifecycle. A deterministic event key
    makes duplicate requests idempotent across workers/restarts. Keep the old task
    terminal, and require completed cleanup before allocating any replacement.
    Return 409 for an active session or absent RTMP source, 503 for media/capacity
    failures. None is reserved for independently authenticated admin calls.
    """
    from app.services.live_autostart import publication_event, remember_publication

    original = load(live_id, token)
    assert original.live is not None
    if not config.LIVE.enabled:
        raise HTTPException(503, "Live is disabled")
    source_id = original.live.source_id
    path = known_sources().get(source_id)
    if path is None:
        raise HTTPException(404, "Unknown Live source")
    async with live_lock(f"publication-{source_id}"):
        if original.live.state not in TERMINAL:
            raise HTTPException(409, "Wait until the previous Live session has finished cleanup")
        replacement = next(
            (
                task
                for task in sessions()
                if task.live
                and task.client_token == original.client_token
                and task.live.event_id == f"restart:{live_id}"
                and task.live.error != "Runner reservation lost"
            ),
            None,
        )
        if replacement:
            return response(await synchronize(replacement.task_id))
        settings = config.LIVE
        client = MediaMTXClient(
            settings.mediamtx_api_url,
            settings.mediamtx_api_user,
            settings.mediamtx_api_password,
            settings.mediamtx_timeout,
        )
        try:
            event = publication_event(source_id, await client.get_path(path))
        except MediaMTXError:
            logger.warning(
                "[LIVE] [RESTART_BLOCKED] MediaMTX control unavailable source_id=%s",
                source_id,
            )
            raise HTTPException(503, "MediaMTX control unavailable") from None
        if event is None:
            raise HTTPException(409, "The RTMP source is not publishing")
        request = LiveStartRequest(
            source_id=source_id,
            event_id=f"restart:{live_id}",
            etab_name=original.etab_name,
            app_name=original.app_name or "MediaMTX",
            equipment=original.live.equipment,
        )
        async with live_lock("allocation"):
            task = _allocate(
                request,
                original.client_token,
                publication_id=event if original.live.automatic else None,
            )
        assert task.live is not None
        if task.live.automatic:
            remember_publication(source_id, task.live.publication_id or task.live.event_id)
        logger.info(
            "[LIVE] [RESTART_REQUESTED] previous_live_id=%s live_id=%s source_id=%s",
            live_id,
            task.task_id,
            source_id,
        )
        await synchronize(task.task_id)
        return response(load(task.task_id, token))


async def source_status(source_id: str) -> dict:
    """Query MediaMTX for a known source and return non-secret media metrics.

    Include the latest automatic session for discovering its identifier and HLS
    URL. API-owned session details remain restricted to their owner. Return HTTP
    404 for an unknown source and HTTP 503 if media control fails.
    """
    path = known_sources().get(source_id)
    if path is None:
        raise HTTPException(404, "Unknown Live source")
    settings = config.LIVE
    client = MediaMTXClient(
        settings.mediamtx_api_url,
        settings.mediamtx_api_user,
        settings.mediamtx_api_password,
        settings.mediamtx_timeout,
    )
    try:
        metrics = path_metrics(await client.get_path(path))
        automatic = [
            task
            for task in sessions()
            if task.live and task.live.automatic and task.live.source_id == source_id
        ]
        latest = max(automatic, key=lambda task: task.created_at, default=None)
        return {
            "source_id": source_id,
            **metrics,
            "live": response(latest).model_dump() if latest else None,
        }
    except MediaMTXError:
        raise HTTPException(503, "MediaMTX control unavailable") from None


async def reconcile_forever() -> None:
    """Reconcile persisted sessions, terminal reservations and RTMP publications.

    Transient failures retain reservations and retry on the next polling cycle.
    Cancellation stops the loop.
    """
    from app.services.live_autostart import reconcile_sources

    while True:
        try:
            for task in sessions():
                assert task.live is not None
                runner = state.runners.get(task.runner_id)
                if task.live.state not in TERMINAL or (
                    runner and runner.live_task_id == task.task_id
                ):
                    await synchronize(task.task_id)
        except Exception:
            logger.warning(
                "[LIVE] [RECONCILIATION_FAILED] Reservations retained; check persistent storage and Runner control"
            )
        await reconcile_sources()
        await asyncio.sleep(config.LIVE.poll_interval)


def metrics() -> str:
    """Render retained session and Runner snapshots as Prometheus gauges.

    Values, including names ending in _total, may decrease after retention
    cleanup or a media reconnection; they are not monotonic counters.
    """
    records = [task.live for task in sessions() if task.live]
    runners = [runner for runner in state.runners.values() if "live" in runner.task_types]
    values = {
        "live_sessions_total": len(records),
        "live_sessions_running": sum(record.state == "running" for record in records),
        "live_runners_total": len(runners),
        "live_runners_busy": sum(
            bool(runner.live_task_id) or runner.availability == "busy" for runner in runners
        ),
        "live_runners_available": sum(
            runner.status == "online"
            and not runner.live_task_id
            and runner.availability == "available"
            for runner in runners
        ),
        "live_input_bytes": sum(record.input_bytes for record in records),
        "live_forward_bytes": sum(record.forward_bytes for record in records),
        "live_transcode_errors_total": sum(record.transcode_errors for record in records),
    }
    # Retention-scoped snapshots are gauges, including names ending in _total.
    return "".join(f"# TYPE {name} gauge\n{name} {value}\n" for name, value in values.items())
