"""Additional coverage for app.core.priorities."""

from __future__ import annotations

from datetime import datetime

from app.core import priorities
from app.models.models import Task


def _task(task_id: str, *, status: str, notify_url: str) -> Task:
    now = datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id="r1",
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters={},
        notify_url=notify_url,
        completion_callback=None,
        created_at=now,
        updated_at=now,
        error=None,
        script_output=None,
    )


def test_hostname_from_url_edge_cases(monkeypatch):
    assert priorities.hostname_from_url("") is None

    def raise_parse(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(priorities, "urlparse", raise_parse)
    assert priorities.hostname_from_url("http://example.com") is None


def test_is_priority_hostname_and_task():
    assert priorities.is_priority_hostname(None, "example.com") is False
    assert priorities.is_priority_hostname("sub.example.com", "example.com") is True

    task = _task("t1", status="running", notify_url="https://priority.example.com")
    assert priorities.is_priority_task(task, "example.com") is True


def test_other_domain_quota_allows_priority_and_rejects_other():
    tasks = {
        "p1": _task("p1", status="running", notify_url="https://priority.example.com"),
        "o1": _task("o1", status="running", notify_url="https://other.test"),
    }

    assert priorities.other_domain_running_count(tasks, "example.com") == 1
    allowed_other = priorities.max_other_concurrent_tasks(runner_capacity=2, max_other_percent=50)
    assert allowed_other == 1

    # Priority request bypasses quota
    assert (
        priorities.would_exceed_other_domain_quota(
            request_notify_url="https://priority.example.com",
            tasks=tasks,
            runner_capacity=2,
            priority_domain="example.com",
            max_other_percent=50,
        )
        is False
    )
    # Non-priority above quota rejected
    tasks["o2"] = _task("o2", status="running", notify_url="https://another.test")
    assert (
        priorities.would_exceed_other_domain_quota(
            request_notify_url="https://other.test",
            tasks=tasks,
            runner_capacity=2,
            priority_domain="example.com",
            max_other_percent=50,
        )
        is True
    )

    # Under quota should allow non-priority
    assert (
        priorities.would_exceed_other_domain_quota(
            request_notify_url="https://other.test",
            tasks={},
            runner_capacity=2,
            priority_domain="example.com",
            max_other_percent=50,
        )
        is False
    )
