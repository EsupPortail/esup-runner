# manager/app/api/openapi.py
"""
OpenAPI configuration for Runner Manager API.
Handles custom documentation, tags, and API schema generation.
"""

from typing import Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.__version__ import __author__, __email__, __version__


def custom_openapi(app: FastAPI) -> Callable[[], Dict]:
    """
    Generate custom OpenAPI schema for Runner Manager API.

    Args:
        app: FastAPI application instance

    Returns:
        Callable: Function that generates OpenAPI schema
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

        # Add contact and license info (per OpenAPI spec: inside info)
        if hasattr(app, "contact") and app.contact:
            openapi_schema.setdefault("info", {})
            openapi_schema["info"]["contact"] = app.contact
        if hasattr(app, "license_info") and app.license_info:
            openapi_schema.setdefault("info", {})
            openapi_schema["info"]["license"] = app.license_info

        # Add custom documentation
        openapi_schema["info"]["x-logo"] = {
            "url": "https://fastapi.tiangolo.com/img/logo-margin/logo-teal.png",
            "altText": "Runner Manager API Logo",
        }

        # Assign tags to endpoints based on route paths
        # Useless for the moment. Kept in case it is needed for a future version.
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
            "name": "API",
            "description": "Endpoints for general API information",
        },
        {
            "name": "Logs",
            "description": "Endpoints for log management",
        },
        {
            "name": "Manager",
            "description": "Endpoints for runer manager management",
        },
        {
            "name": "Runner",
            "description": "Endpoints for managing and monitoring runners",
        },
        {
            "name": "Task",
            "description": "Endpoints for task management and execution",
        },
    ]


def _assign_tags_to_endpoints(openapi_schema: Dict) -> None:
    """
    Assign appropriate tags to endpoints based on their paths.
    Function not used to date. Kept in case it is needed for a future version.

    Args:
        openapi_schema: OpenAPI schema to modify
    """
    for path, methods in openapi_schema["paths"].items():
        for method, details in methods.items():
            # Assign tags based on path patterns
            if "admin" in path:
                details["tags"] = ["Admin"]
            elif "health" in path or "ping" in path:
                details["tags"] = ["Health"]
            elif "register" in path or "heartbeat" in path:
                details["tags"] = ["Runner"]
            elif "task" in path:
                details["tags"] = ["Task"]
            elif "auth" in path or "token" in path:
                details["tags"] = ["Authentication"]
            else:
                details["tags"] = ["API"]


def _add_security_schemes(openapi_schema: Dict) -> None:
    """
    Add security schemes to OpenAPI schema.

    Args:
        openapi_schema: OpenAPI schema to modify
    """
    openapi_schema["components"] = openapi_schema.get("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "Bearer": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Enter your token in the format: Bearer <token>",
        },
        "APIKeyHeader": {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Token",
            "description": "Enter your token in the X-API-Token header.",
        },
    }

    # Appliquer la sécurité à tous les endpoints par défaut
    for path in openapi_schema["paths"].values():
        for method in path.values():
            method["security"] = [{"Bearer": []}, {"APIKeyHeader": []}]


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
            "url": "http://runner.example.com:8080",
            "task_types": ["encoding"],
            "token": "tohken-runner-123",
        }

    # Add example for Task model
    if "Task" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["Task"]["example"] = {
            "id": "task-abc-123",
            "runner_id": "runner-123",
            "status": "running",
            "etab_name": "University of Example",
            "app_name": "Pod",
            "app_version": "4.0.2",
            "task_type": "encoding",
            "source_url": "https://example.com/video.mp4",
            "notify_url": "https://example.com/notify",
            "parameters": {"param1": "value1", "param2": "value2"},
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:30:00Z",
        }

    # Add example for TaskRequest model
    if "TaskRequest" in openapi_schema["components"]["schemas"]:
        openapi_schema["components"]["schemas"]["TaskRequest"]["example"] = {
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


def setup_protected_openapi_routes(app: FastAPI) -> None:
    """
    Set up protected OpenAPI routes that require authentication.

    This function overrides the default /docs, /redoc, and /openapi.json routes
    to require token authentication when API_DOCS_VISIBILITY is "private".

    Args:
        app: FastAPI application instance
    """
    from app.core.auth import verify_openapi_token

    # Set openapi_url for generating the schema
    if not app.openapi_url:
        app.openapi_url = "/openapi.json"

    # Store the non-null openapi_url for use in routes
    openapi_url: str = app.openapi_url

    # Override /docs route
    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html(
        request: Request, token: Optional[str] = Depends(verify_openapi_token)
    ):
        """Protected Swagger UI documentation."""
        # Add token to openapi_url if authentication is required
        # Only add token if it's not None (i.e., when API_DOCS_VISIBILITY is private)
        if token:
            openapi_url_with_token = f"{openapi_url}?token={token}"
        else:
            openapi_url_with_token = openapi_url

        return get_swagger_ui_html(
            openapi_url=openapi_url_with_token,
            title=app.title + " - Swagger UI",
            oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )

    # Override /redoc route
    @app.get("/redoc", include_in_schema=False)
    async def custom_redoc_html(
        request: Request, token: Optional[str] = Depends(verify_openapi_token)
    ):
        """Protected ReDoc documentation."""
        # Add token to openapi_url if authentication is required
        # Only add token if it's not None (i.e., when API_DOCS_VISIBILITY is private)
        if token:
            openapi_url_with_token = f"{openapi_url}?token={token}"
        else:
            openapi_url_with_token = openapi_url

        return get_redoc_html(
            openapi_url=openapi_url_with_token,
            title=app.title + " - ReDoc",
            redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js",
        )

    # Override /openapi.json route
    @app.get("/openapi.json", include_in_schema=False)
    async def custom_openapi_json(
        request: Request, token: Optional[str] = Depends(verify_openapi_token)
    ):
        """Protected OpenAPI schema."""
        return JSONResponse(app.openapi())


class OpenAPIConfig:
    """
    Configuration class for OpenAPI documentation settings.
    """

    # API Metadata
    TITLE = "Runner Manager API"
    DESCRIPTION = """
## Runner Manager API

A distributed task runner management system that allows you to:

* **Register and manage runners** - Dynamically add and monitor task execution nodes
* **Execute distributed tasks** - Distribute tasks across available runners
* **Monitor system health** - Track runner availability and task status
* **Administrative dashboard** - Web interface for system management

### Authentication

This API uses API key authentication. Include your API key in the `X-API-Token` header.

### Web Interface

Visit `/admin` for the administrative dashboard, `/docs` or `/redoc` for interactive API documentation.
"""
    VERSION = __version__
    CONTACT = {
        "name": __author__,
        "email": __email__,
    }
    LICENSE_INFO = {
        "name": "GNU General Public License v3.0",
        "url": "https://www.gnu.org/licenses/gpl-3.0.en.html",
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
