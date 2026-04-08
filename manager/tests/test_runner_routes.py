"""Coverage-oriented tests for app.api.routes.runner."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.auth import verify_runner_version, verify_token
from app.core.state import runners
from app.main import app
from app.models.models import Runner
from app.services import background_service


@pytest.fixture
def runner_client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_token] = lambda: "tok-ok"
    app.dependency_overrides[verify_runner_version] = lambda: "1.0.0"

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.pop(verify_token, None)
    app.dependency_overrides.pop(verify_runner_version, None)


@pytest.fixture
def clean_runners_state():
    original = dict(runners)
    runners.clear()
    yield
    runners.clear()
    runners.update(original)


@pytest.fixture
def runner_module():
    from app.api.routes import runner as runner_module  # type: ignore

    return runner_module


def test_verify_runner_token_false_when_missing(clean_runners_state, runner_module):
    assert runner_module.verify_runner_token("nope", "tok") is False


def test_verify_runner_token_true_when_matches(clean_runners_state, runner_module):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok1",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    assert runner_module.verify_runner_token("r1", "tok1") is True


def test_verify_runner_token_false_when_mismatch(clean_runners_state, runner_module):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok1",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    assert runner_module.verify_runner_token("r1", "wrong") is False


def test_register_runner_sets_token_version_and_heartbeat(runner_client, clean_runners_state):
    payload = {
        "id": "r1",
        "url": "http://r1.example",
        "task_types": ["encoding"],
        "status": "offline",
        "availability": "available",
        "token": "ignored",
        "version": "ignored",
        "last_heartbeat": datetime.now().isoformat(),
    }

    resp = runner_client.post("/runner/register", json=payload)
    assert resp.status_code == 200
    assert resp.json() == {"status": "registered"}

    assert runners["r1"].token == "tok-ok"
    assert runners["r1"].version == "1.0.0"


def test_register_runner_rejects_non_base_url(runner_client, clean_runners_state):
    payload = {
        "id": "r1",
        "url": "http://r1.example/not-allowed-path",
        "task_types": ["encoding"],
        "status": "offline",
        "availability": "available",
        "token": "ignored",
        "version": "ignored",
        "last_heartbeat": datetime.now().isoformat(),
    }

    resp = runner_client.post("/runner/register", json=payload)
    assert resp.status_code == 400
    assert "must not include a path" in resp.json()["detail"]


def test_runner_heartbeat_ok_after_register(runner_client, clean_runners_state):
    payload = {
        "id": "r1",
        "url": "http://r1.example",
        "task_types": ["encoding"],
        "status": "offline",
        "availability": "available",
        "token": "ignored",
        "version": "ignored",
        "last_heartbeat": datetime.now().isoformat(),
    }
    runner_client.post("/runner/register", json=payload)

    before = runners["r1"].last_heartbeat
    resp = runner_client.post("/runner/heartbeat/r1")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert runners["r1"].last_heartbeat >= before


def test_runner_heartbeat_404_when_runner_missing(runner_client, clean_runners_state):
    resp = runner_client.post("/runner/heartbeat/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Runner not found"


def test_runner_heartbeat_403_when_token_mismatch(runner_client, clean_runners_state):
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        token="tok-ok",
        version="1.0.0",
        last_heartbeat=datetime.now(),
        availability="available",
        status="offline",
    )

    # Override token for this request
    app.dependency_overrides[verify_token] = lambda: "tok-bad"
    try:
        resp = runner_client.post("/runner/heartbeat/r1")
    finally:
        app.dependency_overrides[verify_token] = lambda: "tok-ok"

    assert resp.status_code == 403
    assert resp.json()["detail"] == "Token not authorized for this runner"


@pytest.mark.parametrize(
    ("host", "allowed_hosts", "expected"),
    [
        ("sub.example.com.", ["example.com"], True),
        ("example.com", ["", "example.com"], True),
        ("", ["example.com"], False),
        ("runner.other", ["example.com"], False),
    ],
)
def test_host_matches_allowlist_variants(runner_module, host, allowed_hosts, expected):
    assert runner_module._host_matches_allowlist(host, allowed_hosts) is expected


@pytest.mark.parametrize(
    ("ip_value", "expected"),
    [
        ("invalid-ip", True),
        ("127.0.0.1", True),
        ("93.184.216.34", False),
    ],
)
def test_is_disallowed_ip_variants(runner_module, ip_value, expected):
    assert runner_module._is_disallowed_ip(ip_value) is expected


def test_resolve_host_ips_collects_string_ips_only(monkeypatch, runner_module):
    def fake_getaddrinfo(*_args, **_kwargs):
        return [
            (0, 0, 0, "", ("93.184.216.34", 80)),
            (0, 0, 0, "", ("93.184.216.34", 80)),
            (0, 0, 0, "", (12345, 80)),
            (0, 0, 0, "", ()),
        ]

    monkeypatch.setattr(runner_module.socket, "getaddrinfo", fake_getaddrinfo)

    assert runner_module._resolve_host_ips("runner.example") == ["93.184.216.34"]


@pytest.mark.parametrize(
    ("url", "detail"),
    [
        ("", "Runner URL is empty"),
        ("ftp://example.com", "Runner URL must use http or https"),
        ("http:///no-host", "Runner URL is missing host"),
        ("http://user:pass@example.com", "Runner URL must not include userinfo"),
        ("http://example.com?x=1", "Runner URL must not include query or fragment"),
        ("http://example.com/path", "Runner URL must not include a path"),
    ],
)
def test_parse_and_validate_runner_url_errors(runner_module, url, detail):
    with pytest.raises(HTTPException) as exc:
        runner_module._parse_and_validate_runner_url(url)

    assert exc.value.status_code == 400
    assert exc.value.detail == detail


def test_parse_and_validate_runner_url_ok(runner_module):
    parsed = runner_module._parse_and_validate_runner_url("https://runner.example/")
    assert parsed.scheme == "https"
    assert parsed.netloc == "runner.example"


def test_normalize_and_validate_runner_host_rejects_invalid(runner_module):
    parsed = urlparse("http://:80")

    with pytest.raises(HTTPException) as exc:
        runner_module._normalize_and_validate_runner_host(parsed)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Runner URL has invalid host"


def test_normalize_and_validate_runner_host_normalizes_case_and_trailing_dot(runner_module):
    parsed = urlparse("http://ExAmPle.Com./")
    assert runner_module._normalize_and_validate_runner_host(parsed) == "example.com"


def test_validate_runner_host_allowlist_variants(monkeypatch, runner_module):
    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOWED_HOSTS", [])
    runner_module._validate_runner_host_allowlist("runner.example")

    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOWED_HOSTS", ["example.com"])
    runner_module._validate_runner_host_allowlist("sub.example.com")

    with pytest.raises(HTTPException) as exc:
        runner_module._validate_runner_host_allowlist("runner.other")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Runner URL host not allowed"


def test_resolve_host_ips_or_raise_variants(monkeypatch, runner_module):
    def fake_raises(_host: str):
        raise OSError("dns error")

    monkeypatch.setattr(runner_module, "_resolve_host_ips", fake_raises)
    with pytest.raises(HTTPException) as exc1:
        runner_module._resolve_host_ips_or_raise("runner.example")
    assert exc1.value.status_code == 400
    assert exc1.value.detail == "Runner URL host cannot be resolved"

    monkeypatch.setattr(runner_module, "_resolve_host_ips", lambda _host: [])
    with pytest.raises(HTTPException) as exc2:
        runner_module._resolve_host_ips_or_raise("runner.example")
    assert exc2.value.status_code == 400
    assert exc2.value.detail == "Runner URL host cannot be resolved"

    monkeypatch.setattr(runner_module, "_resolve_host_ips", lambda _host: ["93.184.216.34"])
    assert runner_module._resolve_host_ips_or_raise("runner.example") == ["93.184.216.34"]


def test_validate_runner_network_policy_variants(monkeypatch, runner_module):
    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOW_PRIVATE_NETWORKS", True)
    runner_module._validate_runner_network_policy("any.host")

    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOW_PRIVATE_NETWORKS", False)
    with pytest.raises(HTTPException) as exc:
        runner_module._validate_runner_network_policy("localhost")
    assert exc.value.status_code == 400
    assert exc.value.detail == "Runner URL host not allowed"

    monkeypatch.setattr(runner_module, "_resolve_host_ips_or_raise", lambda _host: ["127.0.0.1"])
    with pytest.raises(HTTPException) as exc2:
        runner_module._validate_runner_network_policy("runner.example")
    assert exc2.value.status_code == 400
    assert exc2.value.detail == "Runner URL resolves to a private/loopback/link-local address"

    monkeypatch.setattr(
        runner_module, "_resolve_host_ips_or_raise", lambda _host: ["93.184.216.34"]
    )
    runner_module._validate_runner_network_policy("runner.example")


def test_extract_runner_port_variants(runner_module):
    assert runner_module._extract_runner_port(urlparse("http://example.com")) is None

    with pytest.raises(HTTPException) as exc:
        runner_module._extract_runner_port(urlparse("http://example.com:abc"))

    assert exc.value.status_code == 400
    assert exc.value.detail == "Runner URL has invalid port"


def test_build_runner_origin_formats_ipv4_and_ipv6(runner_module):
    assert (
        runner_module._build_runner_origin("https", "example.com", 443) == "https://example.com:443"
    )
    assert (
        runner_module._build_runner_origin("http", "2001:db8::1", 8080)
        == "http://[2001:db8::1]:8080"
    )


def test_validate_and_normalize_runner_url_ok_with_allowlist(monkeypatch, runner_module):
    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOWED_HOSTS", ["example.com"])
    monkeypatch.setattr(runner_module.config, "RUNNER_URL_ALLOW_PRIVATE_NETWORKS", True)

    normalized = runner_module._validate_and_normalize_runner_url("HTTPS://Sub.Example.com:8443/")
    assert normalized == "https://sub.example.com:8443"
