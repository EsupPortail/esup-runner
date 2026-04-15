#!/usr/bin/env python3
"""
Runtime checks for ESUP Runner Manager.

This script intentionally has no CLI parameters.
It reads manager settings from manager/.env (via app.core.config):
- MANAGER_URL (computed from MANAGER_PROTOCOL/HOST/PORT)
- one AUTHORIZED_TOKENS__* value for authenticated endpoints

Usage:
  uv run scripts/check_runtime.py

Exit codes:
  0: all required checks passed
  1: at least one required check failed
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx


@dataclass
class CheckResult:
    """Represents one runtime check outcome."""

    name: str
    ok: bool
    required: bool
    details: str = ""


def _repo_root() -> Path:
    """Return the manager project root directory."""
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    """Ensure manager root is available in ``sys.path`` for local imports."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_config() -> Any:
    """Load the shared manager config instance (and .env) from app.core.config."""
    _ensure_import_path()
    from app.core.config import get_config  # type: ignore

    return get_config()


def _mask_secret(value: str) -> str:
    """Return a redacted token-like value suitable for terminal output."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}***{value[-4:]}"


def _first_token(config: Any) -> Optional[str]:
    """Return the first configured authorized token, if any."""
    tokens = getattr(config, "AUTHORIZED_TOKENS", {}) or {}
    for token in tokens.values():
        token_text = str(token or "").strip()
        if token_text:
            return token_text
    return None


def _is_ip_literal(host: str) -> bool:
    """Return True when ``host`` is a valid IPv4/IPv6 literal."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve_host_ips(host: str) -> list[str]:
    """Resolve a hostname to unique IP strings using system DNS."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = sorted({info[4][0] for info in infos if info and info[4]})
    return ips


def _request_status(client: httpx.Client, url: str, token: Optional[str] = None) -> tuple[int, str]:
    """Perform a GET request and return ``(status_code, text)``.

    Returns status code 0 when a network error occurs.
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["X-API-Token"] = token

    try:
        response = client.get(url, headers=headers)
        return response.status_code, response.text
    except httpx.RequestError as exc:
        return 0, str(exc)


def _check_manager_url_host(manager_url: str) -> list[CheckResult]:
    """Validate MANAGER_URL host semantics and DNS reachability."""
    results: list[CheckResult] = []
    parts = urlsplit(manager_url)
    host = (parts.hostname or "").strip().lower()

    if not host:
        results.append(
            CheckResult(
                name="Manager URL Host Validity",
                ok=False,
                required=True,
                details=f"Invalid MANAGER_URL host in {manager_url!r}",
            )
        )
        return results

    if host in {"0.0.0.0", "::"}:
        results.append(
            CheckResult(
                name="Manager URL Host Reachability",
                ok=False,
                required=True,
                details=f"MANAGER_URL host {host!r} is not routable for remote runners",
            )
        )
        return results

    if host == "localhost":
        results.append(
            CheckResult(
                name="Manager URL Host Reachability",
                ok=False,
                required=False,
                details="MANAGER_URL uses localhost; remote runners on other machines cannot reach it",
            )
        )
        return results

    if _is_ip_literal(host):
        results.append(
            CheckResult(
                name="Manager URL Host Type",
                ok=True,
                required=True,
                details=f"Host is an IP literal ({host})",
            )
        )
        return results

    try:
        ips = _resolve_host_ips(host)
    except Exception as exc:
        results.append(
            CheckResult(
                name="Manager URL DNS Resolution",
                ok=False,
                required=True,
                details=f"DNS resolution failed for {host}: {exc}",
            )
        )
        return results

    if not ips:
        results.append(
            CheckResult(
                name="Manager URL DNS Resolution",
                ok=False,
                required=True,
                details=f"DNS resolution returned no IP for {host}",
            )
        )
        return results

    results.append(
        CheckResult(
            name="Manager URL DNS Resolution",
            ok=True,
            required=True,
            details=f"{host} -> {', '.join(ips)}",
        )
    )
    return results


def run_checks() -> tuple[list[CheckResult], dict[str, Any]]:
    """Run all runtime checks and return both results and display context."""
    config = _load_config()
    manager_url = str(getattr(config, "MANAGER_URL", "") or "").strip().rstrip("/")
    manager_host = str(getattr(config, "MANAGER_HOST", "") or "").strip()
    manager_bind_host = str(getattr(config, "MANAGER_BIND_HOST", "") or "").strip()
    manager_port = str(getattr(config, "MANAGER_PORT", "") or "").strip()
    token = _first_token(config)

    context: dict[str, Any] = {
        "manager_url": manager_url,
        "manager_host": manager_host,
        "manager_bind_host": manager_bind_host,
        "manager_port": manager_port,
        "token_masked": _mask_secret(token) if token else "(missing)",
        "manager_url_status": "configured" if manager_url else "missing",
        "manager_host_status": "configured" if manager_host else "missing",
        "manager_bind_host_status": "configured" if manager_bind_host else "missing",
        "manager_port_status": "configured" if manager_port else "missing",
        "api_token_status": "configured" if token else "missing",
    }

    results: list[CheckResult] = []

    if not manager_url:
        results.append(
            CheckResult(
                name="Manager URL Configured",
                ok=False,
                required=True,
                details="MANAGER_URL is empty",
            )
        )
        return results, context

    results.extend(_check_manager_url_host(manager_url))

    if not token:
        results.append(
            CheckResult(
                name="Authorized Token Configured",
                ok=False,
                required=True,
                details="No AUTHORIZED_TOKENS__* value found in manager configuration",
            )
        )

    timeout = httpx.Timeout(5.0, connect=5.0)
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        admin_code, admin_details = _request_status(client, f"{manager_url}/admin")
        admin_ok_codes = {200, 401, 301, 302, 303, 307, 308}
        results.append(
            CheckResult(
                name="Admin Endpoint Reachability",
                ok=admin_code in admin_ok_codes,
                required=True,
                details=f"HTTP {admin_code}: {admin_details[:200]}",
            )
        )

        if token:
            version_code, version_details = _request_status(
                client, f"{manager_url}/api/version", token
            )
            results.append(
                CheckResult(
                    name="API Version Endpoint",
                    ok=version_code == 200,
                    required=True,
                    details=f"HTTP {version_code}: {version_details[:200]}",
                )
            )

            health_code, health_details = _request_status(
                client, f"{manager_url}/manager/health", token
            )
            results.append(
                CheckResult(
                    name="Manager Health Endpoint",
                    ok=health_code == 200,
                    required=True,
                    details=f"HTTP {health_code}: {health_details[:200]}",
                )
            )

            runners_code, runners_details = _request_status(
                client, f"{manager_url}/api/runners", token
            )
            runners_ok = runners_code == 200
            runners_count: Optional[int] = None
            if runners_ok:
                try:
                    payload = json.loads(runners_details)
                    data = payload.get("runners", []) if isinstance(payload, dict) else []
                    runners_count = len(data) if isinstance(data, list) else 0
                except Exception:
                    runners_count = None

            details = f"HTTP {runners_code}: {runners_details[:200]}"
            if runners_count is not None:
                details += f" | registered_runners={runners_count}"
                results.append(
                    CheckResult(
                        name="At Least One Runner Registered",
                        ok=runners_count > 0,
                        required=False,
                        details=details,
                    )
                )

            results.append(
                CheckResult(
                    name="Runners Endpoint",
                    ok=runners_ok,
                    required=True,
                    details=details,
                )
            )

    return results, context


def print_report(results: list[CheckResult], context: dict[str, Any]) -> None:
    """Print a human-readable report in a style similar to check_version.py."""
    width = 60

    print("=" * width)
    print("Runtime Check - ESUP Runner Manager")
    print("=" * width)
    print()

    print("Running: Configuration")
    print("-" * width)
    print(f"MANAGER_URL       : {context.get('manager_url_status')} (value hidden)")
    print(f"MANAGER_HOST      : {context.get('manager_host_status')} (value hidden)")
    print(f"MANAGER_BIND_HOST : {context.get('manager_bind_host_status')} (value hidden)")
    print(f"MANAGER_PORT      : {context.get('manager_port_status')} (value hidden)")
    print(f"API token         : {context.get('api_token_status')} (value hidden)")

    for result in results:
        print()
        print(f"Running: {result.name}")
        print("-" * width)
        requirement = "Required" if result.required else "Optional"
        if result.ok:
            print("✓ Check passed")
        elif result.required:
            print("✗ FAILED")
        else:
            print("✗ WARNING")
        print(f"  Type: {requirement}")
        if result.details:
            print(f"  Details: {result.details}")

    required_failures = [r for r in results if r.required and not r.ok]
    warnings = [r for r in results if (not r.required) and (not r.ok)]
    passed = len(results) - len(required_failures) - len(warnings)
    print()
    print("=" * width)
    print(f"Results: {passed} passed, {len(required_failures)} failed, {len(warnings)} warning")
    print("=" * width)


def main() -> int:
    """CLI entry point returning shell-compatible exit code."""
    try:
        results, context = run_checks()
    except Exception as exc:
        print(f"[FAIL] Runtime check failed to start: {exc}")
        return 1

    print_report(results, context)
    has_required_failure = any((not r.ok) and r.required for r in results)
    return 1 if has_required_failure else 0


if __name__ == "__main__":
    sys.exit(main())
