# manager/app/core/auth.py
"""Authentication module for runner management API.

Handles API token verification and dependency injection for protected endpoints.
"""

import hmac
import re
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import (
    APIKeyHeader,
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)

from app.__version__ import __version__ as MANAGER_VERSION
from app.core.config import config
from app.core.setup_logging import setup_default_logging

# Configure logging
logger = setup_default_logging()


_SEMVER_MAJOR_MINOR_RE = re.compile(r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)")


def _parse_major_minor(version: str) -> tuple[int, int]:
    """Extract (major, minor) from a semver-ish string.

    Accepts values like `0.9.0`, `0.9`, `v0.9.1`, `0.9.0-alpha+1`.
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


# Authentication Bearer or X-API token
bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)

# Handler for runner version header
version_header = APIKeyHeader(name="X-Runner-Version", auto_error=False)


async def verify_openapi_token(
    token_query: Optional[str] = Query(None, alias="token"),
    api_token: Optional[str] = Depends(api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[str]:
    """
    Verify token for OpenAPI documentation access if authentication is enabled.

    This function checks if OpenAPI authentication is enabled in config.
    If disabled, it allows access without token verification.
    If enabled, it verifies the token using the same logic as verify_token.

    The token can be provided in three ways (in order of priority):
    1. Query parameter: ?token=xxx
    2. X-API-Token header
    3. Authorization Bearer header

    Args:
        token_query: The API token from query parameter (?token=xxx)
        api_token: The API token extracted from the X-API-Token header
        credentials: For HTTP authorization Bearer

    Returns:
        Optional[str]: The validated token or None if authentication is disabled

    Raises:
        HTTPException: 401 error if token is missing or invalid when authentication is enabled
    """
    # If OpenAPI authentication is disabled, allow access
    if config.API_DOCS_VISIBILITY == "public":
        return None

    # If authentication is enabled, verify the token
    # Priority (default): X-API-Token header > Bearer token > query parameter (optional)
    token = None
    if api_token:
        token = api_token
    elif credentials:
        token = credentials.credentials
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
    for token_name, token_value in config.AUTHORIZED_TOKENS.items():
        if hmac.compare_digest(token, token_value):
            authorized = True
            break

    if not authorized:
        logger.info(
            "Unauthorized OpenAPI access attempt with token: %s",
            _mask_token(token),
        )
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
    api_token: Optional[str] = Depends(api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
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
    for token_name, token_value in config.AUTHORIZED_TOKENS.items():
        if hmac.compare_digest(token, token_value):
            authorized = True
            break

    if not authorized:
        logger.info("Unauthorized token attempt: %s", _mask_token(token))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


async def verify_runner_version(
    runner_version: Optional[str] = Depends(version_header),
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
                "Invalid X-Runner-Version format. Expected something like 'MAJOR.MINOR.PATCH' (e.g. 0.9.0)."
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
