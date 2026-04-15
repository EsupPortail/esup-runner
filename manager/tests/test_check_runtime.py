from types import SimpleNamespace

import httpx

import scripts.check_runtime as runtime


def test_mask_secret_and_first_token_helpers():
    cfg = SimpleNamespace(AUTHORIZED_TOKENS={"a": "   ", "b": "token-12345"})

    assert runtime._mask_secret("short") == "***"
    assert runtime._mask_secret("1234567890") == "1234***7890"
    assert runtime._first_token(cfg) == "token-12345"
    assert runtime._first_token(SimpleNamespace(AUTHORIZED_TOKENS={})) is None


def test_request_status_returns_zero_on_network_error():
    class _FailingClient:
        def get(self, *_args, **_kwargs):
            req = httpx.Request("GET", "http://manager.example.org")
            raise httpx.RequestError("boom", request=req)

    status, details = runtime._request_status(_FailingClient(), "http://manager.example.org")
    assert status == 0
    assert "boom" in details


def test_run_checks_success_with_token_and_runners(monkeypatch):
    cfg = SimpleNamespace(
        MANAGER_URL="http://manager.example.org:8081",
        MANAGER_HOST="manager.example.org",
        MANAGER_BIND_HOST="0.0.0.0",
        MANAGER_PORT=8081,
        AUTHORIZED_TOKENS={"runners": "token-abcdef"},
    )

    monkeypatch.setattr(runtime, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        runtime,
        "_check_manager_url_host",
        lambda _url: [
            runtime.CheckResult(
                name="Manager URL DNS Resolution",
                ok=True,
                required=True,
                details="manager.example.org -> 1.2.3.4",
            )
        ],
    )

    def _fake_request(_client, url, token=None):
        if url.endswith("/admin"):
            return 401, '{"detail":"Not authenticated"}'
        if url.endswith("/api/version"):
            assert token == "token-abcdef"
            return 200, '{"version":"1.1.0"}'
        if url.endswith("/manager/health"):
            return 200, '{"status":"healthy"}'
        if url.endswith("/api/runners"):
            return 200, '{"runners":[{"id":"r1"},{"id":"r2"}]}'
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(runtime, "_request_status", _fake_request)

    results, context = runtime.run_checks()
    by_name = {r.name: r for r in results}

    assert context["manager_url"] == "http://manager.example.org:8081"
    assert context["token_masked"] == "toke***cdef"
    assert by_name["Admin Endpoint Reachability"].ok is True
    assert by_name["API Version Endpoint"].ok is True
    assert by_name["Manager Health Endpoint"].ok is True
    assert by_name["Runners Endpoint"].ok is True
    assert by_name["At Least One Runner Registered"].ok is True
    assert "registered_runners=2" in by_name["At Least One Runner Registered"].details


def test_run_checks_without_token_skips_authenticated_endpoints(monkeypatch):
    cfg = SimpleNamespace(
        MANAGER_URL="http://127.0.0.1:8081",
        MANAGER_HOST="127.0.0.1",
        MANAGER_BIND_HOST="127.0.0.1",
        MANAGER_PORT=8081,
        AUTHORIZED_TOKENS={},
    )

    monkeypatch.setattr(runtime, "_load_config", lambda: cfg)
    monkeypatch.setattr(
        runtime,
        "_check_manager_url_host",
        lambda _url: [runtime.CheckResult(name="Manager URL Host Type", ok=True, required=True)],
    )

    calls: list[tuple[str, str | None]] = []

    def _fake_request(_client, url, token=None):
        calls.append((url, token))
        return 401, '{"detail":"Not authenticated"}'

    monkeypatch.setattr(runtime, "_request_status", _fake_request)

    results, _context = runtime.run_checks()
    names = {r.name for r in results}

    assert "Authorized Token Configured" in names
    assert "Admin Endpoint Reachability" in names
    assert "API Version Endpoint" not in names
    assert "Manager Health Endpoint" not in names
    assert "Runners Endpoint" not in names
    assert len(calls) == 1
    assert calls[0][0].endswith("/admin")
    assert calls[0][1] is None


def test_run_checks_missing_manager_url_short_circuit(monkeypatch):
    cfg = SimpleNamespace(
        MANAGER_URL="",
        MANAGER_HOST="0.0.0.0",
        MANAGER_BIND_HOST="0.0.0.0",
        MANAGER_PORT=8081,
        AUTHORIZED_TOKENS={"runners": "token-abcdef"},
    )

    monkeypatch.setattr(runtime, "_load_config", lambda: cfg)

    results, context = runtime.run_checks()

    assert len(results) == 1
    assert results[0].name == "Manager URL Configured"
    assert results[0].ok is False
    assert context["manager_url"] == ""


def test_main_exit_codes(monkeypatch):
    monkeypatch.setattr(runtime, "print_report", lambda *_args, **_kwargs: None)

    monkeypatch.setattr(
        runtime,
        "run_checks",
        lambda: ([runtime.CheckResult(name="ok", ok=True, required=True)], {}),
    )
    assert runtime.main() == 0

    monkeypatch.setattr(
        runtime,
        "run_checks",
        lambda: ([runtime.CheckResult(name="ko", ok=False, required=True)], {}),
    )
    assert runtime.main() == 1

    monkeypatch.setattr(runtime, "run_checks", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert runtime.main() == 1
