"""MediaMTX v3 wire contract and sanitized failures."""

import httpx
import pytest

from app.services.mediamtx import MediaMTXClient, MediaMTXError, path_metrics


@pytest.mark.asyncio
async def test_health_probe_reads_a_bounded_path_listing(monkeypatch):
    from unittest.mock import AsyncMock

    request = AsyncMock(return_value={"pageCount": 0, "items": []})
    client = MediaMTXClient("http://media")
    monkeypatch.setattr(client, "request", request)

    await client.check_health()

    request.assert_awaited_once_with("GET", "paths/list?page=0&itemsPerPage=1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [None, {}, {"pageCount": True, "items": []}, {"pageCount": 0, "items": [{}, {}]}],
)
async def test_health_probe_rejects_malformed_listing(monkeypatch, data):
    from unittest.mock import AsyncMock

    client = MediaMTXClient("http://media")
    monkeypatch.setattr(client, "request", AsyncMock(return_value=data))

    with pytest.raises(MediaMTXError, match="Invalid"):
        await client.check_health()


@pytest.mark.asyncio
async def test_runtime_discovery_reads_every_page(monkeypatch):
    from unittest.mock import AsyncMock

    request = AsyncMock(
        side_effect=[
            {"pageCount": 2, "items": [{"name": "live/a"}]},
            {"pageCount": 2, "items": [{"name": "live/b"}]},
        ]
    )
    client = MediaMTXClient("http://media")
    monkeypatch.setattr(client, "request", request)
    assert await client.list_paths() == [{"name": "live/a"}, {"name": "live/b"}]
    assert [call.args[1] for call in request.await_args_list] == [
        "paths/list?page=0&itemsPerPage=100",
        "paths/list?page=1&itemsPerPage=100",
    ]
    request.side_effect = None
    request.return_value = {"pageCount": 0, "items": []}
    assert await client.list_paths() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"pageCount": -1, "items": []},
        {"pageCount": True, "items": []},
        {"pageCount": 101, "items": []},
        {"pageCount": 1, "items": None},
        {"pageCount": 1, "items": [None]},
        {"pageCount": 1, "items": [{}] * 101},
    ],
)
async def test_runtime_discovery_rejects_malformed_or_unbounded_listing(monkeypatch, data):
    from unittest.mock import AsyncMock

    client = MediaMTXClient("http://media")
    monkeypatch.setattr(client, "request", AsyncMock(return_value=data))
    with pytest.raises(MediaMTXError, match="Invalid"):
        await client.list_paths()


@pytest.mark.asyncio
async def test_native_pull_create_inspect_remove_and_idempotence(monkeypatch):
    paths = {}
    requests = []

    def handle(request):
        import json

        requests.append(request)
        endpoint = request.url.path
        if "/config/paths/get/" in endpoint:
            return httpx.Response(200, json=paths["relay"]) if paths else httpx.Response(404)
        if "/config/paths/add/" in endpoint:
            paths["relay"] = json.loads(request.content)
            return httpx.Response(200)
        if "/paths/get/" in endpoint:
            return httpx.Response(200, json={"available": True, "inboundBytes": 42})
        paths.clear()
        return httpx.Response(404)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs),
    )
    client = MediaMTXClient("http://media:9997", "api", "password")
    await client.configure_forward("live/123", "rtmp://manager:1935/live/key")
    await client.configure_forward("live/123", "rtmp://manager:1935/live/key")
    assert sum(request.method == "POST" for request in requests) == 1
    assert (await client.get_path("live/123"))["available"]
    with pytest.raises(MediaMTXError, match="ownership"):
        await client.configure_forward("live/123", "rtmp://different/source")
    await client.remove_forward("live/123")
    await client.remove_forward("live/123")
    assert not paths
    assert all(request.headers["authorization"].startswith("Basic ") for request in requests)


@pytest.mark.asyncio
async def test_rtmp_connection_lookup_uses_protocol_connection_endpoint(monkeypatch):
    from unittest.mock import AsyncMock

    request = AsyncMock(return_value={"query": "equipment=SMP351-01"})
    client = MediaMTXClient("http://media")
    monkeypatch.setattr(client, "request", request)

    assert await client.get_rtmp_connection("connection-id") == {"query": "equipment=SMP351-01"}
    request.assert_awaited_once_with("GET", "rtmp/conns/get/connection-id", missing_ok=True)

    request.reset_mock()
    await client.get_rtmp_connection("secure-id", secure=True)
    request.assert_awaited_once_with("GET", "rtmps/conns/get/secure-id", missing_ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload", [(500, {"error": "secret"}), (302, {}), (200, []), (200, "not-json")]
)
async def test_control_errors_never_echo_credentials(monkeypatch, status, payload):
    original = httpx.AsyncClient
    response = (
        httpx.Response(status, json=payload)
        if payload != "not-json"
        else httpx.Response(status, text=payload)
    )
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: response), **kwargs
        ),
    )
    with pytest.raises(MediaMTXError) as error:
        await MediaMTXClient("http://media", "api", "secret").get_path("secret-stream-key")
    assert "secret" not in str(error.value)


def test_track_metrics_match_mediamtx_121_schema():
    result = path_metrics(
        {
            "available": True,
            "inboundBytes": 42,
            "outboundBytes": 21,
            "tracks2": [{"codec": "H264", "codecProps": {"width": 1280, "height": 720}}],
        }
    )
    assert result["resolutions"] == ["1280x720"]
    assert result["input_bytes"] == 42
    assert not path_metrics(None)["source_ready"]
