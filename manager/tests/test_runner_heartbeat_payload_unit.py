"""Unit coverage for runner heartbeat payload handling."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.api.routes import runner as runner_routes
from app.core.state import runners
from app.models.models import Runner


@pytest.fixture
def clean_runners_state():
    original = dict(runners)
    runners.clear()
    yield
    runners.clear()
    runners.update(original)


@pytest.mark.asyncio
async def test_runner_heartbeat_updates_availability_from_payload(clean_runners_state):
    """Validate Runner heartbeat updates availability from payload."""
    runners["r1"] = Runner(
        id="r1",
        url="http://r1.example",
        task_types=["encoding"],
        status="online",
        availability="available",
        last_heartbeat=datetime.now(),
        token="tok-ok",
        version="1.0.0",
    )

    response = await runner_routes.runner_heartbeat(
        runner_id="r1",
        payload=runner_routes.RunnerHeartbeatPayload(availability="busy"),
        current_token="tok-ok",
        current_version="1.0.0",
    )

    assert response == {"status": "ok"}
    assert runners["r1"].availability == "busy"
