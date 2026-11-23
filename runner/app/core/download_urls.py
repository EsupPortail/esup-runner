"""Validate outbound source and asset URLs before each HTTP request."""

import ipaddress
import socket
import urllib.request
from http.client import HTTPMessage, HTTPResponse
from typing import IO, Callable, cast
from urllib.parse import urljoin, urlsplit

import requests  # type: ignore[import-untyped]

from app.core.config import config

_MAX_REDIRECTS = 10
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class DownloadURLRejected(ValueError):
    """Raised when a download URL or redirect violates the download policy."""


def host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Match an allowed hostname or one of its subdomains."""
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def host_resolves_to_public_ip(host: str) -> tuple[bool, str]:
    """Check that all addresses returned by system DNS are public."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except Exception:
        infos = []
    if not infos:
        return False, "Download host cannot be resolved"

    for info in infos:
        ip = info[4][0]
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"Download host resolved to invalid address: {ip}"
        if not address.is_global or address.is_multicast or address.is_reserved:
            return False, f"Download host resolves to private address: {ip}"

    return True, ""


def validate_download_url(url: str) -> None:
    """Reject unsupported requests and enforce the configured download host policy."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise DownloadURLRejected("Invalid download URL") from exc

    if parsed.scheme not in {"http", "https"}:
        raise DownloadURLRejected("Only http/https URLs are supported for downloads")
    if parsed.username is not None or any(char in parsed.netloc for char in "\\%"):
        raise DownloadURLRejected("Invalid download URL authority")
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        not host
        or not host.isascii()
        or any(char.isspace() or ord(char) < 32 for char in parsed.netloc)
    ):
        raise DownloadURLRejected("Invalid download URL host")
    allowed_hosts = [allowed.rstrip(".") for allowed in config.DOWNLOAD_ALLOWED_HOSTS]
    if allowed_hosts and not host_is_allowed(host, allowed_hosts):
        raise DownloadURLRejected("Download URL host not allowed")
    if not config.DOWNLOAD_ALLOW_PRIVATE_NETWORKS:
        public, reason = host_resolves_to_public_ip(parsed.hostname or host)
        if not public:
            raise DownloadURLRejected(reason)


def get_download_response(
    get: Callable[..., requests.Response], url: str, *, timeout: tuple[int, int]
) -> requests.Response:
    """Return the streamed response after following only validated redirects."""
    for _ in range(_MAX_REDIRECTS + 1):
        try:
            prepared_url = requests.Request("GET", url).prepare().url
        except requests.exceptions.RequestException as exc:
            raise DownloadURLRejected("Invalid download URL") from exc
        if prepared_url is None:
            raise DownloadURLRejected("Invalid download URL")
        validate_download_url(prepared_url)
        response = get(prepared_url, timeout=timeout, stream=True, allow_redirects=False)
        if response.status_code not in _REDIRECT_STATUSES or not (
            location := response.headers.get("Location")
        ):
            return response
        with response:
            try:
                url = urljoin(response.url, location)
            except ValueError as exc:
                raise DownloadURLRejected("Invalid download redirect URL") from exc

    raise DownloadURLRejected("Too many download redirects")


class _DownloadRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Apply the download policy to urllib redirects before following them."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Validate the resolved redirect URL before building the next request."""
        try:
            validate_download_url(newurl)
        except DownloadURLRejected:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_download(request: str | urllib.request.Request, *, timeout: int) -> HTTPResponse:
    """Open a download with policy checks on its initial URL and redirects."""
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    validate_download_url(url)
    opener = urllib.request.build_opener(_DownloadRedirectHandler())
    return cast(HTTPResponse, opener.open(request, timeout=timeout))
