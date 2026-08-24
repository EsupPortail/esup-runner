"""Unit coverage for app.main helpers."""

from __future__ import annotations

import importlib
import signal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core.config import config


def test_register_sighup_reload_handles_failure(monkeypatch):
    """Validate Register sighup reload handles failure."""

    def raise_value_error(*_args, **_kwargs):
        raise ValueError("fail")

    monkeypatch.setattr(signal, "signal", raise_value_error)
    main._register_sighup_reload()


@pytest.mark.parametrize(
    "request_path",
    [
        "/runner-manager/static/favicon.png?version=1.7.1",
        "/static/favicon.png?version=1.7.1",
        "/static/logo.png?version=1.7.1",
    ],
)
def test_static_assets_are_served_with_configured_root_path(monkeypatch, request_path):
    """Serve bundled static assets whether the proxy keeps or strips the prefix."""
    monkeypatch.setattr(main.app, "root_path", "/runner-manager")

    with TestClient(main.app) as client:
        response = client.get(request_path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


def test_lifespan_adds_protected_openapi_when_private(monkeypatch):
    """Validate Lifespan adds protected openapi when private."""
    orig_visibility = config.API_DOCS_VISIBILITY

    # Force private docs so both openapi_config and lifespan branch run
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")

    reloaded_main = importlib.reload(main)

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(reloaded_main.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(reloaded_main.background_manager, "stop_all_services", _noop)

    called = {}
    monkeypatch.setattr(
        reloaded_main,
        "setup_protected_openapi_routes",
        lambda app: called.setdefault("called", True),
    )

    # Ensure routers are re-included during startup
    if getattr(reloaded_main.app.state, "routers_included", False):
        reloaded_main.app.state.routers_included = False

    with TestClient(reloaded_main.app) as client:
        client.get("/")

    assert reloaded_main.openapi_config["docs_url"] is None
    assert reloaded_main.openapi_config["redoc_url"] is None
    assert reloaded_main.openapi_config["openapi_url"] is None
    assert called.get("called") is True

    # Restore visibility and module state for subsequent tests
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", orig_visibility)
    importlib.reload(main)


@pytest.mark.asyncio
async def test_lifespan_stops_background_services_on_shutdown(monkeypatch):
    """Validate Lifespan stops background services on shutdown."""
    events: list[str] = []

    async def _start():
        events.append("start")

    async def _stop():
        events.append("stop")

    monkeypatch.setattr(main.background_manager, "start_all_services", _start)
    monkeypatch.setattr(main.background_manager, "stop_all_services", _stop)

    app_stub = SimpleNamespace(state=SimpleNamespace(routers_included=True))

    async with main.lifespan(app_stub):
        assert events == ["start"]

    assert events == ["start", "stop"]
