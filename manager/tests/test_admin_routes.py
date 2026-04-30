"""Coverage-oriented tests for app.api.routes.admin."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.routes import admin as admin_routes
from app.api.routes import statistics as statistics_routes
from app.core import config as config_module
from app.core.auth import verify_admin
from app.core.config import config
from app.core.state import runners, tasks
from app.main import app
from app.models.models import Runner, Task
from app.services import background_service


@pytest.fixture
def admin_client(monkeypatch):
    async def _noop(*_, **__):
        return None

    monkeypatch.setattr(background_service.background_manager, "start_all_services", _noop)
    monkeypatch.setattr(background_service.background_manager, "stop_all_services", _noop)

    app.dependency_overrides[verify_admin] = lambda: True

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.pop(verify_admin, None)


@pytest.fixture
def clean_state():
    original_runners = dict(runners)
    original_tasks = dict(tasks)

    runners.clear()
    tasks.clear()

    yield

    runners.clear()
    runners.update(original_runners)
    tasks.clear()
    tasks.update(original_tasks)


def _make_runner(runner_id: str, *, seconds_ago: int = 0) -> Runner:
    return Runner(
        id=runner_id,
        url=f"http://{runner_id}.example",
        task_types=["encoding"],
        token="",
        version="1.0.0",
        last_heartbeat=datetime.now() - timedelta(seconds=seconds_ago),
        availability="available",
        status="offline",
    )


def _make_task(
    task_id: str,
    runner_id: str,
    *,
    created_at: str,
    status: str = "completed",
    parameters: dict | None = None,
) -> Task:
    now = datetime.now().isoformat()
    return Task(
        task_id=task_id,
        runner_id=runner_id,
        status=status,
        etab_name="UM",
        app_name="pod",
        app_version="1.0",
        task_type="encoding",
        source_url="https://example.com/video.mp4",
        affiliation=None,
        parameters=parameters or {},
        notify_url="https://example.com/notify",
        completion_callback=None,
        created_at=created_at,
        updated_at=now,
        error=None,
        script_output=None,
    )


def test_format_datetime_without_milliseconds_formats_iso_values():
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05.123456")
        == "2026-01-02 03:04:05"
    )
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05Z")
        == "2026-01-02 03:04:05"
    )


def test_format_datetime_without_milliseconds_handles_empty_and_invalid_values():
    assert admin_routes._format_datetime_without_milliseconds(None) == ""
    assert admin_routes._format_datetime_without_milliseconds("") == ""
    assert (
        admin_routes._format_datetime_without_milliseconds("2026-01-02T03:04:05.abc")
        == "2026-01-02 03:04:05"
    )


def test_admin_dashboard_renders_and_orders_tasks(admin_client, clean_state):
    runners["r1"] = _make_runner("r1", seconds_ago=10)

    tasks["t_old"] = _make_task("t_old", "r1", created_at="2026-01-01T00:00:00")
    tasks["t_new"] = _make_task(
        "t_new",
        "r1",
        created_at="2026-01-02T00:00:00.123456",
        parameters={"video_id": "vid-001", "video_slug": "new-video", "video_title": "New Video"},
    )

    admin_client.cookies.set("theme", "dark")
    resp = admin_client.get("/admin")
    assert resp.status_code == 200

    # Newer task should appear before older one in rendered HTML
    assert resp.text.find("t_new") < resp.text.find("t_old")
    assert "vid-001" in resp.text
    assert "new-video" not in resp.text
    assert "New Video" not in resp.text
    assert "2026-01-02 00:00:00" in resp.text
    assert "2026-01-02T00:00:00.123456" not in resp.text


def test_task_detail_not_found(admin_client, clean_state):
    resp = admin_client.get("/admin/task/does-not-exist")
    assert resp.status_code == 404


def test_task_detail_ok(admin_client, clean_state):
    runners["r1"] = _make_runner("r1")
    tasks["t1"] = _make_task(
        "t1",
        "r1",
        created_at="2026-01-01T00:00:00",
        parameters={
            "video_id": "video-123",
            "video_slug": "video-slug-123",
            "video_title": "Video Title 123",
        },
    )

    resp = admin_client.get("/admin/task/t1")
    assert resp.status_code == 200
    assert "t1" in resp.text
    assert "video-123" in resp.text
    assert "video-slug-123" in resp.text
    assert "Video Title 123" in resp.text


def test_runner_detail_not_found(admin_client, clean_state):
    resp = admin_client.get("/admin/runner/does-not-exist")
    assert resp.status_code == 404


def test_runner_detail_ok(admin_client, clean_state):
    runners["r1"] = _make_runner("r1")

    resp = admin_client.get("/admin/runner/r1")
    assert resp.status_code == 200
    assert "r1" in resp.text


def test_admin_tasks_page_renders(admin_client, clean_state):
    runners["r1"] = _make_runner("r1")
    tasks["t1"] = _make_task("t1", "r1", created_at="2026-01-01T00:00:00", status="running")
    tasks["t2"] = _make_task("t2", "r1", created_at="2026-01-01T00:00:01", status="failed")

    resp = admin_client.get("/admin/tasks")
    assert resp.status_code == 200
    assert "Tasks Management" in resp.text
    assert "t1" in resp.text
    assert "t2" in resp.text


def test_toggle_theme_redirects_and_sets_cookie(admin_client):
    # With current=dark -> new=light
    admin_client.cookies.set("theme", "dark")
    resp1 = admin_client.post("/admin/toggle-theme", follow_redirects=False)
    assert resp1.status_code == 303
    assert "theme=light" in resp1.headers.get("set-cookie", "")

    # With no cookie -> new=dark
    admin_client.cookies.clear()
    resp2 = admin_client.post("/admin/toggle-theme", follow_redirects=False)
    assert resp2.status_code == 303
    assert "theme=dark" in resp2.headers.get("set-cookie", "")


def test_credentials_page_renders_token_previews(admin_client, monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(
        config,
        "AUTHORIZED_TOKENS",
        {
            "long": "A" * 25,
            "short": "abcdef",
            "tiny": "xyz",
        },
    )
    monkeypatch.setattr(config, "ADMIN_USERS", {"admin": "hashed"})

    admin_client.cookies.set("theme", "dark")
    resp = admin_client.get("/admin/credentials")
    assert resp.status_code == 200

    # Preview rules from route implementation
    assert "AAAAAAAAAA...AAAA" in resp.text  # 10 + ... + last 4
    assert "abcd..." in resp.text
    assert "***" in resp.text


def test_reload_config_endpoint_returns_expected_payload(admin_client, monkeypatch):
    fake = SimpleNamespace(
        API_DOCS_VISIBILITY="public",
        AUTHORIZED_TOKENS={"x": "y", "z": "w"},
        ADMIN_USERS={"alice": "h1", "bob": "h2"},
    )

    monkeypatch.setattr(config_module, "reload_config_env", lambda: fake)
    monkeypatch.setattr(config_module, "publish_config_reload_event", lambda: 0)

    resp = admin_client.post("/admin/reload-config")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["api_docs_visibility"] == "public"
    assert payload["authorized_tokens"] == ["x", "z"]
    assert payload["authorized_tokens_count"] == 2
    assert payload["admin_users"] == ["alice", "bob"]
    assert payload["admin_users_count"] == 2


@pytest.mark.parametrize(
    "visibility,expected_cookie_set",
    [
        ("private", True),
        ("public", False),
    ],
)
def test_documentation_page_uses_cookie_without_query_token(  # noqa: D401
    admin_client, monkeypatch, visibility: str, expected_cookie_set: bool
):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", visibility)
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"t": "sekret"})
    monkeypatch.setattr(config, "OPENAPI_COOKIE_SECRET", "unit-test-secret")
    monkeypatch.setattr(config, "OPENAPI_COOKIE_MAX_AGE_SECONDS", 900)

    resp = admin_client.get("/admin/docs")
    assert resp.status_code == 200

    assert "?token=sekret" not in resp.text
    set_cookie = resp.headers.get("set-cookie", "")
    if expected_cookie_set:
        assert "openapi_token=" in set_cookie
        assert "openapi_token=sekret" not in set_cookie
        assert "Max-Age=900" in set_cookie
    else:
        assert "openapi_token=" in set_cookie


def test_documentation_page_deletes_cookie_when_builder_returns_none(admin_client, monkeypatch):
    monkeypatch.setattr(config, "API_DOCS_VISIBILITY", "private")
    monkeypatch.setattr(config, "AUTHORIZED_TOKENS", {"t": "sekret"})
    monkeypatch.setattr(admin_routes, "build_openapi_cookie_value", lambda _token: None)

    resp = admin_client.get("/admin/docs")
    assert resp.status_code == 200

    set_cookie = resp.headers.get("set-cookie", "")
    assert "openapi_token=" in set_cookie
    assert "Max-Age=0" in set_cookie


def test_admin_dashboard_logs_csv_errors(admin_client, clean_state, monkeypatch, tmp_path):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text("task_id,date\n1,2026-02-03\n")

    # Ensure csv_path.exists() is True and DictReader raises to hit error branch
    monkeypatch.setattr(admin_routes, "Path", lambda *_a, **_k: csv_dir)

    def _raising_reader(*_args, **_kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(admin_routes.csv, "DictReader", _raising_reader)

    resp = admin_client.get("/admin")
    assert resp.status_code == 200


def test_statistics_helpers_cover_branches(tmp_path, monkeypatch):
    missing = tmp_path / "missing.csv"
    assert statistics_routes._load_task_stats_csv(missing) == []

    csv_path = tmp_path / "data.csv"
    csv_path.write_text("task_id,date\n1,2026-02-01\n")

    def _raising_reader(*_args, **_kwargs):
        raise RuntimeError("nope")

    monkeypatch.setattr(statistics_routes.csv, "DictReader", _raising_reader)
    assert statistics_routes._load_task_stats_csv(csv_path) == []

    counter_out = statistics_routes._sorted_counter(statistics_routes.Counter({"b": 1, "a": 2}))
    assert counter_out[0]["label"] == "a"
    assert counter_out[0]["count"] == 2

    assert statistics_routes._parse_iso_date("2026-02-01") == date(2026, 2, 1)
    assert statistics_routes._parse_iso_date("2026-02-31") is None
    assert statistics_routes._parse_iso_date(None) is None

    rows = [
        {"date": "2026-02-01"},
        {"date": "2026-02-10"},
        {"date": "invalid"},
        {"date": ""},
    ]
    start_bound, end_bound = statistics_routes._available_date_bounds(rows)
    assert start_bound == date(2026, 2, 1)
    assert end_bound == date(2026, 2, 10)

    assert statistics_routes._filter_rows_by_date_range(rows, None, None) == rows
    assert statistics_routes._filter_rows_by_date_range(rows, date(2026, 2, 5), None) == [
        {"date": "2026-02-10"}
    ]


def test_statistics_dashboard_renders_with_data(admin_client, monkeypatch, tmp_path):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text(
        "task_id,date,task_type,etab_name\n" "1,2026-02-01,encode,UM\n" "2,2026-02-02,other,UA\n"
    )

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics")
    assert resp.status_code == 200
    assert "2026-02-01" in resp.text
    assert "2026-02-02" in resp.text
    assert "2026-02-01 \u2192 2026-02-02" in resp.text
    assert "encode" in resp.text
    assert "/statistics/task-stats.csv" in resp.text


def test_statistics_dashboard_filters_by_date_range(admin_client, monkeypatch, tmp_path):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    (csv_dir / "task_stats.csv").write_text(
        "task_id,date,task_type,etab_name\n"
        "1,2026-02-01,encode,UM\n"
        "2,2026-02-10,other,UA\n"
        "3,2026-03-01,encode,UB\n"
        "4,,encode,UC\n"
    )

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics?start_date=2026-02-05&end_date=2026-02-28")
    assert resp.status_code == 200
    assert 'name="start_date"' in resp.text
    assert 'name="end_date"' in resp.text
    assert 'value="2026-02-05"' in resp.text
    assert 'value="2026-02-28"' in resp.text
    assert "2026-02-05 \u2192 2026-02-28" in resp.text

    data_match = re.search(
        r'<script id="statistics-data" type="application/json">\s*(.*?)\s*</script>',
        resp.text,
        re.DOTALL,
    )
    assert data_match is not None
    stats_data = json.loads(data_match.group(1))
    assert stats_data["by_date"] == [{"label": "2026-02-10", "count": 1}]
    assert stats_data["by_type"] == [{"label": "other", "count": 1}]
    assert stats_data["by_etab"] == [{"label": "UA", "count": 1}]


def test_statistics_csv_download_returns_attachment(admin_client, monkeypatch, tmp_path):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()
    csv_content = "task_id,date,task_type,etab_name\n1,2026-02-01,encode,UM\n"
    (csv_dir / "task_stats.csv").write_text(csv_content)

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv")
    assert resp.status_code == 200
    assert resp.text == csv_content

    content_disposition = resp.headers.get("content-disposition", "")
    assert "attachment" in content_disposition
    assert re.search(r"task_stats_\d{8}\.csv", content_disposition)


def test_statistics_csv_download_returns_404_when_missing(admin_client, monkeypatch, tmp_path):
    csv_dir = tmp_path / "data"
    csv_dir.mkdir()

    monkeypatch.setattr(statistics_routes, "Path", lambda *_a, **_k: csv_dir)

    resp = admin_client.get("/statistics/task-stats.csv")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Task stats CSV not found"
