"""Unit coverage for app.main helpers."""

from __future__ import annotations

import importlib
import signal

from fastapi.testclient import TestClient

from app import main
from app.core.config import config


def test_register_sighup_reload_handles_failure(monkeypatch):
    def raise_value_error(*_args, **_kwargs):
        raise ValueError("fail")

    monkeypatch.setattr(signal, "signal", raise_value_error)
    main._register_sighup_reload()


def test_lifespan_adds_protected_openapi_when_private(monkeypatch):
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
