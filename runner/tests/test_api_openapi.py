import pytest
from fastapi import FastAPI

from app.api.openapi import OpenAPIConfig, custom_openapi, setup_openapi_config


def test_custom_openapi_includes_logo_and_tags():
    app = FastAPI(**OpenAPIConfig.get_fastapi_config())
    setup_openapi_config(app)

    schema = app.openapi()

    assert schema["info"]["title"] == OpenAPIConfig.TITLE
    assert "x-logo" in schema["info"], "OpenAPI schema should expose logo metadata"
    assert schema["components"]["securitySchemes"]["APIKeyHeader"]["type"] == "apiKey"

    tag_names = {t["name"] for t in schema.get("tags", [])}
    assert {"Storage", "Task"}.issubset(tag_names)


@pytest.mark.asyncio
async def test_custom_openapi_cached_schema():
    app = FastAPI(**OpenAPIConfig.get_fastapi_config())
    app.openapi = custom_openapi(app)  # type: ignore[method-assign]

    first = app.openapi()
    second = app.openapi()

    assert first is second, "Schema should be cached after first generation"


def test_enhance_schemas_with_examples():
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
