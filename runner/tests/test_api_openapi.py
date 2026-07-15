"""Validates OpenAPI schema generation, caching, and example enhancement for API documentation."""

import pytest
from fastapi import FastAPI

from app.api.openapi import OpenAPIConfig, custom_openapi, setup_openapi_config


def test_custom_openapi_includes_logo_and_tags():
    """Validate Custom openapi includes logo and tags."""
    app = FastAPI(**OpenAPIConfig.get_fastapi_config())
    setup_openapi_config(app)

    schema = app.openapi()

    assert schema["info"]["title"] == OpenAPIConfig.TITLE
    assert "x-logo" in schema["info"], "OpenAPI schema should expose logo metadata"
    assert schema["info"]["contact"] == OpenAPIConfig.CONTACT
    assert schema["info"]["license"] == OpenAPIConfig.LICENSE_INFO
    assert "contact" not in schema
    assert "license" not in schema

    tag_names = {t["name"] for t in schema.get("tags", [])}
    assert {"Storage", "Task"}.issubset(tag_names)


def test_app_openapi_includes_task_stop_route():
    """Validate runner app OpenAPI exposes the task stop endpoint."""
    from app.main import app as runner_app

    schema = runner_app.openapi()

    assert "/task/stop/{task_id}" in schema["paths"]
    assert "post" in schema["paths"]["/task/stop/{task_id}"]

    declared_schemes = schema["components"]["securitySchemes"]
    assert declared_schemes["APIKeyHeader"]["name"] == "X-API-Token"
    assert declared_schemes["HTTPBearer"]["scheme"] == "bearer"

    referenced_schemes = {
        scheme_name
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for requirement in operation.get("security", [])
        for scheme_name in requirement
    }
    assert referenced_schemes == {"APIKeyHeader", "HTTPBearer"}
    assert referenced_schemes <= declared_schemes.keys()
    assert "security" not in schema["paths"]["/"]["get"]


@pytest.mark.asyncio
async def test_custom_openapi_cached_schema():
    """Validate Custom openapi cached schema."""
    app = FastAPI(**OpenAPIConfig.get_fastapi_config())
    app.openapi = custom_openapi(app)  # type: ignore[method-assign]

    first = app.openapi()
    second = app.openapi()

    assert first is second, "Schema should be cached after first generation"


def test_enhance_schemas_with_examples():
    """Validate Enhance schemas with examples."""
    from app.api.openapi import _enhance_schemas_with_examples

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
