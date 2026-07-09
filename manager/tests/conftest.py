"""Pytest configuration and fixtures for the Manager test suite."""

import asyncio
import os
import sys
import threading
import warnings
from typing import Any, Dict

import anyio.to_thread
import fastapi.testclient as fastapi_testclient
import httpx
import pytest
import starlette.testclient as starlette_testclient

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


async def _run_sync_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run anyio thread callbacks inline for deterministic sandboxed tests."""
    kwargs.pop("abandon_on_cancel", None)
    kwargs.pop("cancellable", None)
    kwargs.pop("limiter", None)
    return func(*args, **kwargs)


anyio.to_thread.run_sync = _run_sync_inline


def _run_async_blocking(awaitable: Any) -> Any:
    """Run an awaitable synchronously, even when another event loop is active."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
        """Execute the awaitable inside an isolated event loop thread."""
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=_runner)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else None


class _SyncASGITransport(httpx.BaseTransport):
    """Synchronous transport adapter around httpx ASGITransport."""

    def __init__(
        self,
        app: Any,
        *,
        raise_app_exceptions: bool,
        root_path: str,
        client: tuple[str, int],
    ) -> None:
        """Create the wrapped async ASGI transport."""
        self._transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
            root_path=root_path,
            client=client,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Dispatch a synchronous request through the async ASGI transport."""

        async def _send_request() -> httpx.Response:
            """Read the async response body before returning a sync response."""
            response = await self._transport.handle_async_request(request)
            body = await response.aread()
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=body,
                request=request,
                extensions=response.extensions,
            )

        return _run_async_blocking(_send_request())

    def close(self) -> None:
        """Close the wrapped async transport from synchronous test code."""
        _run_async_blocking(self._transport.aclose())


class _SyncTestClient(httpx.Client):
    """Thread-free ASGI test client for sandboxed CI environments."""

    __test__ = False

    def __init__(
        self,
        app: Any,
        base_url: str = "http://testserver",
        raise_server_exceptions: bool = True,
        root_path: str = "",
        backend: str = "asyncio",
        backend_options: dict[str, Any] | None = None,
        cookies: httpx._types.CookieTypes | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
        client: tuple[str, int] = ("testclient", 50000),
    ) -> None:
        """Create a synchronous test client using the custom ASGI transport."""
        del backend, backend_options

        self.app = app
        self.app_state: dict[str, Any] = {}
        self._lifespan_cm: Any | None = None
        if headers is None:
            headers = {}
        headers.setdefault("user-agent", "testclient")

        super().__init__(
            base_url=base_url,
            headers=headers,
            transport=_SyncASGITransport(
                app,
                raise_app_exceptions=raise_server_exceptions,
                root_path=root_path,
                client=client,
            ),
            follow_redirects=follow_redirects,
            cookies=cookies,
        )

    def __enter__(self) -> "_SyncTestClient":
        """Enter the app lifespan context when the test client is used as a context manager."""
        lifespan_context = getattr(getattr(self.app, "router", None), "lifespan_context", None)
        if lifespan_context is not None:
            self._lifespan_cm = lifespan_context(self.app)
            _run_async_blocking(self._lifespan_cm.__aenter__())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        """Exit the app lifespan context and close the client."""
        if self._lifespan_cm is not None:
            _run_async_blocking(self._lifespan_cm.__aexit__(exc_type, exc, tb))
            self._lifespan_cm = None
        self.close()


fastapi_testclient.TestClient = _SyncTestClient
starlette_testclient.TestClient = _SyncTestClient

# Silence stdlib crypt deprecation warnings when running with -W error
warnings.filterwarnings(
    "ignore",
    message="'crypt' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)
warnings.filterwarnings("ignore", message="Duplicate Operation ID.*", category=UserWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="fastapi.openapi.utils")

from app.core import state as state_module
from app.core.config import config


@pytest.fixture(autouse=True)
def ensure_test_tokens(monkeypatch):
    """Ensure at least one authorized token exists for tests."""

    original_tokens: Dict[str, str] = dict(config.AUTHORIZED_TOKENS)

    if not config.AUTHORIZED_TOKENS:
        monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"test": "test-token"})

    yield

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", original_tokens)


@pytest.fixture(autouse=True)
def isolate_deleted_task_tombstones(monkeypatch):
    """Keep tests independent from real deleted-task tombstones in ./data/.deleted."""

    monkeypatch.setattr(state_module.persistence, "get_deleted_task_ids", lambda: set())
    monkeypatch.setattr(state_module.persistence, "is_task_deleted", lambda _task_id: False)

    yield


@pytest.fixture(autouse=True)
def isolate_runtime_state(monkeypatch):
    """Isolate mutable runtime state and force development mode for deterministic tests."""

    original_tasks = dict(state_module.tasks)

    state_module.tasks.clear()

    monkeypatch.setattr(state_module, "IS_PRODUCTION", False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")

    yield

    state_module.tasks.clear()
    state_module.tasks.update(original_tasks)


@pytest.fixture
def auth_headers() -> Dict[str, str]:
    """Return bearer and API key headers using the first configured token."""

    token = next(iter(config.AUTHORIZED_TOKENS.values()))
    return {
        "Authorization": f"Bearer {token}",
        "X-API-Token": token,
    }


@pytest.fixture
def client(monkeypatch):
    """Test client with background services disabled for fast, deterministic runs."""

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import background_service

    async def _noop(*_, **__):
        """Replace background service startup/shutdown with a no-op."""
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    with TestClient(app) as test_client:
        yield test_client
