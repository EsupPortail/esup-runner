"""Identify Live sources by broadcaster while keeping full RTMP paths private."""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import TypeGuard

from app.core import state
from app.core.config import config


def valid_path(value: object) -> TypeGuard[str]:
    """Require a relative RTMP path ending with a valid public broadcaster identifier."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value) is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value.rsplit("/", 1)[-1]) is not None
    )


def pod_broadcaster_slug(path: str) -> str:
    """Return the Live source identifier from the final RTMP path component."""
    if not valid_path(path):
        raise ValueError("Invalid Live source path")
    return path.rsplit("/", 1)[-1]


def generated_id(path: str) -> str:
    """Return the broadcaster name used as the public source identifier."""
    return pod_broadcaster_slug(path)


def registry_path() -> Path:
    """Locate private source bindings beside the durable publication checkpoints."""
    return state.persistence.data_directory / ".live-sources.json"


def saved_sources() -> dict[str, str]:
    """Read validated bindings; fail closed on corrupt or unreadable persistence."""
    try:
        data = json.loads(registry_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", key) or not valid_path(value)
        for key, value in data.items()
    ):
        raise ValueError("Invalid Live source registry")
    if len(set(data.values())) != len(data):
        raise ValueError("Duplicate Live source bindings")
    if any(name != generated_id(path) for name, path in data.items()):
        raise ValueError("Live source registry identifiers must match broadcaster names")
    return data


def known_sources() -> dict[str, str]:
    """Merge saved broadcaster bindings and optional source declarations.

    Require each identifier to match the final RTMP path component. Reusing a
    broadcaster for a different path is rejected instead of redirecting a session.
    """
    sources = saved_sources()
    paths = set(sources.values())
    for name, path in config.LIVE.sources.items():
        if name != generated_id(path):
            raise ValueError("Live source identifiers must match broadcaster names")
        if name in sources and sources[name] != path:
            raise ValueError("Live source declaration conflicts with a saved binding")
        if path not in paths:
            sources[name] = path
            paths.add(path)
    return sources


def source_id(path: str) -> str:
    """Return the broadcaster identifier for a valid RTMP source path."""
    return generated_id(path)


async def register_paths(paths: list[str]) -> None:
    """Persist newly discovered bindings atomically under the shared discovery lock.

    Use owner-only permissions because paths can contain stream keys. Raise on
    invalid paths, binding collisions or storage errors before task allocation.
    """
    from app.services.live_service import live_lock

    async with live_lock("source-registry"):
        saved = saved_sources()
        sources = known_sources()
        reverse = {path: name for name, path in sources.items()}
        updated = dict(saved)
        for path in paths:
            if not valid_path(path):
                raise ValueError("Invalid Live source path")
            name = reverse.get(path, generated_id(path))
            if name in sources and sources[name] != path:
                raise ValueError("Live source identifier collision")
            updated[name] = path
            sources[name] = path
        if updated == saved:
            return
        target = registry_path()
        descriptor, filename = tempfile.mkstemp(prefix=".live-sources-", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(updated, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
