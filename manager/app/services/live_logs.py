"""Project bounded MediaMTX diagnostics into administration without exposing ingest secrets."""

import re
import subprocess
from datetime import datetime
from pathlib import Path

from app.core.config import config
from app.services.live_sources import source_id, valid_path

MAX_BYTES = 1024 * 1024
MAX_LINES = 2000
MEDIA_LINE = re.compile(r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) (DEB|INF|WAR|ERR) (.*)$")


def entry(timestamp: str, level: str, code: str, message: str) -> dict[str, str]:
    """Build a searchable Live entry from controlled text, never a raw media payload."""
    message = f"[LIVE] [{code}] {message}"
    return {
        "timestamp": timestamp,
        "module": "MediaMTX",
        "level": level,
        "context": "[live_mediamtx]",
        "message": message,
        "raw": f"{timestamp} - MediaMTX - {level} - [live_mediamtx] - {message}",
    }


def media_message(body: str) -> tuple[str, str, str] | None:
    """Classify known media events using fixed descriptions; discard unrecognized content.

    Authentication failures may mean missing/incorrect credentials or denied
    permissions. MediaMTX does not reliably distinguish these cases. Neither
    remote usernames, query strings, full paths nor raw error details are returned.
    Publication events expose only the public broadcaster name as source_id.
    """
    lowered = body.lower()
    if "authentication" in lowered and ("failed" in lowered or "error" in lowered):
        return (
            "WARNING",
            "RTMP_AUTH_FAILED" if "[rtmp" in lowered else "MEDIA_AUTH_FAILED",
            (
                "Authentication refused: credentials missing/incorrect, or IP/path permissions denied. "
                "Check encoder user/password and MediaMTX permissions."
            ),
        )
    published = re.search(r"is publishing to path '([^']+)'|\[path ([^\]]+)\].*ready:", body)
    if published:
        published_path = published[1] or published[2]
        if valid_path(published_path):
            source = source_id(published_path)
            return (
                "INFO",
                "SOURCE_PUBLISHED",
                f"RTMP publication received for source_id={source}.",
            )
        return (
            "WARNING",
            "SOURCE_PATH_REJECTED",
            (
                "A published RTMP path is incompatible with the Runner path format. "
                "Use letters, digits, underscores and hyphens, optionally separated by slashes. "
                "The final broadcaster name must start with a letter or digit and contain at most 100 characters."
            ),
        )
    if "already publishing" in lowered or "already assigned to a publisher" in lowered:
        return (
            "WARNING",
            "PUBLISHER_CONFLICT",
            "Another encoder is already publishing to this path.",
        )
    if "[rtmp" in lowered and "closed:" in lowered:
        if lowered.endswith(("closed: eof", "closed: terminated")):
            return "INFO", "RTMP_DISCONNECTED", "RTMP connection closed."
        return (
            "WARNING",
            "RTMP_CONNECTION_FAILED",
            "RTMP connection closed with an error; check encoder format, connectivity and path permissions.",
        )
    return None


def parse_media_line(line: str) -> dict[str, str] | None:
    """Parse MediaMTX's plain format and expose only recognized, sanitized events."""
    match = MEDIA_LINE.fullmatch(line.rstrip("\r\n"))
    if match is None:
        return None
    classified = media_message(match[3])
    if classified is None:
        return None
    level, code, message = classified
    peer = re.search(r"\[conn ([0-9a-fA-F:.\[\]]{1,64}:\d{1,5})\]", match[3])
    if peer:
        message += f" Peer: {peer[1]}."
    return entry(match[1].replace("/", "-"), level, code, message)


def media_lines() -> list[str]:
    """Read recent media logs from an operator-selected user unit or regular file.

    Journal reads have a three-second timeout and a line limit. File reads are
    limited to the final MiB. Only one source is configured; no request parameter
    controls a filesystem path or command. Errors are handled by read_media_logs.
    """
    settings = config.LIVE
    if settings.mediamtx_log_unit:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "--unit",
                settings.mediamtx_log_unit,
                "--lines",
                str(MAX_LINES),
                "--no-pager",
                "--output=cat",
                "--quiet",
            ],
            capture_output=True,
            check=True,
            timeout=3,
        )
        return result.stdout[-MAX_BYTES:].decode("utf-8", errors="replace").splitlines()
    path = Path(settings.mediamtx_log_file)
    if not path.is_file():
        raise OSError("Media log unavailable")
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - MAX_BYTES))
        if size > MAX_BYTES:
            stream.readline()  # Discard a partial leading line.
        return stream.read(MAX_BYTES).decode("utf-8", errors="replace").splitlines()[-MAX_LINES:]


def read_media_logs() -> list[dict[str, str]]:
    """Return sanitized media diagnostics, or a safe warning if the source is unreadable."""
    if not config.LIVE.enabled or not (
        config.LIVE.mediamtx_log_unit or config.LIVE.mediamtx_log_file
    ):
        return []
    try:
        return [parsed for line in media_lines() if (parsed := parse_media_line(line))]
    except (OSError, ValueError, subprocess.SubprocessError):
        return [
            entry(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "WARNING",
                "MEDIA_LOG_UNAVAILABLE",
                "Cannot read MediaMTX diagnostics. Check LIVE_MEDIAMTX_LOG_UNIT or LIVE_MEDIAMTX_LOG_FILE and service-account permissions.",
            )
        ]
