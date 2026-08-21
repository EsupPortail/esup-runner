"""Helpers for URLs exposed behind an ASGI root path."""

from fastapi import Request


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
