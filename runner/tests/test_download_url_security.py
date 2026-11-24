"""Exercise Runner download policy at the source, metadata and redirect boundaries."""

import io
import socket
import urllib.request
from email.message import Message
from types import SimpleNamespace
from urllib.response import addinfourl

import pytest
import requests

from app.core.config import config
from app.core.download_urls import DownloadURLRejected, get_download_response, validate_download_url
from app.task_handlers import base_handler
from app.task_handlers.encoding.core import dressing_runtime_utils
from app.task_handlers.studio.core import download_runtime_utils, metadata_runtime_utils
from app.task_handlers.studio.studio_handler import StudioEncodingHandler


@pytest.fixture
def requested_urls(monkeypatch):
    """Mock requests at the transport entry and record every destination."""
    calls = []
    monkeypatch.setattr(config, "DOWNLOAD_ALLOWED_HOSTS", ["allowed.example"])
    monkeypatch.setattr(config, "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", True)

    def get(_session, url, *, allow_redirects, **_kwargs):
        assert allow_redirects is False
        calls.append(url)
        response = requests.Response()
        response.url = url
        response.status_code = 200
        response._content = b"download"
        response._content_consumed = True
        if url.endswith("/blocked"):
            response.status_code = 302
            response.headers["Location"] = "https://allowed.example.other.example/source"
        elif url.endswith("/redirect"):
            response.status_code = 307
            response.headers["Location"] = "/source"
        elif url.endswith("/private"):
            response.status_code = 307
            response.headers["Location"] = "https://private.allowed.example/source"
        elif url.endswith("/malformed"):
            response.status_code = 307
            response.headers["Location"] = "https://[invalid]/source"
        elif url.endswith("/loop"):
            response.status_code = 301
            response.headers["Location"] = "/loop"
        return response

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(requests, "get", lambda url, **kwargs: get(None, url, **kwargs))
    monkeypatch.setattr(base_handler.time, "sleep", lambda _: pytest.fail("Policy failure retried"))
    return calls


@pytest.mark.parametrize(
    ("url", "error"),
    [("https://[invalid", "Invalid download URL"), ("https:///media", "Invalid download URL host")],
)
def test_rejects_malformed_download_urls(url, error):
    with pytest.raises(DownloadURLRejected, match=error):
        validate_download_url(url)


def test_rejects_a_request_with_no_prepared_url(monkeypatch):
    monkeypatch.setattr(requests.Request, "prepare", lambda _: SimpleNamespace(url=None))
    with pytest.raises(DownloadURLRejected, match="Invalid download URL"):
        get_download_response(pytest.fail, "https://example.com/media", timeout=(1, 1))


@pytest.mark.parametrize(
    ("url", "expected_requests"),
    [
        ("https://foreign.example/source", 0),
        ("https://allowed.example/blocked", 1),
        ("https://allowed.example/malformed", 1),
        ("https://allowed.example/loop", 11),
        ("https://user:pass@allowed.example/source", 0),
        ("https://allowed.example:70000/source", 0),
        ("ftp://allowed.example/source", 0),
    ],
)
def test_main_source_rejects_invalid_destinations(requested_urls, tmp_path, url, expected_requests):
    destination = tmp_path / "source.mp4"
    result = StudioEncodingHandler().download_source_file(url, str(destination))
    assert result["success"] is False
    assert len(requested_urls) == expected_requests
    assert not list(tmp_path.iterdir())


def test_main_source_follows_allowed_relative_redirect(requested_urls, tmp_path):
    destination = tmp_path / "source.mp4"
    result = StudioEncodingHandler().download_source_file(
        "https://media.allowed.example/redirect", str(destination)
    )
    assert result["success"] is True
    assert destination.read_bytes() == b"download"
    assert requested_urls == [
        "https://media.allowed.example/redirect",
        "https://media.allowed.example/source",
    ]


def test_source_private_network_policy_checks_all_dns_results(
    monkeypatch, requested_urls, tmp_path
):
    destination = tmp_path / "source.mp4"
    url = "https://allowed.example/source"
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (None, None, None, None, ("8.8.8.8", 0)),
            (None, None, None, None, ("::ffff:127.0.0.1", 0)),
        ],
    )
    monkeypatch.setattr(config, "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", False)
    result = StudioEncodingHandler().download_source_file(url, str(destination))
    assert result["success"] is False
    assert "private address" in result["error"]
    assert requested_urls == []
    monkeypatch.setattr(config, "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", True)
    assert StudioEncodingHandler().download_source_file(url, str(destination))["success"]
    assert requested_urls == [url]


def test_source_redirect_checks_private_address_even_on_allowed_subdomain(
    monkeypatch, requested_urls, tmp_path
):
    def resolve(host, *_args, **_kwargs):
        ip = "127.0.0.1" if host == "private.allowed.example" else "8.8.8.8"
        return [(None, None, None, None, (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(config, "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", False)
    url = "https://allowed.example/private"
    result = StudioEncodingHandler().download_source_file(url, str(tmp_path / "source.mp4"))
    assert result["success"] is False
    assert "private address" in result["error"]
    assert requested_urls == [url]


def test_studio_validates_xml_and_smil_before_requesting_them(requested_urls):
    with pytest.raises(DownloadURLRejected, match="host not allowed"):
        metadata_runtime_utils.load_mediapackage_and_layout(
            SimpleNamespace(xml_url="https://foreign.example/package.xml", presenter=None)
        )
    assert metadata_runtime_utils.load_clip_times("https://foreign.example/cut.smil") == (
        None,
        None,
    )
    assert requested_urls == []
    assert metadata_runtime_utils.load_clip_times("https://allowed.example/blocked") == (None, None)
    assert requested_urls == ["https://allowed.example/blocked"]


@pytest.mark.parametrize("kind", ["studio", "dressing"])
@pytest.mark.parametrize("allowed", [False, True])
def test_specialized_download_checks_urllib_redirects(monkeypatch, tmp_path, kind, allowed):
    monkeypatch.setattr(config, "DOWNLOAD_ALLOWED_HOSTS", ["allowed.example"])
    monkeypatch.setattr(config, "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", True)
    monkeypatch.delenv("DOWNLOAD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    requested = []
    redirect_bodies = []
    url = "https://allowed.example/video.mp4"

    def https_open(_handler, request):
        requested.append(request.full_url)
        headers = Message()
        if request.full_url == url:
            headers["Location"] = "/source" if allowed else "https://foreign.example/source"
            body = io.BytesIO()
            redirect_bodies.append(body)
            response = addinfourl(body, headers, request.full_url, 302)
        else:
            response = addinfourl(io.BytesIO(b"source"), headers, request.full_url, 200)
        response.msg = ""
        return response

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    if kind == "studio":
        result = download_runtime_utils.materialize_source(url, str(tmp_path), "presenter")
        assert (result is not None) == allowed
    elif allowed:
        result = dressing_runtime_utils.download_url_to_dir(
            url, str(tmp_path), "opening", sanitize_filename_fn=str
        )
        assert result
    else:
        with pytest.raises(DownloadURLRejected, match="host not allowed"):
            dressing_runtime_utils.download_url_to_dir(
                url, str(tmp_path), "opening", sanitize_filename_fn=str
            )
    assert requested == ([url, "https://allowed.example/source"] if allowed else [url])
    assert redirect_bodies[0].closed
    assert [p.read_bytes() for p in tmp_path.iterdir()] == ([b"source"] if allowed else [])
