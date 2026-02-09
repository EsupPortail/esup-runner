"""OpenAPI authentication regression tests."""

import pytest

from app.core.config import config

# OpenAPI routes to test
OPENAPI_ROUTES = ("/docs", "/redoc", "/openapi.json")


@pytest.mark.filterwarnings("ignore:Duplicate Operation ID.*:UserWarning")
@pytest.mark.parametrize("route", OPENAPI_ROUTES)
def test_openapi_access_control(client, auth_headers, route):
    """Ensure OpenAPI routes respect API_DOCS_VISIBILITY."""

    if config.API_DOCS_VISIBILITY == "public":
        response = client.get(route)
        assert response.status_code == 200
    else:
        # Private mode: no token -> 401
        unauthorized = client.get(route)
        assert unauthorized.status_code == 401

        # Private mode: token -> 200
        authorized = client.get(route, headers={"Authorization": auth_headers["Authorization"]})
        assert authorized.status_code == 200
