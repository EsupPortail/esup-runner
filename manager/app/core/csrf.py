"""CSRF protection for the Basic-authenticated administration interface."""

import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.auth import _openapi_cookie_secret
from app.core.config import config

CSRF_MAX_AGE_SECONDS = 8 * 60 * 60
_TOKEN_PATTERN = re.compile(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}")


def _signature(request: Request, payload: str) -> str:
    """Bind a token to this administrator and deployment without per-worker state."""
    message = "\n".join(
        (
            "admin-csrf:v1",
            config.MANAGER_PUBLIC_URL,
            request.headers.get("authorization", ""),
            payload,
        )
    )
    return hmac.new(_openapi_cookie_secret(), message.encode(), hashlib.sha256).hexdigest()


def build_csrf_token(request: Request) -> str:
    """Issue an expiring, randomized token for an authenticated admin page."""
    payload = f"{int(time.time())}.{secrets.token_hex(16)}"
    return f"{payload}.{_signature(request, payload)}"


def csrf_template_context(request: Request) -> dict[str, str]:
    """Supply forms and JavaScript with the same token for this page rendering."""
    return {"csrf_token": build_csrf_token(request)}


def _valid_token(request: Request, token: str) -> bool:
    if not _TOKEN_PATTERN.fullmatch(token):
        return False
    timestamp, nonce, signature = token.split(".")
    age = int(time.time()) - int(timestamp)
    if not 0 <= age < CSRF_MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(signature, _signature(request, f"{timestamp}.{nonce}"))


def _url_origin(value: str) -> tuple[str, str, int] | None:
    """Normalize only a URL's origin; proxy paths do not belong to the origin."""
    if "\\" in value or any(character.isspace() for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        return parsed.scheme, parsed.hostname.lower(), port
    except ValueError:
        return None


async def verify_csrf(request: Request) -> None:
    """Reject unsafe admin requests before any handler side effects.

    Routes must also depend on verify_admin. API token authentication and runner
    callbacks do not use this dependency.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return

    expected_origin = _url_origin(config.MANAGER_PUBLIC_URL)
    source = request.headers.get("origin", request.headers.get("referer", ""))
    if (
        expected_origin is None
        or _url_origin(source) != expected_origin
        or request.headers.get("sec-fetch-site") == "cross-site"
    ):
        raise HTTPException(status_code=403, detail="Invalid request origin")

    token = request.headers.get("x-csrf-token")
    if token is None:
        form = await request.form()
        value = form.get("csrf_token")
        token = value if isinstance(value, str) else ""
    if not _valid_token(request, token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token; reload the page and retry")
