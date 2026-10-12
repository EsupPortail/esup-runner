#!/usr/bin/env python3
"""Optional real CPU Live test, isolated on loopback with disposable data.

Run from runner/: UV_CACHE_DIR=/tmp/esup-runner-uv-cache uv run --no-sync
python ../scripts/live_smoke.py --mediamtx /path/to/mediamtx --runners 2
Add --grouped for one .env and launcher sharing MediaMTX across Live workers.
Add --automatic to trigger sessions without LIVE_SOURCES or a start API call.
Requires the two development environments, MediaMTX >= 1.21.1, FFmpeg and ffprobe.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    """Return an unused loopback TCP port without reserving it after the call."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def free_port_range(count: int) -> int:
    """Find a contiguous loopback port range for a real multi-instance launcher."""
    for _ in range(100):
        first = free_port()
        if first + count - 1 > 65535:
            continue
        try:
            with ExitStack() as sockets:
                for port in range(first, first + count):
                    sock = sockets.enter_context(socket.socket())
                    sock.bind(("127.0.0.1", port))
            return first
        except OSError:
            continue
    raise RuntimeError("No contiguous port range available for grouped smoke test")


def relay_names(port: int, secret: str) -> set[str]:
    """Read relay configuration names without exposing temporary API credentials."""
    auth = base64.b64encode(f"control:{secret}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v3/config/paths/list",
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return {item["name"] for item in json.load(response)["items"]}


def crash_grouped_worker(directory: Path, process: subprocess.Popen, port: int) -> None:
    """Kill only the known test worker; the real launcher must restart it."""
    log = (directory / "runner-group.log").read_text()
    match = re.search(rf"Started runner instance \d+ \(PID: (\d+)\) on port {port}\b", log)
    if match is None:
        raise RuntimeError("Grouped worker PID was not reported by the test launcher")
    pid = int(match.group(1))
    if os.getpgid(pid) != process.pid:
        raise RuntimeError("Worker is outside the isolated test process group")
    os.kill(pid, signal.SIGKILL)


def api(url: str, token: str, payload: dict | None = None) -> dict:
    """Call an authenticated JSON endpoint using GET, or POST when a payload is supplied."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def wait_for(check, timeout=90):
    """Poll a check until it returns a truthy value, or raise RuntimeError on timeout.

    Retry OSError and ValueError while services start; propagate other failures.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for Live test condition")


def terminate_all(processes, logs) -> None:
    """Terminate spawned process groups, escalate after timeout, and close their logs."""
    for process in reversed(processes):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    for process in reversed(processes):
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
    for log in logs:
        log.close()


def media_template(component: str, secret: str) -> str:
    """Fill the MediaMTX example with SHA-256 verifiers of a random test secret.

    MediaMTX accepts base64-encoded SHA-256 digests with the ``sha256:`` prefix.
    Only verifiers are persisted; clients keep using the high-entropy secret.
    """
    template = (ROOT / component / "production" / "mediamtx-live.yml").read_text()
    verifier = "sha256:" + base64.b64encode(hashlib.sha256(secret.encode()).digest()).decode()
    for placeholder in ("PUBLISH_PASSWORD", "RELAY_PASSWORD", "CONTROL_PASSWORD", "READ_PASSWORD"):
        template = template.replace(f"CHANGE_ME_{placeholder}", verifier)
    return template


def assert_capacity(manager_url: str, token: str, source: str) -> None:
    """Require HTTP 503 when requesting a Live session beyond the available Runners."""
    try:
        api(
            f"{manager_url}/live/start",
            token,
            {"source_id": source, "event_id": "extra", "etab_name": "smoke"},
        )
    except urllib.error.HTTPError as exc:
        assert exc.code == 503
    else:
        raise AssertionError("An occupied live runner accepted another session")


def automatic_session(manager_url: str, token: str, source: str, previous=None) -> dict:
    """Wait for HLS from an RTMP-triggered session without submitting a start request."""

    def ready():
        """Return a newly running automatic session, excluding an earlier publication."""
        item = api(f"{manager_url}/live/sources/{source}", token).get("live")
        if item and item["live_id"] != previous and item["state"] == "running":
            assert item["automatic"] and item["hls_ready"] and item["hls_url"]
            return item
        return None

    return wait_for(ready)


def start_sessions(manager_url, token, count, automatic, source_ids=None):
    """Discover automatically started sessions, or exercise the optional client API."""
    source_ids = source_ids or [f"room{i}" for i in range(count)]
    return [
        (
            automatic_session(manager_url, token, source_ids[i])
            if automatic
            else api(
                f"{manager_url}/live/start",
                token,
                {
                    "source_id": source_ids[i],
                    "event_id": f"smoke-{i}",
                    "etab_name": "smoke",
                },
            )
        )
        for i in range(count)
    ]


def check_publication_lifecycle(lives, sources, manager_url, token, spawn):
    """Check disconnect, publisher replacement and restart while sibling sessions play."""
    original = lives[0]
    publisher = sources[0]
    os.killpg(publisher.pid, signal.SIGTERM)
    publisher.wait(timeout=10)
    url = f"{manager_url}/live/{original['live_id']}"
    wait_for(lambda: api(url, token)["state"] == "completed")
    for sibling in lives[1:]:
        assert api(f"{manager_url}/live/{sibling['live_id']}", token)["state"] == "running"
    sources[0] = spawn("source-republished", publisher.args)
    lives[0] = automatic_session(manager_url, token, original["source_id"], original["live_id"])
    print("PASS: encoder disconnect cleans up; republication produces a new HLS session")
    original = lives[0]
    previous_publisher = sources[0]
    sources[0] = spawn("source-replacement", previous_publisher.args)
    previous_publisher.wait(timeout=30)
    url = f"{manager_url}/live/{original['live_id']}"
    wait_for(lambda: api(url, token)["state"] in {"completed", "failed"})
    lives[0] = automatic_session(manager_url, token, original["source_id"], original["live_id"])
    assert lives[0]["source_id"] == original["source_id"]
    for sibling in lives[1:]:
        assert api(f"{manager_url}/live/{sibling['live_id']}", token)["state"] == "running"
    print("PASS: second publisher replaces the first under the same broadcaster source ID")
    original = lives[0]
    url = f"{manager_url}/live/{original['live_id']}"
    api(f"{url}/stop", token, {})
    wait_for(lambda: api(url, token)["state"] == "completed")
    restarted = api(f"{url}/restart", token, {})
    assert api(f"{url}/restart", token, {})["live_id"] == restarted["live_id"]
    lives[0] = automatic_session(manager_url, token, original["source_id"], original["live_id"])
    assert lives[0]["live_id"] == restarted["live_id"]
    time.sleep(2)
    assert api(f"{manager_url}/live/{restarted['live_id']}", token)["state"] == "running"
    print("PASS: explicit restart restores HLS under the same RTMP publication")


def check_media_authentication(source, directory):
    """Reject anonymous/wrong-password publishers and retain identifiable media diagnostics."""
    base = source.args[-1].split("?", 1)[0]
    for suffix in ("", "?user=publisher&pass=incorrect-test-password"):
        try:
            result = subprocess.run(
                [*source.args[:-1], base + suffix], capture_output=True, timeout=15
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("MediaMTX did not reject invalid authentication promptly") from None
        assert result.returncode != 0, "MediaMTX accepted invalid publisher credentials"
    diagnostics = (directory / "manager-media.log").read_text()
    assert diagnostics.count("authentication failed") >= 2
    print("PASS: absent and incorrect RTMP credentials rejected and logged by MediaMTX")


def check_manager_restart(lives, process, manager_env, manager_url, token, spawn):
    """Restart both Manager workers and retain each active publication's identity."""
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=20)
    spawn("manager-restarted", process.args, manager_env, ROOT / "manager")
    wait_for(lambda: api(f"{manager_url}/api/health", token))
    for item in lives:
        recovered = automatic_session(manager_url, token, item["source_id"])
        assert recovered["live_id"] == item["live_id"]
    print("PASS: Manager restart preserves automatic sessions without duplicate reservations")


def check_automatic_stop(lives, manager_url, token):
    """Ensure explicit stops remain effective while the same encoders keep publishing."""
    time.sleep(11)  # Observe at least two scans with the fixed 5-second interval.
    for item in lives:
        status = api(f"{manager_url}/live/sources/{item['source_id']}", token)
        assert status["source_ready"]
        assert status["live"]["live_id"] == item["live_id"]
        assert status["live"]["state"] == "completed" and status["live"]["hls_url"] is None
    print("PASS: explicit stops remain effective while publishers stay connected")


def stop_sessions(
    lives, manager_url, token, directory, shared_api=None, media_secret="", *, automatic=False
) -> None:
    """Stop each session and verify that remaining relays keep running."""
    if shared_api is not None:
        assert relay_names(shared_api, media_secret) == {
            f"live/{item['live_id']}" for item in lives
        }
    for index, item in enumerate(lives):
        url = f"{manager_url}/live/{item['live_id']}"
        api(f"{url}/stop", token, {})
        ended = wait_for(
            lambda: (value if (value := api(url, token))["state"] == "completed" else None)
        )
        assert ended["hls_url"] is None
        assert api(f"{url}/stop", token, {})["state"] == "completed"
        if shared_api is not None:
            remaining = lives[index + 1 :]
            assert relay_names(shared_api, media_secret) == {
                f"live/{other['live_id']}" for other in remaining
            }
            for other in remaining:
                assert api(f"{manager_url}/live/{other['live_id']}", token)["state"] == "running"
    assert not list(directory.glob("runner-*/hls/**/master.m3u8"))
    if automatic:
        check_automatic_stop(lives, manager_url, token)


def check_crash_recovery(
    manager_url,
    token,
    runner_processes,
    directory,
    spawn,
    shared_api=None,
    media_secret="",
    source_ids=None,
) -> None:
    """Crash one worker, require cleanup on restart, and preserve its Live sibling."""
    source_ids = source_ids or ["room0", "room1"]
    item = api(
        f"{manager_url}/live/start",
        token,
        {
            "source_id": source_ids[0],
            "event_id": "crash-recovery",
            "etab_name": "smoke",
        },
    )
    url = f"{manager_url}/live/{item['live_id']}"
    wait_for(lambda: api(url, token)["state"] == "running")
    sibling = None
    if shared_api is not None and len(runner_processes) > 1:
        sibling = api(
            f"{manager_url}/live/start",
            token,
            {
                "source_id": source_ids[1],
                "event_id": "crash-sibling",
                "etab_name": "smoke",
            },
        )
        wait_for(
            lambda: api(f"{manager_url}/live/{sibling['live_id']}", token)["state"] == "running"
        )
    process, runner_env, port = runner_processes[item["runner"]]
    if shared_api is not None:
        crash_grouped_worker(directory, process, port)
    else:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        spawn("runner-restarted", process.args, runner_env, ROOT / "runner")
    wait_for(lambda: api(url, token)["state"] == "failed")
    wait_for(lambda: api(f"http://127.0.0.1:{port}/runner/ping", token)["available"])
    if sibling:
        other_url = f"{manager_url}/live/{sibling['live_id']}"
        running = api(other_url, token)
        assert running["state"] == "running"
        with urllib.request.urlopen(running["hls_url"], timeout=5) as response:
            assert b"#EXTM3U" in response.read()
        assert relay_names(shared_api, media_secret) == {f"live/{sibling['live_id']}"}
        api(f"{other_url}/stop", token, {})
        wait_for(lambda: api(other_url, token)["state"] == "completed")
    assert not list(directory.glob("runner-*/hls/**/master.m3u8"))
    print("PASS: runner crash, orphan FFmpeg termination and reservation recovery")


def check_discovered_capacity(source, extra_path, manager_url, token, spawn):
    """Discover an extra accepted publication while all Live workers are occupied."""
    original = urllib.parse.urlsplit(source.args[-1])
    extra_url = urllib.parse.urlunsplit(original._replace(path="/" + extra_path))
    process = spawn("source-extra", [*source.args[:-1], extra_url])
    identifier = extra_path.rsplit("/", 1)[-1]
    wait_for(lambda: identifier in api(f"{manager_url}/live/sources", token)["sources"])
    assert_capacity(manager_url, token, identifier)
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=10)
    print("PASS: undeclared publisher discovered without exceeding Live capacity")


def configure_smoke_sources(manager_env, sources, automatic):
    """Remove source declarations in automatic mode and return expected public IDs."""
    if automatic:
        manager_env.pop("LIVE_SOURCES")
        return [path.rsplit("/", 1)[-1] for path in sources.values()]
    return list(sources)


def start_runner_process(
    name, runner_env, directory, spawn, media, media_ports, *, grouped=False, previous=None
):
    """Start one API process, or a grouped launcher with secrets only in its environment."""
    if grouped:
        if previous is None:
            for key in ("RUNNER_INSTANCE_ID", "RUNNER_INSTANCE_URL", "RUNNER_PORT"):
                runner_env.pop(key)
            # Persist only non-secret settings; credentials remain in the environment.
            settings = {
                key: value
                for key, value in runner_env.items()
                if key
                in {
                    "RUNNER_HOST",
                    "RUNNER_BASE_PORT",
                    "RUNNER_BASE_NAME",
                    "RUNNER_INSTANCES",
                    "RUNNER_MONITORING",
                    "RUNNER_TASK_TYPES",
                    "LIVE_MEDIAMTX_API_URL",
                    "LIVE_HLS_DIRECTORY",
                    "MANAGER_URL",
                    "ENCODING_TYPE",
                    "LOG_DIR",
                    "CACHE_DIR",
                    "STORAGE_DIR",
                    "DEBUG",
                    "SMTP_SERVER",
                    "UV_CACHE_DIR",
                }
            }
            env_file = directory / "runner.env"
            env_file.write_text(
                "".join(f"{key}={json.dumps(value)}\n" for key, value in settings.items())
            )
            env_file.chmod(0o600)
            launch_env = {key: value for key, value in runner_env.items() if key not in settings}
            launch_env.update(
                CONFIG_ENV_PATH=str(env_file),
                PYTHONUNBUFFERED="1",
                UV_CACHE_DIR="/tmp/esup-runner-uv-cache",
            )
            previous = spawn(
                name,
                ["uv", "run", "--no-sync", "esup-runner-runner"],
                launch_env,
                ROOT / "runner",
            )
        runner_process = previous
    else:
        media(f"{name}-media", *media_ports)
        runner_process = spawn(
            name,
            [
                "uv",
                "run",
                "--no-sync",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                runner_env["RUNNER_PORT"],
            ],
            runner_env,
            ROOT / "runner",
        )
    return runner_process


def parse_args() -> argparse.Namespace:
    """Parse and validate isolated media test options without starting processes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mediamtx", required=True, type=Path)
    parser.add_argument("--runners", type=int, default=2)
    parser.add_argument("--crash-recovery", action="store_true")
    parser.add_argument(
        "--automatic",
        action="store_true",
        help="Trigger HLS by publishing RTMP only; test disconnect, republication and Manager restart",
    )
    parser.add_argument(
        "--grouped",
        action="store_true",
        help="Use one .env, a VOD group and multiple Live workers sharing one MediaMTX",
    )
    parser.add_argument(
        "--mixed",
        action="store_true",
        help="Advertise encoding, studio and transcription alongside Live",
    )
    args = parser.parse_args()
    if args.runners < 1:
        parser.error("--runners must be positive")
    return args


def main() -> None:
    """Run the isolated CPU media flow, capacity and cleanup checks from CLI options."""
    args = parse_args()
    directory = Path(tempfile.mkdtemp(prefix="live-smoke-"))
    processes: list[subprocess.Popen] = []
    runner_processes = {}
    source_processes = {}
    logs = []
    token = secrets.token_urlsafe(32)
    media_secret = secrets.token_hex(24)
    manager_port, manager_rtmp, manager_api = free_port(), free_port(), free_port()
    manager_url = f"http://127.0.0.1:{manager_port}"
    sources = {f"room{i}": f"live/room{i}" for i in range(args.runners + 1)}
    (directory / "empty.env").touch()

    def spawn(name, command, env=None, cwd=None):
        """Track a new process group and its log, suppressing credential-bearing source output."""
        log = (directory / f"{name}.log").open("wb")
        logs.append(log)
        process = subprocess.Popen(
            command,
            env=env,
            cwd=cwd,
            stdout=subprocess.DEVNULL if name.startswith("source-") else log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append(process)
        return process

    def media(name, rtmp_port, api_port):
        """Start an authenticated MediaMTX instance on the supplied loopback ports."""
        configuration = directory / f"{name}.yml"
        component = "manager" if name == "manager-media" else "runner"
        configuration.write_text(media_template(component, media_secret))
        spawn(
            name,
            [str(args.mediamtx.resolve()), str(configuration)],
            {
                **os.environ,
                "MTX_RTMPADDRESS": f"127.0.0.1:{rtmp_port}",
                "MTX_APIADDRESS": f"127.0.0.1:{api_port}",
                "MTX_METRICSADDRESS": f"127.0.0.1:{free_port()}",
            },
        )

    def environment(name):
        """Build isolated component storage and logging settings with email disabled."""
        return {
            **os.environ,
            "CONFIG_ENV_PATH": str(directory / "empty.env"),
            "DEBUG": "false",
            "UV_CACHE_DIR": "/tmp/esup-runner-uv-cache",
            "LOG_DIR": str(directory / name / "logs"),
            "CACHE_DIR": str(directory / name / "cache"),
            "STORAGE_DIR": str(directory / name / "storage"),
            "SMTP_SERVER": "",
            "MANAGER_EMAIL": "",
            "RUNNER_EMAIL": "",
        }

    try:
        media("manager-media", manager_rtmp, manager_api)
        manager_env = {
            **environment("manager"),
            "ENVIRONMENT": "production",
            "MANAGER_HOST": "127.0.0.1",
            "MANAGER_PORT": str(manager_port),
            "MANAGER_PUBLIC_URL": manager_url,
            "MANAGER_DATA_DIR": str(directory / "manager" / "data"),
            "AUTHORIZED_TOKENS__live_smoke": token,
            "LIVE_ENABLED": "true",
            "LIVE_AUTO_START": str(args.automatic).lower(),
            "LIVE_SOURCES": json.dumps(sources),
            "LIVE_MEDIAMTX_API_URL": f"http://127.0.0.1:{manager_api}",
            "LIVE_MEDIAMTX_API_PASSWORD": media_secret,
            "API_DOCS_VISIBILITY": "public",
        }
        source_ids = configure_smoke_sources(manager_env, sources, args.automatic)
        manager_process = spawn(
            "manager",
            [
                "uv",
                "run",
                "--no-sync",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(manager_port),
                "--workers",
                "2",
            ],
            manager_env,
            ROOT / "manager",
        )
        wait_for(lambda: api(f"{manager_url}/api/health", token))
        grouped_process = None
        shared_rtmp, shared_api = free_port(), free_port()
        base_port = free_port_range(args.runners + 1) if args.grouped else 0
        if args.grouped:
            media("runner-media-shared", shared_rtmp, shared_api)
        for index in range(args.runners):
            port = base_port + index + 1 if args.grouped else free_port()
            rtmp_port, api_port = (
                (shared_rtmp, shared_api) if args.grouped else (free_port(), free_port())
            )
            name = "runner-group" if args.grouped else f"runner-{index}"
            task_types = "encoding,studio,transcription,live" if args.mixed else "live"
            runner_env = {
                **environment(name),
                "RUNNER_HOST": "127.0.0.1",
                "RUNNER_PORT": str(port),
                "RUNNER_BASE_PORT": str(base_port if args.grouped else port),
                "RUNNER_BASE_NAME": name,
                "RUNNER_INSTANCE_ID": "0",
                "RUNNER_INSTANCE_URL": f"http://127.0.0.1:{port}",
                "RUNNER_INSTANCES": str(args.runners + 1 if args.grouped else 1),
                "RUNNER_MONITORING": "true",
                "RUNNER_TASK_TYPES": (
                    f"[1x(encoding,studio),{args.runners}x({task_types})]"
                    if args.grouped
                    else f"[1x({task_types})]"
                ),
                "RUNNER_TOKEN": token,
                "MANAGER_URL": manager_url,
                "ENCODING_TYPE": "CPU",
                "LIVE_MEDIAMTX_API_URL": f"http://127.0.0.1:{api_port}",
                "LIVE_MEDIAMTX_API_PASSWORD": media_secret,
                "LIVE_UPSTREAM_RTMP_URL": f"rtmp://relay:{media_secret}@127.0.0.1:{manager_rtmp}",
                "LIVE_LOCAL_RTMP_URL": f"rtmp://transcoder:{media_secret}@127.0.0.1:{rtmp_port}",
                "LIVE_HLS_DIRECTORY": str(directory / name / "hls" / "instance-{instance_id}"),
            }
            runner_process = start_runner_process(
                name,
                runner_env,
                directory,
                spawn,
                media,
                (rtmp_port, api_port),
                grouped=args.grouped,
                previous=grouped_process,
            )
            grouped_process = runner_process if args.grouped else None
            wait_for(
                lambda p=port: api(f"http://127.0.0.1:{p}/runner/ping", token).get("registered")
            )
            identity = api(f"http://127.0.0.1:{port}/runner/ping", token)["runner_id"]
            runner_processes[identity] = (runner_process, runner_env, port)
            source_processes[index] = spawn(
                f"source-{index}",
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=1280x720:rate=25",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=1000:sample_rate=48000",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-threads",
                    "2",
                    "-g",
                    "50",
                    "-c:a",
                    "aac",
                    "-f",
                    "flv",
                    f"rtmp://127.0.0.1:{manager_rtmp}/{sources[f'room{index}']}?user=publisher&pass={media_secret}",
                ],
            )
        lives = start_sessions(manager_url, token, args.runners, args.automatic, source_ids)
        check_media_authentication(source_processes[0], directory)
        assert len({item["runner"] for item in lives}) == args.runners
        for item in lives:
            url = f"{manager_url}/live/{item['live_id']}"
            running = wait_for(
                lambda: (value if (value := api(url, token))["state"] == "running" else None)
            )
            with urllib.request.urlopen(running["hls_url"], timeout=5) as response:
                master = response.read().decode()
            assert master.count("#EXT-X-STREAM-INF") == 3
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-read_intervals",
                    "%+1",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    running["hls_url"],
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            assert "h264" in probe.stdout and "aac" in probe.stdout
            print(f"Live {item['live_id']}: 3 HLS variants verified")
        if args.automatic:
            check_discovered_capacity(
                source_processes[0],
                sources[f"room{args.runners}"],
                manager_url,
                token,
                spawn,
            )
            check_publication_lifecycle(lives, source_processes, manager_url, token, spawn)
            check_manager_restart(lives, manager_process, manager_env, manager_url, token, spawn)
        else:
            assert_capacity(manager_url, token, f"room{args.runners}")
        stop_sessions(
            lives,
            manager_url,
            token,
            directory,
            shared_api if args.grouped else None,
            media_secret,
            automatic=args.automatic,
        )
        if args.crash_recovery:
            check_crash_recovery(
                manager_url,
                token,
                runner_processes,
                directory,
                spawn,
                shared_api if args.grouped else None,
                media_secret,
                source_ids,
            )
        print(
            f"PASS: {args.runners} live runners, two manager workers, no extra capacity, idempotent cleanup"
        )
    finally:
        terminate_all(processes, logs)
        print(f"Test artifacts: {directory}")


if __name__ == "__main__":
    main()
