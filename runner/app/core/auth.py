# runner/app/core/auth.py
"""
Authentication module for runner.
Handles API token verification and dependency injection for protected endpoints.
"""

import hmac
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import config

# API Key header configuration
bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Token", auto_error=False)


async def verify_token(
    api_token: Optional[str] = Depends(api_key_header),
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    """
    Verify the validity of the provided API token.

    Args:
        api_token: The API token extracted from the request header

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

    # Only accept the per-runner token.
    if not hmac.compare_digest(token, config.RUNNER_TOKEN):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return token


def get_current_manager(token: str = Depends(verify_token)) -> str:
    """
    FastAPI dependency to protect endpoints with token authentication.
    Dependency to verify manager authentication.

    Args:
        token: Verified token from request dependency injection

    Returns:
        str: The validated token for use in endpoint functions
    """
    return token
