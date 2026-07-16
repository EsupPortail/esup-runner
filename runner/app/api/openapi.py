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

        openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            contact=app.contact,
            license_info=app.license_info,
        )

        openapi_schema["tags"] = _get_openapi_tags()

        openapi_schema["info"]["x-logo"] = {
            "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png",
            "altText": "Runner API Logo",
        }

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


def _enhance_schemas_with_examples(openapi_schema: Dict) -> None:
    """
    Enhance schemas with examples for better documentation.

    Args:
        openapi_schema: OpenAPI schema to modify
    """
    if "components" not in openapi_schema:
        openapi_schema["components"] = {}

    if "schemas" not in openapi_schema["components"]:
        openapi_schema["components"]["schemas"] = {}

    if "Runner" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["Runner"]["example"] = {
            "id": "runner-123",
            "url": "https://runner.example.com:8080",
        }

    if "Task" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["Task"]["example"] = {
            "id": "task-abc-123",
            "runner_id": "runner-123",
            "status": "running",
            "task_type": "data_processing",
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:30:00Z",
        }

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
        "name": "GNU General Public License v3.0",
        "url": "https://www.gnu.org/licenses/gpl-3.0.html",
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
