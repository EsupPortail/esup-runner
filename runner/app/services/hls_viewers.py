"""In-memory estimate of clients requesting a Runner-served HLS session."""

import time
from threading import Lock

VIEWER_TTL_SECONDS = 15.0
_viewers: dict[str, dict[str, float]] = {}
_lock = Lock()


def record_request(live_id: str, viewer_id: str | None) -> None:
    """Record a successful HLS request for one client identity."""
    if not viewer_id:
        return
    now = time.monotonic()
    with _lock:
        viewers = _viewers.setdefault(live_id, {})
        viewers[viewer_id] = now
        _remove_expired(viewers, now)


def count(live_id: str) -> int:
    """Return the number of client identities active in the recent request window."""
    now = time.monotonic()
    with _lock:
        viewers = _viewers.get(live_id)
        if not viewers:
            return 0
        _remove_expired(viewers, now)
        return len(viewers)


def clear(live_id: str) -> None:
    """Forget request activity after a session has been cleaned up."""
    with _lock:
        _viewers.pop(live_id, None)


def _remove_expired(viewers: dict[str, float], now: float) -> None:
    """Remove identities that have not requested HLS recently."""
    cutoff = now - VIEWER_TTL_SECONDS
    for viewer_id, last_seen in list(viewers.items()):
        if last_seen < cutoff:
            del viewers[viewer_id]
