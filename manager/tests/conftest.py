"""Pytest configuration and fixtures for the Manager test suite."""

import os
import sys
import warnings
from typing import Dict

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Silence stdlib crypt deprecation emitted during passlib import when running with -W error
warnings.filterwarnings(
    "ignore",
    message="'crypt' is deprecated and slated for removal in Python 3.13",
    category=DeprecationWarning,
)
warnings.filterwarnings("ignore", message="Duplicate Operation ID.*", category=UserWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="fastapi.openapi.utils")

from app.core.config import config


@pytest.fixture(autouse=True)
def ensure_test_tokens(monkeypatch):
    """Ensure at least one authorized token exists for tests."""

    original_tokens: Dict[str, str] = dict(config.AUTHORIZED_TOKENS)

    if not config.AUTHORIZED_TOKENS:
        monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"test": "test-token"})

    yield

    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", original_tokens)


@pytest.fixture
def auth_headers() -> Dict[str, str]:
    """Return bearer and API key headers using the first configured token."""

    token = next(iter(config.AUTHORIZED_TOKENS.values()))
    return {
        "Authorization": f"Bearer {token}",
        "X-API-Token": token,
    }


@pytest.fixture
def client(monkeypatch):
    """Test client with background services disabled for fast, deterministic runs."""

    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import background_service

    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    with TestClient(app) as test_client:
        yield test_client
