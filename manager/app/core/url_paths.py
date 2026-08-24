"""Helpers for URLs exposed behind an ASGI root path."""

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send


class RootPathProxyCompatibilityMiddleware:
    """Restore a proxy-stripped root path before Starlette dispatches mounted apps."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            root_path = str(scope.get("root_path", "") or "").rstrip("/")
            path = str(scope.get("path", "") or "")
            has_root_path = path == root_path or path.startswith(f"{root_path}/")

            if root_path and not has_root_path:
                scope = dict(scope)
                scope["path"] = f"{root_path}/{path.lstrip('/')}"

                raw_path = scope.get("raw_path")
                if isinstance(raw_path, bytes):
                    scope["raw_path"] = root_path.encode() + b"/" + raw_path.lstrip(b"/")

        await self.app(scope, receive, send)


def request_root_path(request: Request) -> str:
    """Return the normalized root path from an incoming ASGI request."""
    return str(request.scope.get("root_path", "") or "").rstrip("/")


def prefixed_path(request: Request, path: str) -> str:
    """Prefix one application path with the request root path."""
    normalized_path = f"/{path.lstrip('/')}"
    return f"{request_root_path(request)}{normalized_path}"


def cookie_path(request: Request) -> str:
    """Scope browser cookies to the configured root path when present."""
    return request_root_path(request) or "/"
