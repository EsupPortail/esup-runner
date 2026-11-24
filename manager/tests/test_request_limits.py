"""Exercise the request-size boundary with realistic ASGI messages."""

import pytest

from app.core import request_limits


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages,expected_body,expected_status",
    [
        ([{"type": "http.request", "body": b"test"}], b"test", None),
        (
            [
                {"type": "http.request", "body": b"first", "more_body": True},
                {"type": "http.request", "body": b"second"},
            ],
            None,
            413,
        ),
        ([{"type": "http.disconnect"}], None, None),
    ],
)
async def test_limit_checks_the_actual_body(monkeypatch, messages, expected_body, expected_status):
    monkeypatch.setattr(request_limits, "MAX_REQUEST_BODY_BYTES", 5)
    messages = iter([*messages, {"type": "http.disconnect"}])
    received = []
    sent = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    async def app(_, receive, send):
        received.append(await receive())
        received.append(await receive())

    await request_limits.RequestBodyLimitMiddleware(app)({"type": "http"}, receive, send)
    if expected_body is not None:
        assert received == [
            {"type": "http.request", "body": expected_body},
            {"type": "http.disconnect"},
        ]
    else:
        assert not received
    if expected_status is not None:
        assert sent[0]["status"] == expected_status
    else:
        assert not sent


@pytest.mark.asyncio
async def test_forwards_non_http_scopes():
    scope = {"type": "lifespan"}
    visited = []

    async def app(*args):
        visited.append(args)

    await request_limits.RequestBodyLimitMiddleware(app)(scope, pytest.fail, pytest.fail)
    assert visited == [(scope, pytest.fail, pytest.fail)]
