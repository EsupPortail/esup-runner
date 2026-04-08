# manager/app/api/routes/runner.py
"""
Runners routes for Runner Manager.
Handles endpoints for runners.
"""

import ipaddress
import socket
from datetime import datetime
from urllib.parse import ParseResult, urlparse, urlunparse

from fastapi import APIRouter, Depends, HTTPException, Path, status

from app.core.auth import verify_runner_version, verify_token
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import runners
from app.models.models import Runner

# Configure logging
logger = setup_default_logging()

# Create API router
router = APIRouter(prefix="/runner", tags=["Runner"])

# ======================================================
# Utility Functions
# ======================================================


def verify_runner_token(runner_id: str, token: str) -> bool:
    """
    Verify that a token is valid for a specific runner.

    Args:
        runner_id: Unique identifier of the runner
        token: Authentication token to verify

    Returns:
        bool: True if token is valid for the runner, False otherwise
    """
    if runner_id not in runners:
        return False

    runner = runners[runner_id]
    is_valid: bool = runner.token == token
    return is_valid


def _host_matches_allowlist(host: str, allowed_hosts: list[str]) -> bool:
    """Return True when host matches one allowlist entry (exact/subdomain)."""
    normalized_host = (host or "").strip().lower().rstrip(".")
    if not normalized_host:
        return False

    for allowed in allowed_hosts:
        normalized_allowed = (allowed or "").strip().lower().rstrip(".")
        if not normalized_allowed:
            continue
        if normalized_host == normalized_allowed or normalized_host.endswith(
            "." + normalized_allowed
        ):
            return True
    return False


def _is_disallowed_ip(ip: str) -> bool:
    """Return True when IP should be blocked for runner registration URL."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_host_ips(host: str) -> list[str]:
    """Resolve all IPs for a host using system DNS."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = sockaddr[0]
        if isinstance(ip, str):
            ips.add(ip)
    return sorted(ips)


def _parse_and_validate_runner_url(url: str) -> ParseResult:
    """Parse and validate structural constraints for runner URL."""
    if not url:
        raise HTTPException(status_code=400, detail="Runner URL is empty")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="Runner URL must use http or https")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="Runner URL is missing host")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Runner URL must not include userinfo")
    if parsed.query or parsed.fragment or parsed.params:
        raise HTTPException(status_code=400, detail="Runner URL must not include query or fragment")
    if parsed.path not in {"", "/"}:
        raise HTTPException(status_code=400, detail="Runner URL must not include a path")
    return parsed


def _normalize_and_validate_runner_host(parsed: ParseResult) -> str:
    """Return normalized host from parsed URL and validate it is usable."""
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail="Runner URL has invalid host")
    return host


def _validate_runner_host_allowlist(host: str) -> None:
    """Validate host against optional configured allowlist."""
    allowed_hosts = getattr(config, "RUNNER_URL_ALLOWED_HOSTS", []) or []
    if allowed_hosts and not _host_matches_allowlist(host, allowed_hosts):
        raise HTTPException(status_code=400, detail="Runner URL host not allowed")


def _resolve_host_ips_or_raise(host: str) -> list[str]:
    """Resolve host IPs and raise a standard HTTPException on failure."""
    try:
        ips = _resolve_host_ips(host)
    except Exception:
        raise HTTPException(status_code=400, detail="Runner URL host cannot be resolved")
    if not ips:
        raise HTTPException(status_code=400, detail="Runner URL host cannot be resolved")
    return ips


def _validate_runner_network_policy(host: str) -> None:
    """Enforce private-network policy for runner URL host."""
    allow_private = getattr(config, "RUNNER_URL_ALLOW_PRIVATE_NETWORKS", True)
    if allow_private:
        return
    if host == "localhost":
        raise HTTPException(status_code=400, detail="Runner URL host not allowed")
    ips = _resolve_host_ips_or_raise(host)
    for ip in ips:
        if _is_disallowed_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="Runner URL resolves to a private/loopback/link-local address",
            )


def _extract_runner_port(parsed: ParseResult) -> int | None:
    """Return parsed URL port, raising a normalized validation error if invalid."""
    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="Runner URL has invalid port")
    return port


def _build_runner_origin(scheme: str, host: str, port: int | None) -> str:
    """Build normalized origin (scheme://host[:port]) from components."""
    host_for_netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = f"{host_for_netloc}:{port}" if port is not None else host_for_netloc
    return urlunparse((scheme, netloc, "", "", "", ""))


def _validate_and_normalize_runner_url(url: str) -> str:
    """Validate and normalize runner base URL."""
    parsed = _parse_and_validate_runner_url(url)
    host = _normalize_and_validate_runner_host(parsed)
    _validate_runner_host_allowlist(host)
    _validate_runner_network_policy(host)
    port = _extract_runner_port(parsed)
    return _build_runner_origin(parsed.scheme, host, port)


# ======================================================
# Endpoints
# ======================================================


@router.post(
    "/register",
    response_model=dict,
    summary="Register a runner",
    description="Register a new runner with the manager",
    tags=["Runner"],
    responses={
        200: {"description": "Runner registered successfully"},
        403: {"description": "Token not authorized to register runners"},
    },
)
async def register_runner(
    runner: Runner,
    current_token: str = Depends(verify_token),
    current_version: str = Depends(verify_runner_version),
) -> dict:
    """
    Register a new runner with the manager.

    Args:
        runner: Runner instance to register
        current_token: Authenticated runner token
        current_version: Verified runner version

    Returns:
        dict: Registration status

    Raises:
        HTTPException: If token is not authorized
    """
    runner.url = _validate_and_normalize_runner_url(runner.url)
    runner.last_heartbeat = datetime.now()
    runner.token = current_token
    runner.version = current_version
    runners[runner.id] = runner

    logger.info(f"Runner v{runner.version} registered: {runner.id} - {runner.url}")
    return {"status": "registered"}


@router.post(
    "/heartbeat/{runner_id}",
    response_model=dict,
    summary="Send heartbeat",
    description="Endpoint for runners to signal they are still active",
    tags=["Runner"],
    dependencies=[Depends(verify_token), Depends(verify_runner_version)],
)
async def runner_heartbeat(
    runner_id: str = Path(..., description="Runner identifier"),
    current_token: str = Depends(verify_token),
    current_version: str = Depends(verify_runner_version),
) -> dict:
    """
    Update runner heartbeat to indicate it's still active.

    Args:
        runner_id: Unique identifier of the runner
        current_token: Authenticated runner token

    Returns:
        dict: Success status

    Raises:
        HTTPException: If runner not found or token invalid
    """
    runner = runners.get(runner_id)
    if runner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Runner not found")

    if runner.token != current_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Token not authorized for this runner"
        )

    runner.last_heartbeat = datetime.now()
    runners[runner_id] = runner
    return {"status": "ok"}
