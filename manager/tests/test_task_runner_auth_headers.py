import pytest
from fastapi import HTTPException

from app.api.routes import task as task_routes
from app.models.models import Runner


def test_runner_auth_headers_raises_when_token_missing():
    runner = Runner(id="r1", url="http://example.org:8082", task_types=["encoding"], token=None)

    with pytest.raises(HTTPException) as exc:
        task_routes._runner_auth_headers(runner, accept="application/json")

    assert exc.value.status_code == 503
