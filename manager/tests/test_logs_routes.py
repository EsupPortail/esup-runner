"""Coverage-oriented tests for app.api.routes.logs."""

from __future__ import annotations

import builtins
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_admin
from app.main import app
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
def logs_module():
    # Import inside fixture so tests can monkeypatch module globals cleanly.
    from app.api.routes import logs as logs_module  # type: ignore

    return logs_module


@pytest.fixture
def restore_log_paths(logs_module):
    original_paths = list(logs_module.log_manager.log_paths)
    yield
    logs_module.log_manager.log_paths = original_paths


def test_logparser_parse_matches_pattern(logs_module):
    line = (
        "2025-10-22 15:43:34 - runner - INFO - [encoding_handler:execute_task:134] - "
        "Encoding task completed successfully\n"
    )

    parsed = logs_module.LogParser.parse_log_line(line)
    assert parsed["timestamp"] == "2025-10-22 15:43:34"
    assert parsed["module"] == "runner"
    assert parsed["level"] == "INFO"
    assert parsed["context"].startswith("[")
    assert "Encoding task" in parsed["message"]


def test_logparser_parse_fallback(logs_module):
    parsed = logs_module.LogParser.parse_log_line("not matching")
    assert parsed["module"] == "UNKNOWN"
    assert parsed["level"] == "UNKNOWN"
    assert parsed["message"] == "not matching"


def test_logmanager_read_logs_filters_and_sorts(tmp_path: Path, logs_module):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-01-01 00:00:01 - manager - INFO - [x] - hello\n"
        "2026-01-01 00:00:02 - manager - ERROR - [x] - boom\n",
        encoding="utf-8",
    )

    manager = logs_module.LogManager([str(log_file)])

    # Filter by level
    only_error = manager.read_logs(limit=100, level_filter=["ERROR"])
    assert len(only_error) == 1
    assert only_error[0]["level"] == "ERROR"

    # Search term
    only_hello = manager.read_logs(limit=100, search_term="hello")
    assert len(only_hello) == 1
    assert "hello" in only_hello[0]["raw"]

    # Chronological display order (oldest -> newest)
    ordered = manager.read_logs(limit=100)
    assert ordered[0]["raw"].endswith("hello")
    assert ordered[1]["raw"].endswith("boom")

    # Sorting + limit
    limited = manager.read_logs(limit=1)
    assert len(limited) == 1
    assert limited[0]["raw"].endswith("boom")


def test_logmanager_groups_multiline_payloads(tmp_path: Path, logs_module):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-02-12 13:56:01 - manager - WARNING - [task:_send_notify_callback:263] - "
        "Notify URL callback failed: 403 - \n"
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        "  <title>403 Forbidden</title>\n"
        "</head>\n",
        encoding="utf-8",
    )

    manager = logs_module.LogManager([str(log_file)])
    logs = manager.read_logs(limit=100)

    assert len(logs) == 1
    assert logs[0]["level"] == "WARNING"
    assert "<!DOCTYPE html>" in logs[0]["message"]
    assert "<title>403 Forbidden</title>" in logs[0]["raw"]

    filtered = manager.read_logs(limit=100, search_term="forbidden")
    assert len(filtered) == 1
    assert filtered[0]["level"] == "WARNING"


def test_logmanager_keeps_leading_unknown_and_skips_blank_lines(tmp_path: Path, logs_module):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "\n"
        "leading unstructured line\n"
        "2026-02-12 13:56:01 - manager - INFO - [x] - structured\n",
        encoding="utf-8",
    )

    manager = logs_module.LogManager([str(log_file)])
    logs = manager.read_logs(limit=100)

    assert len(logs) == 2
    assert any(
        log["level"] == "UNKNOWN" and log["message"] == "leading unstructured line" for log in logs
    )
    assert any(log["level"] == "INFO" and log["message"] == "structured" for log in logs)


def test_logmanager_skips_missing_file(tmp_path: Path, logs_module):
    missing = tmp_path / "missing.log"
    manager = logs_module.LogManager([str(missing)])

    logs = manager.read_logs(limit=10)
    assert logs == []


def test_logmanager_continues_on_read_error(tmp_path: Path, logs_module, monkeypatch):
    bad_file = tmp_path / "bad.log"
    ok_file = tmp_path / "ok.log"

    bad_file.write_text("2026-01-01 00:00:01 - manager - INFO - [x] - bad\n", encoding="utf-8")
    ok_file.write_text("2026-01-01 00:00:02 - manager - INFO - [x] - ok\n", encoding="utf-8")

    original_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if str(path) == str(bad_file):
            raise OSError("boom")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)

    manager = logs_module.LogManager([str(bad_file), str(ok_file)])
    logs = manager.read_logs(limit=10)

    assert len(logs) == 1
    assert logs[0]["raw"].endswith("ok")


def test_get_logs_statistics_counts_unknown(logs_module):
    manager = logs_module.LogManager([])
    stats = manager.get_logs_statistics(
        [
            {"level": "INFO"},
            {"level": "UNKNOWN"},
            {"level": "MYSTERY"},
        ]
    )

    assert stats["INFO"] == 1
    assert stats["UNKNOWN"] == 1
    assert stats["MYSTERY"] == 1


def test_tail_logs_success(tmp_path: Path, logs_module):
    f = tmp_path / "t.log"
    f.write_text("a\n" * 5 + "b\n" * 5, encoding="utf-8")

    tail = logs_module.tail_logs(str(f), n=3)
    assert tail == ["b\n", "b\n", "b\n"]


def test_tail_logs_error_returns_empty(logs_module, tmp_path: Path):
    missing = tmp_path / "missing.log"
    assert logs_module.tail_logs(str(missing), n=10) == []


def test_view_logs_ok(admin_client, logs_module, restore_log_paths, tmp_path: Path):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-01-01 00:00:01 - manager - INFO - [x] - hello\n",
        encoding="utf-8",
    )

    logs_module.log_manager.log_paths = [str(log_file)]

    resp = admin_client.get("/logs/?limit=10&level=INFO&search=hello")
    assert resp.status_code == 200
    assert "Logs" in resp.text
    assert "hello" in resp.text


def test_view_logs_raises_500_on_error(admin_client, logs_module, monkeypatch):
    monkeypatch.setattr(
        logs_module.log_manager, "read_logs", lambda *a, **k: (_ for _ in ()).throw(Exception("x"))
    )

    resp = admin_client.get("/logs/")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Error reading logs"


def test_stream_logs_ok(admin_client, logs_module, restore_log_paths, tmp_path: Path):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-01-01 00:00:01 - manager - INFO - [x] - hello\n",
        encoding="utf-8",
    )
    logs_module.log_manager.log_paths = [str(log_file)]

    resp = admin_client.get("/logs/stream?limit=1")
    assert resp.status_code == 200
    assert "hello" in resp.text


def test_stream_logs_returns_html_error_on_exception(admin_client, logs_module, monkeypatch):
    monkeypatch.setattr(
        logs_module.log_manager, "read_logs", lambda *a, **k: (_ for _ in ()).throw(Exception("x"))
    )

    resp = admin_client.get("/logs/stream")
    assert resp.status_code == 200
    assert "Error reading logs" in resp.text


def test_search_logs_ok(admin_client, logs_module, restore_log_paths, tmp_path: Path):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-01-01 00:00:01 - manager - INFO - [x] - needle\n",
        encoding="utf-8",
    )
    logs_module.log_manager.log_paths = [str(log_file)]

    resp = admin_client.get("/logs/search?q=needle&limit=10")
    assert resp.status_code == 200
    assert "needle" in resp.text


def test_search_logs_raises_500_on_error(admin_client, logs_module, monkeypatch):
    monkeypatch.setattr(
        logs_module.log_manager, "read_logs", lambda *a, **k: (_ for _ in ()).throw(Exception("x"))
    )

    resp = admin_client.get("/logs/search?q=x")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Error during search"


def test_logs_stats_ok(admin_client, logs_module, restore_log_paths, tmp_path: Path):
    log_file = tmp_path / "manager.log"
    log_file.write_text(
        "2026-01-01 00:00:01 - manager - INFO - [x] - hello\n"
        "2026-01-01 00:00:02 - manager - ERROR - [x] - boom\n",
        encoding="utf-8",
    )
    logs_module.log_manager.log_paths = [str(log_file)]

    resp = admin_client.get("/logs/api/stats")
    assert resp.status_code == 200

    payload = resp.json()
    assert payload["total"] == 2
    assert payload["by_level"]["INFO"] == 1
    assert payload["by_level"]["ERROR"] == 1
    assert isinstance(payload["last_updated"], str)


def test_logs_stats_raises_500_on_error(admin_client, logs_module, monkeypatch):
    monkeypatch.setattr(
        logs_module.log_manager, "read_logs", lambda *a, **k: (_ for _ in ()).throw(Exception("x"))
    )

    resp = admin_client.get("/logs/api/stats")
    assert resp.status_code == 500
    assert resp.json()["detail"] == "Error calculating statistics"
