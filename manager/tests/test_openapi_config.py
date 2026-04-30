"""Unit tests for app.api.openapi (schema customization and protected docs routes)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.api.openapi import (
    OpenAPIConfig,
    _add_security_schemes,
    _assign_tags_to_endpoints,
    _enhance_schemas_with_examples,
    _get_openapi_tags,
    custom_openapi,
    setup_openapi_config,
    setup_protected_openapi_routes,
)
from app.core.config import config


def test_get_openapi_tags_contains_expected_names():
    tags = _get_openapi_tags()
    names = {t["name"] for t in tags}
    assert {"API", "Logs", "Manager", "Runner", "Task"}.issubset(names)


def test_assign_tags_to_endpoints_by_path_patterns():
    schema = {
        "paths": {
            "/admin/x": {"get": {}},
            "/health": {"get": {}},
            "/runner/register": {"post": {}},
            "/task/execute": {"post": {}},
            "/auth/token": {"post": {}},
            "/other": {"get": {}},
        }
    }

    _assign_tags_to_endpoints(schema)

    assert schema["paths"]["/admin/x"]["get"]["tags"] == ["Admin"]
    assert schema["paths"]["/health"]["get"]["tags"] == ["Health"]
    assert schema["paths"]["/runner/register"]["post"]["tags"] == ["Runner"]
    assert schema["paths"]["/task/execute"]["post"]["tags"] == ["Task"]
    assert schema["paths"]["/auth/token"]["post"]["tags"] == ["Authentication"]
    assert schema["paths"]["/other"]["get"]["tags"] == ["API"]


def test_add_security_schemes_adds_components_and_default_security():
    schema = {"paths": {"/x": {"get": {}}, "/y": {"post": {}}}}

    _add_security_schemes(schema)

    schemes = schema["components"]["securitySchemes"]
    assert "Bearer" in schemes
    assert "APIKeyHeader" in schemes

    assert schema["paths"]["/x"]["get"]["security"] == [{"Bearer": []}, {"APIKeyHeader": []}]
    assert schema["paths"]["/y"]["post"]["security"] == [{"Bearer": []}, {"APIKeyHeader": []}]


def test_enhance_schemas_with_examples_sets_examples_when_schemas_exist():
    schema = {
        "components": {
            "schemas": {
                "Runner": {},
                "Task": {},
                "TaskRequest": {},
            }
        }
    }

    _enhance_schemas_with_examples(schema)

    assert "example" in schema["components"]["schemas"]["Runner"]
    assert "example" in schema["components"]["schemas"]["Task"]
    assert "example" in schema["components"]["schemas"]["TaskRequest"]


def test_enhance_schemas_with_examples_creates_components_when_missing():
    schema: dict = {}
    _enhance_schemas_with_examples(schema)
    assert "components" in schema
    assert "schemas" in schema["components"]


def test_custom_openapi_sets_tags_logo_security_contact_license_and_caches():
    app = FastAPI(
        title="Test API",
        version="0.0.1",
        description="desc",
        contact={"name": "Alice"},
        license_info={"name": "MIT"},
    )

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.openapi = custom_openapi(app)

    schema1 = app.openapi()
    assert schema1["tags"] == _get_openapi_tags()
    assert schema1["info"]["x-logo"]["url"].startswith("https://")
    assert schema1["info"]["contact"] == {"name": "Alice"}
    assert schema1["info"]["license"] == {"name": "MIT"}

    # Default security applied
    assert schema1["paths"]["/ping"]["get"]["security"] == [{"Bearer": []}, {"APIKeyHeader": []}]

    # Cached schema path
    schema2 = app.openapi()
    assert schema2 is schema1


def test_setup_openapi_config_assigns_openapi_callable():
    app = FastAPI(title="X", version="1", description="d")
    setup_openapi_config(app)
    schema = app.openapi()
    assert "openapi" in schema


def test_set_openapi_auth_cookie_if_needed_skips_when_builder_returns_none():
    from app.api import openapi as openapi_module

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/docs",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 443),
        }
    )
    response = Response()

    openapi_module._set_openapi_auth_cookie_if_needed(
        response=response,
        request=request,
        token="tok",
        token_cookie=None,
        rotate_each_request=True,
        cookie_name="openapi_token",
        cookie_max_age_seconds=900,
        build_cookie_value=lambda _token: None,
    )

    assert response.headers.get("set-cookie") is None


def test_setup_protected_openapi_routes_docs_uses_cookie_and_no_query_token(monkeypatch):
    # Start with docs disabled so we know the override route comes from setup_protected_openapi_routes
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    setup_protected_openapi_routes(app)

    from app.core.auth import verify_openapi_token

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"docs": "tok123"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)
    app.dependency_overrides[verify_openapi_token] = lambda: "tok123"

    with TestClient(app) as client:
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert "openapi.json?token=tok123" not in resp.text
        assert "openapi.json" in resp.text
        set_cookie = resp.headers.get("set-cookie", "")
        assert "openapi_token=" in set_cookie
        assert "openapi_token=tok123" not in set_cookie
        assert "Max-Age=900" in set_cookie


def test_setup_protected_openapi_routes_redoc_uses_cookie_and_no_query_token(monkeypatch):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    setup_protected_openapi_routes(app)

    from app.core.auth import verify_openapi_token

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"docs": "tok456"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)
    app.dependency_overrides[verify_openapi_token] = lambda: "tok456"

    with TestClient(app) as client:
        resp = client.get("/redoc")
        assert resp.status_code == 200
        assert "openapi.json?token=tok456" not in resp.text
        assert "openapi.json" in resp.text
        set_cookie = resp.headers.get("set-cookie", "")
        assert "openapi_token=" in set_cookie
        assert "openapi_token=tok456" not in set_cookie
        assert "Max-Age=900" in set_cookie


def test_setup_protected_openapi_routes_skips_rotation_when_disabled_and_cookie_present(
    monkeypatch,
):
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    setup_protected_openapi_routes(app)

    from app.core.auth import build_openapi_cookie_value, verify_openapi_token

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"docs": "tok-no-rotate"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)
    monkeypatch.setattr(config, "OPENAPI_COOKIE_ROTATE_EACH_REQUEST", False)
    app.dependency_overrides[verify_openapi_token] = lambda: "tok-no-rotate"

    existing_cookie = build_openapi_cookie_value("tok-no-rotate")
    assert existing_cookie is not None

    with TestClient(app) as client:
        client.cookies.set("openapi_token", existing_cookie)
        resp = client.get("/docs")
        assert resp.status_code == 200
        assert resp.headers.get("set-cookie") is None


def test_setup_protected_openapi_routes_without_token_keeps_plain_openapi_url_and_openapi_json_works():
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    setup_protected_openapi_routes(app)

    from app.core.auth import verify_openapi_token

    app.dependency_overrides[verify_openapi_token] = lambda: None

    with TestClient(app) as client:
        docs = client.get("/docs")
        assert docs.status_code == 200
        assert "openapi.json?token=" not in docs.text

        redoc = client.get("/redoc")
        assert redoc.status_code == 200

        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert schema.json()["openapi"]


def test_openapi_config_get_fastapi_config_has_expected_keys():
    cfg = OpenAPIConfig.get_fastapi_config()
    assert cfg["title"] == OpenAPIConfig.TITLE
    assert cfg["openapi_url"] == OpenAPIConfig.OPENAPI_URL
    assert cfg["docs_url"] == OpenAPIConfig.DOCS_URL
    assert cfg["redoc_url"] == OpenAPIConfig.REDOC_URL
