# manager/app/core/auth.py
"""Authentication module for runner management API.

Handles API token verification and dependency injection for protected endpoints.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Annotated, Optional

from fastapi import Cookie, Depends, HTTPException, Query, status
from fastapi.security import (
    APIKeyHeader,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.__version__ import __version__ as MANAGER_VERSION
from app.core import config as config_module
from app.core.config import config
from app.core.setup_logging import setup_default_logging

# Configure logging
logger = setup_default_logging()


_SEMVER_MAJOR_MINOR_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)")


def _parse_major_minor(version: str) -> tuple[int, int]:
    """Extract (major, minor) from a semver-ish string.

    Accepts values like `1.0.0`, `1.0`, `v1.0.1`, `1.0.0-alpha+1`.
    """

    candidate = (version or "").strip()
    match = _SEMVER_MAJOR_MINOR_RE.match(candidate)
    if not match:
        raise ValueError(f"Invalid version format: {version!r}")
    return int(match.group("major")), int(match.group("minor"))


def _mask_token(token: Optional[str], show_start: int = 4, show_end: int = 4) -> str:
    if not token:
        return "<empty>"
    if len(token) <= show_start + show_end:
        if len(token) <= 2:
            return "***"
        return f"{token[:1]}***{token[-1:]}"
    return f"{token[:show_start]}...{token[-show_end:]}"


def _refresh_config_if_needed() -> None:
    """Apply cross-worker config reloads before auth checks."""
    try:
        config_module.reload_config_if_signaled()
    except Exception as exc:
        logger.warning(f"Failed to refresh config from reload marker: {exc}")


# Authentication Bearer or X-API token
bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

# Handler for runner version header
version_header = APIKeyHeader(name="X-Runner-Version", auto_error=False)

OPENAPI_TOKEN_COOKIE_NAME = "openapi_token"


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padded = value + ("=" * ((4 - len(value) % 4) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _openapi_cookie_secret() -> bytes:
    explicit_secret = (getattr(config, "OPENAPI_COOKIE_SECRET", "") or "").strip()
    if explicit_secret:
        return explicit_secret.encode("utf-8")

    digest = hashlib.sha256()
    digest.update(b"openapi-cookie-secret:v1")
    for token_name, token_value in sorted(config.AUTHORIZED_TOKENS.items()):
        digest.update(token_name.encode("utf-8"))
        digest.update(b"=")
        digest.update(token_value.encode("utf-8"))
        digest.update(b";")
    for username, password_hash in sorted(config.ADMIN_USERS.items()):
        digest.update(username.encode("utf-8"))
        digest.update(b"=")
        digest.update(password_hash.encode("utf-8"))
        digest.update(b";")
    return digest.digest()


def _resolve_token_name(token: str) -> Optional[str]:
    for token_name, token_value in config.AUTHORIZED_TOKENS.items():
        if hmac.compare_digest(token, token_value):
            return token_name
    return None


def build_openapi_cookie_value(token: str) -> Optional[str]:
    """Build a signed opaque cookie value for OpenAPI docs auth."""
    token_name = _resolve_token_name(token)
    if not token_name:
        return None

    now = int(time.time())
    payload = {
        "v": 1,
        "t": token_name,
        "iat": now,
        "exp": now + config.OPENAPI_COOKIE_MAX_AGE_SECONDS,
        "jti": secrets.token_urlsafe(8),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(_openapi_cookie_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def _decode_openapi_cookie_parts(cookie_value: str) -> Optional[tuple[bytes, bytes]]:
    try:
        payload_b64, signature_b64 = cookie_value.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        signature = _b64url_decode(signature_b64)
        return payload_bytes, signature
    except Exception:
        return None


def _is_valid_openapi_cookie_signature(payload_bytes: bytes, signature: bytes) -> bool:
    expected_signature = hmac.new(_openapi_cookie_secret(), payload_bytes, hashlib.sha256).digest()
    return hmac.compare_digest(signature, expected_signature)


def _load_openapi_cookie_payload(payload_bytes: bytes) -> Optional[dict]:
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _extract_openapi_token_name(payload: dict) -> Optional[str]:
    if payload.get("v") != 1:
        return None

    token_name = payload.get("t")
    if not isinstance(token_name, str) or not token_name:
        return None

    expires_at = payload.get("exp")
    if not isinstance(expires_at, int):
        return None
    if expires_at <= int(time.time()):
        return None

    return token_name


def resolve_openapi_cookie_token(cookie_value: str) -> Optional[str]:
    """Resolve and validate signed OpenAPI cookie, returning token value when valid."""
    if not cookie_value:
        return None

    decoded_parts = _decode_openapi_cookie_parts(cookie_value)
    if not decoded_parts:
        return None
    payload_bytes, signature = decoded_parts

    if not _is_valid_openapi_cookie_signature(payload_bytes, signature):
        return None

    payload = _load_openapi_cookie_payload(payload_bytes)
    if not payload:
        return None

    token_name = _extract_openapi_token_name(payload)
    if not token_name:
        return None

    return config.AUTHORIZED_TOKENS.get(token_name)


async def verify_openapi_token(
    token_query: Annotated[Optional[str], Query(alias="token")] = None,
    token_cookie: Annotated[Optional[str], Cookie(alias=OPENAPI_TOKEN_COOKIE_NAME)] = None,
    api_token: Annotated[Optional[str], Depends(api_key_header)] = None,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
) -> Optional[str]:
    """
    Verify token for OpenAPI documentation access if authentication is enabled.

    This function checks if OpenAPI authentication is enabled in config.
    If disabled, it allows access without token verification.
    If enabled, it verifies the token using the same logic as verify_token.

    The token can be provided in four ways (in order of priority):
    1. X-API-Token header
    2. Authorization Bearer header
    3. HttpOnly cookie set by the admin docs page
    4. Query parameter: ?token=xxx (only when OPENAPI_ALLOW_QUERY_TOKEN=true)

    Args:
        token_query: The API token from query parameter (?token=xxx)
        token_cookie: The API token from OpenAPI auth cookie
        api_token: The API token extracted from the X-API-Token header
        credentials: For HTTP authorization Bearer

    Returns:
        Optional[str]: The validated token or None if authentication is disabled

    Raises:
        HTTPException: 401 error if token is missing or invalid when authentication is enabled
    """
    _refresh_config_if_needed()

    # If OpenAPI authentication is disabled, allow access
    if config.API_DOCS_VISIBILITY == "public":
        return None

    # If authentication is enabled, verify the token
    # Priority (default): X-API-Token header > Bearer token > OpenAPI auth cookie > query (optional)
    token = None
    if api_token:
        token = api_token
    elif credentials:
        token = credentials.credentials
    elif token_cookie:
        token = resolve_openapi_cookie_token(token_cookie)
    elif token_query and config.OPENAPI_ALLOW_QUERY_TOKEN:
        token = token_query

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token for OpenAPI access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if token is in authorized tokens (constant-time comparison)
    authorized = False
    for token_value in config.AUTHORIZED_TOKENS.values():
        if hmac.compare_digest(token, token_value):
            authorized = True
            break

    if not authorized:
        logger.warning("Unauthorized OpenAPI access attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token for OpenAPI access",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


# Basic authentication
security = HTTPBasic()


async def verify_admin(credentials: HTTPBasicCredentials = Depends(security)) -> bool:
    """
    Verify admin credentials using Basic Auth.
    """
    _refresh_config_if_needed()

    stored_hash = config.ADMIN_USERS.get(credentials.username)
    if not stored_hash or not config.pwd_context.verify(credentials.password, stored_hash):
        logger.info("Invalid admin credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    return True


async def verify_token(
    api_token: Annotated[Optional[str], Depends(api_key_header)] = None,
    credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)] = None,
) -> str:
    """
    Verify the validity of the provided API token, from X-API-Token headers or Authorization Bearer.

    Args:
        api_token: The API token extracted from the request header
        credentials: For HTTP authorisation Bearer

    Returns:
        str: The validated token

    Raises:
        HTTPException: 401 error if token is missing or invalid
    """
    _refresh_config_if_needed()

    if api_token:
        # X-API-Token header priority
        token = api_token
    elif credentials:
        # Fallback on Authorization Bearer header
        token = credentials.credentials
    else:
        token = None

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if token is in authorized tokens (constant-time comparison)
    authorized = False
    for token_value in config.AUTHORIZED_TOKENS.values():
        if hmac.compare_digest(token, token_value):
            authorized = True
            break

    if not authorized:
        logger.warning("Unauthorized token attempt")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


async def verify_runner_version(
    runner_version: Annotated[Optional[str], Depends(version_header)] = None,
) -> str:
    """
    Verify if the runner version matches the manager version at MAJOR+MINOR level.

    The runner must send its version in the X-Runner-Version header.
    This function compares it with the manager's version and returns
    the runner version if they match.

    Args:
        runner_version: The version string sent by the runner in X-Runner-Version header

    Returns:
        str: The validated runner version

    Raises:
        HTTPException: 400 if version header is missing
        HTTPException: 409 if versions don't match
    """
    if not runner_version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Runner-Version header. Runner must send its version.",
        )

    runner_version = runner_version.strip()

    try:
        runner_major, runner_minor = _parse_major_minor(runner_version)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid X-Runner-Version format. Expected something like 'MAJOR.MINOR.PATCH' (e.g. 1.0.0)."
            ),
        )

    try:
        manager_major, manager_minor = _parse_major_minor(MANAGER_VERSION)
    except ValueError:
        logger.error("Invalid MANAGER_VERSION format: %r", MANAGER_VERSION)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Manager version is invalid; cannot verify runner compatibility.",
        )

    if (runner_major, runner_minor) != (manager_major, manager_minor):
        logger.warning(
            "Version mismatch: runner %s != manager %s (expected %s.%s.x)",
            runner_version,
            MANAGER_VERSION,
            manager_major,
            manager_minor,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Version mismatch: Runner version {runner_version} does not match Manager version {MANAGER_VERSION} "
                f"(expected {manager_major}.{manager_minor}.x)"
            ),
        )

    logger.debug(
        "Runner version %s matches Manager version %s (MAJOR.MINOR)",
        runner_version,
        MANAGER_VERSION,
    )
    return runner_version
