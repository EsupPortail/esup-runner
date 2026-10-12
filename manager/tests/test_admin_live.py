"""Live monitoring rendering, freshness, capacity and administrative authorization."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.api.routes import admin
from app.api.routes import live as live_routes
from app.api.routes import statistics
from app.core import state
from app.core.auth import verify_admin
from app.core.config import config
from app.core.live_config import LiveSettings
from app.core.persistence import SafeDailyJSONPersistence
from app.models.live import LiveInfo
from app.models.models import Runner, Task
from app.services import live_admin, live_autostart, live_sources, task_service


def session(**fields):
    """Create a public Live task with private fields that must never be rendered."""
    now = datetime.now().isoformat()
    return Task(
        task_id=str(uuid4()),
        runner_id="mixed-runner",
        task_type="live",
        status="running",
        source_url="rtmp://private:password@media/ingest-key",
        parameters={"source_path": "secret-ingest-key"},
        client_token="client-secret",
        notify_url="",
        etab_name="University",
        app_name="Pod",
        created_at=now,
        updated_at=now,
        live=LiveInfo(source_id="lecture", event_id="event-1", **fields),
    )


def web_request(query=b""):
    """Create a proxied admin request for direct route and Jinja rendering checks."""
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/admin/live",
            "root_path": "/manager",
            "headers": [(b"cookie", b"theme=dark")],
            "query_string": query,
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_live_overview_capacity_freshness_and_no_secrets(monkeypatch):
    now = datetime.now()
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"lecture": "private-key/lecture"})
    )
    ready = session(
        state="running",
        hls_ready=True,
        last_seen=now.isoformat(),
        hls_url="https://live.example/hls/master.m3u8",
        source_ready=True,
        input_bitrate=2_000_000,
        hls_sessions=4,
    )
    stale = session(
        state="running",
        hls_ready=True,
        hls_url="https://live.example/stale",
        last_seen=(now - timedelta(minutes=10)).isoformat(),
    )
    failed = session(state="failed", error="FFmpeg failed")
    failed.created_at = (now - timedelta(minutes=5)).isoformat()
    failed.updated_at = (now - timedelta(minutes=2)).isoformat()
    worker = Runner(
        id="mixed-runner",
        url="http://private",
        task_types=["encoding", "live"],
        last_heartbeat=now,
        token="runner-secret",
    )
    workers = [
        worker,
        worker.model_copy(update={"id": "busy", "availability": "busy"}),
        worker.model_copy(update={"id": "reserved", "live_task_id": ready.task_id}),
        worker.model_copy(update={"id": "offline", "last_heartbeat": now - timedelta(minutes=5)}),
        worker.model_copy(update={"id": "vod", "task_types": ["encoding"]}),
    ]
    vod = ready.model_copy(update={"live": None, "task_type": "encoding"})
    overview = live_admin.build_overview([failed, ready, stale, vod], workers, now)
    assert overview["active_count"] == 2
    assert overview["ready_count"] == 1
    assert overview["attention_count"] == 1
    assert overview["failed_count"] == 1
    assert overview["available_count"] == 1 and len(overview["workers"]) == 4
    assert overview["sessions"][-1]["duration_seconds"] == 180
    assert overview["sessions"][-1]["ended_at"] == failed.updated_at
    assert all(row["ended_at"] is None for row in overview["sessions"] if row["active"])
    assert live_admin.session_row(ready, now)["input_mbps"] == 2
    assert live_admin.session_row(ready, now)["hls_sessions"] == 4
    assert live_admin.session_row(ready, now)["broadcaster"] == "lecture"
    for secret in ("client-secret", "runner-secret", "secret-ingest-key", "rtmp://", "password"):
        assert secret not in str(overview)


def test_generated_source_id_has_a_compact_display_label():
    source_id = "source-" + "a" * 32
    task = session()
    assert task.live is not None
    task.live.source_id = source_id
    row = live_admin.session_row(
        task,
        datetime.now(),
    )

    assert row["source_id"] == source_id
    assert row["source_label"] == source_id


def test_rtmp_and_restart_event_ids_have_compact_display_labels():
    connection_id = "3fba47fa-c3a4-45a1-a7d3-d1b772e95bbc"
    restart_id = "7d9e1945-d72e-4997-acf3-038d0132f02f"

    assert (
        live_admin.display_event_id(f"rtmp:source-{'a' * 32}:{connection_id}")
        == f"rtmp:source-{'a' * 32}:3fba47fa..."
    )
    assert live_admin.display_event_id(f"restart:{restart_id}") == "restart:7d9e1945..."
    assert live_admin.display_event_id("event-1") == "event-1"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:secret@host/a",
        "https://:secret@host/a",
        "https://host/a?token=secret",
        "http://[broken",
        None,
    ],
)
def test_unsafe_hls_url_never_becomes_link(url):
    row = live_admin.session_row(
        session(state="running", hls_ready=True, last_seen=datetime.now().isoformat(), hls_url=url),
        datetime.now(),
    )
    assert row["hls_url"] is None


def test_age_and_missing_measurements():
    now = datetime.now(timezone.utc)
    assert live_admin.elapsed_seconds(now - timedelta(seconds=30), now) == 30
    assert live_admin.elapsed_seconds(now + timedelta(seconds=30), now) == 0
    assert live_admin.elapsed_seconds("invalid", now) is None
    assert live_admin.elapsed_seconds(None, now) is None
    row = live_admin.session_row(session(), now)
    assert row["stale"] and row["attention"] and row["age_seconds"] is None
    stopping = session(
        state="running",
        hls_ready=True,
        hls_url="https://live.example/hls",
        last_seen=now.isoformat(),
        stop_requested=True,
    )
    assert live_admin.session_row(stopping, now)["hls_url"] is None
    assert live_admin.session_row(stopping, now)["ended_at"] is None


def test_session_row_ignores_invalid_configured_source_path(monkeypatch):
    """An invalid configured source path must not break the monitoring snapshot."""
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"lecture": "invalid path"})
    )

    row = live_admin.session_row(session(state="completed"), datetime.now())

    assert row["broadcaster"] is None


@pytest.mark.asyncio
async def test_dashboard_filters_paginates_and_renders_safely(monkeypatch):
    task = session(
        automatic=True,
        state="running",
        hls_ready=True,
        hls_url="https://live.example/hls",
        last_seen=datetime.now().isoformat(),
        source_ready=True,
        codecs=["H264", "MPEG-4 Audio"],
        resolutions=["1920x1080"],
        hls_sessions=2,
    )
    task.live.event_id = '<script>alert("event")</script>'
    task.live.source_id = "agroecologie"
    task.live.error = '<img src=x onerror="alert(1)">'
    task.created_at = "2026-10-06T10:12:13.123456"
    task.updated_at = "2026-10-07T12:00:00.123456"
    tasks = {str(i): task.model_copy(update={"task_id": str(uuid4())}) for i in range(51)}
    ended = session(state="completed")
    ended.created_at = "2026-10-05T09:00:00.123456"
    ended.updated_at = "2026-10-05T10:11:12.123456"
    tasks[ended.task_id] = ended
    unrelated = session(state="running")
    unrelated.live.source_id = "unregistered"
    tasks[unrelated.task_id] = unrelated
    monkeypatch.setattr(admin, "get_tasks_snapshot", lambda: tasks)
    monkeypatch.setattr(admin, "runners", {})
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )
    monkeypatch.setattr(live_admin, "observe_publishing_sources", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"agroecologie": "live/agroecologie"})
    )
    response = await admin.live_dashboard.__wrapped__(
        web_request(b"message=stop_requested"), state_filter="active", search="  AGROECO  ", page=2
    )
    context = response.context
    assert context["page"] == 2 and context["page_count"] == 2
    assert context["filtered_count"] == 51 and len(context["sessions"]) == 1
    assert context["sessions"][0]["broadcaster"] == "agroecologie"
    env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=select_autoescape())
    html = env.get_template("live.html").render(context)
    assert '<script>alert("event")</script>' not in html
    assert "&lt;script&gt;" in html and "&lt;img" in html
    assert "Stop requested." in html and 'data-bs-theme="dark"' in html
    assert "Search broadcaster, source" in html
    assert "/manager/static/live.js" in html
    assert 'action="/manager/admin/live/' in html and '/stop"' in html
    assert 'name="csrf_token"' in html
    assert "https://live.example/hls" in html
    assert '<div class="small text-subtle text-nowrap">H264, MPEG-4 Audio · 1920x1080</div>' in html
    assert "~2 viewers (by IP)" in html
    assert "Manager instance" in html and "MediaMTX status" in html
    assert '<th scope="col">Runner</th><th scope="col">Start / end</th>' in html
    assert f'<time datetime="{task.created_at}">2026-10-06 10:12:13</time>' in html
    assert "<div>End: Ongoing</div>" in html
    assert f'End: <time datetime="{task.updated_at}"' not in html
    session_cell = html.split('<td class="text-break">', 1)[1].split("</td>", 1)[0]
    visible, details = session_cell.split("<details", 1)
    assert "<strong>agroecologie</strong>" in visible
    assert "lecture" not in visible and "&lt;script&gt;" not in visible
    assert "<dt>Source ID</dt>" in details and "<dt>Event ID</dt>" in details
    assert "<dt>Broadcaster</dt>" not in details
    assert "agroecologie" in details and "&lt;script&gt;" in details
    assert "Automatic RTMP" in html
    assert "live/agroecologie" not in html and "client-secret" not in html
    assert "Previous" in html
    first = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    assert "Next" in first.body.decode()
    filtered = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="completed", search="", page=99
    )
    assert filtered.context["page"] == 1 and filtered.context["filtered_count"] == 1
    completed_html = filtered.body.decode()
    assert f'<time datetime="{ended.created_at}">2026-10-05 09:00:00</time>' in completed_html
    assert f'<time datetime="{ended.updated_at}">2026-10-05 10:11:12</time>' in completed_html
    assert "<div>End: Ongoing</div>" not in completed_html
    missing_broadcaster = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="none", page=1
    )
    assert missing_broadcaster.context["filtered_count"] == 0
    empty = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="unknown", page=1
    )
    assert "No Live sessions match" in empty.body.decode()
    assert "Publish RTMP" in empty.body.decode()
    monkeypatch.setattr(config.LIVE, "auto_start", False)
    manual = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="unknown", page=1
    )
    assert "Start a session" in manual.body.decode()


def test_runner_mediamtx_status_is_safe_and_distinguishes_unknown():
    assert admin._runner_mediamtx_status(
        {"available": True, "payload": {"mediamtx": {"status": "healthy"}}}
    ) == {"status": "healthy", "error": ""}
    assert admin._runner_mediamtx_status(
        {"available": True, "payload": {"mediamtx": {"status": "unavailable"}}}
    ) == {"status": "unavailable", "error": "MediaMTX control unavailable."}
    assert admin._runner_mediamtx_status({"available": False}) == {
        "status": "unknown",
        "error": "Runner status unavailable.",
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (None, {"status": "unknown", "error": "Runner does not report MediaMTX status."}),
        (
            {"mediamtx": "private-invalid-payload"},
            {"status": "unknown", "error": "Runner does not report MediaMTX status."},
        ),
        (
            {"mediamtx": {"status": "disabled", "error": "private-detail"}},
            {"status": "disabled", "error": "Live is disabled on this runner."},
        ),
        (
            {"mediamtx": {"status": "private-invalid-status"}},
            {"status": "unknown", "error": "Runner returned an invalid MediaMTX status."},
        ),
    ],
)
def test_runner_mediamtx_status_handles_missing_disabled_and_invalid_reports(payload, expected):
    """Keep incomplete or disabled Runner reports distinguishable without leaking details."""
    assert admin._runner_mediamtx_status({"available": True, "payload": payload}) == expected


@pytest.mark.asyncio
async def test_live_dashboard_associates_media_reports_with_each_worker(monkeypatch):
    """Render each worker's safe MediaMTX status alongside its own runner identity."""
    workers = {
        identifier: Runner(
            id=identifier,
            url="http://private",
            task_types=["live"],
            last_heartbeat=datetime.now(),
            token="runner-secret",
        )
        for identifier in ["healthy-worker", "disabled-worker"]
    }
    monkeypatch.setattr(config, "LIVE", LiveSettings(enabled=True))
    monkeypatch.setattr(admin, "get_tasks_snapshot", lambda: {})
    monkeypatch.setattr(admin, "runners", workers)
    monkeypatch.setattr(live_admin, "observe_publishing_sources", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )

    async def worker_status(worker):
        return {
            "available": True,
            "payload": {
                "mediamtx": {
                    "status": "healthy" if worker.id == "healthy-worker" else "disabled",
                    "error": "private-media-detail",
                }
            },
        }

    probe = AsyncMock(side_effect=worker_status)
    monkeypatch.setattr(admin, "_fetch_runner_live_status", probe)
    response = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    statuses = {row["id"]: row["mediamtx"] for row in response.context["overview"]["workers"]}
    assert statuses == {
        "healthy-worker": {"status": "healthy", "error": ""},
        "disabled-worker": {"status": "disabled", "error": "Live is disabled on this runner."},
    }
    assert probe.await_count == 2
    html = response.body.decode()
    assert "healthy-worker" in html and "disabled-worker" in html
    assert "Live is disabled on this runner." in html
    assert "runner-secret" not in html and "private-media-detail" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "failure", "expected"),
    [
        (False, False, {"status": "disabled", "error": "Live is disabled on this manager."}),
        (True, False, {"status": "healthy", "error": ""}),
        (True, True, {"status": "unavailable", "error": "MediaMTX control unavailable."}),
    ],
)
async def test_manager_mediamtx_status_is_secret_free(monkeypatch, enabled, failure, expected):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def check_health(self):
            if failure:
                raise admin.MediaMTXError("private control detail")

    monkeypatch.setattr(admin, "MediaMTXClient", FakeClient)
    monkeypatch.setattr(config, "LIVE", LiveSettings(enabled=enabled))

    assert await admin._fetch_manager_mediamtx_status() == expected


@pytest.mark.asyncio
async def test_admin_live_auth_and_csrf(monkeypatch, admin_csrf_headers):
    app = FastAPI()
    app.include_router(admin.router)
    stop = AsyncMock()
    monkeypatch.setattr(admin.live_service, "stop", stop)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/manager"), base_url="http://testserver"
    ) as client:
        assert (await client.get("/admin/live")).status_code == 401
        assert (await client.post("/admin/live/example/stop")).status_code == 401
        stop.assert_not_awaited()
        app.dependency_overrides[verify_admin] = lambda: True
        assert (await client.post("/admin/live/example/stop")).status_code == 403
        assert (
            await client.post(
                "/admin/live/example/stop",
                headers={**admin_csrf_headers, "Origin": "http://evil.example"},
            )
        ).status_code == 403
        stop.assert_not_awaited()
        result = await client.post("/admin/live/example/stop", headers=admin_csrf_headers)
        assert result.status_code == 303
        assert result.headers["location"] == "/manager/admin/live?message=stop_requested"
        stop.assert_awaited_once_with("example", token=None)


@pytest.mark.asyncio
async def test_admin_restart_authorization_csrf_and_feedback(monkeypatch, admin_csrf_headers):
    app = FastAPI()
    app.include_router(admin.router)
    restart = AsyncMock()
    monkeypatch.setattr(admin.live_service, "restart", restart)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/manager"), base_url="http://testserver"
    ) as client:
        assert (await client.post("/admin/live/example/restart")).status_code == 401
        app.dependency_overrides[verify_admin] = lambda: True
        assert (await client.post("/admin/live/example/restart")).status_code == 403
        assert (
            await client.post(
                "/admin/live/example/restart",
                headers={**admin_csrf_headers, "Origin": "http://evil.example"},
            )
        ).status_code == 403
        restart.assert_not_awaited()
        response = await client.post("/admin/live/example/restart", headers=admin_csrf_headers)
        assert response.status_code == 303
        assert response.headers["location"] == "/manager/admin/live?message=restart_requested"
        restart.assert_awaited_once_with("example", token=None)
        for status in (404, 409, 503):
            restart.side_effect = HTTPException(status, "private diagnostic")
            result = await client.post("/admin/live/example/restart", headers=admin_csrf_headers)
            assert result.headers["location"] == f"/manager/admin/live?message=restart_{status}"
            assert "private diagnostic" not in result.text
        restart.side_effect = HTTPException(403, "Forbidden")
        assert (
            await client.post("/admin/live/example/restart", headers=admin_csrf_headers)
        ).status_code == 403


@pytest.mark.asyncio
async def test_restart_offered_on_latest_cleaned_session_only(monkeypatch):
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"lecture": "secret/lecture"})
    )
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )
    monkeypatch.setattr(live_admin, "observe_publishing_sources", AsyncMock(return_value=set()))
    old = session(state="completed")
    old.created_at = (datetime.now() - timedelta(minutes=10)).isoformat()
    latest = session(state="completed")
    overview = live_admin.build_overview([old, latest], [], datetime.now())
    assert [row["id"] for row in overview["sessions"] if row["can_restart"]] == [latest.task_id]
    active = session(state="stopping", stop_requested=True)
    assert not any(
        row["can_restart"]
        for row in live_admin.build_overview([latest, active], [], datetime.now())["sessions"]
    )
    monkeypatch.setattr(admin, "get_tasks_snapshot", lambda: {latest.task_id: latest})
    monkeypatch.setattr(admin, "runners", {})
    response = await admin.live_dashboard.__wrapped__(
        web_request(b"message=restart_requested"), state_filter="all", search="", page=1
    )
    html = response.body.decode()
    assert 'data-live-action="restart"' in html and 'data-live-source="lecture"' in html
    assert 'name="csrf_token"' in html and "A new session and HLS URL" in html
    assert "Live diagnostics" in html
    error = await admin.live_dashboard.__wrapped__(
        web_request(b"message=restart_409"), state_filter="all", search="", page=1
    )
    assert "Restart unavailable:" in error.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_days", [7, 0])
async def test_visible_sources_follow_session_retention_without_deleting_registry(
    tmp_path, monkeypatch, cleanup_days
):
    """Retain active/shared sources and durable bindings when terminal history expires."""
    now = datetime.now()
    monkeypatch.setattr(state, "persistence", SafeDailyJSONPersistence(str(tmp_path)))
    monkeypatch.setattr(config, "CLEANUP_TASK_FILES_DAYS", cleanup_days)
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"configured": "private-key/configured"})
    )
    paths = ["live/expired-key", "live/shared-key", "live/active-key", "live/unused-key"]
    await live_sources.register_paths(paths)
    expired_id, shared_id, active_id, unused_id = map(live_sources.generated_id, paths)
    retained = {}
    for source_id, age, live_state in [
        (expired_id, 8, "completed"),
        (shared_id, 8, "failed"),
        (shared_id, 2, "completed"),
        (active_id, 30, "running"),
    ]:
        task = session(state=live_state)
        task.live.source_id = source_id
        task.created_at = (now - timedelta(days=age)).isoformat()
        task.status = "running" if live_state == "running" else live_state
        retained[task.task_id] = task
    monkeypatch.setattr(state, "tasks", retained)
    monkeypatch.setattr(task_service, "tasks", retained)
    assert state.persistence.upsert_tasks(retained)
    live_autostart.remember_publication(expired_id, f"rtmp:{expired_id}:{uuid4()}")
    registry_before = live_sources.registry_path().read_bytes()
    checkpoint_before = live_autostart.fence_path(expired_id).read_bytes()
    before = live_admin.build_overview(state.get_tasks_snapshot().values(), [], now)
    assert set(before["sources"]) == {expired_id, shared_id, active_id, "configured"}

    stop = asyncio.Event()

    async def finish_cleanup(_interval):
        stop.set()

    monkeypatch.setattr(task_service.asyncio, "sleep", finish_cleanup)
    await task_service.cleanup_old_tasks(stop_event=stop)
    # Rebuild from disk to exercise the same visibility after a Manager restart.
    reloaded = [Task.model_validate(payload) for payload in state.persistence.load_tasks().values()]
    after = live_admin.build_overview(reloaded, [], now)
    expected = {shared_id, active_id, "configured"}
    if cleanup_days == 0:
        expected.add(expired_id)
    assert set(after["sources"]) == expected
    assert unused_id not in after["sources"]
    assert live_sources.registry_path().read_bytes() == registry_before
    assert live_autostart.fence_path(expired_id).read_bytes() == checkpoint_before
    assert set((await live_routes.list_sources())["sources"]) == {
        expired_id,
        shared_id,
        active_id,
        unused_id,
        "configured",
    }
    if cleanup_days:
        (last_shared,) = [task for task in reloaded if task.live.source_id == shared_id]
        assert state.delete_task(last_shared.task_id)
        overview = live_admin.build_overview(state.get_tasks_snapshot().values(), [], now)
        assert shared_id not in overview["sources"]
        assert live_sources.registry_path().read_bytes() == registry_before


def test_configured_source_remains_visible_under_its_saved_identifier(monkeypatch):
    """An alias for a previously discovered path must retain its canonical saved ID."""
    monkeypatch.setattr(config, "LIVE", LiveSettings(sources={"new-alias": "live/same-key"}))
    monkeypatch.setattr(
        live_admin, "known_sources", lambda: {"saved-id": "live/same-key", "old": "live/old-key"}
    )
    overview = live_admin.build_overview([], [], datetime.now())
    assert overview["sources"] == ["saved-id"]
    assert overview["source_labels"] == [{"id": "saved-id", "label": "saved-id"}]


@pytest.mark.asyncio
async def test_publishing_sources_include_rtmp_and_rtmps_without_exposing_paths(monkeypatch):
    monkeypatch.setattr(config, "LIVE", LiveSettings(enabled=True))
    sources = {name: f"live/{name}-private-key" for name in ["rtmp", "rtmps", "absent", "rtsp"]}
    monkeypatch.setattr(live_admin, "known_sources", lambda: sources)
    media = AsyncMock()
    media.list_paths.return_value = [
        {
            "name": sources["rtmp"],
            "available": True,
            "source": {"type": "rtmpConn", "id": str(uuid4())},
        },
        {
            "name": sources["rtmps"],
            "available": True,
            "source": {"type": "rtmpsConn", "id": str(uuid4())},
        },
        {"name": sources["absent"], "available": False},
        {"name": sources["rtsp"], "available": True, "source": {"type": "rtspSession"}},
        {
            "name": "live/unknown",
            "available": True,
            "source": {"type": "rtmpConn", "id": str(uuid4())},
        },
        {"name": {"invalid": "path"}, "available": True},
    ]
    monkeypatch.setattr(live_admin, "MediaMTXClient", lambda *_args: media)
    assert await live_admin.observe_publishing_sources() == {"rtmp", "rtmps"}
    media.list_paths.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [live_admin.MediaMTXError("private detail"), TimeoutError()])
async def test_publishing_source_observation_failure_is_unknown(monkeypatch, failure):
    monkeypatch.setattr(config, "LIVE", LiveSettings(enabled=True))
    media = AsyncMock()
    media.list_paths.side_effect = failure
    monkeypatch.setattr(live_admin, "MediaMTXClient", lambda *_args: media)
    assert await live_admin.observe_publishing_sources() is None


@pytest.mark.asyncio
async def test_publishing_source_observation_disabled_avoids_media_requests(monkeypatch):
    monkeypatch.setattr(config, "LIVE", LiveSettings(enabled=False))
    factory = AsyncMock()
    monkeypatch.setattr(live_admin, "MediaMTXClient", factory)
    assert await live_admin.observe_publishing_sources() == set()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_live_source_display_includes_publishers_without_sessions_and_survives_outage(
    tmp_path, monkeypatch
):
    """Filter history without hiding publishers awaiting capacity or mutating durable state."""
    monkeypatch.setattr(state, "persistence", SafeDailyJSONPersistence(str(tmp_path)))
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"configured": "private-key/configured"})
    )
    paths = ["live/publishing-private-key", "live/historical-private-key"]
    await live_sources.register_paths(paths)
    publishing_id, historical_id = map(live_sources.generated_id, paths)
    live_autostart.remember_publication(historical_id, f"rtmp:{historical_id}:{uuid4()}")
    registry_before = live_sources.registry_path().read_bytes()
    checkpoint_before = live_autostart.fence_path(historical_id).read_bytes()
    monkeypatch.setattr(admin, "get_tasks_snapshot", lambda: {})
    monkeypatch.setattr(admin, "runners", {})
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )
    media = AsyncMock()
    media.list_paths.return_value = [
        {"name": paths[0], "available": True, "source": {"type": "rtmpConn", "id": str(uuid4())}}
    ]
    monkeypatch.setattr(live_admin, "MediaMTXClient", lambda *_args: media)
    response = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="completed", search="no-match", page=99
    )
    assert response.context["overview"]["sources"] == sorted(["configured", publishing_id])
    assert response.context["filtered_count"] == 0
    html = response.body.decode()
    assert "Active and recent sources (2)" in html
    assert publishing_id in html and historical_id not in html
    assert all(path not in html for path in paths)

    media.list_paths.side_effect = live_admin.MediaMTXError("private control failure")
    failed = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    assert set(failed.context["overview"]["sources"]) == {
        "configured",
        publishing_id,
        historical_id,
    }
    assert "Current RTMP publications could not be verified" in failed.body.decode()
    assert "private control failure" not in failed.body.decode()

    media.list_paths.side_effect = None
    media.list_paths.return_value = []
    recovered = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    assert recovered.context["overview"]["sources"] == ["configured"]
    assert not recovered.context["overview"]["source_observation_unavailable"]
    assert live_sources.registry_path().read_bytes() == registry_before
    assert live_autostart.fence_path(historical_id).read_bytes() == checkpoint_before


@pytest.mark.parametrize("publishing", [{"lecture"}, set(), None])
def test_source_details_separate_current_signal_from_latest_session_metadata(
    monkeypatch, publishing
):
    """An ended session's cached signal must not override the current RTMP observation."""
    monkeypatch.setattr(
        config, "LIVE", LiveSettings(enabled=True, sources={"lecture": "private-key/lecture"})
    )
    now = datetime.now()
    old = session(state="completed", equipment="old-encoder")
    old.created_at = (now - timedelta(days=2)).isoformat()
    old.etab_name = "Old university"
    latest = session(state="failed", equipment="new-encoder", source_ready=True)
    latest.created_at = (now - timedelta(hours=1)).isoformat()
    latest.etab_name = "Current university"
    overview = live_admin.build_overview([latest, old], [], now)

    (row,) = live_admin.source_rows(overview, publishing)

    assert row["id"] == "lecture" and row["label"] == "lecture"
    assert row["broadcaster"] == "lecture"
    assert row["publishing"] is (None if publishing is None else bool(publishing))
    assert row["latest_session"]["id"] == latest.task_id
    assert row["latest_session"]["state"] == "failed"
    assert row["latest_session"]["etab_name"] == "Current university"
    assert row["latest_session"]["equipment"] == "new-encoder"
    assert not row["latest_session"]["active"]
    assert row["filter_query"] == "state_filter=all&source_filter=lecture"


def test_source_details_without_session_and_disabled_live_are_unconfirmed(monkeypatch):
    """Disabled media observation cannot imply absence; configured sources have no metadata."""
    monkeypatch.setattr(
        config,
        "LIVE",
        LiveSettings(enabled=False, sources={"configured": "private-key/configured"}),
    )
    overview = live_admin.build_overview([], [], datetime.now())
    (row,) = live_admin.source_rows(overview, set())
    assert row["publishing"] is None and row["latest_session"] is None
    assert row["broadcaster"] == "configured"
    assert row["filter_query"] == "state_filter=all&source_filter=configured"


@pytest.mark.asyncio
async def test_source_table_renders_history_missing_metadata_and_current_signal(monkeypatch):
    """Show independent signal/session states, escaped origin values and links for all sources."""
    monkeypatch.setattr(
        config,
        "LIVE",
        LiveSettings(
            enabled=True,
            sources={
                "lecture": "private-key/lecture",
                "other": "private-key/other",
                "empty": "live/empty",
            },
        ),
    )
    ended = session(state="failed", equipment="<encoder>", source_ready=True)
    ended.etab_name = "<University>"
    active = session(state="running", source_ready=True)
    active.live.source_id = "other"
    # No report means the retained active session cannot be confirmed by its Runner.
    active.live.last_seen = None
    monkeypatch.setattr(
        admin, "get_tasks_snapshot", lambda: {ended.task_id: ended, active.task_id: active}
    )
    monkeypatch.setattr(admin, "runners", {})
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )
    observation = AsyncMock(return_value={"lecture", "empty"})
    monkeypatch.setattr(live_admin, "observe_publishing_sources", observation)

    response = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    table = response.body.decode().split("Active and recent sources (3)", 1)[1]
    assert '<th scope="col">Broadcaster</th>' in table
    assert '<strong id="live-source-id-lecture" tabindex="-1">lecture</strong>' in table
    assert '<strong id="live-source-id-other" tabindex="-1">other</strong>' in table
    assert '<strong id="live-source-id-empty" tabindex="-1">empty</strong>' in table
    assert "Full source ID" not in table and '<input id="live-source-id-' not in table
    assert "&lt;University&gt;" in table and "&lt;encoder&gt;" in table
    assert "<University>" not in table and "<encoder>" not in table
    assert "Historical · from latest session" in table
    assert "No retained session" in table and "Not provided" in table
    assert 'text-bg-success">Present' in table and 'text-bg-secondary">Absent' in table
    assert 'text-bg-danger">failed' in table and 'text-bg-success">running' in table
    assert "Last runner report unconfirmed" in table
    assert ended.created_at.replace("T", " ") in table
    assert f"/manager/admin/task/{ended.task_id}" in table
    assert 'data-copy-live-source="lecture"' in table
    assert 'id="live-source-id-lecture"' in table
    assert "/manager/admin/live?state_filter=all&amp;source_filter=lecture" in table
    assert "private-key/lecture" not in table and "client-secret" not in table

    observation.return_value = None
    unconfirmed = await admin.live_dashboard.__wrapped__(
        web_request(), state_filter="all", search="", page=1
    )
    table = unconfirmed.body.decode().split("Active and recent sources (3)", 1)[1]
    assert table.count('text-bg-warning">Not verified') == 3
    assert 'text-bg-secondary">Absent' not in table


@pytest.mark.asyncio
async def test_view_source_sessions_uses_exact_id_and_preserves_pagination(monkeypatch):
    """Source navigation must exclude similarly named sources and retain the filter on paging."""
    monkeypatch.setattr(
        config,
        "LIVE",
        LiveSettings(
            enabled=True,
            sources={
                "lecture": "private-key/lecture",
                "lecture-other": "private-key/lecture-other",
            },
        ),
    )
    matching = [session(state="completed") for _ in range(51)]
    other = session(state="completed")
    other.live.source_id = "lecture-other"
    other.etab_name = "lecture"
    tasks = {item.task_id: item for item in [*matching, other]}
    monkeypatch.setattr(admin, "get_tasks_snapshot", lambda: tasks)
    monkeypatch.setattr(admin, "runners", {})
    monkeypatch.setattr(
        admin,
        "_fetch_manager_mediamtx_status",
        AsyncMock(return_value={"status": "healthy", "error": ""}),
    )
    monkeypatch.setattr(live_admin, "observe_publishing_sources", AsyncMock(return_value=set()))
    response = await admin.live_dashboard.__wrapped__(
        web_request(b"source_filter=lecture"), state_filter="all", search="", page=2
    )
    context = response.context
    assert context["filtered_count"] == 51 and context["page_count"] == 2
    assert len(context["sessions"]) == 1
    assert all(row["source_id"] == "lecture" for row in context["sessions"])
    assert "source_filter=lecture" in context["filter_query"]
    html = response.body.decode()
    assert 'name="source_filter" value="lecture"' in html
    assert "Showing sessions for source" in html and "Previous" in html
    assert 'href="/manager/admin/live">Reset' in html


@pytest.mark.asyncio
async def test_live_historical_statistics_date_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MANAGER_DATA_DIR", str(tmp_path))
    (tmp_path / "task_stats.csv").write_text(
        "task_id,date,task_type,status,etab_name\na,2026-09-01,live,completed,U\nb,2026-09-02,live,failed,U\nc,2026-09-02,encoding,completed,U\n"
    )
    app = FastAPI()
    app.include_router(statistics.router)
    request = web_request()
    request.scope["router"] = app.router
    response = await statistics.statistics_dashboard(
        request, start_date="2026-09-02", end_date="2026-09-02"
    )
    assert response.context["live_sessions"] == 1 and response.context["total_tasks"] == 2
    assert "1 ended Live sessions" in response.body.decode()
