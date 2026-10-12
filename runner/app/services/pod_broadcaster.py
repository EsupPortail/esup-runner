"""Small Esup-Pod broadcaster API adapter with sanitized failures."""

import asyncio
import re
from urllib.parse import quote

import httpx


class PodBroadcasterError(RuntimeError):
    """A broadcaster status update failed."""


class PodBroadcasterClient:
    """Update one Esup-Pod broadcaster through its REST API."""

    def __init__(self, url: str, user: str, password: str, timeout: float = 5):
        """Configure the Pod base URL, Basic authentication and request timeout."""
        self.url = url.rstrip("/")
        self.auth = httpx.BasicAuth(user, password)
        self.timeout = timeout

    async def set_status(self, slug: str, enabled: bool) -> None:
        """Match the Live identifier supplied as ``slug`` against Pod's ``source_id``.

        Update only a unique match, within one timeout for lookup and PATCH.
        Never fall back to matching a Pod slug or follow response URLs/redirects.
        Raise PodBroadcasterError without exposing credentials or response bodies.
        """
        if not slug:
            raise PodBroadcasterError("Live source identifier is required")
        try:
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    auth=self.auth,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    broadcaster_slug = await self._resolve_slug(client, slug)
                    response = await client.patch(
                        f"{self.url}/rest/broadcasters/{quote(broadcaster_slug, safe='')}/",
                        json={"status": enabled},
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                    )
                    if not response.is_success:
                        raise PodBroadcasterError(
                            f"Pod broadcaster API returned HTTP {response.status_code}"
                        )
        except (httpx.HTTPError, ValueError, TimeoutError):
            raise PodBroadcasterError("Pod broadcaster API unavailable") from None

    async def _resolve_slug(self, client: httpx.AsyncClient, source_id: str) -> str:
        """Find a unique source_id through Pod's filter using only the configured API URL."""
        matched_slug = None
        page = 1
        while True:
            response = await client.get(
                f"{self.url}/rest/broadcasters/",
                params={"source_id": source_id, "page": page},
                headers={"Accept": "application/json"},
            )
            if not response.is_success:
                raise PodBroadcasterError(
                    f"Pod broadcaster API returned HTTP {response.status_code}"
                )
            payload = response.json()
            rows = payload.get("results") if isinstance(payload, dict) else payload
            next_page = payload.get("next") if isinstance(payload, dict) else None
            if (
                not isinstance(rows, list)
                or any(not isinstance(row, dict) for row in rows)
                or (next_page is not None and (not isinstance(next_page, str) or not rows))
            ):
                raise PodBroadcasterError("Invalid Pod broadcaster response")
            for row in rows:
                if row.get("source_id") != source_id:
                    continue
                if matched_slug is not None:
                    raise PodBroadcasterError("Multiple Pod broadcasters match the Live source")
                value = row.get("slug")
                if (
                    not isinstance(value, str)
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", value) is None
                ):
                    raise PodBroadcasterError("Invalid Pod broadcaster response")
                matched_slug = value
            if not next_page:
                break
            page += 1
        if matched_slug is None:
            raise PodBroadcasterError("No Pod broadcaster matches the Live source")
        return matched_slug
