"""Unit tests for app.api.openapi (schema customization and protected docs routes)."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from app.api.openapi import (
    OpenAPIConfig,
    _assign_tags_to_endpoints,
    _enhance_schemas_with_examples,
    _get_openapi_tags,
    _mark_runner_version_headers_required,
    custom_openapi,
    setup_openapi_config,
    setup_protected_openapi_routes,
)
from app.core.auth import verify_admin, verify_runner_version, verify_token
from app.core.config import config


def test_get_openapi_tags_contains_expected_names():
    """Validate Get openapi tags contains expected names."""
    tags = _get_openapi_tags()
    names = {t["name"] for t in tags}
    assert {"API", "Logs", "Manager", "Runner", "Task"}.issubset(names)


def test_assign_tags_to_endpoints_by_path_patterns():
    """Validate Assign tags to endpoints by path patterns."""
    schema = {
        "paths": {
            "/admin/x": {"get": {}},
            "/health": {"get": {}},
            "/runner/register": {"post": {}},
            "/task/execute": {"post": {}},
            "/task/stop/{task_id}": {"post": {}},
            "/auth/token": {"post": {}},
            "/other": {"get": {}},
        }
    }

    _assign_tags_to_endpoints(schema)

    assert schema["paths"]["/admin/x"]["get"]["tags"] == ["Admin"]
    assert schema["paths"]["/health"]["get"]["tags"] == ["Health"]
    assert schema["paths"]["/runner/register"]["post"]["tags"] == ["Runner"]
    assert schema["paths"]["/task/execute"]["post"]["tags"] == ["Task"]
    assert schema["paths"]["/task/stop/{task_id}"]["post"]["tags"] == ["Task"]
    assert schema["paths"]["/auth/token"]["post"]["tags"] == ["Authentication"]
    assert schema["paths"]["/other"]["get"]["tags"] == ["API"]


def test_enhance_schemas_with_examples_sets_examples_when_schemas_exist():
    """Validate Enhance schemas with examples sets examples when schemas exist."""
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
    """Validate Enhance schemas with examples creates components when missing."""
    schema: dict = {}
    _enhance_schemas_with_examples(schema)
    assert "components" in schema
    assert "schemas" in schema["components"]


def test_custom_openapi_sets_tags_logo_contact_license_and_caches():
    """Validate Custom openapi sets tags logo contact license and caches."""
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

    assert "security" not in schema1["paths"]["/ping"]["get"]

    # Cached schema path
    schema2 = app.openapi()
    assert schema2 is schema1


def test_custom_openapi_preserves_security_and_header_contracts():
    """Keep FastAPI security definitions aligned with route dependencies."""
    app = FastAPI(title="Security API", version="1")

    @app.get("/public")
    def public_route():
        return None

    @app.get("/token", dependencies=[Depends(verify_token)])
    def token_route():
        return None

    @app.get("/admin", dependencies=[Depends(verify_admin)])
    def admin_route():
        return None

    @app.get(
        "/runner",
        dependencies=[Depends(verify_token), Depends(verify_runner_version)],
    )
    def runner_route():
        return None

    app.openapi = custom_openapi(app)
    schema = app.openapi()

    declared_schemes = schema["components"]["securitySchemes"]
    assert declared_schemes["APIKeyHeader"]["name"] == "X-API-Token"
    assert declared_schemes["HTTPBearer"]["scheme"] == "bearer"
    assert declared_schemes["HTTPBasic"]["scheme"] == "basic"

    public_operation = schema["paths"]["/public"]["get"]
    token_operation = schema["paths"]["/token"]["get"]
    admin_operation = schema["paths"]["/admin"]["get"]
    runner_operation = schema["paths"]["/runner"]["get"]

    assert "security" not in public_operation
    assert {
        scheme_name for requirement in token_operation["security"] for scheme_name in requirement
    } == {"APIKeyHeader", "HTTPBearer"}
    assert admin_operation["security"] == [{"HTTPBasic": []}]
    assert runner_operation["security"] == token_operation["security"]
    assert any(
        parameter["name"] == "X-Runner-Version"
        and parameter["in"] == "header"
        and parameter["required"] is True
        for parameter in runner_operation["parameters"]
    )

    referenced_schemes = {
        scheme_name
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security", [])
        for scheme_name in requirement
    }
    assert referenced_schemes <= declared_schemes.keys()


def test_mark_runner_version_header_ignores_path_level_parameters():
    """Handle OpenAPI path-level parameters while updating operations."""
    schema = {
        "paths": {
            "/runner": {
                "parameters": [],
                "get": {
                    "parameters": [
                        {
                            "name": "X-Runner-Version",
                            "in": "header",
                            "required": False,
                        }
                    ]
                },
            }
        }
    }

    _mark_runner_version_headers_required(schema)

    assert schema["paths"]["/runner"]["get"]["parameters"][0]["required"] is True


def test_runner_version_dependency_keeps_explicit_missing_header_error():
    """Return the existing HTTP 400 response when the version header is absent."""
    app = FastAPI()

    @app.get("/runner", dependencies=[Depends(verify_runner_version)])
    def runner_route():
        return None

    with TestClient(app) as client:
        response = client.get("/runner")

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Missing X-Runner-Version header")


def test_setup_openapi_config_assigns_openapi_callable():
    """Validate Setup openapi config assigns openapi callable."""
    app = FastAPI(title="X", version="1", description="d")
    setup_openapi_config(app)
    schema = app.openapi()
    assert "openapi" in schema


def test_set_openapi_auth_cookie_if_needed_skips_when_builder_returns_none():
    """Validate Set openapi auth cookie if needed skips when builder returns none."""
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
    """Validate Setup protected openapi routes docs uses cookie and no query token."""
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
    """Validate Setup protected openapi routes redoc uses cookie and no query token."""
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
    """Validate Setup protected openapi routes skips rotation when disabled and cookie present."""
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
    """Validate Setup protected openapi routes without token keeps plain openapi url and openapi json works."""
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
    """Validate Openapi config get fastapi config has expected keys."""
    cfg = OpenAPIConfig.get_fastapi_config()
    assert cfg["title"] == OpenAPIConfig.TITLE
    assert cfg["openapi_url"] == OpenAPIConfig.OPENAPI_URL
    assert cfg["docs_url"] == OpenAPIConfig.DOCS_URL
    assert cfg["redoc_url"] == OpenAPIConfig.REDOC_URL
