"""Public HLS files; control remains on the authenticated task API."""

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.core.config import config
from app.core.state import get_task_status
from app.services.hls_viewers import record_request
from app.services.live_paths import contained_path
from app.services.live_runtime import directory_for

router = APIRouter(tags=["Live HLS"])


class LiveAccessFilter(logging.Filter):
    """Keep errors while avoiding one log record per successful HLS segment."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Suppress successful HLS access records while retaining errors and other requests."""
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            return not (
                str(args[2]).startswith("/hls/") and isinstance(args[4], int) and args[4] < 400
            )
        return True


def configure_access_logging() -> None:
    """Install the HLS access filter once for a Runner configured to accept Live."""
    if "live" in config.RUNNER_TASK_TYPES:
        access = logging.getLogger("uvicorn.access")
        if not any(isinstance(item, LiveAccessFilter) for item in access.filters):
            access.addFilter(LiveAccessFilter())


@router.get("/hls/{live_id}/{filename:path}")
async def hls_file_endpoint(live_id: str, filename: str, request: Request) -> FileResponse:
    """Serve an HLS file and record the requesting client identity."""
    viewer_id = request.client.host if request.client else None
    return await hls_file(live_id, filename, viewer_id)


async def hls_file(live_id: str, filename: str, viewer_id: str | None = None) -> FileResponse:
    """Serve an allowed HLS file from a ready session's contained output directory.

    Return HTTP 404 for unavailable sessions or invalid paths. Playlists use
    no-store caching; media segments may be cached briefly.
    """
    if "live" not in config.RUNNER_TASK_TYPES:
        raise HTTPException(404, "HLS not found")
    payload = get_task_status(live_id) or {}
    if not payload.get("live", {}).get("hls_ready"):
        raise HTTPException(404, "HLS not ready")
    if not re.fullmatch(
        r"master\.m3u8|[a-z][a-z0-9_]{0,20}/(?:index\.m3u8|segment_[0-9]+\.ts)", filename
    ):
        raise HTTPException(404, "HLS not found")
    try:
        directory = directory_for(live_id)
        file = contained_path(directory, filename)
        if not file.is_file():
            raise ValueError("Invalid HLS file")
    except (ValueError, RuntimeError, OSError):
        raise HTTPException(404, "HLS not found") from None
    record_request(live_id, viewer_id)
    playlist = filename.endswith(".m3u8")
    return FileResponse(
        file,
        media_type="application/vnd.apple.mpegurl" if playlist else "video/mp2t",
        headers={"Cache-Control": "no-store" if playlist else "public, max-age=60"},
    )
