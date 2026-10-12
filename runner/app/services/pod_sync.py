"""Durable Pod status retries independent of a Live session's media reservation."""

import asyncio
import uuid
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from app.core import state
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.models.models import TaskRequest
from app.services.pod_broadcaster import PodBroadcasterClient, PodBroadcasterError

logger = setup_default_logging()
RETRY_INTERVAL_SECONDS = 10
_sync_lock = asyncio.Lock()
_ACTIVE_STATES = {"pending", "waiting_source", "starting", "running", "stopping"}


def enabled_for(request: TaskRequest) -> bool:
    """Require configured Pod credentials and the request's establishment scope."""
    return bool(
        config.LIVE.pod_url
        and isinstance(request.parameters.get("broadcaster_id"), str)
        and (
            not config.LIVE.pod_establishment_name
            or request.etab_name == config.LIVE.pod_establishment_name
        )
    )


def pending(task_id: str) -> bool | None:
    """Return the persisted desired Pod status, with None for no pending update."""
    value = (state.get_task_status(task_id) or {}).get("live", {}).get("pod_status_pending")
    return value if isinstance(value, bool) else None


async def set_status(request: TaskRequest, desired: bool, client: PodBroadcasterClient) -> None:
    """Checkpoint an intent before its first attempt; retain it on API failure.

    Serialize updates with background retries. A successful response clears only
    the same intent, so a delayed activation cannot acknowledge a newer stop.
    Pod errors propagate to the media runtime's existing sanitized diagnostics.
    """
    from app.services.live_runtime import update

    update(request.task_id, pod_status_pending=desired)
    async with _sync_lock:
        if pending(request.task_id) != desired:
            return
        await client.set_status(request.parameters["broadcaster_id"], desired)
        if pending(request.task_id) == desired:
            update(request.task_id, pod_status_pending=None)


async def _manager_status(request: TaskRequest) -> tuple[bool, str | None]:
    """Resolve the current session's desired status before replaying an old intent.

    Use only configured Manager URLs and credentials. Uncertain observations
    defer the retry; a replacement's source/stop state supersedes an old request.
    Confirm its establishment before updating this Pod for another Runner.
    """
    source_id = quote(request.parameters["source_id"], safe="")
    headers = {"Authorization": f"Bearer {config.RUNNER_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.get(
                f"{config.MANAGER_URL.rstrip('/')}/live/sources/{source_id}", headers=headers
            )
            response.raise_for_status()
            payload = response.json()
            live = payload.get("live")
            if live is None:
                return False, None
            if not isinstance(live, dict) or live.get("state") not in {
                *_ACTIVE_STATES,
                "failed",
                "completed",
            }:
                raise ValueError
            identifier = live.get("live_id")
            if not isinstance(identifier, str):
                raise ValueError
            uuid.UUID(identifier)
            desired = bool(
                live["state"] in _ACTIVE_STATES
                and live["state"] != "stopping"
                and not live.get("stop_requested")
                and payload.get("source_ready")
            )
            if identifier == request.task_id:
                local = state.get_task_status(request.task_id) or {}
                desired = bool(
                    desired
                    and pending(request.task_id) is True
                    and local.get("status") == "running"
                    and not local.get("stop_requested")
                )
            if desired and identifier != request.task_id and config.LIVE.pod_establishment_name:
                task = await client.get(
                    f"{config.MANAGER_URL.rstrip('/')}/task/status/{quote(identifier, safe='')}",
                    headers=headers,
                )
                task.raise_for_status()
                desired = task.json().get("etab_name") == config.LIVE.pod_establishment_name
            return desired, identifier
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        raise PodBroadcasterError("Manager Live status unavailable") from None


async def retry(request: TaskRequest) -> None:
    """Retry a pending update against the latest Manager state, with bounded work.

    Recheck after PATCH to compensate for a session replacement during delivery.
    At most two PATCHes run per cycle; unsettled or failed updates stay durable
    for a later cycle, without retaining FFmpeg, HLS files or a Runner reservation.
    """
    from app.services.live_runtime import update

    async with _sync_lock:
        original = pending(request.task_id)
        if original is None or not enabled_for(request):
            return
        client = PodBroadcasterClient(
            config.LIVE.pod_url, config.LIVE.pod_username, config.LIVE.pod_password
        )
        expected = await _manager_status(request)
        for _ in range(2):
            await client.set_status(request.parameters["broadcaster_id"], expected[0])
            current = await _manager_status(request)
            if current == expected:
                if pending(request.task_id) == original:
                    update(request.task_id, pod_status_pending=None)
                    logger.info(
                        "Live Pod status synchronized live_id=%s active=%s",
                        request.task_id,
                        expected[0],
                    )
                return
            expected = current


async def retry_loop() -> None:
    """Replay owned persisted intents after outages/restarts, including terminal sessions."""
    while True:
        for payload in list(state.get_runner_state().get("task_statuses", {}).values()):
            task_id = payload.get("task_id", "")
            if payload.get("runner_id") != state.get_runner_id() or pending(task_id) is None:
                continue
            try:
                request = TaskRequest.model_validate(payload.get("task_request"))
                if request.task_id != task_id:
                    continue
                await retry(request)
            except (PodBroadcasterError, HTTPException, OSError, ValueError, TypeError, KeyError):
                # The initial failure was logged by the runtime; avoid repeating it every scan.
                continue
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)
