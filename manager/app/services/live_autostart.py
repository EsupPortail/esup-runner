"""Turn authenticated RTMP publications into exclusive, persistent Live sessions."""

import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import HTTPException

from app.core import state
from app.core.config import config
from app.models.live import LiveStartRequest
from app.models.models import Task
from app.services import live_service as live
from app.services.live_sources import known_sources, register_paths, valid_path
from app.services.mediamtx import MediaMTXClient, MediaMTXError

_EQUIPMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")


def extract_equipment(query: str | None) -> str | None:
    """Extract one bounded equipment identifier without retaining credentials."""
    if not isinstance(query, str) or len(query) > 8192:
        return None
    try:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=64).get("equipment", [])
    except ValueError:
        return None
    if len(values) != 1:
        return None
    value = values[0]
    if _EQUIPMENT_PATTERN.fullmatch(value) is None:
        return None
    return value


def extract_etablishment(query: str | None) -> str | None:
    """Extract one decoded RTMP establishment name without retaining credentials."""
    if not isinstance(query, str) or len(query) > 8192:
        return None
    try:
        values = parse_qs(query, keep_blank_values=True, max_num_fields=64).get("etablishment", [])
    except ValueError:
        return None
    if len(values) != 1:
        return None
    value = values[0].strip()
    if not value or len(value) > 200:
        return None
    return value


def publication_connection(payload: dict | None) -> tuple[str, bool] | None:
    """Return the MediaMTX connection UUID and whether it uses RTMPS."""
    if payload is None or payload.get("available") is False:
        return None
    source = payload.get("source")
    if payload.get("available") is not True or not isinstance(source, dict):
        raise MediaMTXError("Invalid publication observation")
    source_type = source.get("type")
    if source_type not in {"rtmpConn", "rtmpsConn"}:
        return None
    try:
        identifier = uuid.UUID(str(source.get("id")))
    except ValueError:
        raise MediaMTXError("Invalid publication identifier") from None
    return str(identifier), source_type == "rtmpsConn"


def publication_event(source_id: str, payload: dict | None) -> str | None:
    """Identify an available RTMP connection; reject malformed observations safely.

    A 404 or an explicitly unavailable path means publication has ended.
    Connection UUIDs distinguish rapid republications between polling cycles.
    """
    connection = publication_connection(payload)
    if connection is None:
        return None
    identifier, _ = connection
    return f"rtmp:{source_id}:{identifier}"


def fence_path(source_id: str) -> Path:
    """Return the persistent publication fence for a configured or discovered source."""
    if source_id not in known_sources():
        raise ValueError("Unknown Live source")
    return state.persistence.data_directory / f".live-publication-{source_id}.json"


def read_fence(source_id: str) -> str | None:
    """Read the last allocated publication, failing closed on corrupt/unreadable state."""
    try:
        event = json.loads(fence_path(source_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(event, str) or not event.startswith(f"rtmp:{source_id}:"):
        raise ValueError("Invalid publication checkpoint")
    return event


def remember_publication(source_id: str, event: str) -> None:
    """Atomically checkpoint a publication under its source lock, outside task retention.

    This prevents an admin-stopped or failed publication from being restarted
    after a Manager restart or deletion of its terminal task history.
    """
    if read_fence(source_id) == event:
        return
    path = fence_path(source_id)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(event, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def shutdown_recovery(records: list[Task], event: str) -> Task | None:
    """Return a cleaned-up automatic session interrupted by Runner shutdown or recovery."""
    return max(
        (
            task
            for task in records
            if task.live
            and task.live.automatic
            and task.live.state in live.TERMINAL
            and (task.live.publication_id or task.live.event_id) == event
            # Persisted sessions from older versions may still have the previous message.
            and task.live.error
            in (
                "Runner stopped",
                "Runner shutting down",
                "Runner restarted; session stopped safely",
            )
            and not task.live.stop_requested
        ),
        key=lambda task: task.created_at,
        default=None,
    )


def replacement_exists(records: list[Task], event: str) -> bool:
    """Return whether this exact automatic recovery was already allocated."""
    return any(
        task.live and task.live.automatic and task.live.event_id == event for task in records
    )


async def stop_changed_active(records: list[Task], source_id: str, event: str | None) -> bool:
    """Stop automatic sessions whose publication has changed and report activity."""
    active = [task for task in records if task.live and task.live.state not in live.TERMINAL]
    for task in active:
        assert task.live is not None
        if task.live.automatic:
            publication = task.live.publication_id or task.live.event_id
            remember_publication(source_id, publication)
            if publication != event:
                await live.stop(task.task_id, None)
    return bool(active)


async def reconcile_source(source_id: str, client: MediaMTXClient) -> None:
    """Observe and reconcile one source while serializing competing Manager workers.

    Existing API-created sessions retain their lifecycle and ownership. An
    automatic session ends when its publication disappears or is replaced;
    a new publication waits for cleanup and a free Runner before allocation.
    """
    async with live.live_lock(f"publication-{source_id}"):
        payload = await client.get_path(known_sources()[source_id])
        event = publication_event(source_id, payload)
        records = [
            task for task in live.sessions() if task.live and task.live.source_id == source_id
        ]
        if await stop_changed_active(records, source_id, event) or event is None:
            return
        recovery = shutdown_recovery(records, event)
        if recovery is not None:
            assert recovery.live is not None
            replacement_event = f"restart:{recovery.task_id}"
            if replacement_exists(records, replacement_event):
                return
            state.runners.release_live(recovery.runner_id, recovery.task_id)
            request = LiveStartRequest(
                source_id=source_id,
                event_id=replacement_event,
                equipment=recovery.live.equipment,
                etab_name=recovery.etab_name,
                app_name=recovery.app_name or "MediaMTX",
            )
            async with live.live_lock("allocation"):
                task = live._allocate(request, None, publication_id=event)
            remember_publication(source_id, event)
            live.logger.info(
                "[LIVE] [AUTO_RESTART] previous_live_id=%s live_id=%s source_id=%s reason=runner_shutdown",
                recovery.task_id,
                task.task_id,
                source_id,
            )
            await live.synchronize(task.task_id)
            return
        # Recover a checkpoint interrupted after task persistence or reservation.
        previous = next(
            (
                task
                for task in records
                if task.live
                and task.live.automatic
                and (task.live.publication_id or task.live.event_id) == event
                and task.live.error != "Runner reservation lost"
            ),
            None,
        )
        if previous is not None:
            remember_publication(source_id, event)
            return
        if read_fence(source_id) == event:
            return
        connection = publication_connection(payload)
        equipment = None
        etab_name = config.LIVE.auto_etab_name
        if connection is not None:
            connection_id, secure = connection
            connection_payload = await client.get_rtmp_connection(connection_id, secure=secure)
            if isinstance(connection_payload, dict):
                query = connection_payload.get("query")
                equipment = extract_equipment(query)
                etab_name = extract_etablishment(query) or etab_name
        request = LiveStartRequest(
            source_id=source_id,
            event_id=event,
            equipment=equipment,
            etab_name=etab_name,
            app_name="MediaMTX",
        )
        async with live.live_lock("allocation"):
            task = live._allocate(request, None, publication_id=event)
        remember_publication(source_id, event)
        await live.synchronize(task.task_id)


async def discover_sources(client: MediaMTXClient) -> set[str]:
    """Register safe active RTMP publications and return their stable identifiers.

    Ignore non-RTMP inputs and isolate malformed observations. Re-read individual
    paths during reconciliation so a changing paginated snapshot cannot stop a
    valid session or allocate a disconnected publication.
    """
    paths = []
    for item in await client.list_paths():
        try:
            event = publication_event("discovery", item)
        except MediaMTXError:
            live.logger.warning(
                "[LIVE] [SOURCE_OBSERVATION_FAILED] Invalid publication observation"
            )
            continue
        if event is None:
            continue
        path = item.get("name")
        if not valid_path(path):
            live.logger.warning(
                "[LIVE] [SOURCE_PATH_REJECTED] A published RTMP path is incompatible with the Runner path format"
            )
            continue
        paths.append(path)
    await register_paths(paths)
    selected = set(paths)
    return {name for name, path in known_sources().items() if path in selected}


async def reconcile_sources() -> None:
    """Discover accepted RTMP publishers, then reconcile known sources independently.

    Discovery failures do not imply disconnection. Continue observing saved
    sources individually and retry both discovery and capacity on the next scan.
    """
    settings = config.LIVE
    if not settings.enabled or not settings.auto_start:
        return
    client = MediaMTXClient(
        settings.mediamtx_api_url,
        settings.mediamtx_api_user,
        settings.mediamtx_api_password,
        settings.mediamtx_timeout,
    )
    discovered: set[str] = set()
    try:
        discovered = await discover_sources(client)
    except Exception:
        live.logger.warning(
            "[LIVE] [SOURCE_DISCOVERY_FAILED] Check MediaMTX API access and the persistent source registry"
        )
    try:
        sources = known_sources()
        active = {
            task.live.source_id
            for task in live.sessions()
            if task.live and task.live.state not in live.TERMINAL
        }
    except (OSError, ValueError):
        live.logger.warning("[LIVE] [SOURCE_REGISTRY_UNAVAILABLE] Check persistent source bindings")
        return
    observed = discovered | active | set(settings.sources)
    for source_id in (name for name in sources if name in observed):
        try:
            await reconcile_source(source_id, client)
        except HTTPException as exc:
            live.logger.info(
                "[LIVE] [ALLOCATION_PENDING] Automatic Live pending source_id=%s status=%s; check Runner capacity and persistent storage",
                source_id,
                exc.status_code,
            )
        except Exception:
            live.logger.warning(
                "[LIVE] [SOURCE_OBSERVATION_FAILED] source_id=%s; check MediaMTX API credentials, connectivity and publication checkpoints",
                source_id,
            )
