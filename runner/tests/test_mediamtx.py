"""MediaMTX v3 wire contract and sanitized failures."""

import httpx
import pytest

from app.services.mediamtx import (
    MediaMTXClient,
    MediaMTXError,
    MediaMTXUnavailable,
    path_metrics,
)


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


@pytest.mark.asyncio
async def test_control_server_error_is_retryable(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs
        ),
    )

    with pytest.raises(MediaMTXUnavailable):
        await MediaMTXClient("http://media").check_health()


@pytest.mark.asyncio
async def test_control_connection_error_is_retryable(monkeypatch):
    original = httpx.AsyncClient

    def fail(request):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(fail), **kwargs),
    )

    with pytest.raises(MediaMTXUnavailable):
        await MediaMTXClient("http://media").check_health()


@pytest.mark.asyncio
async def test_invalid_control_payload_is_not_retryable(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not-json")),
            **kwargs,
        ),
    )

    with pytest.raises(MediaMTXError) as error:
        await MediaMTXClient("http://media").check_health()
    assert not isinstance(error.value, MediaMTXUnavailable)


def test_track_metrics_match_mediamtx_121_schema():
    result = path_metrics(
        {
            "available": True,
            "inboundBytes": 42,
            "outboundBytes": 21,
            "readers": [{"type": "hlsSession"}, {"type": "rtmpConn"}],
            "tracks2": [{"codec": "H264", "codecProps": {"width": 1280, "height": 720}}],
        }
    )
    assert result["resolutions"] == ["1280x720"]
    assert result["input_bytes"] == 42
    assert result["hls_sessions"] == 1
    assert path_metrics({"hls_sessions": 3})["hls_sessions"] == 3
    assert path_metrics({"hlsSessions": [{}, {}]})["hls_sessions"] == 2
    assert path_metrics({"hls_sessions": "invalid", "readers": []})["hls_sessions"] == 0
    assert not path_metrics(None)["source_ready"]
