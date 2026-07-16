"""Validation, delivery and retry handling for task completion callbacks."""

import asyncio
import ipaddress
import json
import socket
from datetime import datetime
from typing import Any, List, cast
from urllib.parse import ParseResult, urlparse

import httpx
from fastapi import HTTPException

from app.models.models import Task, TaskCompletionNotification


def host_matches_allowlist(host: str, allowed_hosts: List[str]) -> bool:
    """Return whether a host is an exact or subdomain allowlist match."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    for allowed in allowed_hosts:
        normalized = (allowed or "").strip().lower().rstrip(".")
        if normalized and (host == normalized or host.endswith("." + normalized)):
            return True
    return False


def is_disallowed_ip(ip: str) -> bool:
    """Return whether an IP must be blocked for outbound callbacks."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


async def resolve_host_ips(host: str) -> List[str]:
    """Resolve all IPs for a hostname using system DNS."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = {sockaddr[0] for info in infos if (sockaddr := info[4]) and isinstance(sockaddr[0], str)}
    return sorted(ips)


def parse_notify_url(url: str) -> tuple[ParseResult, str]:
    """Parse and syntactically validate a notify URL."""
    if not url:
        raise HTTPException(status_code=400, detail="notify_url is empty")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(status_code=400, detail="notify_url must use http or https")
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="notify_url is missing host")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="notify_url must not include userinfo")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HTTPException(status_code=400, detail="notify_url has invalid host")
    return parsed, host


def validate_notify_url_host(context: Any, host: str) -> None:
    """Validate a notify URL hostname against configured policy rules."""
    if context.config.NOTIFY_URL_ALLOWED_HOSTS and not context._host_matches_allowlist(
        host, context.config.NOTIFY_URL_ALLOWED_HOSTS
    ):
        raise HTTPException(status_code=400, detail="notify_url host not allowed")
    if host == "localhost":
        raise HTTPException(status_code=400, detail="notify_url host not allowed")


async def resolve_notify_url_ips(context: Any, host: str) -> List[str]:
    """Resolve a notify host and convert DNS failures to HTTP 400."""
    try:
        ips = await context._resolve_host_ips(host)
    except Exception:
        raise HTTPException(status_code=400, detail="notify_url host cannot be resolved")
    if not ips:
        raise HTTPException(status_code=400, detail="notify_url host cannot be resolved")
    return cast(List[str], ips)


def validate_notify_url_public_ips(context: Any, ips: List[str]) -> None:
    """Reject private, loopback and reserved callback destinations."""
    for ip in ips:
        if context._is_disallowed_ip(ip):
            raise HTTPException(
                status_code=400,
                detail="notify_url resolves to a private/loopback/link-local address",
            )


async def validate_notify_url(context: Any, url: str) -> str:
    """Run full notify URL validation and return the original URL."""
    _, host = context._parse_notify_url(url)
    context._validate_notify_url_host(host)
    ips = await context._resolve_notify_url_ips(host)
    if not context.config.NOTIFY_URL_ALLOW_PRIVATE_NETWORKS:
        context._validate_notify_url_public_ips(ips)
    return url


async def send_notify_callback(
    context: Any,
    task: Task,
    notification: TaskCompletionNotification,
) -> tuple[bool, str | None]:
    """Send a single notify URL callback attempt."""
    if not task.notify_url:
        return False, "notify_url is empty"

    await context._validate_notify_url(task.notify_url)
    context.logger.info(
        "Sending notify URL callback to %s for task %s",
        task.notify_url,
        notification.task_id,
    )
    timeout = httpx.Timeout(
        connect=5.0,
        read=context._NOTIFY_CALLBACK_READ_TIMEOUT_SECONDS,
        write=5.0,
        pool=5.0,
    )
    payload = {
        "task_id": notification.task_id,
        "status": notification.status,
        "error_message": getattr(notification, "error_message", None),
        "script_output": notification.script_output,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    client_token = getattr(task, "client_token", None)
    if client_token:
        headers["Authorization"] = f"Bearer {client_token}"

    async with context.httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(task.notify_url, content=body, headers=headers)

    if 200 <= response.status_code < 300:
        context.logger.info(
            "Notify URL callback %s successful for task %s",
            task.notify_url,
            notification.task_id,
        )
        return True, None

    error_message = (
        f"Notify URL callback {task.notify_url} failed: "
        f"{response.status_code} - {response.text}"
    )
    context.logger.warning(error_message)
    return False, error_message


def set_notify_warning(context: Any, task_id: str, message: str) -> None:
    """Persist callback warning without losing task failure diagnostics."""
    task = context.tasks[task_id]
    if task.status in context._FAILURE_TASK_STATUSES:
        if task.error and message not in task.error:
            task.error = f"{task.error}\n\nNotify callback warning: {message}"
        elif not task.error:
            task.error = message
    else:
        task.status = "warning"
        task.error = message
    task.updated_at = datetime.now().isoformat()
    context.save_tasks()


def restore_status_after_notify(
    context: Any,
    task_id: str,
    notification: TaskCompletionNotification,
) -> None:
    """Restore the terminal task status after a successful retry."""
    task = context.tasks[task_id]
    task.status = notification.status
    task.updated_at = datetime.now().isoformat()
    if notification.status == "completed":
        task.error = None
    elif notification.error_message:
        task.error = notification.error_message
    context.save_tasks()


def task_run_matches(task: Task | None, expected_run_id: str | None) -> bool:
    """Return whether a task belongs to the expected execution run."""
    if task is None:
        return False
    return expected_run_id is None or getattr(task, "run_id", None) == expected_run_id


def get_retry_notify_task(
    context: Any,
    task_id: str,
    expected_run_id: str | None,
) -> Task | None:
    """Return the task eligible for notify retry, if still current."""
    task = context.get_task_from_state(task_id)
    if not context._task_run_matches(task, expected_run_id):
        return None
    if task is None or not task.notify_url:
        return None
    return cast(Task, task)


async def run_single_notify_retry_attempt(
    context: Any,
    task_id: str,
    notification: TaskCompletionNotification,
    expected_run_id: str | None,
    attempt: int,
    max_retries: int,
) -> bool:
    """Execute one retry attempt and report whether the loop must stop."""
    current_task = context._get_retry_notify_task(task_id, expected_run_id)
    if current_task is None:
        return True
    try:
        notify_ok, _ = await context._send_notify_callback(current_task, notification)
        if not notify_ok:
            return False
        if context._get_retry_notify_task(task_id, expected_run_id) is None:
            return True
        context._restore_status_after_notify(task_id, notification)
        context.logger.info(
            "Notify URL callback succeeded after retry %s/%s for task %s",
            attempt,
            max_retries,
            task_id,
        )
        return True
    except Exception as exc:
        context.logger.error(
            "Error during notify URL retry to %s: %s", current_task.notify_url, exc
        )
        return False


async def handle_notify_retry_exhausted(
    context: Any,
    task_id: str,
    expected_run_id: str | None,
    max_retries: int,
) -> None:
    """Log and optionally email when callback retries are exhausted."""
    context.logger.warning(
        "Notify URL callback retries exhausted for task %s after %s attempts",
        task_id,
        max_retries,
    )
    current_task = context._get_retry_notify_task(task_id, expected_run_id)
    if current_task is None or current_task.status != "warning":
        return
    try:
        await context.send_notify_retry_exhausted_email(
            task_id=task_id,
            status=current_task.status,
            notify_url=current_task.notify_url or "",
            attempts=max_retries,
            error_message=current_task.error,
        )
    except Exception as exc:
        context.logger.error(
            "Failed to trigger notify retry exhausted email for task %s: %s",
            task_id,
            exc,
        )


async def retry_notify_callback(
    context: Any,
    task_id: str,
    notification: TaskCompletionNotification,
    expected_run_id: str | None = None,
) -> None:
    """Retry a callback with backoff while rejecting stale task runs."""
    if context._get_retry_notify_task(task_id, expected_run_id) is None:
        return
    max_retries = context.config.COMPLETION_NOTIFY_MAX_RETRIES
    delay_seconds = context.config.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS
    backoff_factor = context.config.COMPLETION_NOTIFY_BACKOFF_FACTOR

    for attempt in range(1, max_retries + 1):
        if delay_seconds > 0:
            await context.asyncio.sleep(delay_seconds)
        should_stop = await context._run_single_notify_retry_attempt(
            task_id=task_id,
            notification=notification,
            expected_run_id=expected_run_id,
            attempt=attempt,
            max_retries=max_retries,
        )
        if should_stop:
            return
        delay_seconds = int(delay_seconds * backoff_factor)

    await context._handle_notify_retry_exhausted(task_id, expected_run_id, max_retries)


async def handle_notify_callback(
    context: Any,
    task: Task,
    notification: TaskCompletionNotification,
) -> None:
    """Send a callback and schedule guarded retries when it fails."""
    if not task.notify_url:
        return
    expected_run_id = getattr(task, "run_id", None)
    try:
        notify_ok, notify_error = await context._send_notify_callback(task, notification)
        current_task = context.get_task_from_state(notification.task_id)
        if not context._task_run_matches(current_task, expected_run_id):
            context.logger.info(
                "Skipping stale notify callback update for task %s (run changed)",
                notification.task_id,
            )
            return
        if notify_ok:
            context._restore_status_after_notify(notification.task_id, notification)
            return
        context._set_notify_warning(
            notification.task_id,
            notify_error or "Notify URL callback failed: non-2xx response",
        )
    except Exception as exc:
        current_task = context.get_task_from_state(notification.task_id)
        if not context._task_run_matches(current_task, expected_run_id):
            context.logger.info(
                "Skipping stale notify callback exception update for task %s (run changed)",
                notification.task_id,
            )
            return
        context._set_notify_warning(
            notification.task_id,
            f"Notify URL callback {task.notify_url} failed: server error - {exc}",
        )
        context.logger.error("Error during notify URL callback to %s: %s", task.notify_url, exc)

    context.asyncio.create_task(
        context._retry_notify_callback(
            notification.task_id,
            notification,
            expected_run_id=expected_run_id,
        )
    )
