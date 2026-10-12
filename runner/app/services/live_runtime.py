"""One long-running Live task per runner, with durable stop and cleanup."""

import asyncio
import os
import re
import shutil
import signal
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.core import state
from app.core.config import config
from app.core.setup_logging import setup_default_logging
from app.models.models import TaskRequest
from app.services import hls_viewers, pod_sync
from app.services.live_media import build_command, hls_ready, rtmp_url
from app.services.live_paths import contained_path
from app.services.mediamtx import (
    MediaMTXClient,
    MediaMTXError,
    MediaMTXUnavailable,
    path_metrics,
)
from app.services.pod_broadcaster import PodBroadcasterClient, PodBroadcasterError

logger = setup_default_logging()
JOBS: dict[str, asyncio.Task] = {}


def validate_request(request: TaskRequest) -> None:
    """Validate a Manager dispatch before accepting it on a Live-capable Runner.

    Require a UUID, configured media settings, safe source identifiers and the
    configured Manager completion callback. Reject client URLs and extra
    parameters with HTTP 400; configuration validation may raise ValueError.
    """
    try:
        uuid.UUID(request.task_id)
    except ValueError:
        raise HTTPException(400, "Live task_id must be a UUID") from None
    if "live" not in config.RUNNER_TASK_TYPES:
        raise HTTPException(400, "Live is not enabled on this runner")
    config.LIVE.validate_enabled()
    for field in ("source_id", "source_path"):
        value = request.parameters.get(field)
        if (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", value)
            or len(value) > 500
        ):
            raise HTTPException(400, "Invalid Live source")
    broadcaster_id = request.parameters.get("broadcaster_id")
    if (
        request.source_url
        or request.notify_url
        or not {"source_id", "source_path"}.issubset(request.parameters)
        or set(request.parameters) - {"source_id", "source_path", "broadcaster_id"}
    ):
        raise HTTPException(400, "Unsupported Live parameters")
    if broadcaster_id is not None and (
        not isinstance(broadcaster_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", broadcaster_id) is None
    ):
        raise HTTPException(400, "Invalid Live broadcaster identifier")
    if config.LIVE.pod_url and not isinstance(broadcaster_id, str):
        raise HTTPException(400, "Live broadcaster identifier is required for Pod integration")
    expected_callback = f"{config.MANAGER_URL.rstrip('/')}/task/completion"
    if request.completion_callback != expected_callback:
        raise HTTPException(400, "Invalid Live callback")


def directory_for(task_id: str) -> Path:
    """Resolve a UUID session beneath the configured HLS root without following a session symlink."""
    uuid.UUID(task_id)
    try:
        return contained_path(Path(config.LIVE.hls_directory), task_id)
    except ValueError:
        raise RuntimeError("Invalid Live output directory") from None


def update(task_id: str, *, durable: bool = True, **fields: Any) -> dict:
    """Merge Live metadata, optionally require a durable checkpoint, and log transitions.

    Return the merged metadata. A durable checkpoint failure restores the
    previous Live metadata and propagates OSError. With durable=False, writes
    remain best effort so that media cleanup can proceed despite storage errors.
    """
    payload = state.get_task_status(task_id) or {}
    live = dict(payload.get("live") or {})
    previous = live.get("state")
    live.update(fields)
    state.set_task_metadata(task_id, live=live)
    if durable:
        try:
            state.persist_live_checkpoint()
        except OSError:
            state.set_task_metadata(task_id, live=payload.get("live") or {})
            raise
    if previous != live.get("state"):
        request = payload.get("task_request") or {}
        logger.info(
            "Live transition live_id=%s stream_id=%s runner_id=%s state=%s",
            task_id,
            request.get("parameters", {}).get("source_id", ""),
            state.get_runner_id(),
            live.get("state"),
        )
    return live


def accept(request: TaskRequest) -> None:
    """Checkpoint the initial Live state and schedule its media lifecycle.

    If persistence fails, schedule recovery cleanup and return HTTP 503 rather
    than acknowledging a successful start.
    """
    try:
        update(
            request.task_id,
            state="waiting_source",
            hls_ready=False,
            hls_url=None,
            cleanup_complete=False,
        )
    except OSError:
        schedule(request, recovering=True)
        raise HTTPException(503, "Live state could not be persisted") from None
    schedule(request)


def schedule(request: TaskRequest, *, recovering: bool = False) -> None:
    """Schedule one lifecycle job per task identifier and remove it when finished.

    With recovering=True, the job cleans up the interrupted session instead
    of starting another transcode.
    """
    if request.task_id in JOBS:
        return
    job = asyncio.create_task(
        LiveRuntime(request).run(recovering=recovering), name=f"live-{request.task_id}"
    )
    JOBS[request.task_id] = job
    job.add_done_callback(lambda done: JOBS.pop(request.task_id, None))


def stopped(task_id: str) -> bool:
    """Return whether the task has a recorded stop request."""
    return bool((state.get_task_status(task_id) or {}).get("stop_requested"))


def request_stop(task_id: str) -> dict:
    """Record a stop request or a durable tombstone that fences a delayed dispatch.

    Return the task identifier and general status. Unknown tasks become
    completed tombstones; active sessions retain their reservation for cleanup.
    Invalid identifiers return HTTP 400 and existing non-Live tasks return
    HTTP 409. A failed tombstone checkpoint returns HTTP 503 without leaving a
    completed status in memory.
    """
    try:
        uuid.UUID(task_id)
    except ValueError:
        raise HTTPException(400, "Invalid Live identifier") from None
    payload = state.get_task_status(task_id)
    if payload is not None and not payload.get("live"):
        raise HTTPException(409, "Task is not a Live session")
    if payload is None or (payload.get("stop_requested") and not payload.get("task_request")):
        # Fence a dispatch that was delayed or not yet accepted.
        state.set_task_metadata(
            task_id,
            runner_id=state.get_runner_id(),
            stop_requested="live_stop",
            live={
                "state": "completed",
                "cleanup_complete": True,
                "hls_ready": False,
                "hls_url": None,
            },
        )
        state.set_task_status(task_id, "completed")
        try:
            state.persist_live_checkpoint()
        except OSError:
            state.clear_task_status(task_id)
            raise HTTPException(503, "Live stop could not be persisted") from None
        return {"task_id": task_id, "status": "completed"}
    if payload.get("status") == "running":
        state.set_task_metadata(task_id, stop_requested="live_stop")
        update(task_id, state="stopping", hls_ready=False, hls_url=None)
    return {"task_id": task_id, "status": (state.get_task_status(task_id) or {}).get("status")}


class LiveRuntime:
    """Supervise one session's relay, FFmpeg process, HLS readiness and cleanup."""

    def __init__(self, request: TaskRequest):
        """Bind a request to its output directory, relay client and explicit Live encoder backend."""
        self.request = request
        self.task_id = request.task_id
        self.settings = config.LIVE
        self.directory = directory_for(self.task_id)
        self.path = f"live/{self.task_id}"
        self.source_url = ""
        self.path_missing = False
        self.client = MediaMTXClient(
            self.settings.mediamtx_api_url,
            self.settings.mediamtx_api_user,
            self.settings.mediamtx_api_password,
            self.settings.mediamtx_timeout,
        )
        self.pod_source_id = self.request.parameters.get("broadcaster_id")
        pod_establishment_matches = (
            not self.settings.pod_establishment_name
            or self.request.etab_name == self.settings.pod_establishment_name
        )
        self.pod_broadcaster = (
            PodBroadcasterClient(
                self.settings.pod_url,
                self.settings.pod_username,
                self.settings.pod_password,
            )
            if self.settings.pod_url
            and isinstance(self.pod_source_id, str)
            and pod_establishment_matches
            else None
        )
        self.pod_broadcaster_started = False
        self.pod_broadcaster_stopped = False
        self.process: asyncio.subprocess.Process | None = None
        self.source_ready = False
        self.gpu = self.settings.transcoder == "nvenc"
        self.errors = 0
        self.previous_bytes = 0
        self.previous_sample = time.monotonic()

    def checked_directory(self) -> Path:
        """Recheck the bound output directory before creation or deletion.

        Raise LiveFailure if a session symlink or a changed root would redirect
        filesystem access after this runtime was constructed.
        """
        try:
            directory = directory_for(self.task_id)
        except (ValueError, RuntimeError) as exc:
            raise LiveFailure("Invalid Live output directory") from exc
        if directory != self.directory:
            raise LiveFailure("Live output directory changed")
        return directory

    async def run(self, *, recovering: bool = False) -> None:
        """Run or recover the session, then clean up after completion, failure or cancellation.

        Recovery terminates owned orphan processes and marks the session as failed
        instead of restarting it. A temporary MediaMTX outage resumes the same
        session after the Control API recovers. Only safe diagnostics are recorded.
        """
        if recovering and self.pod_broadcaster is not None:
            self.pod_broadcaster_started = True
        error: str | None = None
        try:
            if recovering:
                await self.terminate_orphans()
                error = "Runner restarted; session stopped safely"
            elif not stopped(self.task_id):
                await self.run_until_stopped()
        except asyncio.CancelledError:
            error = "Runner stopped"
        except Exception as exc:
            error = str(exc) if isinstance(exc, LiveFailure) else "Live media operation failed"
            logger.warning(
                "Live failed live_id=%s runner_id=%s reason=%s",
                self.task_id,
                state.get_runner_id(),
                error,
            )
        await self.cleanup(error)

    async def run_until_stopped(self) -> None:
        """Run transcoding and resume the same session after transient MediaMTX outages."""
        while not stopped(self.task_id):
            try:
                await self.transcode()
                return
            except MediaMTXUnavailable:
                if not await self.wait_for_mediamtx():
                    return

    async def wait_for_mediamtx(self) -> bool:
        """Stop stale media output and wait for the Control API before retrying this session."""
        await self.stop_process()
        directory = self.checked_directory()
        if directory.exists():
            shutil.rmtree(directory)
        hls_viewers.clear(self.task_id)
        self.path_missing = True
        self.previous_bytes = 0
        self.previous_sample = time.monotonic()
        update(
            self.task_id,
            state="waiting_source",
            hls_ready=False,
            hls_url=None,
            error="MediaMTX control unavailable; waiting for recovery",
        )
        logger.warning(
            "MediaMTX unavailable; waiting for recovery live_id=%s runner_id=%s",
            self.task_id,
            state.get_runner_id(),
        )
        while not stopped(self.task_id):
            try:
                await self.client.check_health()
            except MediaMTXUnavailable:
                await asyncio.sleep(self.settings.poll_interval)
                continue
            update(self.task_id, error=None)
            return True
        return False

    async def transcode(self) -> None:
        """Configure the native relay and supervise bounded FFmpeg restart attempts.

        Wait for a compatible source before each attempt. A permitted GPU failure
        switches subsequent attempts to CPU; exhausting retries raises LiveFailure.
        """
        source = rtmp_url(self.settings.upstream_rtmp_url, self.request.parameters["source_path"])
        self.source_url = source
        update(
            self.task_id,
            state="waiting_source",
            hls_ready=False,
            hls_url=None,
            cleanup_complete=False,
        )
        await self.client.configure_forward(self.path, source)
        self.path_missing = False
        logger.info(
            "MediaMTX forwarding configured live_id=%s runner_id=%s",
            self.task_id,
            state.get_runner_id(),
        )
        while not stopped(self.task_id):
            if not await self.wait_for_source():
                return
            await self.start_process()
            if await self.monitor():
                return
            await self.stop_process()
            if not self.source_ready:
                await asyncio.sleep(self.settings.poll_interval)
                continue
            self.errors += 1
            update(
                self.task_id,
                state="waiting_source",
                hls_ready=False,
                hls_url=None,
                transcode_errors=self.errors,
            )
            if self.gpu and self.settings.cpu_fallback:
                self.gpu = False
                logger.info(
                    "Live CPU fallback live_id=%s runner_id=%s", self.task_id, state.get_runner_id()
                )
            elif self.errors > self.settings.restart_attempts:
                raise LiveFailure("FFmpeg restart limit exceeded")
            await asyncio.sleep(self.settings.poll_interval)

    async def sample_source(self) -> dict:
        """Persist non-secret relay metrics with bitrate estimated since the previous sample."""
        payload = await self.client.get_path(self.path)
        self.path_missing = payload is None
        metrics = path_metrics(payload)
        local_viewers = hls_viewers.count(self.task_id)
        if local_viewers or metrics["hls_sessions"] is None:
            metrics["hls_sessions"] = local_viewers
        now = time.monotonic()
        metrics["input_bitrate"] = (
            max(0, metrics["input_bytes"] - self.previous_bytes)
            * 8
            / max(0.001, now - self.previous_sample)
        )
        self.previous_bytes = metrics["input_bytes"]
        self.previous_sample = now
        self.source_ready = bool(metrics["source_ready"])
        update(self.task_id, **metrics)
        return metrics

    async def wait_for_source(self) -> bool:
        """Wait for an H.264/AAC source, returning False if a stop is requested.

        Return True when the source is ready. Raise LiveFailure for a source
        timeout or incompatible codecs; MediaMTX control errors propagate.
        """
        deadline = time.monotonic() + self.settings.source_timeout
        while not stopped(self.task_id):
            metrics = await self.sample_source()
            if self.path_missing:
                await self.client.configure_forward(self.path, self.source_url)
                self.path_missing = False
                if time.monotonic() >= deadline:
                    raise LiveFailure("Live source timeout")
                await asyncio.sleep(self.settings.poll_interval)
                continue
            if metrics["source_ready"]:
                await self.activate_pod_broadcaster()
                codecs = set(metrics["codecs"])
                if not {"H264", "MPEG-4 Audio"}.issubset(codecs):
                    raise LiveFailure("Live source must contain H.264 video and AAC audio")
                return True
            if time.monotonic() >= deadline:
                raise LiveFailure("Live source timeout")
            await asyncio.sleep(self.settings.poll_interval)
        return False

    async def activate_pod_broadcaster(self) -> None:
        """Mark the configured Pod broadcaster active when the source appears."""
        if (
            self.pod_broadcaster is None
            or not isinstance(self.pod_source_id, str)
            or self.pod_broadcaster_started
        ):
            return
        self.pod_broadcaster_started = True
        try:
            await pod_sync.set_status(self.request, True, self.pod_broadcaster)
        except PodBroadcasterError:
            logger.warning(
                "Live Pod broadcaster activation failed live_id=%s; media processing continues",
                self.task_id,
            )

    async def deactivate_pod_broadcaster(self) -> None:
        """Attempt deactivation, retaining a durable retry without blocking media cleanup."""
        if (
            self.pod_broadcaster is None
            or not isinstance(self.pod_source_id, str)
            or not self.pod_broadcaster_started
            or self.pod_broadcaster_stopped
        ):
            return
        self.pod_broadcaster_stopped = True
        try:
            await pod_sync.set_status(self.request, False, self.pod_broadcaster)
        except PodBroadcasterError:
            logger.warning(
                "Live Pod broadcaster deactivation failed live_id=%s; media cleanup continues",
                self.task_id,
            )

    async def start_process(self) -> None:
        """Recreate session output directories, launch FFmpeg and checkpoint its process IDs.

        Discard raw process output because FFmpeg diagnostics may contain RTMP
        credentials. HLS remains unavailable until all renditions pass readiness.
        """
        directory = self.checked_directory()
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        for variant in self.settings.variants:
            contained_path(directory, variant.name).mkdir()
        command = build_command(
            self.settings,
            rtmp_url(self.settings.local_rtmp_url, self.path),
            self.directory,
            gpu=self.gpu,
            gpu_device=config.GPU_HWACCEL_DEVICE,
        )
        update(self.task_id, state="starting", hls_ready=False, hls_url=None)
        # stderr can echo credential-bearing input URLs. Keep it out of logs.
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        state.set_task_metadata(
            self.task_id, process_pid=self.process.pid, process_pgid=self.process.pid
        )
        state.persist_live_checkpoint()
        logger.info(
            "Transcoding started live_id=%s runner_id=%s backend=%s",
            self.task_id,
            state.get_runner_id(),
            "nvenc" if self.gpu else "cpu",
        )

    async def monitor(self) -> bool:
        """Refresh source metrics and HLS readiness until stop, process exit or a stall.

        Return True for a requested stop and False when the caller should restart
        FFmpeg. Advertise the public URL only while every rendition is ready.
        """
        assert self.process is not None
        deadline = time.monotonic() + self.settings.startup_timeout
        was_ready = False
        while not stopped(self.task_id):
            if self.process.returncode is not None:
                try:
                    payload = await self.client.get_path(self.path)
                    self.path_missing = payload is None
                    self.source_ready = path_metrics(payload)["source_ready"]
                except MediaMTXUnavailable:
                    raise
                except MediaMTXError:
                    pass
                return False
            metrics = await self.sample_source()
            if self.path_missing:
                return False
            ready = metrics["source_ready"] and hls_ready(self.directory, self.settings)
            if ready:
                deadline = time.monotonic() + self.settings.stall_timeout
                update(
                    self.task_id,
                    state="running",
                    hls_ready=True,
                    hls_url=f"{self.settings.public_hls_base_url.rstrip('/')}/{self.task_id}/master.m3u8",
                    error=None,
                )
                was_ready = True
            elif was_ready:
                update(self.task_id, state="starting", hls_ready=False, hls_url=None)
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.settings.poll_interval)
        return True

    async def stop_process(self) -> None:
        """Interrupt the owned FFmpeg process, escalate to SIGKILL on timeout, and clear its IDs."""
        if self.process is None or self.process.returncode is not None:
            return
        try:
            self.process.send_signal(signal.SIGINT)
            await asyncio.wait_for(self.process.wait(), timeout=self.settings.stop_timeout)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            self.process.kill()
            await asyncio.wait_for(self.process.wait(), timeout=self.settings.stop_timeout)
        state.set_task_metadata(self.task_id, process_pid=None, process_pgid=None)

    async def terminate_orphans(self) -> None:
        """Only signal FFmpeg processes whose argv contains this exact output path."""
        marker = str(self.directory / "%v" / "index.m3u8").encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                args = (entry / "cmdline").read_bytes().split(b"\0")
                if marker not in args or not args or Path(os.fsdecode(args[0])).name != "ffmpeg":
                    continue
                os.kill(int(entry.name), signal.SIGKILL)
                deadline = time.monotonic() + self.settings.stop_timeout
                while (entry / "cmdline").exists() and (entry / "cmdline").read_bytes():
                    if time.monotonic() >= deadline:
                        raise LiveFailure("Orphan FFmpeg termination not confirmed")
                    await asyncio.sleep(0.05)
            except (FileNotFoundError, ProcessLookupError):
                continue

    async def cleanup(self, error: str | None) -> None:
        """Stop media, remove the relay and files, then persist the terminal session state.

        Retry media, filesystem and checkpoint failures while keeping the Runner
        busy. Only after cleanup succeeds may availability change and the existing
        Manager completion callback be sent.
        """
        terminal = "completed" if stopped(self.task_id) or error is None else "failed"
        while True:
            try:
                update(
                    self.task_id,
                    durable=False,
                    state="stopping",
                    hls_ready=False,
                    hls_url=None,
                    error=error,
                )
                await self.stop_process()
                await self.terminate_orphans()
                await self.client.remove_forward(self.path)
                await self.deactivate_pod_broadcaster()
                directory = self.checked_directory()
                if directory.exists():
                    shutil.rmtree(directory)
                hls_viewers.clear(self.task_id)
                update(
                    self.task_id,
                    state=terminal,
                    cleanup_complete=True,
                    error=error if terminal == "failed" else None,
                )
                state.set_task_status(self.task_id, terminal, error_message=error)
                state.persist_live_checkpoint()
                break
            except (MediaMTXError, OSError, LiveFailure, asyncio.TimeoutError):
                state.set_task_status(self.task_id, "running")
                update(
                    self.task_id,
                    durable=False,
                    state="stopping",
                    cleanup_complete=False,
                    error="Live cleanup pending; runner remains busy",
                )
                await asyncio.sleep(self.settings.poll_interval)
        state.set_available(not bool(state.get_running_task_statuses()))
        from app.api.routes.task import notify_completion

        if self.request.completion_callback:
            await notify_completion(self.request.completion_callback, self.task_id, terminal, error)


class LiveFailure(RuntimeError):
    """A safe diagnostic containing no network credentials or source path."""


async def recover() -> None:
    """Recover owned active Live records by completing tombstones or scheduling cleanup.

    Retained terminal sessions and records owned by another Runner are left
    untouched.
    """
    for payload in state.get_runner_state().get("task_statuses", {}).values():
        if not payload.get("live") or payload.get("runner_id") != state.get_runner_id():
            continue
        if payload.get("status") == "running":
            if payload.get("stop_requested") and not payload.get("task_request"):
                request_stop(payload["task_id"])
                continue
            request = TaskRequest.model_validate(payload["task_request"])
            schedule(request, recovering=True)


async def shutdown() -> None:
    """Cancel lifecycle jobs and wait a bounded time for their cleanup.

    If cleanup times out, leave persisted recovery records for the next startup.
    """
    jobs = list(JOBS.values())
    for job in jobs:
        job.cancel()
    if jobs:
        try:
            await asyncio.wait_for(
                asyncio.gather(*jobs, return_exceptions=True),
                timeout=config.LIVE.stop_timeout * 2 + config.LIVE.mediamtx_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Live cleanup deferred to startup recovery; runner remains busy")
