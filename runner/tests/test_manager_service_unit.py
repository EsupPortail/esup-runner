import pytest

import app.services.manager_service as manager_service


class FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class FakeAsyncClient:
    def __init__(self, responder):
        self.responder = responder

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *_, **__):
        return await self.responder()

    async def get(self, *_, **__):
        return await self.responder()


@pytest.mark.asyncio
async def test_register_with_manager_success(monkeypatch):
    async def responder():
        return FakeResponse(200)

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    ok = await manager_service.register_with_manager()
    assert ok is True


@pytest.mark.asyncio
async def test_register_with_manager_failure(monkeypatch):
    async def responder():
        return FakeResponse(500, text="boom")

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    ok = await manager_service.register_with_manager()
    assert ok is False


@pytest.mark.asyncio
async def test_send_heartbeat(monkeypatch):
    # Ensure is_registered() returns True
    monkeypatch.setattr(manager_service, "is_registered", lambda: True)

    async def responder():
        return FakeResponse(200)

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    ok = await manager_service.send_heartbeat()
    assert ok is True


@pytest.mark.asyncio
async def test_send_heartbeat_failure_status(monkeypatch):
    monkeypatch.setattr(manager_service, "is_registered", lambda: True)

    async def responder():
        return FakeResponse(500, text="nope")

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    ok = await manager_service.send_heartbeat()
    assert ok is False


@pytest.mark.asyncio
async def test_send_heartbeat_exception(monkeypatch):
    monkeypatch.setattr(manager_service, "is_registered", lambda: True)

    async def responder():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    ok = await manager_service.send_heartbeat()
    assert ok is False


@pytest.mark.asyncio
async def test_send_heartbeat_not_registered(monkeypatch):
    monkeypatch.setattr(manager_service, "is_registered", lambda: False)
    ok = await manager_service.send_heartbeat()
    assert ok is False


@pytest.mark.asyncio
async def test_check_manager_health(monkeypatch):
    async def responder():
        return FakeResponse(200, json_data={"status": "healthy"})

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    healthy = await manager_service.check_manager_health()
    assert healthy is True


@pytest.mark.asyncio
async def test_check_manager_health_failure_status(monkeypatch):
    async def responder():
        return FakeResponse(500, json_data={"status": "down"})

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(responder)
    )

    healthy = await manager_service.check_manager_health()
    assert healthy is False


@pytest.mark.asyncio
async def test_register_with_manager_exception(monkeypatch):
    async def _responder():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(_responder)
    )

    ok = await manager_service.register_with_manager()
    assert ok is False


@pytest.mark.asyncio
async def test_check_manager_health_exception(monkeypatch):
    async def _responder():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        manager_service.httpx, "AsyncClient", lambda *_, **__: FakeAsyncClient(_responder)
    )

    healthy = await manager_service.check_manager_health()
    assert healthy is False
