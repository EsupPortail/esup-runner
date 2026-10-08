"""Tests for task result retrieval and streaming."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from task_routes_helpers import (
    clean_state,
    client,
)
from task_routes_helpers import make_runner as _runner
from task_routes_helpers import make_task as _task
from task_routes_helpers import task_module

from app.core.state import runners, tasks
from app.models.models import Runner, Task

__all__ = ["clean_state", "client", "task_module"]


def test_runner_auth_headers_raises_when_runner_token_missing(task_module):
    """Validate Runner auth headers raises when runner token missing."""
    runner = Runner(id="r1", url="http://r1.example", task_types=["encoding"], token=None)

    with pytest.raises(HTTPException) as exc:
        task_module._runner_auth_headers(runner, accept="application/json")

    assert exc.value.status_code == 503


def test_validate_result_path_rejects_traversal(task_module):
    """Validate Validate result path rejects traversal."""
    with pytest.raises(Exception):
        task_module._validate_result_path("../secret")


def test_resolve_shared_storage_base_errors(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base errors."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path / "nope"))

    with pytest.raises(Exception):
        task_module._resolve_shared_storage_base()


def test_resolve_shared_storage_base_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base resolve exception."""
    (tmp_path / "base").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path / "base"))

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._resolve_shared_storage_base()
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_resolve_shared_storage_base_happy_path(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base happy path."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    base = task_module._resolve_shared_storage_base()
    assert base.exists() and base.is_dir()


def test_resolve_shared_storage_base_prefers_new_var_name(monkeypatch, task_module, tmp_path: Path):
    """Validate Resolve shared storage base prefers new var name."""
    new_base = tmp_path / "new-storage"
    legacy_base = tmp_path / "legacy-storage"
    new_base.mkdir()
    legacy_base.mkdir()

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(new_base), raising=False)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_PATH", str(legacy_base), raising=False)

    base = task_module._resolve_shared_storage_base()
    assert base == new_base.resolve()


def test_get_local_task_dir_rejects_outside_base(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir rejects outside base."""
    tmp_path.mkdir(exist_ok=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("../evil")
    assert exc.value.status_code == 500


def test_get_local_task_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir 404 when missing."""
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_task_dir("t-missing")
    assert exc.value.status_code == 404


def test_get_local_task_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local task dir resolve exception."""
    monkeypatch.setattr(task_module, "_resolve_shared_storage_base", lambda: tmp_path)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_task_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_resolve_exception(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir resolve exception."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_output_dir("t1")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_output_dir_rejects_symlink_outside(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir rejects symlink outside."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (task_dir / "output").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 500


def test_get_local_output_dir_404_when_missing(monkeypatch, task_module, tmp_path: Path):
    """Validate Get local output dir 404 when missing."""
    task_dir = tmp_path / "t1"
    task_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))
    with pytest.raises(HTTPException) as exc:
        task_module._get_local_output_dir("t1")
    assert exc.value.status_code == 404


def test_mark_warning_as_completed_calls_save_tasks(monkeypatch, task_module, clean_state):
    """Validate Mark warning as completed calls save tasks."""
    tasks["t1"] = _task("t1", "r1", status="warning")

    called = {"count": 0}

    def fake_save():
        called["count"] += 1

    monkeypatch.setattr(task_module, "save_tasks", fake_save)
    task_module._mark_warning_as_completed("t1")
    assert tasks["t1"].status == "completed"
    assert called["count"] == 1


def test_get_local_manifest_and_file_happy_path(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Get local manifest and file happy path."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    base = tmp_path
    (base / "t1" / "output").mkdir(parents=True)
    (base / "t1" / "manifest.json").write_text(json.dumps({"files": ["a.txt"]}), encoding="utf-8")
    (base / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(base))

    manifest_resp = task_module._get_local_manifest(tasks["t1"])
    assert manifest_resp.status_code == 200
    assert manifest_resp.headers["X-Task-ID"] == "t1"
    assert tasks["t1"].status == "completed"  # warning -> completed

    file_resp = task_module._stream_local_file(tasks["t1"], "a.txt")
    assert file_resp.status_code == 200


def test_get_local_manifest_missing_file_404(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Get local manifest missing file 404."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._get_local_manifest(tasks["t1"])
    assert exc.value.status_code == 404


def test_get_local_manifest_resolve_exception_500(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Get local manifest resolve exception 500."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    task_dir = tmp_path / "t1"
    task_dir.mkdir()
    monkeypatch.setattr(task_module, "_get_local_task_dir", lambda _task_id: task_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._get_local_manifest(tasks["t1"])
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


def test_get_local_manifest_invalid_json(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Get local manifest invalid json."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text("{not json", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(Exception):
        task_module._get_local_manifest(tasks["t1"])


def test_stream_local_file_missing(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Stream local file missing."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(Exception):
        task_module._stream_local_file(tasks["t1"], "missing.txt")


def test_stream_local_file_rejects_path_outside_output(
    monkeypatch, task_module, clean_state, tmp_path: Path
):
    """Validate Stream local file rejects path outside output."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    with pytest.raises(HTTPException) as exc:
        task_module._stream_local_file(tasks["t1"], "../evil")
    assert exc.value.status_code == 400


def test_stream_local_file_resolve_exception(monkeypatch, task_module, clean_state, tmp_path: Path):
    """Validate Stream local file resolve exception."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    output_dir = tmp_path / "t1" / "output"
    output_dir.mkdir(parents=True)
    monkeypatch.setattr(task_module, "_get_local_output_dir", lambda _task_id: output_dir)

    original_resolve = task_module.PathlibPath.resolve

    def boom(self, *_, **__):
        raise RuntimeError("nope")

    monkeypatch.setattr(task_module.PathlibPath, "resolve", boom)
    try:
        with pytest.raises(HTTPException) as exc:
            task_module._stream_local_file(tasks["t1"], "a.txt")
        assert exc.value.status_code == 500
    finally:
        monkeypatch.setattr(task_module.PathlibPath, "resolve", original_resolve)


# -----------------------------
# Runner streaming helpers
# -----------------------------


class _FakeHTTPXResponse:
    def __init__(
        self, *, status_code: int = 200, body: bytes = b"ok", headers: dict[str, str] | None = None
    ):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = body.decode("utf-8", errors="ignore")
        self.closed = False

    async def aiter_bytes(self, chunk_size: int = 64 * 1024) -> AsyncIterator[bytes]:
        for offset in range(0, len(self._body), chunk_size):
            yield self._body[offset : offset + chunk_size]

    async def aclose(self) -> None:
        self.closed = True


class _FakeHTTPXClient:
    def __init__(self, response: _FakeHTTPXResponse | Exception):
        self._response_or_exc = response
        self.closed = False

    def build_request(self, method, url, **kwargs):
        timeout = kwargs.pop("timeout")
        return httpx.Request(method, url, extensions={"timeout": timeout.as_dict()}, **kwargs)

    async def send(self, *_args, **_kwargs):
        if isinstance(self._response_or_exc, Exception):
            raise self._response_or_exc
        return self._response_or_exc

    async def aclose(self) -> None:
        self.closed = True


class _TrackingHTTPXStream(httpx.AsyncByteStream):
    """Track reads and closure without buffering a complete result file."""

    def __init__(self):
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self):
        for _ in range(4):
            self.chunks_read += 1
            yield b"x" * (64 * 1024)

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("file_path", [None, "sub dir/video.ts"])
async def test_runner_result_stream_reads_only_when_consumed(
    monkeypatch, task_module, clean_state, file_path
):
    """HTTPX must leave the body unread until the downstream client consumes it."""
    runner = _runner("r1")
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task
    stream = _TrackingHTTPXStream()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=stream)

    upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda: upstream_client)
    try:
        if file_path is None:
            result = await task_module._stream_runner_manifest(task, runner)
            expected_path = b"/task/result/t1"
            expected_accept = "application/json"
        else:
            result = await task_module._stream_runner_file(task, runner, file_path)
            expected_path = b"/task/result/t1/file/sub%20dir/video.ts"
            expected_accept = "application/octet-stream"

        assert stream.chunks_read == 0
        assert not stream.closed
        assert not upstream_client.is_closed
        assert requests[0].url.raw_path == expected_path
        assert requests[0].headers["Authorization"] == "Bearer tok"
        assert requests[0].headers["Accept"] == expected_accept
        assert requests[0].extensions["timeout"] == {
            "connect": 5.0,
            "read": None,
            "write": None,
            "pool": 5.0,
        }
        assert result.headers["X-Task-ID"] == "t1"

        first_chunk = await anext(result.body_iterator)
        assert first_chunk == b"x" * (64 * 1024)
        assert stream.chunks_read == 1
        remaining = [chunk async for chunk in result.body_iterator]
        assert len(remaining) == 3
        assert stream.closed
        assert upstream_client.is_closed
    finally:
        await upstream_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("disconnect_at", ["headers", "body"])
async def test_runner_file_disconnect_closes_upstream(
    monkeypatch, task_module, clean_state, disconnect_at
):
    """A disconnected client must release even an unread upstream response."""
    runner = _runner("r1")
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task
    stream = _TrackingHTTPXStream()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )
    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda: upstream_client)
    result = await task_module._stream_runner_file(task, runner, "video.ts")

    async def receive():
        await asyncio.Event().wait()

    async def send(message):
        if disconnect_at == "headers" or message["type"] == "http.response.body":
            raise OSError("client disconnected")

    try:
        with pytest.raises(Exception):
            await result({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert stream.closed
        assert upstream_client.is_closed
        assert stream.chunks_read == (0 if disconnect_at == "headers" else 1)
    finally:
        await result.body_iterator.aclose()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_runner_file_asgi_disconnect_shields_upstream_cleanup(
    monkeypatch, task_module, clean_state
):
    """ASGI disconnect cancellation must allow asynchronous upstream cleanup to finish."""
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task
    first_chunk_sent = asyncio.Event()

    class PausingStream(_TrackingHTTPXStream):
        async def __aiter__(self):
            self.chunks_read += 1
            yield b"x" * (64 * 1024)
            await asyncio.Event().wait()

        async def aclose(self):
            await asyncio.sleep(0)
            await super().aclose()

    stream = PausingStream()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )
    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda: upstream_client)
    result = await task_module._stream_runner_file(task, _runner("r1"), "video.ts")

    async def receive():
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body":
            first_chunk_sent.set()

    try:
        await result({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        assert stream.chunks_read == 1
        assert stream.closed
        assert upstream_client.is_closed
    finally:
        await result.body_iterator.aclose()
        await upstream_client.aclose()


@pytest.mark.asyncio
async def test_runner_result_error_is_logged_without_reading_body(task_module, clean_state, caplog):
    """Report the runner and resource without consuming an error body or logging secrets."""
    caplog.set_level(logging.INFO, logger="httpx")
    stream = _TrackingHTTPXStream()
    runner = _runner("r1")
    url = "http://user:password@r1.example/task/result/t1/file/video.ts?token=secret#private"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404, stream=stream))
    ) as upstream_client:
        with pytest.raises(HTTPException) as exc:
            await task_module._fetch_runner_resource(
                upstream_client, runner, url, httpx.Timeout(1.0), "application/octet-stream"
            )
    assert exc.value.status_code == 404
    assert stream.chunks_read == 0
    assert stream.closed
    manager_errors = [
        record.getMessage()
        for record in caplog.records
        if record.name == task_module.logger.name and record.levelno == logging.ERROR
    ]
    assert len(manager_errors) == 1
    message = manager_errors[0]
    assert "runner_id=r1" in message
    assert "http://r1.example/task/result/t1/file/video.ts" in message
    assert "404" in message
    for secret in ("user", "password", "token=secret", "private", runner.token):
        assert secret not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ReadError("broken stream"), asyncio.CancelledError()])
async def test_runner_result_interrupted_read_closes_upstream(
    monkeypatch, task_module, clean_state, failure
):
    """Read errors and cancellation after headers must release the upstream connection."""
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task

    class InterruptedStream(_TrackingHTTPXStream):
        async def __aiter__(self):
            yield b"x" * (64 * 1024)
            raise failure

    stream = InterruptedStream()
    upstream_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
    )
    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda: upstream_client)
    result = await task_module._stream_runner_file(task, _runner("r1"), "video.ts")
    with pytest.raises(type(failure)):
        async for _ in result.body_iterator:
            pass
    assert stream.closed
    assert upstream_client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize("resource", ["manifest", "file"])
@pytest.mark.parametrize("failure_at", ["fetch", "build"])
async def test_runner_result_pre_stream_failure_closes_resources(
    monkeypatch, task_module, clean_state, resource, failure_at
):
    """Cancellation or failed response construction must not leave an open upstream."""
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task
    response = _FakeHTTPXResponse()
    upstream_client = _FakeHTTPXClient(response)
    monkeypatch.setattr(task_module.httpx, "AsyncClient", lambda: upstream_client)

    async def cancelled_fetch(*args, **kwargs):
        raise asyncio.CancelledError()

    def failed_build(*args, **kwargs):
        raise ValueError("unable to build response")

    if failure_at == "fetch":
        monkeypatch.setattr(task_module, "_fetch_runner_resource", cancelled_fetch)
        expected_exception = asyncio.CancelledError
    else:
        monkeypatch.setattr(task_module, "_build_streaming_response", failed_build)
        expected_exception = HTTPException

    with pytest.raises(expected_exception):
        if resource == "manifest":
            await task_module._stream_runner_manifest(task, _runner("r1"))
        else:
            await task_module._stream_runner_file(task, _runner("r1"), "video.ts")
    assert upstream_client.closed
    if failure_at == "build":
        assert response.closed


@pytest.mark.asyncio
async def test_runner_stream_closes_client_when_response_close_fails(task_module, clean_state):
    """Always close the client even if closing its response raises an exception."""
    tasks["t1"] = _task("t1", "r1", status="completed")

    class BrokenCloseResponse(_FakeHTTPXResponse):
        async def aclose(self):
            raise RuntimeError("unable to close response")

    response = BrokenCloseResponse()
    client = _FakeHTTPXClient(response)
    result = task_module._build_streaming_response("t1", response, client)
    with pytest.raises(RuntimeError, match="unable to close response"):
        async for _ in result.body_iterator:
            pass
    assert client.closed


@pytest.mark.asyncio
async def test_fetch_runner_resource_non_200_raises(task_module):
    """Validate Fetch runner resource non 200 raises."""
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=500)
    client = _FakeHTTPXClient(resp)

    with pytest.raises(Exception):
        await task_module._fetch_runner_resource(
            client=client,
            runner=runner,
            url="http://r1.example/x",
            timeout=httpx.Timeout(1.0),
            accept="application/json",
        )


@pytest.mark.asyncio
async def test_fetch_runner_resource_200_returns_response(task_module):
    """Validate Fetch runner resource 200 returns response."""
    runner = _runner("r1")
    resp = _FakeHTTPXResponse(status_code=200)
    client = _FakeHTTPXClient(resp)

    out = await task_module._fetch_runner_resource(
        client=client,
        runner=runner,
        url="http://r1.example/x",
        timeout=httpx.Timeout(1.0),
        accept="application/json",
    )
    assert out is resp


@pytest.mark.asyncio
async def test_build_streaming_response_sets_headers_and_closes(task_module, clean_state):
    """Validate Build streaming response sets headers and closes."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")

    response = _FakeHTTPXResponse(headers={"content-type": "application/json"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(
        task_id="t1",
        response=response,
        client=client,
        media_type="application/json",
        filename="manifest.json",
    )

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    chunks = await _collect(sr.body_iterator)  # type: ignore[attr-defined]
    assert chunks == b"ok"
    assert tasks["t1"].status == "completed"


@pytest.mark.asyncio
async def test_build_streaming_response_uses_response_content_disposition(
    monkeypatch, task_module, clean_state
):
    """Validate Build streaming response uses response content disposition."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    response = _FakeHTTPXResponse(headers={"content-disposition": "attachment; filename=x.bin"})
    client = _FakeHTTPXClient(response)

    sr = task_module._build_streaming_response(task_id="t1", response=response, client=client)
    assert sr.headers["Content-Disposition"] == "attachment; filename=x.bin"


@pytest.mark.asyncio
async def test_stream_runner_manifest_success(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest success."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b'{"task_id": "t1"}'
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/json"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def send(self, request, **kwargs):
            self.last_url = str(request.url)
            return await super().send(request, **kwargs)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert tasks["t1"].status == "completed"
    assert created["client"].closed is True
    assert created["client"].last_url and created["client"].last_url.endswith("/task/result/t1")


@pytest.mark.asyncio
async def test_stream_runner_file_success_and_encodes_path(monkeypatch, task_module, clean_state):
    """Validate Stream runner file success and encodes path."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="warning")
    monkeypatch.setattr(task_module, "save_tasks", lambda: None)

    body = b"bin"
    response = _FakeHTTPXResponse(body=body, headers={"content-type": "application/octet-stream"})

    class CapturingClient(_FakeHTTPXClient):
        def __init__(self):
            super().__init__(response)
            self.last_url: str | None = None

        async def send(self, request, **kwargs):
            self.last_url = str(request.url)
            return await super().send(request, **kwargs)

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = CapturingClient()
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    sr = await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a b/c.txt")

    async def _collect(aiter):
        out = []
        async for c in aiter:
            out.append(c)
        return b"".join(out)

    assert await _collect(sr.body_iterator) == body  # type: ignore[attr-defined]
    assert "a%20b/c.txt" in (created["client"].last_url or "")


@pytest.mark.asyncio
async def test_stream_runner_manifest_timeout(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest timeout."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def send(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_manifest_request_error(monkeypatch, task_module, clean_state):
    """Validate Stream runner manifest request error."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def send(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_runner_manifest_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner manifest http exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_manifest_unexpected_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner manifest unexpected exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def aclose(self):
            self.closed = True

    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = FakeAsyncClient()
        return created["client"]

    async def boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)
    monkeypatch.setattr(task_module, "_fetch_runner_resource", boom)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_manifest(tasks["t1"], runners["r1"])
    assert exc.value.status_code == 500
    assert created["client"].closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_request_error(monkeypatch, task_module, clean_state):
    """Validate Stream runner file request error."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    req = httpx.Request("GET", "http://r1.example/x")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def send(self, *_a, **_k):
            raise httpx.RequestError("boom", request=req)

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(Exception):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")


@pytest.mark.asyncio
async def test_stream_runner_file_http_exception_closes_client(
    monkeypatch, task_module, clean_state
):
    """Validate Stream runner file http exception closes client."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    response = _FakeHTTPXResponse(status_code=500)
    created: dict[str, Any] = {}

    def fake_async_client(*_a, **_k):
        created["client"] = _FakeHTTPXClient(response)
        return created["client"]

    monkeypatch.setattr(task_module.httpx, "AsyncClient", fake_async_client)

    with pytest.raises(HTTPException):
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert created["client"].closed is True
    assert response.closed is True


@pytest.mark.asyncio
async def test_stream_runner_file_timeout(monkeypatch, task_module, clean_state):
    """Validate Stream runner file timeout."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def send(self, *_a, **_k):
            raise httpx.TimeoutException("timeout")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 504


@pytest.mark.asyncio
async def test_stream_runner_file_unexpected_exception(monkeypatch, task_module, clean_state):
    """Validate Stream runner file unexpected exception."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    class FakeAsyncClient(_FakeHTTPXClient):
        def __init__(self, *_, **__):
            self.closed = False

        async def send(self, *_a, **_k):
            raise ValueError("boom")

        async def aclose(self):
            self.closed = True

    monkeypatch.setattr(task_module.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(HTTPException) as exc:
        await task_module._stream_runner_file(tasks["t1"], runners["r1"], "a.txt")
    assert exc.value.status_code == 500


# -----------------------------
# Result endpoints
# -----------------------------


def test_get_valid_task_rejects_missing(task_module, clean_state):
    """Validate Get valid task rejects missing."""
    with pytest.raises(Exception):
        task_module._get_valid_task("nope")


def test_get_valid_task_rejects_failed(task_module, clean_state):
    """Validate Get valid task rejects failed."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="failed")
    tasks["t1"].error = "boom"

    with pytest.raises(Exception):
        task_module._get_valid_task("t1")


def test_get_task_runner_raises_when_runner_missing(task_module, clean_state):
    """Validate Get task runner raises when runner missing."""
    tasks["t1"] = _task("t1", "missing", status="completed")
    with pytest.raises(HTTPException) as exc:
        task_module._get_task_runner(tasks["t1"])
    assert exc.value.status_code == 500


def test_get_task_result_425_when_running(client, clean_state):
    """Validate Get task result 425 when running."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="running")

    resp = client.get("/task/result/t1")
    assert resp.status_code == 425


def test_get_task_result_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    """Validate Get task result local storage."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1").mkdir()
    (tmp_path / "t1" / "manifest.json").write_text(json.dumps({"task_id": "t1"}), encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    async def run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(task_module.asyncio, "to_thread", run_inline)

    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["task_id"] == "t1"


@pytest.mark.asyncio
async def test_get_task_result_local_storage_uses_to_thread(monkeypatch, task_module, clean_state):
    """Validate local manifest reads are moved off the asyncio event loop."""
    task = _task("t1", "r1", status="completed")
    tasks[task.task_id] = task
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)

    expected_response = JSONResponse({"task_id": task.task_id})

    def fake_get_local_manifest(_task: Task):
        return expected_response

    called = {}

    async def fake_to_thread(func, *args, **kwargs):
        called.update(func=func, args=args, kwargs=kwargs)
        return func(*args, **kwargs)

    monkeypatch.setattr(task_module, "_get_local_manifest", fake_get_local_manifest)
    monkeypatch.setattr(task_module.asyncio, "to_thread", fake_to_thread)

    response = await task_module.get_task_result(task.task_id)

    assert response is expected_response
    assert called == {
        "func": fake_get_local_manifest,
        "args": (task,),
        "kwargs": {},
    }


def test_get_task_result_file_local_storage(
    monkeypatch, client, task_module, clean_state, tmp_path: Path
):
    """Validate Get task result file local storage."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    (tmp_path / "t1" / "output").mkdir(parents=True)
    (tmp_path / "t1" / "manifest.json").write_text(
        json.dumps({"files": ["a.txt"]}), encoding="utf-8"
    )
    (tmp_path / "t1" / "output" / "a.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", True)
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_DIR", str(tmp_path))

    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200


def test_get_task_result_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    """Validate Get task result proxies to runner when storage disabled."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner):
        return JSONResponse({"task_id": "t1", "proxied": True})

    monkeypatch.setattr(task_module, "_stream_runner_manifest", fake_stream)
    resp = client.get("/task/result/t1")
    assert resp.status_code == 200
    assert resp.json()["proxied"] is True


def test_get_task_result_file_rejects_traversal(client, clean_state):
    """Validate Get task result file rejects traversal."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")

    # Use an encoded traversal sequence; plain "../" may be normalized away by the ASGI stack
    # and never reach the route handler.
    resp = client.get("/task/result/t1/file/%2e%2e%2fsecret")
    assert resp.status_code == 400


def test_get_task_result_file_proxies_to_runner_when_storage_disabled(
    monkeypatch, client, task_module, clean_state
):
    """Validate Get task result file proxies to runner when storage disabled."""
    runners["r1"] = _runner("r1")
    tasks["t1"] = _task("t1", "r1", status="completed")
    monkeypatch.setattr(task_module.config, "RUNNERS_STORAGE_ENABLED", False)

    async def fake_stream(_task: Task, _runner: Runner, _path: str):
        return JSONResponse({"ok": True})

    monkeypatch.setattr(task_module, "_stream_runner_file", fake_stream)
    resp = client.get("/task/result/t1/file/a.txt")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# -----------------------------
# Completion endpoint
# -----------------------------
