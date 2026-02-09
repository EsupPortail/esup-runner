# manager/app/main.py
"""
Runner Manager API
------------------
This module defines a FastAPI application for managing distributed runners and executing tasks asynchronously.
It handles:
- Runner registration and heartbeat monitoring
- Task execution delegation to runners
- Task lifecycle (pending, running, completed, failed, timeout)
- Admin dashboards and API endpoints for monitoring
"""

import signal
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.__version__ import __version__
from app.api.openapi import OpenAPIConfig, setup_openapi_config, setup_protected_openapi_routes
from app.core import config as config_module
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.services.background_service import background_manager

# Configure logging
logger = setup_default_logging()


def _register_sighup_reload():
    """Register SIGHUP handler in the worker process to reload config."""
    try:
        signal.signal(signal.SIGHUP, lambda signum, frame: config_module.reload_config_env())
    except Exception as exc:  # signal may not be available on some platforms
        logger.warning(f"Failed to register SIGHUP reload handler: {exc}")


_register_sighup_reload()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for FastAPI application.

    Handles startup and shutdown events including:
    - Update checks
    - Background task management
    - Resource cleanup

    Args:
        app: FastAPI application instance
    """
    # Startup logic
    logger.info("Starting Runner Manager application")

    # Import and include routers once per app instance.
    # In tests, the same global `app` is started/stopped many times via TestClient.
    # Re-including routers on every startup duplicates routes and can trigger recursion
    # issues during schema/dependency construction.
    if not getattr(app.state, "routers_included", False):
        # Import routers inside lifespan to avoid circular imports at module level
        from app.api.routes import admin, api, logs, manager, runner, statistics, task

        # Include routers
        app.include_router(admin.router)
        app.include_router(api.router)
        app.include_router(manager.router)
        app.include_router(runner.router)
        app.include_router(task.router)
        app.include_router(logs.router)
        app.include_router(statistics.router)

        # Setup protected OpenAPI routes if authentication is enabled
        if config.API_DOCS_VISIBILITY == "private":
            setup_protected_openapi_routes(app)

        app.state.routers_included = True

    # Start background services
    await background_manager.start_all_services()

    yield

    # Shutdown logic
    logger.info("Shutting down Runner Manager application")

    # Stop background services
    await background_manager.stop_all_services()


# FastAPI application configuration
# Disable default OpenAPI routes if authentication is enabled
openapi_config = OpenAPIConfig.get_fastapi_config()
if config.API_DOCS_VISIBILITY == "private":
    # Disable default routes, we'll create protected ones in lifespan
    openapi_config["docs_url"] = None
    openapi_config["redoc_url"] = None
    openapi_config["openapi_url"] = None

app = FastAPI(lifespan=lifespan, **openapi_config)

# Rate limiting configuration
limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Setup custom OpenAPI configuration
setup_openapi_config(app)

# CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ALLOW_ORIGINS,
    allow_credentials=config.CORS_ALLOW_CREDENTIALS,
    allow_methods=config.CORS_ALLOW_METHODS,
    allow_headers=config.CORS_ALLOW_HEADERS,
)

# Static files and templates
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
templates = Jinja2Templates(directory="app/web/templates")


@app.get(
    "/",
    summary="Root health endpoint",
    description="Health check endpoint to verify manager is running properly",
    tags=["Manager"],
)
async def root():
    """
    Root endpoint with API information and links. Not protected, always available.

    Returns:
        Dict: API information and available endpoints
    """
    return {
        "message": "Runner Manager",
        "version": __version__,
        "health_check": "/manager/health",
        "admin_dashboard": "/admin",
        "api_docs_visibility": config.API_DOCS_VISIBILITY,
        "documentation": {"swagger": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
    }
