"""Pod broadcaster API contract and optional Live lifecycle integration."""

import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.pod_broadcaster import PodBroadcasterClient, PodBroadcasterError


@pytest.mark.asyncio
async def test_pod_broadcaster_updates_status_with_basic_auth(monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"source_id": "", "slug": "amphi-1"},
                        {"source_id": "amphi-1", "slug": "pod-lecture-hall"},
                    ],
                    "next": None,
                },
            )
        return httpx.Response(200)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    client = PodBroadcasterClient("https://pod.example/base/", "api-user", "api-password")

    await client.set_status("amphi-1", True)
    await client.set_status("amphi-1", False)

    assert [request.method for request in requests] == ["GET", "PATCH", "GET", "PATCH"]
    assert [str(request.url) for request in requests] == [
        "https://pod.example/base/rest/broadcasters/?source_id=amphi-1&page=1",
        "https://pod.example/base/rest/broadcasters/pod-lecture-hall/",
        "https://pod.example/base/rest/broadcasters/?source_id=amphi-1&page=1",
        "https://pod.example/base/rest/broadcasters/pod-lecture-hall/",
    ]
    assert [json.loads(request.content) for request in requests if request.method == "PATCH"] == [
        {"status": True},
        {"status": False},
    ]
    assert all(request.headers["authorization"].startswith("Basic ") for request in requests)
    assert all(request.headers["accept"] == "application/json" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["GET", "PATCH"])
async def test_pod_broadcaster_errors_do_not_echo_response_or_credentials(
    monkeypatch, failed_method
):
    original = httpx.AsyncClient

    def handle(request):
        if request.method == failed_method:
            return httpx.Response(500, json={"detail": "api-password"})
        return httpx.Response(200, json=[{"source_id": "amphi-1", "slug": "pod-hall"}])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )

    with pytest.raises(PodBroadcasterError) as error:
        await PodBroadcasterClient("https://pod.example", "api-user", "api-password").set_status(
            "amphi-1", True
        )
    assert "api-password" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_page", [1, 2])
async def test_pod_broadcaster_lookup_checks_all_pages_on_the_configured_url(
    monkeypatch, matching_page
):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.method == "PATCH":
            return httpx.Response(200)
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "source_id": "amphi-1" if page == matching_page else "unrelated",
                        "slug": "pod-hall" if page == matching_page else "another-hall",
                    }
                ],
                "next": "https://untrusted.example/steal-auth/?page=2" if page == 1 else None,
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    await PodBroadcasterClient("https://pod.example/base", "user", "password").set_status(
        "amphi-1", True
    )

    assert [str(request.url) for request in requests] == [
        "https://pod.example/base/rest/broadcasters/?source_id=amphi-1&page=1",
        "https://pod.example/base/rest/broadcasters/?source_id=amphi-1&page=2",
        "https://pod.example/base/rest/broadcasters/pod-hall/",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"slug": "amphi-1"}],
        [{"slug": "amphi-1", "source_id": ""}],
        [{"slug": "amphi-1", "source_id": "AMPHI-1"}],
    ],
)
async def test_pod_broadcaster_requires_an_exact_source_id_without_slug_fallback(monkeypatch, rows):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(PodBroadcasterError, match="No Pod broadcaster"):
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status(
            "amphi-1", True
        )

    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("same_page", [True, False])
async def test_pod_broadcaster_rejects_duplicate_source_ids_before_patching(monkeypatch, same_page):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        page = int(request.url.params["page"])
        rows = [{"source_id": "amphi-1", "slug": "pod-hall"}]
        if same_page:
            rows.append({"source_id": "amphi-1", "slug": "another-hall"})
        return httpx.Response(
            200,
            json={
                "results": rows,
                "next": (
                    "https://pod.example/rest/broadcasters/?page=2"
                    if not same_page and page == 1
                    else None
                ),
            },
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(PodBroadcasterError, match="Multiple Pod broadcasters"):
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status(
            "amphi-1", True
        )
    assert all(request.method == "GET" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        "api-password",
        {},
        {"results": {}},
        {"results": [None]},
        {"results": [], "next": "https://pod.example/?page=2"},
        {"results": [{}], "next": 2},
        [{"source_id": "amphi-1"}],
        [{"source_id": "amphi-1", "slug": 42}],
        [{"source_id": "amphi-1", "slug": ""}],
        [{"source_id": "amphi-1", "slug": ".."}],
        [{"source_id": "amphi-1", "slug": "../escape"}],
        [{"source_id": "amphi-1", "slug": "https://untrusted.example"}],
    ],
)
async def test_pod_broadcaster_rejects_malformed_responses_without_patching(monkeypatch, payload):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        return httpx.Response(
            200, content=json.dumps(payload), headers={"Content-Type": "application/json"}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(PodBroadcasterError, match="Invalid Pod broadcaster response") as error:
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status(
            "amphi-1", True
        )
    assert "api-password" not in str(error.value)
    assert [request.method for request in requests] == ["GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_method", ["GET", "PATCH"])
async def test_pod_broadcaster_does_not_follow_redirects(monkeypatch, failed_method):
    requests = []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if request.method == failed_method:
            return httpx.Response(
                302, headers={"Location": "https://untrusted.example/steal-auth/"}
            )
        return httpx.Response(200, json=[{"source_id": "amphi-1", "slug": "pod-hall"}])

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(PodBroadcasterError, match="HTTP 302"):
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status(
            "amphi-1", True
        )
    assert all(request.url.host == "pod.example" for request in requests)


@pytest.mark.asyncio
async def test_pod_broadcaster_rejects_non_json_responses(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="api-password")),
            **kwargs,
        ),
    )
    with pytest.raises(PodBroadcasterError, match="unavailable"):
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status(
            "amphi-1", True
        )


@pytest.mark.asyncio
async def test_pod_broadcaster_lookup_and_patch_share_one_timeout(monkeypatch):
    original = httpx.AsyncClient
    requests = []

    async def handle(request):
        requests.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={"results": [{"source_id": "unrelated"}], "next": "https://pod.example/?page=2"},
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    with pytest.raises(PodBroadcasterError, match="unavailable"):
        await PodBroadcasterClient(
            "https://pod.example", "user", "password", timeout=0.015
        ).set_status("amphi-1", True)
    assert all(request.method == "GET" for request in requests)


@pytest.mark.asyncio
async def test_pod_broadcaster_rejects_empty_source_identifier():
    with pytest.raises(PodBroadcasterError, match="identifier is required"):
        await PodBroadcasterClient("https://pod.example", "user", "password").set_status("", True)


@pytest.mark.asyncio
async def test_pod_broadcaster_network_error_is_sanitized(monkeypatch):
    client = AsyncMock()
    client.__aenter__.side_effect = httpx.ConnectError("api-password")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)

    with pytest.raises(PodBroadcasterError, match="unavailable"):
        await PodBroadcasterClient("https://pod.example", "api-user", "api-password").set_status(
            "amphi-1", True
        )
