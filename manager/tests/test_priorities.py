"""Tests for domain-based priorities and quota rejection."""

from datetime import datetime

import pytest

from app.core.priorities import (
    hostname_from_url,
    is_priority_hostname,
    max_other_concurrent_tasks,
    other_domain_running_count,
    would_exceed_other_domain_quota,
)
from app.core.state import runners, tasks
from app.models.models import Runner, Task


def _make_task(task_id: str, notify_url: str, status: str = "running") -> Task:
    return Task(
        task_id=task_id,
        runner_id="runner-1",
        status=status,
        etab_name="test_etab",
        app_name="test_app",
        app_version="1.0.0",
        task_type="video",
        source_url="http://example.com/source",
        affiliation="qa",
        parameters={},
        notify_url=notify_url,
        created_at=datetime.now().isoformat(),
        updated_at=datetime.now().isoformat(),
    )


def test_hostname_from_url():
    assert hostname_from_url("https://pod.umontpellier.fr/callback") == "pod.umontpellier.fr"
    assert hostname_from_url("http://UMONTPELLIER.FR") == "umontpellier.fr"
    assert hostname_from_url("not-a-url") is None


@pytest.mark.parametrize(
    "hostname,domain,expected",
    [
        ("umontpellier.fr", "umontpellier.fr", True),
        ("pod.umontpellier.fr", "umontpellier.fr", True),
        ("evilumontpellier.fr", "umontpellier.fr", False),
        ("umontpellier.fr", "", False),
        (None, "umontpellier.fr", False),
    ],
)
def test_is_priority_hostname(hostname, domain, expected):
    assert is_priority_hostname(hostname, domain) is expected


def test_quota_math_flooring():
    assert max_other_concurrent_tasks(10, 20) == 2
    assert max_other_concurrent_tasks(3, 20) == 0
    assert max_other_concurrent_tasks(5, 0) == 0
    assert max_other_concurrent_tasks(5, 100) == 5


def test_other_domain_running_count():
    local_tasks = {
        "p1": _make_task("p1", "https://pod.umontpellier.fr/cb", status="running"),
        "o1": _make_task("o1", "https://other.fr/cb", status="running"),
        "o2": _make_task("o2", "https://other.fr/cb", status="completed"),
    }

    assert other_domain_running_count(local_tasks, "umontpellier.fr") == 1


def test_would_exceed_quota_rejects_non_priority():
    local_tasks = {
        "o1": _make_task("o1", "https://other.fr/cb", status="running"),
        "o2": _make_task("o2", "https://other.fr/cb", status="running"),
    }

    assert (
        would_exceed_other_domain_quota(
            request_notify_url="https://other.fr/cb",
            tasks=local_tasks,
            runner_capacity=10,
            priority_domain="umontpellier.fr",
            max_other_percent=20,
        )
        is True
    )


def test_would_exceed_quota_allows_priority_even_if_full():
    local_tasks = {
        "o1": _make_task("o1", "https://other.fr/cb", status="running"),
        "o2": _make_task("o2", "https://other.fr/cb", status="running"),
    }

    assert (
        would_exceed_other_domain_quota(
            request_notify_url="https://pod.umontpellier.fr/cb",
            tasks=local_tasks,
            runner_capacity=10,
            priority_domain="umontpellier.fr",
            max_other_percent=20,
        )
        is False
    )


def test_execute_rejects_when_quota_reached(client, auth_headers, monkeypatch):
    # Arrange config
    from app.core.config import config

    monkeypatch.setattr(config, "PRIORITIES_ENABLED", True)
    monkeypatch.setattr(config, "PRIORITY_DOMAIN", "umontpellier.fr")
    monkeypatch.setattr(config, "MAX_OTHER_DOMAIN_TASK_PERCENT", 20)

    # Arrange state
    original_tasks = dict(tasks)
    original_runners = dict(runners)

    try:
        tasks.clear()
        runners.clear()

        # Capacity = 10 runners => allowed_other = floor(10 * 0.2) = 2
        for i in range(10):
            runners[f"r{i}"] = Runner(
                id=f"r{i}",
                url=f"http://127.0.0.1:{9000+i}",
                task_types=["video"],
                token="t",
                version="1.0.0",
                last_heartbeat=datetime.now(),
            )

        tasks["o1"] = _make_task("o1", "https://other.fr/cb", status="running")
        tasks["o2"] = _make_task("o2", "https://other.fr/cb", status="running")

        # Act
        resp = client.post(
            "/task/execute",
            headers=auth_headers,
            json={
                "etab_name": "um",
                "app_name": "pod",
                "app_version": "1.0.0",
                "task_type": "video",
                "source_url": "https://example.com/source",
                "affiliation": "qa",
                "parameters": {},
                "notify_url": "https://other.fr/callback",
            },
        )

        # Assert
        assert resp.status_code == 503
    finally:
        tasks.clear()
        tasks.update(original_tasks)
        runners.clear()
        runners.update(original_runners)
