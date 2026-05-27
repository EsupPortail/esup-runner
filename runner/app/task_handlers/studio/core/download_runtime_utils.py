"""Secure source download helpers for studio runtime."""

from __future__ import annotations

import os
import urllib.parse
import urllib.request
from typing import Callable


def download_allowed_hosts_from_env() -> list[str]:
    """Return the configured allowlist of download hosts."""
    allowed_hosts_raw = os.getenv("DOWNLOAD_ALLOWED_HOSTS", "")
    return [
        host.strip().lower().rstrip(".") for host in allowed_hosts_raw.split(",") if host.strip()
    ]


def download_allow_private_networks_from_env() -> bool:
    """Return whether private-network downloads are allowed."""
    allow_private_raw = os.getenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    return allow_private_raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Return whether a host matches the configured allowlist."""
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def host_resolves_to_public_ip(host: str) -> tuple[bool, str]:
    """Check whether a host resolves only to public IP addresses."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos if info and info[4]})
    except Exception:
        ips = []

    if not ips:
        return False, "Download host cannot be resolved"

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"Download host resolved to invalid address: {ip}"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False, f"Download host resolves to private address: {ip}"

    return True, ""


def download_http_source(
    url: str,
    work_dir: str,
    label: str,
    parsed: urllib.parse.ParseResult,
) -> str:
    """Download an HTTP(S) source into the work directory."""
    os.makedirs(work_dir, exist_ok=True)
    base = os.path.basename(parsed.path) or f"{label}.mp4"
    base = base.split("?")[0] or f"{label}.mp4"
    local_path = os.path.join(work_dir, base)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path

    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        with open(local_path, "wb") as file_handle:
            file_handle.write(data)
        print(f"Downloaded remote source to {local_path}")
        return local_path
    except Exception as exc:
        print(f"Failed to download remote source {url}: {exc}")
        return url


def materialize_source(
    url: str | None,
    work_dir: str,
    label: str,
    *,
    download_allowed_hosts_from_env_fn: Callable[[], list[str]] = download_allowed_hosts_from_env,
    download_allow_private_networks_from_env_fn: Callable[
        [], bool
    ] = download_allow_private_networks_from_env,
    host_is_allowed_fn: Callable[[str, list[str]], bool] = host_is_allowed,
    host_resolves_to_public_ip_fn: Callable[[str], tuple[bool, str]] = host_resolves_to_public_ip,
    download_http_source_fn: Callable[[str, str, str, urllib.parse.ParseResult], str] = (
        download_http_source
    ),
) -> str | None:
    """Download remote URL to work_dir if it is HTTP(S); otherwise return original path."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme not in ("http", "https"):
        print(f"Unsupported URL scheme for {label}: {scheme}")
        return None
    if scheme not in ("http", "https"):
        return url

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        print(f"Invalid source URL host for {label}")
        return None

    allowed_hosts = download_allowed_hosts_from_env_fn()
    if allowed_hosts and not host_is_allowed_fn(host, allowed_hosts):
        print(f"Download host not allowed for {label}: {host}")
        return None

    allow_private = download_allow_private_networks_from_env_fn()
    if not allow_private and host in {"localhost"}:
        print(f"Download host not allowed for {label}: {host}")
        return None

    if not allow_private:
        is_public, reason = host_resolves_to_public_ip_fn(host)
        if not is_public:
            print(f"{reason} for {label}: {host}")
            return None

    return download_http_source_fn(url, work_dir, label, parsed)
