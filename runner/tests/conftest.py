"""Pytest configuration for adding the project root to sys.path."""

import asyncio
import os
import sys
import threading
from typing import Any

import anyio.to_thread
import httpx
from fastapi.testclient import TestClient

# Ensure the repository root (containing the `app` package) is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


async def _run_sync_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
    kwargs.pop("abandon_on_cancel", None)
    kwargs.pop("cancellable", None)
    kwargs.pop("limiter", None)
    return func(*args, **kwargs)


anyio.to_thread.run_sync = _run_sync_inline


def _run_async_blocking(awaitable: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: list[Any] = []
    error: list[BaseException] = []

    def _runner() -> None:
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
    def __init__(
        self,
        app: Any,
        *,
        raise_app_exceptions: bool,
        root_path: str,
        client: tuple[str, int],
    ) -> None:
        self._transport = httpx.ASGITransport(
            app=app,
            raise_app_exceptions=raise_app_exceptions,
            root_path=root_path,
            client=client,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        async def _send_request() -> httpx.Response:
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
        _run_async_blocking(self._transport.aclose())


def _sync_test_client_init(
    self: TestClient,
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
    del backend, backend_options

    self.app = app
    self.app_state = {}
    if headers is None:
        headers = {}
    headers.setdefault("user-agent", "testclient")

    httpx.Client.__init__(
        self,
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


def _sync_test_client_enter(self: TestClient) -> TestClient:
    return self


def _sync_test_client_exit(
    self: TestClient,
    exc_type: type[BaseException] | None,
    exc: BaseException | None,
    tb: Any,
) -> None:
    self.close()


TestClient.__init__ = _sync_test_client_init  # type: ignore[method-assign]
TestClient.__enter__ = _sync_test_client_enter  # type: ignore[method-assign]
TestClient.__exit__ = _sync_test_client_exit  # type: ignore[method-assign]
