"""Shared task-result storage access and runner result proxying."""

import json
import time
from pathlib import Path
from typing import Any, Dict, cast
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.models.models import Runner, Task


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
    try:
        return (task_dir / "manifest.json").resolve(strict=False)
    except Exception:
        raise HTTPException(500, "Invalid manifest path")


def read_manifest_with_retry(context: Any, manifest_resolved: Path) -> Any:
    """Read manifest JSON with short retries to absorb write races."""
    manifest_data = None
    last_json_error = False
    for attempt in range(1, context._MANIFEST_READ_ATTEMPTS + 1):
        if manifest_resolved.exists() and manifest_resolved.is_file():
            try:
                manifest_data = json.loads(manifest_resolved.read_text(encoding="utf-8"))
                break
            except json.JSONDecodeError:
                last_json_error = True
        if attempt < context._MANIFEST_READ_ATTEMPTS:
            time.sleep(context._MANIFEST_READ_DELAY_SECONDS)
    if manifest_data is not None:
        return manifest_data
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
    context._mark_warning_as_completed(task.task_id)
    return JSONResponse(content=manifest_data, headers={"X-Task-ID": task.task_id})


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
    """Fetch a runner resource and reject non-success responses."""
    response = await client.get(
        url,
        headers=context._runner_auth_headers(runner, accept=accept),
        timeout=timeout,
    )
    if response.status_code != 200:
        context.logger.error("Error fetching result from runner: %s", response.status_code)
        await response.aclose()
        raise HTTPException(response.status_code, "Error fetching result from runner")
    return response


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
            async for chunk in response.aiter_bytes():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    context._mark_warning_as_completed(task_id)
    headers: Dict[str, str] = {"X-Task-ID": task_id}
    content_disposition = response.headers.get("content-disposition")
    if filename:
        headers["Content-Disposition"] = f"attachment; filename={filename}"
    elif content_disposition:
        headers["Content-Disposition"] = content_disposition
    return StreamingResponse(
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
    try:
        response = await context._fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task.task_id}",
            timeout,
            accept="application/json",
        )
        return cast(
            StreamingResponse,
            context._build_streaming_response(
                task.task_id, response, client, media_type="application/json"
            ),
        )
    except HTTPException:
        await client.aclose()
        raise
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(502, f"Error contacting runner: {exc}")
    except Exception as exc:
        await client.aclose()
        raise HTTPException(500, f"Unexpected error: {exc}")


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
    try:
        response = await context._fetch_runner_resource(
            client,
            runner,
            f"{runner.url}/task/result/{task.task_id}/file/{encoded_path}",
            timeout,
            accept="application/octet-stream",
        )
        return cast(
            StreamingResponse,
            context._build_streaming_response(
                task.task_id,
                response,
                client,
                media_type=response.headers.get("content-type", "application/octet-stream"),
                filename=context.PathlibPath(file_path).name,
            ),
        )
    except HTTPException:
        await client.aclose()
        raise
    except httpx.TimeoutException:
        await client.aclose()
        raise HTTPException(504, "Runner request timed out")
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(502, f"Error contacting runner: {exc}")
    except Exception as exc:
        await client.aclose()
        raise HTTPException(500, f"Unexpected error: {exc}")
