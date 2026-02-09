# runner/app/main.py
"""
Main FastAPI application for Runner with multi-instance support.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.openapi import OpenAPIConfig, setup_openapi_config
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.core.state import set_runner_instance_id, set_runner_instance_url
from app.managers.service_manager import background_manager
from app.services.manager_service import register_with_manager

# Instance-specific configurations
runner_instance_id: int = int(os.getenv("RUNNER_INSTANCE_ID", "0"))
runner_instance_port: int = int(os.getenv("RUNNER_PORT", "8081"))
runner_instance_url: str = os.getenv(
    "RUNNER_INSTANCE_URL", f"{config.RUNNER_PROTOCOL}://{config.RUNNER_HOST}:{runner_instance_port}"
)

# Set instance-specific runner informations
set_runner_instance_id(
    runner_instance_id=runner_instance_id,
    runner_base_name=config.RUNNER_BASE_NAME,
    runner_host=config.RUNNER_HOST,
    runner_instance_port=runner_instance_port,
)
set_runner_instance_url(runner_instance_url)

# Configure logging with instance ID
logger = setup_default_logging()

logger.info(f"Starting runner instance {runner_instance_id} on port {runner_instance_port}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for FastAPI application.

    Handles startup and shutdown events including:
    - Background task management
    - Resource cleanup

    Args:
        app: FastAPI application instance
    """
    # Startup logic
    logger.info(f"Runner instance {runner_instance_id} started successfully")

    # Import routers inside lifespan to avoid circular imports at module level
    from app.api.routes import runner, task

    # Include routers
    app.include_router(task.router)
    app.include_router(runner.router)

    # Initial registration with Manager
    await register_with_manager()

    # Start background services
    await background_manager.start_all_services()

    yield

    # Shutdown logic
    logger.info(f"Shutting down runner instance {runner_instance_id}")

    # Stop background services
    await background_manager.stop_all_services()


# FastAPI application configuration
app = FastAPI(lifespan=lifespan, **OpenAPIConfig.get_fastapi_config())

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


@app.get("/", tags=["Runner"])
async def root():
    """
    Root endpoint with API information and links.

    Returns:
        Dict: API information and available endpoints
    """
    return {
        "message": "Runner API",
        "version": OpenAPIConfig.VERSION,
        "documentation": {"swagger": "/docs", "redoc": "/redoc", "openapi": "/openapi.json"},
        "health_check": "/runner/health",
    }
