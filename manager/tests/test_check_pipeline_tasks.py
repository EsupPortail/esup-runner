"""Validates manager pipeline smoke-check task payload helpers."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

import scripts.check_pipeline_tasks as pipeline


def test_build_task_request_disables_notify_callback_by_default(monkeypatch):
    """Validate Build task request disables notify callback by default."""
    monkeypatch.delenv("RUNNER_NOTIFY_URL", raising=False)

    request = pipeline._build_task_request(
        "encoding",
        "https://example.com/source.mp4",
        {"rendition": "{}"},
    )

    assert request["notify_url"] == ""


def test_build_task_request_uses_explicit_notify_url(monkeypatch):
    """Validate Build task request uses explicit notify url."""
    monkeypatch.setenv("RUNNER_NOTIFY_URL", " https://callback.example.org/hook ")

    request = pipeline._build_task_request(
        "encoding",
        "https://example.com/source.mp4",
        {"rendition": "{}"},
    )

    assert request["notify_url"] == "https://callback.example.org/hook"


def test_resolve_source_urls_keeps_montpellier_source_first(monkeypatch):
    """Validate Resolve source urls keeps Montpellier media first."""
    monkeypatch.delenv("RUNNER_SOURCE_URL", raising=False)
    monkeypatch.delenv("SOURCE_FILE", raising=False)

    encoding_sources = pipeline._resolve_source_urls(with_transcription_translation=False)
    transcription_sources = pipeline._resolve_source_urls(with_transcription_translation=True)

    assert encoding_sources[0] == pipeline.UMONTPELLIER_TEST_SOURCE_URL
    assert transcription_sources[0] == pipeline.UMONTPELLIER_TEST_SOURCE_URL
    assert pipeline.WIKITONGUES_FRENCH_SOURCE_URL in transcription_sources


def test_resolve_source_urls_keeps_explicit_override_first(monkeypatch):
    """Validate Resolve source urls keeps explicit override first."""
    monkeypatch.setenv("RUNNER_SOURCE_URL", " https://media.example.org/input.mp4 ")

    assert pipeline._resolve_source_urls(with_transcription_translation=True) == [
        "https://media.example.org/input.mp4"
    ]


def test_format_script_output_excerpt_truncates_from_tail(monkeypatch):
    """Validate Format script output excerpt truncates from tail."""
    monkeypatch.setattr(pipeline, "SCRIPT_OUTPUT_EXCERPT_CHARS", 8)

    excerpt = pipeline._format_script_output_excerpt("0123456789abcdef")

    assert "last 8 characters" in excerpt
    assert excerpt.endswith("89abcdef")


@pytest.mark.parametrize(
    "transport_error",
    [
        httpx.ReadError("connection reset"),
        httpx.ReadTimeout("read timed out"),
        httpx.RemoteProtocolError("peer closed connection"),
    ],
    ids=["network-error", "timeout", "remote-protocol-error"],
)
@pytest.mark.asyncio
async def test_wait_for_terminal_state_retries_transient_read_error(
    monkeypatch, capsys, transport_error
):
    """Retry an idempotent status read after a transient connection failure."""
    get_status = AsyncMock(
        side_effect=[
            transport_error,
            {"status": "completed"},
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    result = await pipeline.wait_for_terminal_state(
        AsyncMock(),
        "http://manager",
        "token",
        "task-1",
        max_wait_seconds=10,
    )

    assert result == {"status": "completed"}
    assert get_status.await_count == 2
    sleep.assert_awaited_once_with(float(pipeline.POLL_SECONDS))
    output = capsys.readouterr().out
    assert type(transport_error).__name__ in output
    assert str(transport_error) in output
    assert "retrying" in output


@pytest.mark.asyncio
async def test_wait_for_terminal_state_bounds_in_flight_request(monkeypatch):
    """Cancel a status read that consumes the remaining task deadline."""
    request_started = asyncio.Event()
    request_cancelled = asyncio.Event()

    async def hanging_status(*_args, **_kwargs):
        request_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            request_cancelled.set()

    monkeypatch.setattr(pipeline, "get_task_status", hanging_status)

    with pytest.raises(TimeoutError, match="status request exceeded"):
        await pipeline.wait_for_terminal_state(
            AsyncMock(),
            "http://manager",
            "token",
            "task-1",
            max_wait_seconds=0.01,
        )

    assert request_started.is_set()
    assert request_cancelled.is_set()


@pytest.mark.asyncio
async def test_wait_for_terminal_state_transport_error_at_deadline(monkeypatch):
    """Stop transport retries when the existing task deadline is exhausted."""
    times = iter([0.0, 0.0, 0.9, 1.0])

    class FakeLoop:
        @staticmethod
        def time():
            return next(times)

    transport_error = httpx.ReadError("connection reset")
    get_status = AsyncMock(side_effect=transport_error)
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError, match="last transport error=ReadError") as exc_info:
        await pipeline.wait_for_terminal_state(
            AsyncMock(),
            "http://manager",
            "token",
            "task-1",
            max_wait_seconds=1,
        )

    assert exc_info.value.__cause__ is transport_error
    get_status.assert_awaited_once()
    assert sleep.await_count == 1
    assert sleep.await_args.args[0] == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_wait_for_terminal_state_does_not_retry_http_errors(monkeypatch):
    """Do not hide HTTP application errors returned by the Manager."""
    get_status = AsyncMock(side_effect=RuntimeError("HTTP 503 calling GET /task/status"))
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    with pytest.raises(RuntimeError, match="HTTP 503"):
        await pipeline.wait_for_terminal_state(
            AsyncMock(),
            "http://manager",
            "token",
            "task-1",
            max_wait_seconds=10,
        )

    get_status.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_terminal_state_does_not_retry_unsupported_protocol(monkeypatch):
    """Do not retry permanent client configuration errors."""
    get_status = AsyncMock(side_effect=httpx.UnsupportedProtocol("unsupported protocol"))
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    with pytest.raises(httpx.UnsupportedProtocol, match="unsupported protocol"):
        await pipeline.wait_for_terminal_state(
            AsyncMock(),
            "invalid://manager",
            "token",
            "task-1",
            max_wait_seconds=10,
        )

    get_status.assert_awaited_once()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_for_terminal_state_bounds_normal_poll_sleep(monkeypatch):
    """Do not sleep past the task deadline after a successful status read."""
    times = iter([0.0, 0.0, 0.75, 1.0])

    class FakeLoop:
        @staticmethod
        def time():
            return next(times)

    get_status = AsyncMock(return_value={"status": "running"})
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    with pytest.raises(TimeoutError, match="Last status='running'"):
        await pipeline.wait_for_terminal_state(
            AsyncMock(),
            "http://manager",
            "token",
            "task-1",
            max_wait_seconds=1,
        )

    get_status.assert_awaited_once()
    assert sleep.await_count == 1
    assert sleep.await_args.args[0] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_wait_for_terminal_state_treats_timeout_as_terminal(monkeypatch):
    """Return immediately when the Runner reports its terminal timeout state."""
    get_status = AsyncMock(return_value={"status": "timeout", "error": "script timeout"})
    sleep = AsyncMock()
    monkeypatch.setattr(pipeline, "get_task_status", get_status)
    monkeypatch.setattr(pipeline.asyncio, "sleep", sleep)

    result = await pipeline.wait_for_terminal_state(
        AsyncMock(),
        "http://manager",
        "token",
        "task-1",
        max_wait_seconds=10,
    )

    assert result["status"] == "timeout"
    get_status.assert_awaited_once()
    sleep.assert_not_awaited()
