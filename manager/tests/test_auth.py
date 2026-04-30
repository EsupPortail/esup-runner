"""Authentication and authorization regression tests."""

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials

import app.core.auth as auth_module
from app.__version__ import __version__
from app.core.auth import (
    _mask_token,
    build_openapi_cookie_value,
    resolve_openapi_cookie_token,
    verify_admin,
    verify_openapi_token,
    verify_runner_version,
    verify_token,
)
from app.core.config import config


def test_root_endpoint_is_public(client):
    """Root endpoint is public and returns basic metadata."""

    response = client.get("/")
    assert response.status_code == 200

    payload = response.json()
    assert payload["message"] == "Runner Manager"
    assert payload["version"] == __version__


def test_api_version_requires_token(client):
    """Protected endpoints should reject anonymous requests."""

    response = client.get("/api/version")
    assert response.status_code == 401


def test_api_version_accepts_bearer_token(client, auth_headers):
    """Bearer token grants access to /api/version."""

    response = client.get("/api/version", headers={"Authorization": auth_headers["Authorization"]})
    assert response.status_code == 200

    payload = response.json()
    assert payload["version_info"]["major"] >= 0


def test_api_version_accepts_api_key_header(client, auth_headers):
    """X-API-Token header also grants access."""

    response = client.get("/api/version", headers={"X-API-Token": auth_headers["X-API-Token"]})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    (
        {"Authorization": "Bearer invalid-token"},
        {"X-API-Token": "invalid-token"},
    ),
)
def test_invalid_tokens_are_rejected(client, headers):
    """Any wrong token must return 401."""

    response = client.get("/api/version", headers=headers)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_verify_openapi_token_public_allows_without_token(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "public")
    assert await verify_openapi_token(token_query=None, api_token=None, credentials=None) is None


@pytest.mark.asyncio
async def test_verify_openapi_token_private_missing_token_raises(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"t": "tok"})

    with pytest.raises(HTTPException) as exc:
        await verify_openapi_token(token_query=None, api_token=None, credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_openapi_token_private_query_has_priority(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a", "b": "tok-b"})
    monkeypatch.setattr(config, "OPENAPI_ALLOW_QUERY_TOKEN", True)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok-a")
    out = await verify_openapi_token(token_query="tok-b", api_token="tok-a", credentials=creds)
    # Query token is optional and lower priority than headers.
    assert out == "tok-a"


@pytest.mark.asyncio
async def test_verify_openapi_token_private_query_works_when_enabled(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"b": "tok-b"})
    monkeypatch.setattr(config, "OPENAPI_ALLOW_QUERY_TOKEN", True)

    out = await verify_openapi_token(token_query="tok-b", api_token=None, credentials=None)
    assert out == "tok-b"


@pytest.mark.asyncio
async def test_verify_openapi_token_private_cookie_works(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"b": "tok-b"})
    monkeypatch.setattr(config, "OPENAPI_ALLOW_QUERY_TOKEN", False)
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")

    cookie_value = build_openapi_cookie_value("tok-b")
    assert cookie_value is not None

    out = await verify_openapi_token(
        token_query=None,
        token_cookie=cookie_value,
        api_token=None,
        credentials=None,
    )
    assert out == "tok-b"


def test_openapi_cookie_helpers_detect_tamper_and_expiry(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 1)
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")

    cookie_value = build_openapi_cookie_value("tok-a")
    assert cookie_value is not None
    assert resolve_openapi_cookie_token(cookie_value) == "tok-a"

    payload_b64, sig_b64 = cookie_value.split(".", 1)
    tampered_payload = ("A" if payload_b64[0] != "A" else "B") + payload_b64[1:]
    assert resolve_openapi_cookie_token(f"{tampered_payload}.{sig_b64}") is None

    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode("utf-8"))
    payload["exp"] = 0
    payload_raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    expired_payload_b64 = base64.urlsafe_b64encode(payload_raw).decode("ascii").rstrip("=")
    signature = hmac.new(b"unit-test-secret", payload_raw, hashlib.sha256).digest()
    expired_sig_b64 = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    assert resolve_openapi_cookie_token(f"{expired_payload_b64}.{expired_sig_b64}") is None


def test_openapi_cookie_helper_internal_branches(monkeypatch):
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"b": "tok-b", "a": "tok-a"})
    monkeypatch.setattr(config, "ADMIN_USERS", {"z": "hash-z", "m": "hash-m"})

    # Covers derived-secret fallback path and deterministic output.
    secret = auth_module._openapi_cookie_secret()
    assert isinstance(secret, bytes)
    assert len(secret) == 32
    assert auth_module._openapi_cookie_secret() == secret

    # Unknown token path in resolver/builder.
    assert auth_module._resolve_token_name("missing-token") is None
    assert build_openapi_cookie_value("missing-token") is None

    # Decode failure branch.
    assert auth_module._decode_openapi_cookie_parts("malformed-cookie") is None

    # Payload decode branches.
    assert auth_module._load_openapi_cookie_payload(b"not-json") is None
    assert auth_module._load_openapi_cookie_payload(json.dumps(["x"]).encode("utf-8")) is None

    future = int(time.time()) + 3600
    assert auth_module._extract_openapi_token_name({"v": 2, "t": "a", "exp": future}) is None
    assert auth_module._extract_openapi_token_name({"v": 1, "t": "", "exp": future}) is None
    assert auth_module._extract_openapi_token_name({"v": 1, "t": 1, "exp": future}) is None
    assert auth_module._extract_openapi_token_name({"v": 1, "t": "a", "exp": "nope"}) is None

    # Resolve branches: empty cookie, malformed parts, payload parse failure after valid signature.
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    assert resolve_openapi_cookie_token("") is None
    assert resolve_openapi_cookie_token("still-malformed") is None

    bad_payload = b"not-json"
    bad_payload_b64 = base64.urlsafe_b64encode(bad_payload).decode("ascii").rstrip("=")
    bad_signature = hmac.new(b"unit-test-secret", bad_payload, hashlib.sha256).digest()
    bad_signature_b64 = base64.urlsafe_b64encode(bad_signature).decode("ascii").rstrip("=")
    assert resolve_openapi_cookie_token(f"{bad_payload_b64}.{bad_signature_b64}") is None


@pytest.mark.asyncio
async def test_verify_openapi_token_private_header_then_bearer(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})

    # Header path
    out = await verify_openapi_token(token_query=None, api_token="tok-a", credentials=None)
    assert out == "tok-a"

    # Bearer path
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok-a")
    out2 = await verify_openapi_token(token_query=None, api_token=None, credentials=creds)
    assert out2 == "tok-a"


@pytest.mark.asyncio
async def test_verify_openapi_token_private_invalid_token_raises(monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})

    with pytest.raises(HTTPException) as exc:
        # Use header token path so we hit the "invalid token" branch (not the
        # "missing token" one).
        await verify_openapi_token(token_query=None, api_token="bad", credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_missing_raises(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})
    with pytest.raises(HTTPException) as exc:
        await verify_token(api_token=None, credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_api_key_header_has_priority(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a", "b": "tok-b"})
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok-b")
    out = await verify_token(api_token="tok-a", credentials=creds)
    assert out == "tok-a"


@pytest.mark.asyncio
async def test_verify_token_bearer_works(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="tok-a")
    out = await verify_token(api_token=None, credentials=creds)
    assert out == "tok-a"


@pytest.mark.asyncio
async def test_verify_token_invalid_raises(monkeypatch):
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})
    with pytest.raises(HTTPException) as exc:
        await verify_token(api_token="bad", credentials=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_token_triggers_config_refresh(monkeypatch):
    calls = {"count": 0}

    def _refresh():
        calls["count"] += 1
        return False

    monkeypatch.setattr(auth_module.config_module, "reload_config_if_signaled", _refresh)
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"a": "tok-a"})

    out = await verify_token(api_token="tok-a", credentials=None)
    assert out == "tok-a"
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_verify_admin_username_missing_raises(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hash"})
    with pytest.raises(HTTPException) as exc:
        await verify_admin(HTTPBasicCredentials(username="nope", password="x"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_admin_triggers_config_refresh(monkeypatch):
    calls = {"count": 0}

    def _refresh():
        calls["count"] += 1
        return False

    monkeypatch.setattr(auth_module.config_module, "reload_config_if_signaled", _refresh)
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hash"})
    monkeypatch.setattr(config.pwd_context, "verify", lambda *_a, **_k: True)

    assert await verify_admin(HTTPBasicCredentials(username="admin", password="ok")) is True
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_verify_admin_incorrect_password_raises(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hash"})
    monkeypatch.setattr(config.pwd_context, "verify", lambda *_a, **_k: False)
    with pytest.raises(HTTPException) as exc:
        await verify_admin(HTTPBasicCredentials(username="admin", password="bad"))
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_admin_ok(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hash"})
    monkeypatch.setattr(config.pwd_context, "verify", lambda *_a, **_k: True)
    assert await verify_admin(HTTPBasicCredentials(username="admin", password="ok")) is True


@pytest.mark.asyncio
async def test_verify_runner_version_missing_header_raises():
    with pytest.raises(HTTPException) as exc:
        await verify_runner_version(runner_version=None)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_runner_version_major_mismatch_raises(monkeypatch):
    major = __version__.split(".")[0]
    other_major = str((int(major) + 1) if major.isdigit() else 999)
    with pytest.raises(HTTPException) as exc:
        await verify_runner_version(runner_version=f"{other_major}.0.0")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_verify_runner_version_minor_mismatch_raises():
    parts = __version__.split(".")
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else "0"
    other_minor = str((int(minor) + 1) if minor.isdigit() else 999)

    with pytest.raises(HTTPException) as exc:
        await verify_runner_version(runner_version=f"{major}.{other_minor}.0")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_verify_runner_version_major_minor_match_ok():
    parts = __version__.split(".")
    major = parts[0]
    minor = parts[1] if len(parts) > 1 else "0"
    out = await verify_runner_version(runner_version=f"{major}.{minor}.99")
    assert out.startswith(f"{major}.{minor}.")


@pytest.mark.asyncio
async def test_verify_runner_version_invalid_format_raises():
    with pytest.raises(HTTPException) as exc:
        await verify_runner_version(runner_version="not-a-version")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_verify_runner_version_invalid_manager_version_raises(monkeypatch):
    monkeypatch.setattr(auth_module, "MANAGER_VERSION", "not-a-version")

    with pytest.raises(HTTPException) as exc:
        await verify_runner_version(runner_version="0.1.0")
    assert exc.value.status_code == 500


def test_mask_token_handles_empty_and_short_values():
    assert _mask_token(None) == "<empty>"
    assert _mask_token("") == "<empty>"
    # length <= 2 returns obscured placeholder
    assert _mask_token("x") == "***"
    assert _mask_token("yz") == "***"


def test_mask_token_obscures_small_tokens():
    assert _mask_token("abcd") == "a***d"
    assert _mask_token("abcdefgh") == "a***h"


def test_mask_token_obscures_long_tokens():
    assert _mask_token("abcdefghijklmnop") == "abcd...mnop"


def test_refresh_config_if_needed_logs_warning_on_error(monkeypatch):
    messages = []

    def _raise_refresh_error():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        auth_module.config_module, "reload_config_if_signaled", _raise_refresh_error
    )
    monkeypatch.setattr(auth_module.logger, "warning", lambda message: messages.append(message))

    auth_module._refresh_config_if_needed()

    assert len(messages) == 1
    assert "Failed to refresh config from reload marker" in messages[0]
    assert "boom" in messages[0]
