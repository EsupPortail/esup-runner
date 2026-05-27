"""Validates task request URL safety checking for private networks and protocol schemes."""

import pytest

from app.models.models import TaskRequest


def test_task_request_allows_empty_urls_but_keeps_value():
    # _validate_safe_url returns early when the value is falsy.
    """Validate Task request allows empty urls but keeps value."""
    req = TaskRequest(
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="",
        affiliation=None,
        parameters={},
        notify_url="",
    )
    assert req.source_url == ""
    assert req.notify_url == ""


@pytest.mark.parametrize(
    "url,expected",
    (
        ("ftp://example.com/x", "must use http or https"),
        ("http:///x", "must have a valid hostname"),
        ("http://127.0.0.1/x", "must not point to a private"),
        ("http://169.254.169.254/x", "must not point to a private"),
    ),
)
def test_task_request_rejects_unsafe_urls(url: str, expected: str):
    """Validate Task request rejects unsafe urls."""
    with pytest.raises(ValueError, match=expected):
        TaskRequest(
            etab_name="UM",
            app_name="pod",
            app_version="1.0",
            task_type="encoding",
            source_url=url,
            affiliation=None,
            parameters={},
            notify_url="https://example.com/notify",
        )
