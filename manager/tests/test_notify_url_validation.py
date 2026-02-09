import pytest
from fastapi import HTTPException

import app.api.routes.task as task_module


def test_host_matches_allowlist_variants():
    assert task_module._host_matches_allowlist("example.com", ["example.com"]) is True
    assert task_module._host_matches_allowlist("a.example.com", ["example.com"]) is True
    assert task_module._host_matches_allowlist("example.com", [""]) is False
    assert task_module._host_matches_allowlist("", ["example.com"]) is False
    assert task_module._host_matches_allowlist("evil.com", ["example.com"]) is False


def test_is_disallowed_ip_invalid_is_true():
    assert task_module._is_disallowed_ip("not-an-ip") is True
    assert task_module._is_disallowed_ip("127.0.0.1") is True
    assert task_module._is_disallowed_ip("8.8.8.8") is False


@pytest.mark.parametrize(
    "url,detail",
    (
        ("", "notify_url is empty"),
        ("ftp://example.com/x", "notify_url must use http or https"),
        ("http:///x", "notify_url is missing host"),
        ("http://user:pass@example.com/x", "must not include userinfo"),
        ("http://./x", "notify_url has invalid host"),
    ),
)
def test_parse_notify_url_rejects_bad_inputs(url: str, detail: str):
    with pytest.raises(HTTPException) as exc:
        task_module._parse_notify_url(url)
    assert exc.value.status_code == 400
    assert detail in str(exc.value.detail)


def test_validate_notify_url_host_rejects_allowlist_miss_and_localhost(monkeypatch):
    monkeypatch.setattr(task_module.config, "NOTIFY_URL_ALLOWED_HOSTS", ["allowed.example"])
    with pytest.raises(HTTPException):
        task_module._validate_notify_url_host("evil.example")

    monkeypatch.setattr(task_module.config, "NOTIFY_URL_ALLOWED_HOSTS", [])
    with pytest.raises(HTTPException):
        task_module._validate_notify_url_host("localhost")


@pytest.mark.asyncio
async def test_resolve_notify_url_ips_raises_on_exception(monkeypatch):
    async def boom(_host: str):
        raise RuntimeError("dns")

    monkeypatch.setattr(task_module, "_resolve_host_ips", boom)
    with pytest.raises(HTTPException) as exc:
        await task_module._resolve_notify_url_ips("example.com")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_resolve_notify_url_ips_raises_on_empty_list(monkeypatch):
    async def empty(_host: str):
        return []

    monkeypatch.setattr(task_module, "_resolve_host_ips", empty)
    with pytest.raises(HTTPException) as exc:
        await task_module._resolve_notify_url_ips("example.com")
    assert exc.value.status_code == 400


def test_validate_notify_url_public_ips_rejects_private():
    with pytest.raises(HTTPException) as exc:
        task_module._validate_notify_url_public_ips(["127.0.0.1"])
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_validate_notify_url_happy_path_without_dns(monkeypatch):
    monkeypatch.setattr(task_module.config, "NOTIFY_URL_ALLOWED_HOSTS", [])
    monkeypatch.setattr(task_module.config, "NOTIFY_URL_ALLOW_PRIVATE_NETWORKS", False)

    async def ips(_host: str):
        return ["8.8.8.8"]

    monkeypatch.setattr(task_module, "_resolve_notify_url_ips", ips)

    out = await task_module._validate_notify_url("https://example.com/notify")
    assert out == "https://example.com/notify"


@pytest.mark.asyncio
async def test_send_notify_callback_handles_empty_notify_url(monkeypatch):
    class DummyTask:
        notify_url = ""

    class DummyNotification:
        task_id = "t1"
        status = "completed"
        script_output = None

    ok, err = await task_module._send_notify_callback(DummyTask(), DummyNotification())
    assert ok is False
    assert err == "notify_url is empty"


@pytest.mark.asyncio
async def test_send_notify_callback_sends_auth_header_from_task_token(monkeypatch):
    async def noop(_url: str):
        return _url

    monkeypatch.setattr(task_module, "_validate_notify_url", noop)

    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["content"] = content
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    class DummyTask:
        notify_url = "https://example.com/notify"
        client_token = "client-secret"

    class DummyNotification:
        task_id = "t1"
        status = "completed"
        script_output = "hello"

    ok, err = await task_module._send_notify_callback(DummyTask(), DummyNotification())
    assert ok is True
    assert err is None
    assert captured["headers"]["Authorization"] == "Bearer client-secret"


@pytest.mark.asyncio
async def test_send_notify_callback_non_200_returns_error(monkeypatch):
    async def noop(_url: str):
        return _url

    monkeypatch.setattr(task_module, "_validate_notify_url", noop)

    class FakeResponse:
        status_code = 500
        text = "no"

    class FakeAsyncClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *_a, **_k):
            return FakeResponse()

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    class DummyTask:
        notify_url = "https://example.com/notify"
        client_token = "client-secret"

    class DummyNotification:
        task_id = "t1"
        status = "completed"
        script_output = None

    ok, err = await task_module._send_notify_callback(DummyTask(), DummyNotification())
    assert ok is False
    assert "failed" in (err or "")
