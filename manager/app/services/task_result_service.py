"""Shared task-result storage access and runner result proxying."""

import json
import os
import stat
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, cast
from urllib.parse import quote

import anyio
import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.types import Receive, Scope, Send

from app.models.models import Runner, Task, TaskResultManifest


def resolve_shared_storage_base(context: Any) -> Path:
    """Resolve and validate the configured shared-storage base directory."""
    storage_dir = (
        getattr(context.config, "RUNNERS_STORAGE_DIR", None)
        or getattr(context.config, "RUNNERS_STORAGE_PATH", None)
        or "/tmp/esup-runner"
    )
    base_dir = context.PathlibPath(storage_dir).expanduser()
    try:
        base_resolved = base_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid RUNNERS_STORAGE_DIR or RUNNERS_STORAGE_PATH")
    if not base_resolved.exists() or not base_resolved.is_dir():
        raise HTTPException(500, "RUNNERS_STORAGE_DIR or RUNNERS_STORAGE_PATH is not a directory")
    return cast(Path, base_resolved)


def mark_warning_as_completed(context: Any, task_id: str) -> None:
    """Convert warning status to completed after a successful result fetch."""
    if context.tasks[task_id].status == "warning":
        context.tasks[task_id].status = "completed"
        context.tasks[task_id].error = None
        context.save_tasks()


def validate_result_path(context: Any, file_path: str) -> None:
    """Reject absolute paths and traversal in requested result paths."""
    path = context.PathlibPath(file_path)
    if path.is_absolute() or ".." in path.parts:
        raise HTTPException(400, "Invalid result file path")


def get_local_task_dir(context: Any, task_id: str) -> Path:
    """Return a validated local task directory below shared storage."""
    base_resolved = context._resolve_shared_storage_base()
    task_dir = base_resolved / task_id
    try:
        task_resolved = task_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid task result path")
    if task_resolved != base_resolved and base_resolved not in task_resolved.parents:
        raise HTTPException(500, "Resolved result path is outside RUNNERS_STORAGE_DIR")
    if not task_resolved.exists() or not task_resolved.is_dir():
        raise HTTPException(404, "Result directory not found in shared storage")
    return cast(Path, task_resolved)


def get_local_output_dir(context: Any, task_id: str) -> Path:
    """Return the validated output directory for a stored task."""
    task_dir = context._get_local_task_dir(task_id)
    output_dir = task_dir / "output"
    try:
        output_resolved = output_dir.resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid task output path")
    if output_resolved != task_dir and task_dir not in output_resolved.parents:
        raise HTTPException(500, "Resolved output path is outside task directory")
    if not output_resolved.exists() or not output_resolved.is_dir():
        raise HTTPException(404, "Result output directory not found in shared storage")
    return cast(Path, output_resolved)


def resolve_local_manifest_path(context: Any, task_dir: Path) -> Path:
    """Resolve the manifest path from a validated task directory."""
    manifest_path = task_dir / "manifest.json"
    try:
        if manifest_path.is_symlink() or manifest_path.resolve(strict=False).parent != task_dir:
            raise ValueError("Manifest must be a direct regular file")
    except Exception:
        raise HTTPException(500, "Invalid manifest path")
    # Keep the original path: resolving a symlink would bypass O_NOFOLLOW below.
    return manifest_path


def _read_manifest_file(manifest_path: Path) -> Any:
    """Open without following symlinks, including concurrently replaced parents."""
    with ExitStack() as stack:
        directory_fd = None
        for part in manifest_path.absolute().parent.parts:
            directory_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
            stack.callback(os.close, directory_fd)
        fd = os.open(
            manifest_path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        stack.callback(os.close, fd)
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as manifest_file:
            if not stat.S_ISREG(os.fstat(manifest_file.fileno()).st_mode):
                raise HTTPException(500, "Invalid manifest file")
            return json.load(manifest_file)


def read_manifest_with_retry(context: Any, manifest_resolved: Path) -> Any:
    """Read manifest JSON with short retries to absorb write races."""
    last_json_error = False
    for attempt in range(1, context._MANIFEST_READ_ATTEMPTS + 1):
        try:
            return _read_manifest_file(manifest_resolved)
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, UnicodeError):
            last_json_error = True
        except OSError:
            raise HTTPException(500, "Unable to read manifest safely")
        if attempt < context._MANIFEST_READ_ATTEMPTS:
            time.sleep(context._MANIFEST_READ_DELAY_SECONDS)
    if last_json_error:
        raise HTTPException(500, "Invalid manifest JSON")
    raise HTTPException(404, "Manifest not found in shared storage")


def get_local_manifest(context: Any, task: Task) -> JSONResponse:
    """Return a task manifest directly from shared storage."""
    task_dir = context._get_local_task_dir(task.task_id)
    manifest_path = context._resolve_local_manifest_path(task_dir)
    manifest_data = context._read_manifest_with_retry(manifest_path)
    if isinstance(manifest_data, dict):
        manifest_data.setdefault("task_id", task.task_id)
    try:
        manifest = TaskResultManifest.model_validate(manifest_data, strict=True)
    except ValidationError:
        raise HTTPException(500, "Invalid manifest content")
    if manifest.task_id != task.task_id:
        raise HTTPException(500, "Invalid manifest task ID")
    context._mark_warning_as_completed(task.task_id)
    return JSONResponse(content=manifest.model_dump(), headers={"X-Task-ID": task.task_id})


def stream_local_file(context: Any, task: Task, file_path: str) -> FileResponse:
    """Return a single task file from shared storage."""
    output_dir = context._get_local_output_dir(task.task_id)
    try:
        file_resolved = (output_dir / file_path).resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid result file path")
    if file_resolved != output_dir and output_dir not in file_resolved.parents:
        raise HTTPException(400, "Invalid result file path")
    if not file_resolved.exists() or not file_resolved.is_file():
        raise HTTPException(404, "Result file not found in shared storage")
    context._mark_warning_as_completed(task.task_id)
    return FileResponse(
        path=str(file_resolved),
        media_type="application/octet-stream",
        filename=file_resolved.name,
        headers={"X-Task-ID": task.task_id},
    )


def get_valid_task(context: Any, task_id: str) -> Task:
    """Get a task and validate that its results are available."""
    task = context.get_task_from_state(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task.status == "failed":
        raise HTTPException(400, f"Task failed: {task.error}")
    if task.status not in {"completed", "warning"}:
        raise HTTPException(425, f"Task not completed. Status: {task.status}")
    return cast(Task, task)


def get_task_runner(context: Any, task: Task) -> Runner:
    """Return the runner associated with a result-bearing task."""
    if task.runner_id not in context.runners:
        raise HTTPException(500, "Runner not available")
    return cast(Runner, context.runners[task.runner_id])


async def fetch_runner_resource(
    context: Any,
    client: httpx.AsyncClient,
    runner: Runner,
    url: str,
    timeout: httpx.Timeout,
    accept: str,
) -> httpx.Response:
    """Open a runner resource without buffering its body and reject HTTP errors."""
    request = client.build_request(
        "GET",
        url,
        headers=context._runner_auth_headers(runner, accept=accept),
        timeout=timeout,
    )
    response = await client.send(request, stream=True)
    if response.status_code != 200:
        safe_url = request.url.copy_with(userinfo=b"", query=None, fragment=None)
        context.logger.error(
            "Error fetching result from runner: %s (runner_id=%s, resource_url=%s)",
            response.status_code,
            runner.id,
            safe_url,
        )
        await response.aclose()
        raise HTTPException(response.status_code, "Error fetching result from runner")
    return response


async def _close_runner_resources(
    response: httpx.Response | None, client: httpx.AsyncClient
) -> None:
    """Release upstream resources even during cancellation or failed response closure."""
    with anyio.CancelScope(shield=True):
        try:
            if response is not None:
                await response.aclose()
        finally:
            await client.aclose()


def build_streaming_response(
    context: Any,
    task_id: str,
    response: httpx.Response,
    client: httpx.AsyncClient,
    media_type: str | None = None,
    filename: str | None = None,
) -> StreamingResponse:
    """Build a streaming response that closes runner network resources."""

    async def content_generator():
        try:
            async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                yield chunk
        finally:
            await _close_runner_resources(response, client)

    class _RunnerStreamingResponse(StreamingResponse):
        """Also close the upstream when sending headers or a chunk fails."""

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            try:
                await super().__call__(scope, receive, send)
            finally:
                await _close_runner_resources(response, client)

    context._mark_warning_as_completed(task_id)
    headers: Dict[str, str] = {"X-Task-ID": task_id}
    content_disposition = response.headers.get("content-disposition")
    if filename:
        headers["Content-Disposition"] = f"attachment; filename={filename}"
    elif content_disposition:
        headers["Content-Disposition"] = content_disposition
    return _RunnerStreamingResponse(
        content_generator(),
        media_type=media_type or response.headers.get("content-type", "application/octet-stream"),
        headers=headers,
    )


async def stream_runner_manifest(
    context: Any,
    task: Task,
    runner: Runner,
) -> StreamingResponse:
    """Proxy-stream a result manifest from its runner."""
    client = context.httpx.AsyncClient()
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)
    response = None
    streaming_response = None
    try:
        response = await context._fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task.task_id}",
            timeout,
            accept="application/json",
        )
        streaming_response = cast(
            StreamingResponse,
            context._build_streaming_response(
                task.task_id, response, client, media_type="application/json"
            ),
        )
        return streaming_response
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Error contacting runner: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Unexpected error: {exc}")
    finally:
        if streaming_response is None:
            await _close_runner_resources(response, client)


async def stream_runner_file(
    context: Any,
    task: Task,
    runner: Runner,
    file_path: str,
) -> StreamingResponse:
    """Proxy-stream one result file from its runner."""
    client = context.httpx.AsyncClient()
    timeout = httpx.Timeout(connect=5.0, read=None, write=None, pool=5.0)
    encoded_path = quote(file_path, safe="/")
    response = None
    streaming_response = None
    try:
        response = await context._fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task.task_id}/file/{encoded_path}",
            timeout,
            accept="application/octet-stream",
        )
        streaming_response = cast(
            StreamingResponse,
            context._build_streaming_response(
                task.task_id,
                response,
                client,
                media_type=response.headers.get("content-type", "application/octet-stream"),
                filename=context.PathlibPath(file_path).name,
            ),
        )
        return streaming_response
    except HTTPException:
        raise
    except httpx.TimeoutException:
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Error contacting runner: {exc}")
    except Exception as exc:
        raise HTTPException(500, f"Unexpected error: {exc}")
    finally:
        if streaming_response is None:
            await _close_runner_resources(response, client)
