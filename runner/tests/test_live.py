"""Live state machine, real HLS readiness, FFmpeg arguments and recovery."""

import asyncio
import copy
import signal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.routes import task
from app.api.routes.live import hls_file, hls_file_endpoint
from app.core import state
from app.core.config import config
from app.core.live_config import LiveSettings, LiveVariant
from app.models.models import TaskRequest
from app.services import hls_viewers
from app.services import live_media as live_media_module
from app.services import live_runtime as live
from app.services.live_media import build_command, hls_ready
from app.services.mediamtx import MediaMTXError, MediaMTXUnavailable
from app.services.pod_broadcaster import PodBroadcasterError


@pytest.fixture(
    params=[{"live"}, {"encoding", "studio", "transcription", "live"}], ids=["dedicated", "mixed"]
)
def live_env(tmp_path, monkeypatch, request):
    monkeypatch.setenv("RUNNER_TASK_STATUS_FILE", str(tmp_path / "status.json"))
    monkeypatch.setattr(
        state,
        "_RUNNER_STATE",
        {
            **copy.deepcopy(state._RUNNER_STATE),
            "runner_id": "live-1",
            "task_statuses": {},
            "is_available": True,
            "is_registered": True,
        },
    )
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", set(request.param))
    monkeypatch.setattr(
        config,
        "LIVE",
        LiveSettings(
            upstream_rtmp_url="rtmp://reader:secret@manager:1935",
            local_rtmp_url="rtmp://reader:secret@localhost:2935",
            public_hls_base_url="https://public.example/hls",
            hls_directory=str(tmp_path / "hls"),
            poll_interval=0.001,
            source_timeout=0.01,
            startup_timeout=0.01,
            stall_timeout=0.01,
            stop_timeout=0.01,
        ),
    )
    monkeypatch.setattr(task, "notify_completion", AsyncMock(return_value=True))
    monkeypatch.setattr(live, "JOBS", {})
    request = TaskRequest(
        task_id=str(uuid4()),
        task_type="live",
        etab_name="university",
        app_name="Pod",
        source_url="",
        parameters={"source_id": "room", "source_path": "live/random-key"},
        notify_url="",
        completion_callback=f"{config.MANAGER_URL.rstrip('/')}/task/completion",
    )
    return request


def tracked(request):
    state.set_task_metadata(
        request.task_id,
        runner_id="live-1",
        task_request=request.model_dump(),
        completion_callback=request.completion_callback,
        live={"state": "waiting_source"},
    )
    state.set_task_status(request.task_id, "running")
    state.set_available(False)


def source_payload(ready=True):
    return {
        "available": ready,
        "inboundBytes": 1234,
        "outboundBytes": 1000,
        "hls_sessions": 2,
        "tracks2": [
            {"codec": "H264", "codecProps": {"width": 1280, "height": 720}},
            {"codec": "MPEG-4 Audio"},
        ],
    }


class Process:
    pid = 12345

    def __init__(self):
        self.returncode = None
        self.signals = []

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 0

    async def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.mark.asyncio
async def test_live_requires_explicit_declaration_even_with_media_settings(live_env, monkeypatch):
    """Media settings alone must not enable dispatch or HLS on a VOD sibling."""
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"encoding", "studio"})
    with pytest.raises(HTTPException, match="Live is not enabled") as rejected:
        await task.run_task(live_env, BackgroundTasks(), "token")
    assert rejected.value.status_code == 400
    with pytest.raises(HTTPException, match="HLS not found") as hidden:
        await hls_file(live_env.task_id, "master.m3u8")
    assert hidden.value.status_code == 404
    assert state.get_task_status(live_env.task_id) is None
    assert state.is_available()


@pytest.mark.asyncio
async def test_dedicated_live_runner_rejects_vod_tasks(live_env, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"live"})
    vod_request = live_env.model_copy(update={"task_type": "encoding"})

    with pytest.raises(HTTPException, match="Dedicated live runner"):
        await task.run_task(vod_request, BackgroundTasks(), "token")


@pytest.mark.asyncio
async def test_declaration_exclusive_run_and_duplicate_dispatch(live_env, monkeypatch):
    monkeypatch.setattr(live, "schedule", lambda request: None)
    result = await task.run_task(live_env, BackgroundTasks(), "token")
    assert result["status"] == "started" and not state.is_available()
    assert (await task.run_task(live_env, BackgroundTasks(), "token"))["status"] == "running"
    other = live_env.model_copy(update={"task_id": str(uuid4())})
    with pytest.raises(HTTPException, match="busy"):
        await task.run_task(other, BackgroundTasks(), "token")
    payload = state.get_task_status(live_env.task_id)
    assert payload["live"]["state"] == "waiting_source"
    state.reload_task_statuses_from_disk()
    assert state.get_task_status(live_env.task_id)["task_request"]["task_type"] == "live"


@pytest.mark.asyncio
@pytest.mark.parametrize("first_type", ["live", "encoding"])
async def test_mixed_runner_serializes_live_and_vod_and_routes_stop_by_task_type(
    live_env, monkeypatch, first_type
):
    """A mixed instance keeps one reservation and uses the correct cancellation path."""
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"live", "encoding", "studio"})
    monkeypatch.setattr(live, "schedule", lambda request: None)
    vod = live_env.model_copy(update={"task_id": str(uuid4()), "task_type": "encoding"})
    first, second = (live_env, vod) if first_type == "live" else (vod, live_env)
    assert (await task.run_task(first, BackgroundTasks(), "token"))["status"] == "started"
    with pytest.raises(HTTPException, match="busy"):
        await task.run_task(second, BackgroundTasks(), "token")
    terminated = []
    monkeypatch.setattr(
        task,
        "_terminate_running_task_processes",
        lambda task_id, payload: (terminated.append(task_id) is None, True),
    )
    response = await task.stop_task(first.task_id, "token")
    if first_type == "live":
        assert response["status"] == "running"
        assert state.get_task_status(first.task_id)["live"]["state"] == "stopping"
        assert not terminated
    else:
        assert response.status_code == 202
        assert terminated == [first.task_id]
        assert "live" not in state.get_task_status(first.task_id)
    assert not state.is_available()


@pytest.mark.asyncio
async def test_vod_identifier_cannot_be_reused_or_stopped_as_live(live_env, monkeypatch):
    """Persisted task identity prevents cross-type duplicate dispatch or cancellation."""
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"encoding", "live"})
    vod = live_env.model_copy(update={"task_type": "encoding"})
    await task.run_task(vod, BackgroundTasks(), "token")
    with pytest.raises(HTTPException, match="another task type"):
        await task.run_task(live_env, BackgroundTasks(), "token")
    with pytest.raises(HTTPException, match="not a Live session"):
        live.request_stop(live_env.task_id)
    assert "live" not in state.get_task_status(live_env.task_id)


@pytest.mark.asyncio
async def test_mixed_recovery_defers_vod_retries_until_live_cleanup(live_env, monkeypatch):
    """A persisted Live reservation fences old VOD retries during restart recovery."""
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"encoding", "live"})
    tracked(live_env)
    vod_id = str(uuid4())
    state.set_task_metadata(
        vod_id,
        runner_id="live-1",
        task_request=live_env.model_copy(
            update={"task_id": vod_id, "task_type": "encoding"}
        ).model_dump(),
    )
    state.set_task_status(vod_id, "failed")
    state.reload_task_statuses_from_disk()
    monkeypatch.setattr(live, "schedule", lambda *args, **kwargs: None)
    retry = AsyncMock()
    monkeypatch.setattr(task, "_recover_owned_failed_tasks", retry)
    await task.recover_running_tasks_after_restart()
    retry.assert_not_awaited()
    assert not state.is_available()
    state.set_task_status(live_env.task_id, "completed")
    await task.recover_running_tasks_after_restart()
    retry.assert_awaited_once()
    assert set(retry.call_args.args[0]) == {vod_id}


@pytest.mark.asyncio
async def test_vod_group_preserves_live_sibling_storage(live_env, monkeypatch):
    """An age sweep from a VOD sibling cannot delete shared Live recovery records."""
    from app.managers import service_manager

    monkeypatch.setattr(config, "RUNNER_TASK_TYPES", {"encoding", "studio"})
    monkeypatch.setattr(config, "RUNNER_TASK_TYPES_BY_INSTANCE", [{"encoding", "studio"}, {"live"}])
    monkeypatch.setattr(config, "MAX_FILE_AGE_DAYS", 1)
    monkeypatch.setattr(
        service_manager.storage_manager,
        "cleanup_old_files",
        lambda days: pytest.fail("Shared Live storage must be preserved"),
    )
    await service_manager.storage_cleanup_loop()


@pytest.mark.asyncio
async def test_stop_before_start_is_durable_and_idempotent(live_env, monkeypatch):
    with pytest.raises(HTTPException, match="Invalid Live identifier"):
        live.request_stop("not-a-uuid")
    first = live.request_stop(live_env.task_id)
    assert first["status"] == "completed"
    state.reload_task_statuses_from_disk()
    assert live.request_stop(live_env.task_id)["status"] == "completed"
    assert (await task.run_task(live_env, BackgroundTasks(), "token"))["status"] == "completed"
    assert state.is_available()


def test_failed_stop_tombstone_is_not_reported_as_completed(live_env, monkeypatch):
    from unittest.mock import Mock

    checkpoint = state.persist_live_checkpoint
    monkeypatch.setattr(state, "persist_live_checkpoint", Mock(side_effect=OSError("disk full")))
    with pytest.raises(HTTPException, match="stop could not be persisted"):
        live.request_stop(live_env.task_id)
    assert state.get_task_status(live_env.task_id) is None
    monkeypatch.setattr(state, "persist_live_checkpoint", checkpoint)
    assert live.request_stop(live_env.task_id)["status"] == "completed"


@pytest.mark.asyncio
async def test_recovery_finishes_interrupted_stop_tombstone(live_env):
    state.set_task_metadata(
        live_env.task_id,
        runner_id="live-1",
        stop_requested="live_stop",
        live={"state": "completed", "cleanup_complete": True},
    )
    state.reload_task_statuses_from_disk()
    await live.recover()
    assert state.get_task_status(live_env.task_id)["status"] == "completed"
    assert not live.JOBS


@pytest.mark.parametrize("nvdec", [False, True])
def test_build_cpu_gpu_commands_always_transcode(live_env, nvdec):
    settings = config.LIVE
    settings.nvdec = nvdec
    directory = live.directory_for(live_env.task_id)
    cpu = build_command(settings, "rtmp://localhost/stream", directory, gpu=False)
    assert "libx264" in cpu and "copy" not in cpu
    assert "master.m3u8" in cpu and "v:0,a:0,name:high v:1,a:1,name:mid v:2,a:2,name:low" in cpu
    assert any("force_original_aspect_ratio=decrease:force_divisible_by=2" in arg for arg in cpu)
    gpu = build_command(settings, "rtmp://localhost/stream", directory, gpu=True, gpu_device=2)
    assert "h264_nvenc" in gpu
    assert ("cuda" in gpu) is nvdec
    assert gpu[gpu.index("-gpu:v:0") + 1] == "2"
    assert "-hwaccel" not in cpu and "-hwaccel_device" not in cpu
    if nvdec:
        assert gpu[gpu.index("-hwaccel_device") + 1] == "2"
    else:
        assert "-hwaccel" not in gpu and "-hwaccel_device" not in gpu
    for command in (cpu, gpu):
        assert "aac" in command
        assert all(
            "scale=" in command[index + 1] and "scale_cuda" not in command[index + 1]
            for index, argument in enumerate(command)
            if argument.startswith("-filter:v:")
        )


@pytest.mark.parametrize("encoding_type", ["CPU", "GPU"])
@pytest.mark.parametrize("transcoder", [None, "cpu", "nvenc"], ids=["default", "cpu", "nvenc"])
def test_live_backend_is_independent_of_vod_encoding_type(
    live_env, monkeypatch, encoding_type, transcoder
):
    monkeypatch.setattr(config, "ENCODING_TYPE", encoding_type)
    if transcoder is not None:
        config.LIVE.transcoder = transcoder
    runtime = live.LiveRuntime(live_env)
    assert runtime.gpu is (transcoder == "nvenc")
    assert config.ENCODING_TYPE == encoding_type


def write_hls(directory, settings):
    directory.mkdir(parents=True, exist_ok=True)
    master = "#EXTM3U\n"
    for variant in settings.variants:
        folder = directory / variant.name
        folder.mkdir(exist_ok=True)
        master += f"#EXT-X-STREAM-INF:BANDWIDTH=1000000\n{variant.name}/index.m3u8\n"
        (folder / "index.m3u8").write_text(
            "#EXTM3U\n#EXTINF:2,\nsegment_1.ts\n#EXTINF:2,\nsegment_2.ts\n"
        )
        (folder / "segment_1.ts").write_bytes(b"media")
        (folder / "segment_2.ts").write_bytes(b"media")
    (directory / "master.m3u8").write_text(master)


@pytest.mark.asyncio
async def test_ready_requires_all_variants_and_public_files_are_contained(live_env):
    directory = live.directory_for(live_env.task_id)
    assert not hls_ready(directory, config.LIVE)
    with pytest.raises(HTTPException, match="HLS not ready"):
        await hls_file(live_env.task_id, "master.m3u8")
    config.LIVE.stall_timeout = 20
    write_hls(directory, config.LIVE)
    assert hls_ready(directory, config.LIVE)
    tracked(live_env)
    live.update(live_env.task_id, hls_ready=True)
    response = await hls_file(live_env.task_id, "master.m3u8")
    assert response.headers["cache-control"] == "no-store"
    request = type("Request", (), {"client": type("Client", (), {"host": "client-a"})()})()
    await hls_file_endpoint(live_env.task_id, "master.m3u8", request)
    assert hls_viewers.count(live_env.task_id) == 1
    with pytest.raises(HTTPException, match="HLS not found"):
        await hls_file(live_env.task_id, "high/segment_999.ts")
    with pytest.raises(HTTPException):
        await hls_file(live_env.task_id, "../../status.json")
    (directory / "low" / "segment_2.ts").unlink()
    assert not hls_ready(directory, config.LIVE)


@pytest.mark.asyncio
async def test_source_arrives_then_ffmpeg_and_complete_cleanup(live_env, monkeypatch):
    tracked(live_env)
    config.LIVE.source_timeout = 1
    runtime = live.LiveRuntime(live_env)
    assert runtime.path == f"live/{live_env.task_id}"
    runtime.client.configure_forward = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    runtime.client.get_path = AsyncMock(
        side_effect=[source_payload(False), source_payload(), source_payload()]
    )
    runtime.terminate_orphans = AsyncMock()
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    def ready(*args):
        live.request_stop(live_env.task_id)
        return True

    monkeypatch.setattr(live, "hls_ready", ready)
    await runtime.run()
    assert spawn.await_count == 1
    runtime.client.configure_forward.assert_awaited_once_with(
        runtime.path, "rtmp://manager:1935/live/random-key?user=reader&pass=secret"
    )
    runtime.client.remove_forward.assert_awaited_once_with(runtime.path)
    assert process.signals == [signal.SIGINT]
    assert not runtime.directory.exists()
    assert state.is_available()
    payload = state.get_task_status(live_env.task_id)
    assert payload["status"] == "completed" and payload["live"]["cleanup_complete"]
    assert payload["live"]["hls_url"] is None
    assert payload["live"]["hls_sessions"] == 2
    task.notify_completion.assert_awaited_once()


@pytest.mark.asyncio
async def test_pod_broadcaster_is_enabled_on_source_and_disabled_on_cleanup(live_env, monkeypatch):
    live_env.parameters["broadcaster_id"] = "pod-broadcaster"
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    runtime.client.get_path = AsyncMock(return_value=source_payload())
    runtime.terminate_orphans = AsyncMock()
    runtime.pod_broadcaster.set_status = AsyncMock()
    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    def stop_after_ready(*args):
        live.request_stop(live_env.task_id)
        return True

    monkeypatch.setattr(live, "hls_ready", stop_after_ready)
    await runtime.run()

    assert runtime.pod_broadcaster.set_status.await_args_list[0].args == (
        "pod-broadcaster",
        True,
    )
    assert runtime.pod_broadcaster.set_status.await_args_list[1].args == (
        "pod-broadcaster",
        False,
    )


@pytest.mark.asyncio
async def test_pod_broadcaster_is_skipped_for_another_establishment(live_env):
    live_env.parameters["broadcaster_id"] = "pod-broadcaster"
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"
    config.LIVE.pod_establishment_name = "another-university"

    runtime = live.LiveRuntime(live_env)

    assert runtime.pod_broadcaster is None
    await runtime.activate_pod_broadcaster()
    await runtime.deactivate_pod_broadcaster()


@pytest.mark.asyncio
async def test_pod_broadcaster_activation_failure_is_non_blocking(live_env):
    live_env.parameters["broadcaster_id"] = "pod-broadcaster"
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"
    runtime = live.LiveRuntime(live_env)
    runtime.pod_broadcaster.set_status = AsyncMock(side_effect=PodBroadcasterError("unavailable"))

    await runtime.activate_pod_broadcaster()

    runtime.pod_broadcaster.set_status.assert_awaited_once_with("pod-broadcaster", True)
    assert runtime.pod_broadcaster_started


@pytest.mark.asyncio
async def test_pod_broadcaster_failure_does_not_block_cleanup(live_env):
    live_env.parameters["broadcaster_id"] = "pod-broadcaster"
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock(side_effect=MediaMTXError("unavailable"))
    runtime.client.remove_forward = AsyncMock()
    runtime.terminate_orphans = AsyncMock()
    runtime.pod_broadcaster.set_status = AsyncMock(side_effect=PodBroadcasterError("unavailable"))
    runtime.pod_broadcaster_started = True

    await runtime.run(recovering=True)

    runtime.pod_broadcaster.set_status.assert_awaited_once_with("pod-broadcaster", False)
    assert state.is_available()


@pytest.mark.asyncio
async def test_source_timeout_fails_and_releases_only_after_cleanup(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock()
    runtime.client.get_path = AsyncMock(return_value=None)
    runtime.client.remove_forward = AsyncMock()
    runtime.terminate_orphans = AsyncMock()
    await runtime.run()
    payload = state.get_task_status(live_env.task_id)
    assert payload["status"] == "failed" and "source timeout" in payload["live"]["error"]
    assert state.is_available()


@pytest.mark.asyncio
async def test_mediamtx_unavailable_keeps_busy_until_cleanup_succeeds(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock(side_effect=MediaMTXError("do not leak input URL"))
    runtime.terminate_orphans = AsyncMock()
    attempts = []

    async def remove(path):
        attempts.append(path)
        assert not state.is_available()
        assert state.get_task_status(live_env.task_id)["live"]["state"] == "stopping"
        if len(attempts) == 1:
            raise MediaMTXError("unreachable")

    runtime.client.remove_forward = remove
    await runtime.run()
    assert len(attempts) == 2 and state.is_available()
    assert "input URL" not in str(state.get_task_status(live_env.task_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("cpu_fallback", [True, False])
async def test_ffmpeg_crashes_cpu_fallback_and_bounded_restarts(live_env, cpu_fallback):
    config.LIVE.transcoder = "nvenc"
    config.LIVE.cpu_fallback = cpu_fallback
    config.LIVE.restart_attempts = 1
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    runtime.wait_for_source = AsyncMock(return_value=True)
    attempted_backends = []

    async def start_process():
        attempted_backends.append("nvenc" if runtime.gpu else "cpu")

    runtime.start_process = AsyncMock(side_effect=start_process)
    runtime.monitor = AsyncMock(return_value=False)
    runtime.stop_process = AsyncMock()
    runtime.source_ready = True
    runtime.terminate_orphans = AsyncMock()
    await runtime.run()
    assert runtime.gpu is not cpu_fallback and runtime.errors == 2
    assert attempted_backends == (["nvenc", "cpu"] if cpu_fallback else ["nvenc", "nvenc"])
    assert state.get_task_status(live_env.task_id)["status"] == "failed"
    assert state.get_task_status(live_env.task_id)["live"]["transcode_errors"] == 2
    assert state.get_task_status(live_env.task_id)["live"]["cleanup_complete"]
    assert state.is_available()


@pytest.mark.asyncio
async def test_source_disconnect_does_not_count_as_transcode_error(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock()
    runtime.wait_for_source = AsyncMock(side_effect=[True, False])
    runtime.start_process = AsyncMock()

    async def source_disappeared():
        runtime.source_ready = False
        return False

    runtime.monitor = AsyncMock(side_effect=source_disappeared)
    runtime.stop_process = AsyncMock()

    await runtime.transcode()

    assert runtime.errors == 0
    assert state.get_task_status(live_env.task_id)["live"].get("transcode_errors", 0) == 0


@pytest.mark.asyncio
async def test_restart_cleans_owned_session_without_restarting_terminal_live(live_env, monkeypatch):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.terminate_orphans = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    await runtime.run(recovering=True)
    assert state.get_task_status(live_env.task_id)["status"] == "failed"
    schedule = []
    monkeypatch.setattr(live, "schedule", lambda *args, **kwargs: schedule.append(args))
    state.reload_task_statuses_from_disk()
    await live.recover()
    assert not schedule


@pytest.mark.asyncio
async def test_hls_disappearance_withholds_running_and_restarts(live_env, monkeypatch):
    from itertools import count
    from types import SimpleNamespace

    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    ticks = count(step=0.005)
    monkeypatch.setattr(live, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    runtime.process = Process()
    runtime.sample_source = AsyncMock(return_value={"source_ready": True})
    readiness = iter([True] + [False] * 100)
    monkeypatch.setattr(live, "hls_ready", lambda *args: next(readiness))
    assert not await runtime.monitor()
    assert state.get_task_status(live_env.task_id)["live"]["state"] == "starting"
    assert state.get_task_status(live_env.task_id)["live"]["hls_url"] is None


def test_hls_readiness_rejects_invalid_stale_and_incomplete_playlists(live_env, tmp_path):
    import os
    import time

    settings = config.LIVE.model_copy(update={"stall_timeout": 20})
    directory = tmp_path / "hls"
    directory.mkdir()
    master = directory / "master.m3u8"

    master.write_text("not-an-m3u8")
    assert not live_media_module.hls_ready(directory, settings)
    master.write_text("#EXTM3U\n")
    assert not live_media_module.hls_ready(directory, settings)

    write_hls(directory, settings)
    playlist = directory / "high" / "index.m3u8"
    os.utime(playlist, (0, 0))
    assert not live_media_module.hls_ready(directory, settings)
    now = time.time()
    os.utime(playlist, (now, now))
    playlist.write_text("#EXTM3U\n#EXTINF:2,\nsegment_1.ts\n")
    assert not live_media_module.hls_ready(directory, settings)


@pytest.mark.parametrize(
    "parameters",
    [
        {"source_id": "room", "source_path": "../../etc/passwd"},
        {"source_id": "room", "source_path": "rtmp://external"},
        {"source_id": "room", "source_path": "live/key", "ffmpeg_args": "-c copy"},
    ],
)
def test_reject_arbitrary_sources_and_ffmpeg_options(live_env, parameters):
    live_env.parameters = parameters
    with pytest.raises(HTTPException):
        live.validate_request(live_env)


def test_pod_broadcaster_identifier_is_required_and_validated_when_configured(live_env):
    config.LIVE.pod_url = "https://pod.example"
    config.LIVE.pod_username = "api-user"
    config.LIVE.pod_password = "api-password"

    with pytest.raises(HTTPException, match="required"):
        live.validate_request(live_env)

    live_env.parameters["broadcaster_id"] = "invalid identifier"
    with pytest.raises(HTTPException, match="identifier"):
        live.validate_request(live_env)


def test_even_dimensions_unique_variants_and_public_url(live_env):
    with pytest.raises(ValueError):
        LiveVariant(name="mid", width=721, height=406, video_bitrate=560000, audio_bitrate=96000)
    with pytest.raises(ValueError):
        LiveSettings(variants=config.LIVE.variants[:1])
    config.LIVE.public_hls_base_url = "https://user:password@public/hls"
    with pytest.raises(ValueError):
        config.LIVE.validate_enabled()


def test_live_configuration_rejects_invalid_output_profile(live_env):
    relative_output = config.LIVE.model_copy(update={"hls_directory": "relative"})
    with pytest.raises(ValueError, match="absolute"):
        relative_output.validate_enabled()

    duplicate_variants = config.LIVE.model_copy(
        update={"variants": [config.LIVE.variants[0], config.LIVE.variants[0]]}
    )
    with pytest.raises(ValueError, match="unique"):
        duplicate_variants.validate_enabled()

    invalid_gop = config.LIVE.model_copy(update={"fps": 1, "gop": 2, "segment_seconds": 1})
    with pytest.raises(ValueError, match="GOP"):
        invalid_gop.validate_enabled()


def test_live_request_validation_rejects_invalid_uuid_and_callback(live_env):
    with pytest.raises(HTTPException, match="UUID"):
        live.validate_request(live_env.model_copy(update={"task_id": "not-a-uuid"}))
    with pytest.raises(HTTPException, match="callback"):
        live.validate_request(
            live_env.model_copy(update={"completion_callback": "https://invalid.example"})
        )


def test_persistence_failure_prevents_live_checkpoint(live_env, monkeypatch):
    monkeypatch.setattr(
        state.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full"))
    )
    tracked(live_env)
    with pytest.raises(OSError, match="disk full"):
        live.update(live_env.task_id, state="starting")
    assert not state.is_available()


@pytest.mark.asyncio
async def test_failed_initial_checkpoint_schedules_cleanup(live_env, monkeypatch):
    from unittest.mock import Mock

    scheduled = Mock()
    monkeypatch.setattr(live, "schedule", scheduled)
    monkeypatch.setattr(state, "persist_live_checkpoint", Mock(side_effect=OSError("disk full")))
    with pytest.raises(HTTPException, match="persisted"):
        await task.run_task(live_env, BackgroundTasks(), "token")
    scheduled.assert_called_once_with(live_env, recovering=True)
    assert not state.is_available()


def test_live_status_stays_private_after_best_effort_writes(live_env):
    tracked(live_env)
    live.update(live_env.task_id, state="starting")
    state.set_task_metadata(live_env.task_id, process_pid=12345)
    assert state._resolve_task_status_file().stat().st_mode & 0o777 == 0o600


def test_output_symlinks_rejected(live_env, tmp_path):
    base = Path(config.LIVE.hls_directory)
    base.mkdir()
    (base / live_env.task_id).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError):
        live.directory_for(live_env.task_id)


@pytest.mark.parametrize("identifier", ["../outside", "/etc", "not-a-uuid", "", "a/b"])
def test_output_directory_rejects_invalid_ids_before_filesystem_access(live_env, identifier):
    with pytest.raises(ValueError):
        live.directory_for(identifier)
    assert not Path(config.LIVE.hls_directory).exists()


@pytest.mark.parametrize("form", ["uppercase", "compact", "braces"])
def test_output_directory_preserves_valid_uuid_spellings(live_env, form):
    identifier = {
        "uppercase": live_env.task_id.upper(),
        "compact": live_env.task_id.replace("-", ""),
        "braces": "{" + live_env.task_id + "}",
    }[form]
    assert live.directory_for(identifier) == Path(config.LIVE.hls_directory) / identifier


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative", ["session", "master.m3u8", "high", "high/index.m3u8", "high/segment_2.ts"]
)
async def test_hls_readiness_and_public_access_reject_symlinks(live_env, tmp_path, relative):
    directory = live.directory_for(live_env.task_id)
    write_hls(directory, config.LIVE)
    link = directory if relative == "session" else directory / relative
    target = tmp_path / "outside"
    is_directory = link.is_dir()
    link.rename(target)
    link.symlink_to(target, target_is_directory=is_directory)
    assert not hls_ready(directory, config.LIVE)
    tracked(live_env)
    live.update(live_env.task_id, hls_ready=True)
    if relative in {"high/index.m3u8", "high/segment_2.ts"}:
        filename = relative
    else:
        filename = "high/segment_2.ts" if relative == "high" else "master.m3u8"
    with pytest.raises(HTTPException) as error:
        await hls_file(live_env.task_id, filename)
    assert error.value.status_code == 404
    assert target.exists()


@pytest.mark.parametrize("segment", ["..", ".", "../outside.ts", "/etc/passwd", "index.m3u8"])
def test_hls_readiness_rejects_non_segment_playlist_entries(live_env, segment):
    directory = live.directory_for(live_env.task_id)
    write_hls(directory, config.LIVE)
    (directory / "high/index.m3u8").write_text(
        f"#EXTM3U\n#EXTINF:2,\n{segment}\n#EXTINF:2,\n{segment}\n"
    )
    assert not hls_ready(directory, config.LIVE)


@pytest.mark.parametrize("empty", [False, True], ids=["directory", "empty-file"])
def test_hls_readiness_requires_nonempty_segment_files(live_env, empty):
    directory = live.directory_for(live_env.task_id)
    write_hls(directory, config.LIVE)
    segment = directory / "high/segment_2.ts"
    segment.unlink()
    if empty:
        segment.write_bytes(b"")
    else:
        segment.mkdir()
    assert not hls_ready(directory, config.LIVE)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["start", "wait", "cleanup"])
async def test_runtime_rechecks_output_symlinks_before_writes_and_cleanup(
    live_env, tmp_path, monkeypatch, operation
):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.directory.parent.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("private data")
    runtime.directory.symlink_to(target, target_is_directory=True)
    runtime.stop_process = AsyncMock()
    runtime.terminate_orphans = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    if operation == "cleanup":

        async def cancel_retry(_):
            raise asyncio.CancelledError

        monkeypatch.setattr(live.asyncio, "sleep", cancel_retry)
        with pytest.raises(asyncio.CancelledError):
            await runtime.cleanup(None)
        payload = state.get_task_status(live_env.task_id)
        assert payload["status"] == "running"
        assert not payload["live"].get("cleanup_complete", False)
        assert not state.is_available()
        task.notify_completion.assert_not_awaited()
    else:
        with pytest.raises(live.LiveFailure, match="Invalid Live output directory"):
            if operation == "start":
                await runtime.start_process()
            else:
                await runtime.wait_for_mediamtx()
    assert marker.read_text() == "private data"


def test_runtime_rejects_output_root_changes(live_env, tmp_path):
    runtime = live.LiveRuntime(live_env)
    config.LIVE.hls_directory = str(tmp_path / "replacement")
    with pytest.raises(live.LiveFailure, match="output directory changed"):
        runtime.checked_directory()
    assert not Path(config.LIVE.hls_directory).exists()


@pytest.mark.asyncio
async def test_source_codec_rejected_before_ffmpeg(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.get_path = AsyncMock(
        return_value={"available": True, "tracks2": [{"codec": "H265"}]}
    )
    with pytest.raises(live.LiveFailure, match="H.264"):
        await runtime.wait_for_source()
    live.request_stop(live_env.task_id)
    assert not await runtime.wait_for_source()


@pytest.mark.asyncio
async def test_existing_relay_without_signal_times_out_and_cleans_up(live_env):
    """An existing but silent relay must fail safely without launching FFmpeg."""
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.directory.mkdir(parents=True)
    (runtime.directory / "stale.m3u8").write_text("stale")
    runtime.client.configure_forward = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    runtime.client.get_path = AsyncMock(return_value=source_payload(False))
    runtime.terminate_orphans = AsyncMock()
    runtime.start_process = AsyncMock()

    await runtime.run()

    runtime.start_process.assert_not_awaited()
    runtime.client.configure_forward.assert_awaited_once_with(runtime.path, runtime.source_url)
    runtime.client.remove_forward.assert_awaited_once_with(runtime.path)
    assert not runtime.directory.exists()
    payload = state.get_task_status(live_env.task_id)
    assert payload["status"] == "failed"
    assert payload["live"]["error"] == "Live source timeout"
    assert payload["live"]["cleanup_complete"] and not payload["live"]["hls_ready"]
    assert payload["live"]["hls_url"] is None
    assert state.is_available()
    task.notify_completion.assert_awaited_once_with(
        live_env.completion_callback, live_env.task_id, "failed", "Live source timeout"
    )


@pytest.mark.asyncio
async def test_process_timeout_escalates_to_kill(live_env):
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.process.wait = AsyncMock(side_effect=[asyncio.TimeoutError, 0])
    await runtime.stop_process()
    assert runtime.process.returncode == -9
    assert runtime.process.wait.await_count == 2
    runtime.process = Process()
    runtime.process.send_signal = lambda value: (_ for _ in ()).throw(ProcessLookupError())
    await runtime.stop_process()


@pytest.mark.asyncio
async def test_runtime_cancellation_is_reported_and_cleaned(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.transcode = AsyncMock(side_effect=asyncio.CancelledError)
    runtime.cleanup = AsyncMock()

    await runtime.run()

    runtime.cleanup.assert_awaited_once_with("Runner stopped")


@pytest.mark.asyncio
async def test_transcode_returns_when_source_stops_before_ffmpeg(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.client.configure_forward = AsyncMock()
    runtime.wait_for_source = AsyncMock(return_value=False)
    runtime.start_process = AsyncMock()

    await runtime.transcode()

    runtime.wait_for_source.assert_awaited_once()
    runtime.start_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_process_creates_variant_directories(live_env, monkeypatch):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.directory.mkdir(parents=True)
    (runtime.directory / "stale.ts").write_bytes(b"stale")
    process = Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))

    await runtime.start_process()

    assert runtime.directory.is_dir()
    assert all((runtime.directory / variant.name).is_dir() for variant in config.LIVE.variants)


@pytest.mark.asyncio
async def test_monitor_returns_when_process_has_exited(live_env):
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.process.returncode = 0
    runtime.client.get_path = AsyncMock(return_value=source_payload(False))

    assert await runtime.monitor() is False
    assert runtime.source_ready is False


@pytest.mark.asyncio
async def test_monitor_ignores_mediamtx_error_after_process_exit(live_env):
    """A vanished MediaMTX path must not hide the already observed process exit."""
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.process.returncode = 0
    runtime.client.get_path = AsyncMock(side_effect=MediaMTXError("path unavailable"))

    assert await runtime.monitor() is False


@pytest.mark.asyncio
async def test_monitor_propagates_media_outage_after_process_exit(live_env):
    """Keep the session reserved so a transient control outage can resume the same Live."""
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.process.returncode = 1
    outage = MediaMTXUnavailable("private-control-detail")
    runtime.client.get_path = AsyncMock(side_effect=outage)

    with pytest.raises(MediaMTXUnavailable) as result:
        await runtime.monitor()

    assert result.value is outage
    assert runtime.errors == 0
    assert state.get_task_status(live_env.task_id)["status"] == "running"
    assert not state.is_available()
    runtime.client.get_path.assert_awaited_once_with(runtime.path)


@pytest.mark.asyncio
async def test_monitor_detects_missing_relay_while_ffmpeg_is_running(live_env):
    """A missing relay requests recovery and preserves the Runner's Live reservation."""
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.client.get_path = AsyncMock(return_value=None)

    assert await runtime.monitor() is False

    assert runtime.path_missing and not runtime.source_ready
    assert runtime.process.returncode is None
    assert runtime.errors == 0
    payload = state.get_task_status(live_env.task_id)
    assert payload["status"] == "running" and not payload["live"]["source_ready"]
    assert not state.is_available()
    runtime.client.get_path.assert_awaited_once_with(runtime.path)


@pytest.mark.asyncio
async def test_missing_relay_path_is_recreated_before_waiting_for_source(live_env):
    config.LIVE.source_timeout = 1
    runtime = live.LiveRuntime(live_env)
    runtime.source_url = "rtmp://manager/live/source"
    runtime.client.get_path = AsyncMock(side_effect=[None, source_payload()])
    runtime.client.configure_forward = AsyncMock()

    assert await runtime.wait_for_source()

    runtime.client.configure_forward.assert_awaited_once_with(runtime.path, runtime.source_url)
    assert not runtime.path_missing


@pytest.mark.asyncio
async def test_temporary_mediamtx_outage_waits_and_retries_the_same_session(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.directory.mkdir(parents=True)
    (runtime.directory / "stale.m3u8").write_text("stale")
    runtime.stop_process = AsyncMock()
    runtime.client.check_health = AsyncMock(side_effect=[MediaMTXUnavailable("offline"), None])
    runtime.cleanup = AsyncMock()
    calls = 0

    async def transcode():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaMTXUnavailable("offline")
        state.set_task_metadata(live_env.task_id, stop_requested="live_stop")

    runtime.transcode = AsyncMock(side_effect=transcode)
    await runtime.run()

    assert runtime.transcode.await_count == 2
    assert runtime.client.check_health.await_count == 2
    runtime.stop_process.assert_awaited_once()
    assert not runtime.directory.exists()
    assert runtime.path_missing
    runtime.cleanup.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_nontransient_mediamtx_error_does_not_retry(live_env):
    runtime = live.LiveRuntime(live_env)
    runtime.transcode = AsyncMock(side_effect=MediaMTXError("path ownership conflict"))
    runtime.client.check_health = AsyncMock()
    runtime.cleanup = AsyncMock()

    await runtime.run()

    runtime.transcode.assert_awaited_once()
    runtime.client.check_health.assert_not_awaited()
    runtime.cleanup.assert_awaited_once_with("Live media operation failed")


@pytest.mark.asyncio
async def test_stop_during_mediamtx_outage_does_not_restart_session(live_env):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.transcode = AsyncMock()
    runtime.stop_process = AsyncMock()
    runtime.client.check_health = AsyncMock()
    runtime.cleanup = AsyncMock()

    async def stop_then_fail():
        state.set_task_metadata(live_env.task_id, stop_requested="live_stop")
        raise MediaMTXUnavailable("offline")

    runtime.transcode.side_effect = stop_then_fail
    await runtime.run()

    runtime.transcode.assert_awaited_once()
    runtime.client.check_health.assert_not_awaited()
    runtime.cleanup.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_orphan_kill_requires_exact_ffmpeg_output_path(live_env, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    runtime = live.LiveRuntime(live_env)
    output = str(runtime.directory / "%v" / "index.m3u8")
    commands = {
        "100": ["ffmpeg", "-i", "stream", output],
        "101": ["other-program", output],
        "102": ["ffmpeg", "/another/live/index.m3u8"],
    }
    for pid, args in commands.items():
        (proc / pid).mkdir()
        (proc / pid / "cmdline").write_bytes("\0".join(args).encode())
    (proc / "not-a-pid").mkdir()
    monkeypatch.setattr(live, "Path", lambda value: proc if value == "/proc" else Path(value))
    signalled = []

    def kill(pid, sig):
        signalled.append((pid, sig))
        (proc / str(pid) / "cmdline").write_bytes(b"")

    monkeypatch.setattr(live.os, "kill", kill)
    await runtime.terminate_orphans()
    assert signalled == [(100, signal.SIGKILL)]


@pytest.mark.asyncio
async def test_orphan_kill_timeout_and_missing_proc_entries(live_env, tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    proc.mkdir()
    runtime = live.LiveRuntime(live_env)
    marker = str(runtime.directory / "%v" / "index.m3u8")
    timeout_entry = proc / "100"
    timeout_entry.mkdir()
    (timeout_entry / "cmdline").write_bytes(f"ffmpeg\0{marker}".encode())
    monkeypatch.setattr(live, "Path", lambda value: proc if value == "/proc" else Path(value))
    monkeypatch.setattr(live.os, "kill", lambda *_args: None)
    clock = iter([0, 0, 1])
    monkeypatch.setattr(live.time, "monotonic", lambda: next(clock, 1))
    monkeypatch.setattr(live.asyncio, "sleep", AsyncMock())

    with pytest.raises(live.LiveFailure, match="not confirmed"):
        await runtime.terminate_orphans()

    (timeout_entry / "cmdline").write_bytes(b"")
    missing_entry = proc / "101"
    missing_entry.mkdir()
    await runtime.terminate_orphans()


@pytest.mark.asyncio
async def test_recovery_schedules_owned_running_live_only(live_env, monkeypatch):
    tracked(live_env)
    calls = []
    monkeypatch.setattr(live, "schedule", lambda request, **kwargs: calls.append((request, kwargs)))
    state.set_task_metadata(str(uuid4()), runner_id="another-runner", live={"state": "running"})
    await live.recover()
    assert len(calls) == 1 and calls[0][1] == {"recovering": True}
    assert calls[0][0].task_id == live_env.task_id


@pytest.mark.asyncio
async def test_scheduled_runtime_is_cancelled_and_cleanup_awaited(live_env, monkeypatch):
    entered = asyncio.Event()
    cleaned = []

    async def run(self, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(self.task_id)

    monkeypatch.setattr(live.LiveRuntime, "run", run)
    live.schedule(live_env)
    live.schedule(live_env)
    assert len(live.JOBS) == 1
    await entered.wait()
    await live.shutdown()
    assert cleaned == [live_env.task_id] and not live.JOBS


@pytest.mark.asyncio
async def test_shutdown_defers_when_cleanup_wait_times_out(live_env, monkeypatch):
    job = asyncio.create_task(asyncio.sleep(10))
    live.JOBS["pending"] = job

    async def timeout(awaitable, *, timeout):
        awaitable.cancel()
        try:
            await awaitable
        except asyncio.CancelledError:
            pass
        raise asyncio.TimeoutError

    monkeypatch.setattr(live.asyncio, "wait_for", timeout)
    await live.shutdown()

    assert job.cancelled()
    live.JOBS.clear()


@pytest.mark.asyncio
async def test_live_storage_never_uses_age_based_vod_cleanup(live_env, monkeypatch):
    from app.managers import service_manager

    monkeypatch.setattr(
        service_manager.storage_manager,
        "cleanup_old_files",
        lambda days: pytest.fail("VOD cleanup must not delete Live state"),
    )
    await service_manager.storage_cleanup_loop()


def test_successful_hls_access_logging_is_filtered(live_env):
    import logging

    from app.api.routes.live import LiveAccessFilter, configure_access_logging

    access = logging.getLogger("uvicorn.access")
    previous = list(access.filters)
    try:
        configure_access_logging()
        configure_access_logging()
        assert sum(isinstance(item, LiveAccessFilter) for item in access.filters) == 1
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "file",
            1,
            "",
            ("client", "GET", "/hls/id/high/segment_1.ts", "1.1", 200),
            None,
        )
        assert not LiveAccessFilter().filter(record)
        record.args = ("client", "GET", "/hls/id/high/segment_1.ts", "1.1", 404)
        assert LiveAccessFilter().filter(record)
        record.args = None
        assert LiveAccessFilter().filter(record)
    finally:
        access.filters = previous


@pytest.mark.asyncio
async def test_disk_full_still_stops_media_before_retrying_final_persistence(live_env, monkeypatch):
    tracked(live_env)
    runtime = live.LiveRuntime(live_env)
    runtime.process = Process()
    runtime.terminate_orphans = AsyncMock()
    runtime.client.remove_forward = AsyncMock()
    runtime.directory.mkdir(parents=True)
    (runtime.directory / "temporary.ts").write_bytes(b"media")
    original = state.persist_live_checkpoint
    failures = []

    def checkpoint():
        assert runtime.process.returncode == 0
        assert not runtime.directory.exists()
        if not failures:
            failures.append(True)
            raise OSError("disk full")
        original()

    monkeypatch.setattr(state, "persist_live_checkpoint", checkpoint)
    await runtime.cleanup("FFmpeg failed")
    assert failures and state.is_available()
    assert state.get_task_status(live_env.task_id)["live"]["cleanup_complete"]
