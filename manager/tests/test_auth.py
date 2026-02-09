"""Authentication and authorization regression tests."""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials

import app.core.auth as auth_module
from app.__version__ import __version__
from app.core.auth import (
    _mask_token,
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
async def test_verify_admin_username_missing_raises(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hash"})
    with pytest.raises(HTTPException) as exc:
        await verify_admin(HTTPBasicCredentials(username="nope", password="x"))
    assert exc.value.status_code == 401


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
