# runner/app/api/openapi.py
"""OpenAPI configuration for Runner API.

Keeps the exposed OpenAPI/FastAPI version aligned with the Runner package version.
"""

from typing import Callable, Dict, List

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.__version__ import __version__


def custom_openapi(app: FastAPI) -> Callable[[], Dict]:
    """
    Generate custom OpenAPI schema for Runner API.

    Args:
        app: FastAPI application instance

    Returns:
        callable: Function that generates OpenAPI schema
    """

    def _custom_openapi() -> Dict:
        if app.openapi_schema:
            return app.openapi_schema

        # Generate base OpenAPI schema
        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )

        # Add custom tags for organization
        openapi_schema["tags"] = _get_openapi_tags()

        # Add contact and license info
        if hasattr(app, "contact") and app.contact:
            openapi_schema["contact"] = app.contact
        if hasattr(app, "license_info") and app.license_info:
            openapi_schema["license"] = app.license_info

        # Add custom documentation
        openapi_schema["info"]["x-logo"] = {
            "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png",
            "altText": "Runner API Logo",
        }

        # Assign tags to endpoints based on route paths
        # _assign_tags_to_endpoints(openapi_schema)

        # Add security schemes
        _add_security_schemes(openapi_schema)

        # Add examples and response schemas
        _enhance_schemas_with_examples(openapi_schema)

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    return _custom_openapi


def _get_openapi_tags() -> List[Dict]:
    """
    Define OpenAPI tags for organizing endpoints in documentation.

    Returns:
        List[Dict]: List of tag definitions
    """
    return [
        {
            "name": "Storage",
            "description": "Endpoints for storage management",
        },
        {
            "name": "Task",
            "description": "Endpoints for task management and execution",
        },
    ]


"""
def _assign_tags_to_endpoints(openapi_schema: Dict) -> None:

    Assign appropriate tags to endpoints based on their paths.

    Args:
        openapi_schema: OpenAPI schema to modify

    for path, methods in openapi_schema["paths"].items():
        for method, details in methods.items():
            # Assign tags based on path patterns
            if "health" in path or "ping" in path:
                details["tags"] = ["Health"]
            elif "storage" in path:
                details["tags"] = ["Storage"]
            elif "task" in path:
                details["tags"] = ["Tasks"]
            elif "auth" in path or "token" in path:
                details["tags"] = ["Authentication"]
            else:
                details["tags"] = ["API"]
"""


def _add_security_schemes(openapi_schema: Dict) -> None:
    """
    Add security schemes to OpenAPI schema.

    Args:
        openapi_schema: OpenAPI schema to modify
    """
    openapi_schema["components"] = openapi_schema.get("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "APIKeyHeader": {
            "type": "apiKey",
            "name": "X-API-Token",
            "in": "header",
            "description": "API Key for authentication",
        }
    }


def _enhance_schemas_with_examples(openapi_schema: Dict) -> None:
    """
    Enhance schemas with examples for better documentation.

    Args:
        openapi_schema: OpenAPI schema to modify
    """
    # Add examples to components if they exist
    if "components" not in openapi_schema:
        openapi_schema["components"] = {}

    if "schemas" not in openapi_schema["components"]:
        openapi_schema["components"]["schemas"] = {}

    # Add example for Runner model
    if "Runner" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["Runner"]["example"] = {
            "id": "runner-123",
            "url": "https://runner.example.com:8080",
        }

    # Add example for Task model
    if "Task" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["Task"]["example"] = {
            "id": "task-abc-123",
            "runner_id": "runner-123",
            "status": "running",
            "task_type": "data_processing",
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:30:00Z",
        }

    # Add example for TaskRequest model
    if "TaskRequest" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["TaskRequest"]["example"] = {
            "task_id": "task-abc-123",
            "etab_name": "University of Example",
            "app_name": "Pod",
            "app_version": "4.0.2",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "notify_url": "https://example.com/notify",
            "parameters": {"param1": "value1", "param2": "value2"},
        }


def setup_openapi_config(app: FastAPI) -> None:
    """
    Set up custom OpenAPI configuration for FastAPI app.

    Args:
        app: FastAPI application instance to configure
    """
    app.openapi = custom_openapi(app)  # type: ignore[method-assign]


class OpenAPIConfig:  # pragma: no cover
    """
    Configuration class for OpenAPI documentation settings.
    """

    # API Metadata
    TITLE = "Runner API"
    DESCRIPTION = """  # pragma: no cover
## Runner API

A distributed task runner system that allows you to:

* **Register and manage runners** - Dynamically add and monitor task execution nodes
* **Submit and track tasks** - Create, monitor, and retrieve results of tasks executed by runners

### Authentication

This API uses API key authentication. Include your API key in the `X-API-Token` header or in the `Bearer <token>` format.

### Web Interface

Visit `/docs` for interactive API documentation.
"""
    VERSION = __version__
    CONTACT = {
        "name": "Loïc Bonavent",
        "email": "loic.bonavent@umontpellier.fr",
    }
    LICENSE_INFO = {
        "name": "MIT License",
        "url": "https://opensource.org/licenses/MIT",
    }

    # OpenAPI specific settings
    OPENAPI_URL = "/openapi.json"
    DOCS_URL = "/docs"
    REDOC_URL = "/redoc"

    @classmethod
    def get_fastapi_config(cls) -> Dict:
        """
        Get FastAPI configuration for OpenAPI.

        Returns:
            Dict: Configuration dictionary for FastAPI app
        """
        return {
            "title": cls.TITLE,
            "description": cls.DESCRIPTION,
            "version": cls.VERSION,
            "contact": cls.CONTACT,
            "license_info": cls.LICENSE_INFO,
            "openapi_url": cls.OPENAPI_URL,
            "docs_url": cls.DOCS_URL,
            "redoc_url": cls.REDOC_URL,
        }
