"""Build secret-free Live monitoring snapshots and read-only source observations."""

import asyncio
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import urlencode

from app.core.config import config
from app.models.models import Runner, Task
from app.services.live_autostart import publication_event
from app.services.live_service import TERMINAL, _public_url
from app.services.live_sources import known_sources, pod_broadcaster_slug, valid_path
from app.services.mediamtx import MediaMTXClient, MediaMTXError


def elapsed_seconds(value: str | datetime | None, now: datetime) -> int | None:
    """Return a non-negative age in seconds, or None for an absent/invalid timestamp."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value or "")
        return max(0, int((now.astimezone() - parsed.astimezone()).total_seconds()))
    except ValueError:
        return None


def display_source_id(source_id: str) -> str:
    """Display the full broadcaster name used as the source identifier."""
    return source_id


def display_event_id(event_id: str) -> str:
    """Return a compact UI label while preserving the full event identifier."""
    if event_id.startswith("rtmp:"):
        parts = event_id.split(":", 2)
        if len(parts) == 3:
            return f"rtmp:{display_source_id(parts[1])}:{parts[2][:8]}..."
    if event_id.startswith("restart:"):
        return f"restart:{event_id.removeprefix('restart:')[:8]}..."
    return event_id


def source_broadcaster(source_id: str) -> str | None:
    """Resolve the broadcaster's Live source identifier without exposing its full path.

    Return None when the binding is missing or its RTMP path is invalid.
    """
    try:
        source_path = known_sources().get(source_id)
        if source_path is not None:
            return pod_broadcaster_slug(source_path)
    except ValueError:
        pass
    return None


def source_summary(source_ids: Iterable[str]) -> dict[str, Any]:
    """Return sorted public source identifiers and their compact display labels."""
    identifiers = sorted(set(source_ids))
    return {
        "sources": identifiers,
        "source_labels": [
            {"id": identifier, "label": display_source_id(identifier)} for identifier in identifiers
        ],
    }


def source_rows(
    overview: dict[str, Any], publishing_sources: set[str] | None
) -> list[dict[str, Any]]:
    """Describe visible sources using current RTMP observation and latest retained metadata.

    Keep publication presence separate from session state. An unavailable or
    disabled observation is unknown, even if an old Runner report had a signal.
    Resolve Broadcaster from source bindings, including sources with no session.
    Build local filter links without exposing private RTMP paths or credentials.
    """
    latest = {}
    for session in sorted(overview["sessions"], key=lambda row: row["created_at"]):
        latest[session["source_id"]] = session
    return [
        {
            **source,
            "broadcaster": source_broadcaster(source["id"]),
            "publishing": (
                None
                if publishing_sources is None or not config.LIVE.enabled
                else source["id"] in publishing_sources
            ),
            "latest_session": latest.get(source["id"]),
            "filter_query": urlencode({"state_filter": "all", "source_filter": source["id"]}),
        }
        for source in overview["source_labels"]
    ]


async def observe_publishing_sources() -> set[str] | None:
    """Read current known RTMP publishers without registering paths or starting sessions.

    Return None when MediaMTX observation fails; callers must not infer that
    publishers disappeared. Bound the complete paginated scan to one timeout.
    Disabled Live returns an empty set without contacting MediaMTX.
    """
    settings = config.LIVE
    if not settings.enabled:
        return set()
    sources = known_sources()
    client = MediaMTXClient(
        settings.mediamtx_api_url,
        settings.mediamtx_api_user,
        settings.mediamtx_api_password,
        settings.mediamtx_timeout,
    )
    try:
        paths = await asyncio.wait_for(client.list_paths(), timeout=settings.mediamtx_timeout)
        publishing_paths = {
            item["name"]
            for item in paths
            if valid_path(item.get("name")) and publication_event("observation", item) is not None
        }
    except (MediaMTXError, TimeoutError):
        return None
    return {identifier for identifier, path in sources.items() if path in publishing_paths}


def session_row(task: Task, now: datetime) -> dict[str, Any]:
    """Project public session fields and suppress playback when observations are stale.

    Session creation marks the start, including waiting for the source. Expose
    the last persisted update as the end only after a terminal state is confirmed.
    """
    assert task.live is not None
    live = task.live
    broadcaster = source_broadcaster(live.source_id)
    active = live.state not in TERMINAL
    age = elapsed_seconds(live.last_seen, now)
    stale_after = max(15, config.LIVE.poll_interval * 3 + config.LIVE.runner_timeout)
    stale = active and (age is None or age > stale_after)
    hls_url = None
    if live.hls_ready and live.state == "running" and not stale and not live.stop_requested:
        try:
            hls_url = _public_url(live.hls_url)
        except (RuntimeError, ValueError):
            pass
    duration = elapsed_seconds(task.created_at, now)
    if not active:
        updated_age = elapsed_seconds(task.updated_at, now)
        duration = max(0, (duration or 0) - (updated_age or 0))
    return {
        "id": task.task_id,
        "runner_id": task.runner_id,
        "source_id": live.source_id,
        "source_label": display_source_id(live.source_id),
        "event_id": live.event_id,
        "event_label": display_event_id(live.event_id),
        "equipment": live.equipment,
        "broadcaster": broadcaster,
        "etab_name": task.etab_name,
        "state": live.state,
        "automatic": live.automatic,
        "active": active,
        "stale": stale,
        "age_seconds": age,
        "duration_seconds": duration,
        "created_at": task.created_at,
        "ended_at": None if active else task.updated_at,
        "last_seen": live.last_seen,
        "hls_url": hls_url,
        "source_ready": live.source_ready,
        "input_mbps": max(0, live.input_bitrate) / 1_000_000,
        "input_mib": max(0, live.input_bytes) / 1_048_576,
        "forward_mib": max(0, live.forward_bytes) / 1_048_576,
        "hls_sessions": live.hls_sessions,
        "codecs": live.codecs,
        "resolutions": live.resolutions,
        "transcode_errors": live.transcode_errors,
        "error": live.error,
        "stop_requested": live.stop_requested,
        "attention": active and (stale or bool(live.error) or live.transcode_errors > 0),
    }


def build_overview(
    tasks: Iterable[Task], runners: Iterable[Runner], now: datetime
) -> dict[str, Any]:
    """Summarize retained sessions, their broadcasters and declared sources without media I/O."""
    sessions = sorted(
        (session_row(task, now) for task in tasks if task.live is not None),
        key=lambda row: (row["active"], row["created_at"]),
        reverse=True,
    )
    latest = {}
    for row in sorted(sessions, key=lambda row: row["created_at"]):
        latest[row["source_id"]] = row["id"]
    active_sources = {row["source_id"] for row in sessions if row["active"]}
    sources = known_sources()
    for row in sessions:
        row["can_restart"] = (
            config.LIVE.enabled
            and not row["active"]
            and row["source_id"] in sources
            and row["source_id"] not in active_sources
            and latest[row["source_id"]] == row["id"]
        )
    workers = []
    for runner in runners:
        if "live" not in runner.task_types:
            continue
        age = elapsed_seconds(runner.last_heartbeat, now)
        online = runner.status == "online" and age is not None and age < 60
        available = online and runner.availability == "available" and not runner.live_task_id
        workers.append(
            {
                "id": runner.id,
                "online": online,
                "available": available,
                "availability": runner.availability,
                "session_id": runner.live_task_id,
                "task_types": runner.task_types,
                "age_seconds": age,
            }
        )
    configured_paths = set(config.LIVE.sources.values())
    source_ids = (
        identifier
        for identifier, path in sources.items()
        if identifier in latest or path in configured_paths
    )
    return {
        "sessions": sessions,
        "workers": workers,
        "active_count": sum(row["active"] for row in sessions),
        "ready_count": sum(bool(row["hls_url"]) for row in sessions),
        "attention_count": sum(row["attention"] for row in sessions),
        "failed_count": sum(row["state"] == "failed" for row in sessions),
        "available_count": sum(worker["available"] for worker in workers),
        "enabled": config.LIVE.enabled,
        "auto_start": config.LIVE.auto_start,
        **source_summary(source_ids),
    }
