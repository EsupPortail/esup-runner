"""Runner reservation, task deduplication and execution dispatch."""

import json
import uuid
from datetime import datetime
from logging import Logger
from typing import Any, Callable, Dict, cast

import httpx
from fastapi import HTTPException, status

from app.core.token_roles import token_matches
from app.models.models import Runner, Task, TaskRequest
from app.services.runner_selection import rank_runners


async def execute_task_background(
    context: Any,
    task_id: str,
    runner: Runner,
    task_request: TaskRequest,
) -> None:
    """Execute a queued task on its reserved runner."""
    try:
        context.logger.info("Starting background task %s on runner %s", task_id, runner.id)
        context.tasks[task_id].status = "running"
        context.tasks[task_id].updated_at = datetime.now().isoformat()

        async with context.httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{runner.url}/task/run",
                json={
                    "task_id": task_id,
                    "run_id": context.tasks[task_id].run_id,
                    "etab_name": task_request.etab_name,
                    "app_name": task_request.app_name,
                    "app_version": task_request.app_version,
                    "task_type": task_request.task_type,
                    "source_url": task_request.source_url,
                    "affiliation": task_request.affiliation,
                    "parameters": task_request.parameters,
                    "notify_url": task_request.notify_url,
                    "completion_callback": f"{context.config.MANAGER_URL.rstrip('/')}/task/completion",
                    "completion_callback_public": f"{context.config.MANAGER_PUBLIC_URL.rstrip('/')}/task/completion",
                },
                headers=context._runner_auth_headers(runner, accept="application/json"),
            )

            if response.status_code == 200:
                runner.availability = "busy"
            else:
                task = context.tasks[task_id]
                task.status = "failed"
                task.error = f"Runner returned status {response.status_code}: {response.text}"
                task.updated_at = datetime.now().isoformat()
                runner.availability = "available"
                context.logger.error("Task %s failed with status %s", task_id, response.status_code)
            context.runners[runner.id] = runner
            context.save_tasks()
    except Exception as exc:
        task = context.tasks[task_id]
        task.status = "failed"
        task.error = str(exc)
        task.updated_at = datetime.now().isoformat()
        runner.availability = "available"
        context.runners[runner.id] = runner
        context.logger.error("Error executing task %s: %s", task_id, exc)
        context.save_tasks()


def task_request_fingerprint(
    *,
    task_type: str,
    source_url: str,
    parameters: Dict[str, Any] | None,
    notify_url: str | None,
    app_name: str,
    etab_name: str,
) -> str:
    """Build a stable fingerprint used to deduplicate in-flight requests."""
    payload = {
        "task_type": task_type,
        "source_url": source_url,
        "parameters": parameters or {},
        "notify_url": notify_url or "",
        "app_name": app_name,
        "etab_name": etab_name,
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def find_inflight_duplicate_task_id(
    context: Any,
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
) -> str | None:
    """Return the newest in-flight duplicate task ID, when one exists."""
    target_fingerprint = context._task_request_fingerprint(
        task_type=task_request.task_type,
        source_url=task_request.source_url,
        parameters=task_request.parameters,
        notify_url=task_request.notify_url,
        app_name=task_request.app_name,
        etab_name=task_request.etab_name,
    )
    matches = [
        task
        for task in tasks_snapshot.values()
        if task.status in {"pending", "running"}
        and context._task_request_fingerprint(
            task_type=task.task_type,
            source_url=task.source_url,
            parameters=task.parameters,
            notify_url=task.notify_url,
            app_name=task.app_name,
            etab_name=task.etab_name,
        )
        == target_fingerprint
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item.updated_at or "", reverse=True)
    return matches[0].task_id


def build_inflight_dedup_response(
    context: Any,
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
    *,
    log_message: str,
) -> dict | None:
    """Build the HTTP payload reusing an existing in-flight task."""
    duplicate_task_id = context._find_inflight_duplicate_task_id(task_request, tasks_snapshot)
    if duplicate_task_id is None:
        return None
    duplicate_task = tasks_snapshot.get(duplicate_task_id)
    context.logger.info("%s %s", log_message, duplicate_task_id)
    return {
        "task_id": duplicate_task_id,
        "status": duplicate_task.status if duplicate_task else "running",
    }


def try_reuse_inflight_duplicate(
    context: Any,
    task_request: TaskRequest,
    tasks_snapshot: Dict[str, Task],
    *,
    dedup_enabled: bool,
    log_message: str,
) -> dict | None:
    """Return a deduplication response when the feature is enabled."""
    if not dedup_enabled:
        return None
    return cast(
        dict | None,
        context._build_inflight_dedup_response(
            task_request, tasks_snapshot, log_message=log_message
        ),
    )


def try_reuse_inflight_duplicate_with_fresh_snapshot(
    context: Any,
    task_request: TaskRequest,
    *,
    dedup_enabled: bool,
    log_message: str,
    client_token: str | None = None,
) -> dict | None:
    """Repeat deduplication against a fresh multi-instance snapshot."""
    if not dedup_enabled:
        return None
    return cast(
        dict | None,
        context._build_inflight_dedup_response(
            task_request,
            _client_tasks(context.get_tasks_snapshot(), client_token),
            log_message=log_message,
        ),
    )


def _client_tasks(tasks_snapshot: Dict[str, Task], client_token: str | None) -> Dict[str, Task]:
    """Limit duplicate candidates to tasks submitted by the same application."""
    return {
        key: task
        for key, task in tasks_snapshot.items()
        if client_token is not None and token_matches(client_token, task.client_token)
    }


def try_reserve_runner_for_dispatch(context: Any, runner_id: str) -> Runner | None:
    """Atomically reserve a runner before creating a task."""
    reserve_method = getattr(context.runners, "try_reserve", None)
    if callable(reserve_method):
        typed_reserve = cast(Callable[[str], Runner | None], reserve_method)
        return typed_reserve(runner_id)

    runner = context.runners.get(runner_id)
    if runner is None or runner.availability != "available":
        return None
    runner.availability = "busy"
    context.runners[runner_id] = runner
    return cast(Runner, runner)


def resolve_runner_for_dispatch(
    context: Any,
    runner_id: str,
    runner: Runner,
    *,
    preferred_task_id: str | None,
) -> Runner | None:
    """Reserve the runner for both new tasks and restarts."""
    reserved_runner = cast(Runner | None, context._try_reserve_runner_for_dispatch(runner_id))
    if reserved_runner is None:
        context.logger.info(
            "Runner %s became busy before reservation; trying next runner", runner_id
        )
    return reserved_runner


def persist_task_before_dispatch(
    context: Any, task_id: str, previous_task: Task | None, runner: Runner
) -> None:
    """Require a successful save, restoring the task and reservation on failure."""
    if context.save_tasks() is not False:
        return
    if previous_task is None:
        context.tasks.pop(task_id, None)
    else:
        context.tasks[task_id] = previous_task
    runner.availability = "available"
    context.runners[runner.id] = runner
    raise HTTPException(503, "Task could not be persisted; retry")


def _runner_ping_supports_task(
    response: httpx.Response, task_type: str, runner_id: str, logger: Logger
) -> bool:
    """Check a Runner's ping response before reserving a task slot."""
    if not 200 <= response.status_code < 300:
        logger.warning("Runner %r ping returned HTTP %s", runner_id, response.status_code)
        return False
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Runner %r ping returned invalid JSON", runner_id)
        return False
    if not isinstance(payload, dict):
        logger.warning("Runner %r ping returned an unexpected payload", runner_id)
        return False
    task_types = payload.get("task_types")
    return (
        payload.get("available") is True
        and payload.get("registered") is True
        and isinstance(task_types, list)
        and task_type in task_types
    )


async def queue_task_execution(
    context: Any,
    task_request: TaskRequest,
    client_token: str | None,
    *,
    preferred_task_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Select and reserve a runner, persist the task, then schedule execution."""
    context.logger.info("Starting async task execution")
    tasks_snapshot = context.get_tasks_snapshot()
    dedup_enabled = preferred_task_id is None
    dedup_response = cast(
        dict | None,
        context._try_reuse_inflight_duplicate(
            task_request,
            _client_tasks(tasks_snapshot, client_token),
            dedup_enabled=dedup_enabled,
            log_message="Deduplicating task execute request: reusing in-flight task",
        ),
    )
    if dedup_response is not None:
        return dedup_response

    if task_request.notify_url:
        await context._validate_notify_url(task_request.notify_url)

    if context.config.PRIORITIES_ENABLED and context.would_exceed_other_domain_quota(
        request_notify_url=task_request.notify_url,
        tasks=tasks_snapshot,
        runner_capacity=len(context.runners),
        priority_domain=context.config.PRIORITY_DOMAIN,
        max_other_percent=context.config.MAX_OTHER_DOMAIN_TASK_PERCENT,
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Non-priority domain rejected: maximum other-domain task quota reached",
        )

    for runner in rank_runners(context.runners.values()):
        runner_id = runner.id
        context.logger.info("Checking runner: %r", runner_id)
        try:
            async with context.httpx.AsyncClient(timeout=5.0) as client:
                context.logger.info("Pinging runner at: %r", runner.url)
                response = await client.get(f"{runner.url}/runner/ping")
                if not _runner_ping_supports_task(
                    response, task_request.task_type, runner_id, context.logger
                ):
                    continue

                runner_for_dispatch = context._resolve_runner_for_dispatch(
                    runner_id,
                    runner,
                    preferred_task_id=preferred_task_id,
                )
                if runner_for_dispatch is None:
                    continue

                task_id = preferred_task_id or str(uuid.uuid4())
                previous_task = context.tasks.get(task_id)
                now_iso = datetime.now().isoformat()
                context.tasks[task_id] = Task(
                    task_id=task_id,
                    runner_id=runner_id,
                    status="running",
                    etab_name=task_request.etab_name,
                    app_name=task_request.app_name,
                    app_version=task_request.app_version,
                    task_type=task_request.task_type,
                    source_url=task_request.source_url,
                    affiliation=task_request.affiliation,
                    parameters=task_request.parameters,
                    notify_url=task_request.notify_url,
                    client_token=client_token,
                    completion_callback=None,
                    run_id=str(uuid.uuid4()),
                    created_at=created_at or now_iso,
                    updated_at=now_iso,
                    error=None,
                    script_output=None,
                )
                persist_task_before_dispatch(context, task_id, previous_task, runner_for_dispatch)
                context.asyncio.create_task(
                    context.execute_task_async_background(
                        task_id, runner_for_dispatch, task_request
                    )
                )
                return {"task_id": task_id, "status": "running"}
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            context.logger.warning("Runner %r unavailable: %s", runner_id, exc)

    dedup_response = cast(
        dict | None,
        context._try_reuse_inflight_duplicate_with_fresh_snapshot(
            task_request,
            dedup_enabled=dedup_enabled,
            log_message="Reusing in-flight task after runner exhaustion:",
            client_token=client_token,
        ),
    )
    if dedup_response is not None:
        return dedup_response
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="No runners available",
    )
