"""Small MediaMTX v3 Control API adapter. Never include URLs in errors."""

from typing import Any
from urllib.parse import quote

import httpx


class MediaMTXError(RuntimeError):
    """A media control operation failed or returned an invalid response."""


class MediaMTXUnavailable(MediaMTXError):
    """The MediaMTX Control API is temporarily unreachable or overloaded."""


class MediaMTXClient:
    """Control instance-owned relay paths through the MediaMTX v3 HTTP API."""

    def __init__(self, url: str, user: str = "", password: str = "", timeout: float = 5):
        """Configure the API base URL, optional Basic authentication and request timeout."""
        self.url = url.rstrip("/")
        self.auth = httpx.BasicAuth(user, password) if user else None
        self.timeout = timeout

    async def request(
        self, method: str, endpoint: str, payload: dict | None = None, *, missing_ok: bool = False
    ) -> dict[str, Any] | None:
        """Call an endpoint relative to /v3 and return its JSON object.

        Return None for HTTP 404 only when missing_ok is set, or an empty dict for
        an empty successful response. Raise MediaMTXUnavailable for transport and
        server errors, and MediaMTXError for other invalid responses, without
        exposing URLs, credentials or response bodies.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, auth=self.auth, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.request(method, f"{self.url}/v3/{endpoint}", json=payload)
            if missing_ok and response.status_code == 404:
                return None
            if not response.is_success:
                if response.status_code >= 500:
                    raise MediaMTXUnavailable("MediaMTX control unavailable")
                raise MediaMTXError(f"MediaMTX control returned HTTP {response.status_code}")
            data = response.json() if response.content else {}
            if not isinstance(data, dict):
                raise MediaMTXError("Invalid MediaMTX control response")
            return data
        except httpx.HTTPError:
            raise MediaMTXUnavailable("MediaMTX control unavailable") from None
        except ValueError:
            raise MediaMTXError("Invalid MediaMTX control response") from None

    async def get_path(self, path: str) -> dict[str, Any] | None:
        """Return runtime source and track information, or None for a missing path."""
        return await self.request("GET", f"paths/get/{quote(path, safe='')}", missing_ok=True)

    async def check_health(self) -> None:
        """Verify that the MediaMTX control API responds with a valid path listing."""
        data = await self.request("GET", "paths/list?page=0&itemsPerPage=1")
        if (
            data is None
            or type(data.get("pageCount")) is not int
            or data["pageCount"] < 0
            or not isinstance(data.get("items"), list)
            or len(data["items"]) > 1
            or any(not isinstance(item, dict) for item in data["items"])
        ):
            raise MediaMTXError("Invalid MediaMTX health response")

    async def configure_forward(self, path: str, source: str) -> None:
        """Create an instance-owned native pull relay, idempotently."""
        endpoint = quote(path, safe="")
        existing = await self.request("GET", f"config/paths/get/{endpoint}", missing_ok=True)
        if existing is not None:
            if existing.get("source") != source:
                raise MediaMTXError("MediaMTX path ownership conflict")
            return
        await self.request(
            "POST", f"config/paths/add/{endpoint}", {"source": source, "sourceOnDemand": False}
        )

    async def remove_forward(self, path: str) -> None:
        """Delete the relay configuration, treating an already absent path as success."""
        await self.request("DELETE", f"config/paths/delete/{quote(path, safe='')}", missing_ok=True)


def path_metrics(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract non-secret counters, viewer estimates and track information."""
    data = payload or {}
    tracks = data.get("tracks2") or []
    return {
        "source_ready": bool(data.get("available")),
        "input_bytes": max(0, int(data.get("inboundBytes") or 0)),
        "forward_bytes": max(0, int(data.get("outboundBytes") or 0)),
        "hls_sessions": _hls_session_count(data),
        "codecs": [str(track.get("codec", "")) for track in tracks if isinstance(track, dict)],
        "resolutions": [
            f"{props['width']}x{props['height']}"
            for track in tracks
            if isinstance(track, dict)
            if isinstance(props := track.get("codecProps"), dict)
            and props.get("width")
            and props.get("height")
        ],
    }


def _hls_session_count(data: dict[str, Any]) -> int | None:
    """Return MediaMTX's current HLS session count when the API exposes it."""
    value = data.get("hls_sessions", data.get("hlsSessions"))
    if isinstance(value, list):
        return len(value)
    if value is not None and not isinstance(value, bool):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    readers = data.get("readers")
    if isinstance(readers, list):
        return sum(
            1
            for reader in readers
            if isinstance(reader, dict) and reader.get("type") == "hlsSession"
        )
    return None
