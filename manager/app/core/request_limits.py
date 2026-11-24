"""Limit incoming HTTP bodies before the application parses them."""

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024


class RequestBodyLimitMiddleware:
    """Buffer at most the allowed request size and reject larger bodies."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application while leaving non-HTTP scopes untouched."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the actual body size, including requests sent in chunks."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > MAX_REQUEST_BODY_BYTES:
                response = JSONResponse({"detail": "Request body too large"}, status_code=413)
                await response(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        replay: Message | None = {"type": "http.request", "body": bytes(body)}

        async def receive_body() -> Message:
            """Replay the validated body once, then forward disconnects."""
            nonlocal replay
            if replay is not None:
                message, replay = replay, None
                return message
            return await receive()

        await self.app(scope, receive_body, send)
