"""Domain-based priority helpers.

These helpers implement a simple, config-driven policy:
- If priorities are enabled, a priority domain is defined (suffix match), and a
  maximum percentage for non-priority tasks is configured.
- The percentage is applied to the runner capacity (number of registered runners)
  to compute a maximum number of concurrently running non-priority tasks.
- If capacity is greater than 0 and percentage is greater than 0, at least one
  non-priority task is allowed.

This keeps behavior deterministic without introducing a global queue.
"""

from __future__ import annotations

import math
from typing import Mapping, Optional
from urllib.parse import urlparse

from app.core.setup_logging import setup_default_logging
from app.models.models import Task

logger = setup_default_logging()


def hostname_from_url(url: str) -> Optional[str]:
    """Extract and normalize hostname from a URL.

    Args:
        url: URL string that may contain a hostname in netloc.

    Returns:
        Lowercased hostname when parsing succeeds and a hostname is present,
        otherwise ``None``.
    """
    if not url:
        logger.debug("hostname_from_url: empty url")
        return None
    try:
        parsed = urlparse(url)
    except Exception:
        # Avoid logging full URL; it may contain sensitive query params.
        logger.debug("hostname_from_url: urlparse failed")
        return None

    hostname = parsed.hostname
    if not hostname:
        logger.debug("hostname_from_url: no hostname")
        return None

    normalized = hostname.strip().lower()
    logger.debug("hostname_from_url: extracted hostname=%s", normalized)
    return normalized


def is_priority_hostname(hostname: Optional[str], priority_domain: str) -> bool:
    """Check whether a hostname belongs to the configured priority domain.

    Matching is suffix-based and supports exact domain and subdomains.

    Args:
        hostname: Hostname to evaluate. ``None`` or empty values are non-priority.
        priority_domain: Configured priority domain.

    Returns:
        ``True`` when ``hostname`` equals ``priority_domain`` or is one of its
        subdomains, else ``False``.
    """
    if not hostname:
        logger.debug("is_priority_hostname: no hostname")
        return False
    domain = (priority_domain or "").strip().lower()
    if not domain:
        logger.debug("is_priority_hostname: empty priority_domain")
        return False

    is_priority = hostname == domain or hostname.endswith(f".{domain}")
    logger.debug(
        "is_priority_hostname: hostname=%s domain=%s is_priority=%s",
        hostname,
        domain,
        is_priority,
    )
    return is_priority


def is_priority_task(task: Task, priority_domain: str) -> bool:
    """Determine whether a task is priority based on its ``notify_url`` hostname.

    Args:
        task: Task model to classify.
        priority_domain: Configured priority domain.

    Returns:
        ``True`` if task notify host matches the priority domain policy.
    """
    hostname = hostname_from_url(task.notify_url)
    return is_priority_hostname(hostname, priority_domain)


def other_domain_running_count(tasks: Mapping[str, Task], priority_domain: str) -> int:
    """Count running tasks that are not in the priority domain.

    Args:
        tasks: Mapping of task_id to task.
        priority_domain: Configured priority domain used for classification.

    Returns:
        Number of tasks in status ``running`` whose notify hostname is
        non-priority.
    """
    count = 0
    for task in tasks.values():
        if task.status != "running":
            continue
        if not is_priority_task(task, priority_domain):
            count += 1
    logger.debug(
        "other_domain_running_count: priority_domain=%s count=%d",
        (priority_domain or "").strip().lower(),
        count,
    )
    return count


def max_other_concurrent_tasks(runner_capacity: int, max_other_percent: int) -> int:
    """Compute max allowed concurrent non-priority tasks.

    Rules:
    - Inputs are clamped to ``capacity >= 0`` and ``0 <= percent <= 100``.
    - Base quota is ``floor(capacity * percent / 100)``.
    - If ``capacity > 0`` and ``percent > 0``, a minimum quota of ``1`` applies.

    Args:
        runner_capacity: Number of registered runners.
        max_other_percent: Configured quota percentage for non-priority tasks.

    Returns:
        Maximum number of concurrently running non-priority tasks.
    """
    capacity = max(0, int(runner_capacity))
    pct = max(0, min(100, int(max_other_percent)))
    max_other = int(math.floor(capacity * (pct / 100.0)))
    if capacity > 0 and pct > 0:
        max_other = max(1, max_other)
    logger.debug(
        "max_other_concurrent_tasks: capacity=%d pct=%d max_other=%d",
        capacity,
        pct,
        max_other,
    )
    return max_other


def would_exceed_other_domain_quota(
    *,
    request_notify_url: str,
    tasks: Mapping[str, Task],
    runner_capacity: int,
    priority_domain: str,
    max_other_percent: int,
) -> bool:
    """Check whether accepting a task would violate non-priority quota.

    A priority request is always accepted by this check and never counted toward
    the non-priority quota.

    Args:
        request_notify_url: Notify URL for the incoming task request.
        tasks: Snapshot of existing tasks.
        runner_capacity: Number of registered runners.
        priority_domain: Configured priority domain.
        max_other_percent: Configured quota percentage for non-priority tasks.

    Returns:
        ``True`` when the request is non-priority and current non-priority
        running tasks are already at quota, else ``False``.
    """

    # If request is priority, never reject here.
    request_hostname = hostname_from_url(request_notify_url)
    if is_priority_hostname(request_hostname, priority_domain):
        logger.debug(
            "would_exceed_other_domain_quota: priority request hostname=%s domain=%s -> allow",
            request_hostname,
            (priority_domain or "").strip().lower(),
        )
        return False

    allowed_other = max_other_concurrent_tasks(runner_capacity, max_other_percent)
    current_other = other_domain_running_count(tasks, priority_domain)

    reject = current_other >= allowed_other
    if reject:
        logger.info(
            "Priority quota reached: reject non-priority task (hostname=%s domain=%s other_running=%d allowed_other=%d capacity=%d pct=%d)",
            request_hostname,
            (priority_domain or "").strip().lower(),
            current_other,
            allowed_other,
            int(runner_capacity),
            int(max_other_percent),
        )
    else:
        logger.debug(
            "would_exceed_other_domain_quota: allow non-priority (hostname=%s domain=%s other_running=%d allowed_other=%d)",
            request_hostname,
            (priority_domain or "").strip().lower(),
            current_other,
            allowed_other,
        )

    # If allowed_other is 0, reject all non-priority tasks.
    return reject
