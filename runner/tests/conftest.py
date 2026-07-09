"""Pytest configuration for adding the project root to sys.path."""

import asyncio
import os
import sys
import threading
from typing import Any

import anyio.to_thread
import fastapi.testclient as fastapi_testclient
import httpx
import starlette.testclient as starlette_testclient

# Ensure the repository root (containing the `app` package) is on sys.path
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
        """Return the test client for context-manager compatibility."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        """Close the client when leaving the context manager."""
        self.close()


fastapi_testclient.TestClient = _SyncTestClient
starlette_testclient.TestClient = _SyncTestClient
