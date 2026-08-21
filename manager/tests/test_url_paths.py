"""Tests for root-path-aware public URL helpers."""

from starlette.requests import Request

from app.core.url_paths import cookie_path, prefixed_path, request_root_path


def _request(root_path: str = "") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/admin",
            "root_path": root_path,
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 443),
        }
    )


def test_url_paths_use_configured_request_root_path():
    """Prefix public paths and scope cookies below the deployment subpath."""
    request = _request("/manager/")

    assert request_root_path(request) == "/manager"
    assert prefixed_path(request, "/static/esup-runner.css") == "/manager/static/esup-runner.css"
    assert prefixed_path(request, "admin") == "/manager/admin"
    assert cookie_path(request) == "/manager"


def test_url_paths_preserve_root_deployment_defaults():
    """Keep current root-level URLs when no deployment prefix is configured."""
    request = _request()

    assert request_root_path(request) == ""
    assert prefixed_path(request, "/admin") == "/admin"
    assert cookie_path(request) == "/"
