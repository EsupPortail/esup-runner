"""Sanitized MediaMTX diagnostics and their integration into administration logs."""

import subprocess
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.routes import logs
from app.core.config import config
from app.core.live_config import LiveSettings
from app.services import live_logs


@pytest.fixture
def settings(monkeypatch):
    value = LiveSettings(enabled=True, sources={"room": "secret-key/room"})
    monkeypatch.setattr(config, "LIVE", value)
    return value


@pytest.mark.parametrize(
    "detail", ["invalid credentials", "no credentials", "IP not allowed", "path not allowed"]
)
def test_authentication_events_are_warnings_without_remote_secrets(settings, detail):
    line = f"2026/10/01 11:22:33 INF [RTMP] [conn 127.0.0.1:1234] closed: authentication failed: {detail} pass=secret-token path=secret-key/room <script>"
    result = live_logs.parse_media_line(line)
    assert result["level"] == "WARNING"
    assert "[LIVE] [RTMP_AUTH_FAILED]" in result["message"]
    assert result["timestamp"] == "2026-10-01 11:22:33"
    for secret in ("secret-token", "secret-key", "<script>", detail):
        assert secret not in str(result)


@pytest.mark.parametrize(
    "body,code",
    [
        ("[API] authentication error: invalid credentials", "MEDIA_AUTH_FAILED"),
        ("[path secret-key/room] [RTMP source] ready: 2 tracks", "SOURCE_PUBLISHED"),
        (
            "[RTMP] [conn 127.0.0.1:1234] is publishing to path 'secret-key/room'",
            "SOURCE_PUBLISHED",
        ),
        (
            "[RTMP] [conn [::1]:1234] is publishing to path 'unknown-secret/other'",
            "SOURCE_PUBLISHED",
        ),
        (
            "[path unknown-secret/other] [RTMP source] ready: 2 tracks",
            "SOURCE_PUBLISHED",
        ),
        ("[path ../secret] [RTMP source] ready: 2 tracks", "SOURCE_PATH_REJECTED"),
        ("[RTMP] [conn 127.0.0.1:12] closed: EOF", "RTMP_DISCONNECTED"),
        ("[RTMP] [conn 127.0.0.1:12] closed: terminated", "RTMP_DISCONNECTED"),
        (
            "[RTMP] [conn 127.0.0.1:12] closed: malformed pass=secret",
            "RTMP_CONNECTION_FAILED",
        ),
        ("[path live/secret] already publishing", "PUBLISHER_CONFLICT"),
    ],
)
def test_known_events_hide_paths_and_credentials(settings, body, code):
    result = live_logs.parse_media_line("2026/10/01 11:22:33 INF " + body)
    assert code in result["message"] and "secret" not in str(result)
    if code == "SOURCE_PUBLISHED":
        broadcaster = "other" if "/other" in body else "room"
        assert f"source_id={broadcaster}." in result["message"]


@pytest.mark.parametrize(
    "line",
    [
        "",
        "invalid",
        "2026/10/01 11:22:33 ERR [API] path not found",
        "2026/10/01 11:22:33 INF [RTMP] opened",
    ],
)
def test_irrelevant_or_malformed_lines_are_not_exposed(line):
    assert live_logs.parse_media_line(line) is None


def test_unconfigured_reader_does_not_spawn(settings, monkeypatch):
    monkeypatch.setattr(live_logs, "media_lines", lambda: pytest.fail("unexpected read"))
    assert live_logs.read_media_logs() == []
    settings.enabled = False
    settings.mediamtx_log_unit = "esup-runner-mediamtx@manager.service"
    assert live_logs.read_media_logs() == []


def test_journal_is_scoped_bounded_and_without_shell(settings, monkeypatch):
    settings.mediamtx_log_unit = "esup-runner-mediamtx@manager.service"

    def run(args, **kwargs):
        assert args == [
            "journalctl",
            "--user",
            "--unit",
            settings.mediamtx_log_unit,
            "--lines",
            "2000",
            "--no-pager",
            "--output=cat",
            "--quiet",
        ]
        assert kwargs == {"capture_output": True, "check": True, "timeout": 3}
        return SimpleNamespace(
            stdout=b"2026/10/01 11:22:33 INF [RTMP] closed: authentication failed: invalid credentials\n"
        )

    monkeypatch.setattr(live_logs.subprocess, "run", run)
    assert "RTMP_AUTH_FAILED" in live_logs.read_media_logs()[0]["message"]


@pytest.mark.parametrize(
    "error",
    [
        OSError("private-path"),
        subprocess.TimeoutExpired("secret", 3),
        subprocess.CalledProcessError(1, "secret"),
    ],
)
def test_read_failures_do_not_echo_exception(settings, monkeypatch, error):
    settings.mediamtx_log_unit = "esup-runner-mediamtx@manager.service"

    def fail():
        raise error

    monkeypatch.setattr(live_logs, "media_lines", fail)
    result = live_logs.read_media_logs()
    assert "MEDIA_LOG_UNAVAILABLE" in result[0]["message"]
    assert "secret" not in str(result) and "private-path" not in str(result)


def test_file_tail_and_manager_filters(settings, tmp_path, monkeypatch):
    path = tmp_path / "mediamtx.log"
    settings.mediamtx_log_file = str(path)
    assert "MEDIA_LOG_UNAVAILABLE" in live_logs.read_media_logs()[0]["message"]
    path.write_text(
        "truncated-secret-data\n" * 200
        + "2026/10/01 11:22:33 INF [RTMP] closed: authentication failed\n"
    )
    monkeypatch.setattr(live_logs, "MAX_BYTES", 200)
    assert len(live_logs.read_media_logs()) == 1
    manager = logs.LogManager([])
    assert manager.read_logs(search_term="[LIVE]")[0]["level"] == "WARNING"
    assert manager.read_logs(level_filter=["ERROR"]) == []
    assert manager.read_logs(search_term="missing-word") == []
    env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape())
    html = env.get_template("logs_search.html").render(
        logs=manager.read_logs(), search_term="[LIVE]", total_results=1
    )
    assert "Live</span>" in html and "RTMP_AUTH_FAILED" in html


def test_logging_configuration_rejects_ambiguous_or_unsafe_sources():
    with pytest.raises(ValueError):
        LiveSettings(mediamtx_log_unit="--all")
    with pytest.raises(ValueError, match="Choose"):
        LiveSettings(
            enabled=True, mediamtx_log_unit="example.service", mediamtx_log_file="/tmp/a"
        ).validate_enabled()
    with pytest.raises(ValueError, match="absolute"):
        LiveSettings(enabled=True, mediamtx_log_file="relative.log").validate_enabled()
