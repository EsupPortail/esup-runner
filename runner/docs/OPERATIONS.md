# Operations Guide (Runner)

This document centralizes day-to-day production operations for the runner.

For RTMP-to-HLS Live operations and recovery, use the Manager's dedicated
[Live operations runbook](../../manager/docs/LIVE_OPERATIONS.md) and the
[Runner Live reference](LIVE.md). For a new deployment, start with the
[short installation path](../../manager/docs/LIVE_INSTALLATION.md).

## Scope and assumptions

- Installation path: `/opt/esup-runner/runner`
- Service name: `esup-runner-runner` (systemd user service)
- Service user: `esup-runner`
- Configuration file: `/opt/esup-runner/runner/.env`

## Daily command quick reference

```bash
# Service state
systemctl --user status esup-runner-runner
systemctl --user is-active --quiet esup-runner-runner && echo "OK: service is running"

# Lifecycle
systemctl --user restart esup-runner-runner
systemctl --user stop esup-runner-runner
systemctl --user start esup-runner-runner

# Logs
journalctl --user -u esup-runner-runner -f
journalctl --user -u esup-runner-runner -n 200 --no-pager
```

## Task handling after service restart

When you restart the service:

```bash
systemctl --user restart esup-runner-runner
```

the runner automatically checks tasks that were in progress before restart.

In most cases, no manual action is needed:

- tasks that are still running continue to be tracked
- tasks that already produced final files are marked as completed
- tasks in `failed`/`timeout` with a persisted request are automatically restarted
- tasks that cannot be recovered are marked as failed (or timeout)

The runner then sends updated task status back to the manager.
During this startup reconciliation window, runner availability is kept `false`
to avoid accepting new work too early.

For tasks with a completion callback, deletion in the Manager prevents automatic
recovery and failure emails. If the Manager cannot confirm their existence,
recovery is deferred: restart the Runner or relaunch the task from the Manager
once it is available.

## Periodic running-task verification

Each instance checks running file-based tasks hourly and finalizes abandoned
executions, notifying the Manager. Set `TASK_PROCESS_CHECK_INTERVAL_SECONDS`
(`600` for ten minutes, `0` to disable), then restart the Runner. Live uses
separate supervision.

Relaunch failed tasks from the Manager; periodic checks do not retry them. Update
both components to ignore results from older attempts. See [verification rules and limits](PARAMETERS.md#task-verification).

## Stopping a running task

The manager can request cancellation of a task that is still tracked as
`running` on a runner instance.

```bash
RUNNER_URL="http://127.0.0.1:<RUNNER_PORT>"
TASK_ID="some-task-id"

curl -sS -X POST \
  -H "Authorization: Bearer ${RUNNER_TOKEN}" \
  "${RUNNER_URL}/task/stop/${TASK_ID}"
```

Expected responses:

- `202`: stop was requested and at least one external process was terminated
- `200`: the task is already terminal on the runner (`completed`, `failed`, or `timeout`)
- `404`: the runner does not know this task id
- `409`: the task is marked as running, but no killable external process was found yet

After a successful stop request, the task completes through the normal failure
flow with the stable error message `Cancelled by user.`. User-stopped tasks are
not automatically restarted by startup recovery.

### Quick operator check

After a restart, verify only these points:

1. Service is up.
2. API health/readiness endpoints respond.
3. Logs do not show repeated recovery errors.

```bash
systemctl --user status esup-runner-runner
journalctl --user -u esup-runner-runner -n 200 --no-pager
journalctl --user -u esup-runner-runner -n 300 --no-pager | grep -E "Recovering|Inspecting|Scheduled automatic restart|Failed to recover|Skipping automatic restart"
```

## API health and readiness checks

The runner exposes:

- root endpoint: `/`
- health endpoint: `/runner/health`
- readiness endpoint: `/runner/ping`
- status endpoint: `/runner/status`

Example checks:

```bash
curl "http://127.0.0.1:<RUNNER_PORT>/"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/health"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/ping"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/status"
```

Expected behavior:

- `/runner/health` should report `status=healthy`.
- `/runner/ping` should report `available=true` when the instance is idle.
- `/runner/ping` and `/runner/status` should report `registered=true` after successful manager registration.

## Task failure emails

Configure `SMTP_SERVER` and `MANAGER_EMAIL` in `runner/.env` for task failure
and timeout emails, which include `parameters.video_id` when provided.

Set `MANAGER_PUBLIC_URL` to the browser-facing Manager base URL for email links,
including GPU warnings; it falls back to `MANAGER_URL`. Restart the Runner after
changing these settings.

## Automatic NVIDIA driver monitoring

With `ENCODING_TYPE=GPU`, the runner checks `nvidia-smi` once at startup and
every 12 hours by default, even when no tasks are running. Instance 0 performs
the check for the shared host, avoiding duplicate warnings from sibling
instances. Keep instance 0 running for this monitoring to remain active.

Configure the existing `SMTP_SERVER`, `SMTP_PORT`, `SMTP_SENDER` and
`MANAGER_EMAIL` settings in the Runner environment; use an administrator address
or mailing list for `MANAGER_EMAIL`. Optional STARTTLS and authentication use
`SMTP_USE_TLS`, `SMTP_USERNAME` and `SMTP_PASSWORD`.

```properties
ENCODING_TYPE=GPU
GPU_HEALTH_CHECK_INTERVAL_HOURS=12
```

Set the interval to `24` for one periodic check per day, or `0` to disable
monitoring. Restart the Runner after changing its environment. This control is
enabled only by `ENCODING_TYPE=GPU`, independently of Live configuration.

A missing command, non-zero exit status or 10-second timeout logs a warning and
sends an email identifying the runner/host and including the failure diagnostics.
While the failure persists, one reminder is sent per check interval; startup
checks also send a warning if needed. Successful checks only produce a log entry.
SMTP configuration or delivery failures are logged, and monitoring retries on
the next scheduled check. Monitoring does not stop tasks, change readiness or
attempt driver repairs. A successful `nvidia-smi` check does not validate FFmpeg,
CUDA or NVENC capabilities; use the GPU diagnostic scripts for those checks.

## Multi-instance checks

For grouped task-type deployments, check every port from `RUNNER_BASE_PORT` to
`RUNNER_BASE_PORT + instance_count - 1`.

Example for ports `8082..8085`:

```bash
for p in 8082 8083 8084 8085; do
  echo "---- port ${p} ----"
  curl -s "http://127.0.0.1:${p}/runner/ping"
  echo
done
```

## Operational validation scripts

From `/opt/esup-runner/runner`:

```bash
uv run scripts/check_config.py
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_gpu.py
uv run scripts/check_runner_resources.py
uv run scripts/check_runner_storage.py
```

Use these checks:

- after installation/upgrade
- after changing `.env`
- after changing FFmpeg/GPU drivers
- before and after maintenance windows

Run `check_config.py` first after editing `.env`. It validates the same effective
configuration used at Runner startup, prints only non-sensitive summary values,
and returns `2` when one or more settings are invalid.

### Live resource estimates

`check_runner_resources.py` includes Live when `RUNNER_TASK_TYPES` declares
`live` in at least one instance, including a mixed group. Live resource and
network sections stay hidden otherwise, even when `LIVE_MEDIAMTX_API_PASSWORD`,
`LIVE_UPSTREAM_RTMP_URL` or `--live-viewers` are set. Relay credentials alone do
not enable Live. It reads `LIVE_TRANSCODER` (independently of `ENCODING_TYPE`), the
configured `LIVE_VARIANTS`, `LIVE_CPU_FALLBACK` and `GPU_HWACCEL_DEVICE`.
Each instance runs one task at a time: mixed groups use the maximum CPU, RAM
and VRAM requirement among their supported tasks, while dedicated instances
add their requirements. See [Live operation](LIVE.md) for the media pipeline.

The starting budgets for the default three-rendition H.264/AAC profile are:

| Live transcoder | vCPU per session | Host RAM per session | VRAM per session |
| --- | ---: | ---: | ---: |
| CPU (`libx264`) | 2 | 2 GB | 0 |
| GPU (`nvenc`), CPU fallback disabled | 1 | 2 GB | 1 GB |
| GPU (`nvenc`), CPU fallback enabled (default) | 2 | 2 GB | 1 GB |

These are planning assumptions, not measured maximum usage. The CPU and viewer
figures come from historical Nginx RTMP sizing; the GPU figures are initial
estimates. Profiles with more than three renditions increase the budgets
proportionally, rounding vCPU upward. A larger source/output resolution or frame
rate still requires measurement even when the rendition count is unchanged.
NVDEC does not reduce the CPU reserve: the current pipeline still scales video,
encodes AAC and muxes HLS on the CPU.

Pass the **total concurrent viewers served by this host across all sessions**:

```bash
UV_CACHE_DIR=/tmp/esup-runner-uv-cache uv run scripts/check_runner_resources.py --live-viewers 4000
```

The script adds `ceil(viewers / 2000)` vCPU once for delivery, for either
transcoder. Without this option, the viewer count defaults to zero and the
output explicitly states that viewer delivery is excluded. This CLI option is
only a sizing input; it does not limit viewers or change the Runner configuration.
For example, two dedicated CPU Lives and 4,000 viewers require an estimated
6 vCPU and 4 GB RAM before additional system/service overhead.

The Live report also shows the network impact of every concurrent viewer.
For each configured rendition, it adds the video and audio bitrates, then
reports the lowest and highest per-viewer payload rates in Mbps (1,000,000
bits/s). Each viewer plays one rendition, so the rendition bitrates are not
added together. The host-wide viewer count is applied once across all sessions,
for either CPU or NVENC transcoding.

With the default profile, the range is 0.344–1.228 Mbps per viewer. At 4,000
concurrent viewers, the estimated outgoing payload is 1,376–4,912 Mbps, or about
1.38–4.91 Gbps. Without `--live-viewers`, the per-viewer range is still shown but
the aggregate bandwidth is not estimated. Size egress for the highest rendition
and add headroom for HLS/HTTP/TLS overhead and traffic peaks. These estimates
assume direct HLS delivery from the Runner; caching/CDN can reduce Runner
traffic. The script does not measure or validate network link capacity, and
network sizing does not change its CPU/RAM/GPU adequacy result.

When Live is enabled, recommendations grow the configured task groups in
proportion to their declared counts while the budgets fit. They retain the
groups' task types, including mixed groups, and may omit a group that cannot fit.
If this would exclude Live entirely, the script tries adding `live` as an
alternative task on one retained instance, checking all its resource budgets
again. For example, on an 8-vCPU host with at least 10 GB RAM, a CPU recommendation
can be `[1x(encoding,studio,transcription,live)]`: all four task types share the
same instance, and only one task can run at a time. A viewer budget or a larger
Live profile can prevent this addition.
This is a greedy suggestion, not an optimal allocation. Review the printed
recommendation before applying it. If no configured group can fit even one
instance, the script reports that no feasible configuration is available.
Configurations without Live keep the existing VOD
recommendation logic and GPU model policy.

Each Live rendition creates a separate NVENC encoding session. All Lives use
the configured `GPU_HWACCEL_DEVICE`; their VRAM and known-model encoding budgets
must fit that device, even on a host with multiple GPUs. Existing per-model VOD
encoding maxima are reused conservatively as encoding budgets, counting every
Live rendition. Unknown models use VRAM estimates and cannot validate encoding
throughput or driver session limits. NVIDIA documents these separate constraints
in its [NVENC application note](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/nvenc-application-note/index.html).

Benchmark the actual RTMP-to-HLS workload before setting production capacity.
Check CPU use, NVENC/NVDEC utilization, driver session limits, network egress,
TLS/reverse-proxy overhead and shared MediaMTX/service overhead. GPU VOD CPU
overhead remains outside this script's model. HLS segments under `/dev/shm/hls`
also consume RAM and are not measured by the script; validate their retained size
and the tmpfs capacity separately, especially for custom high-bitrate profiles.

## Storage and retention operations

Task outputs/manifests are stored under `STORAGE_DIR`.

Automatic cleanup behavior:

- `MAX_FILE_AGE_DAYS`:
  - `0` means unlimited retention (no age-based deletion)
  - `> 0` enables age-based deletion
- `CLEANUP_INTERVAL_HOURS` controls periodic cleanup cadence

Manual cleanup helper:

```bash
cd /opt/esup-runner/runner
uv run scripts/manual_cleanup.py
```

The script reads `.env`, previews items older than `MAX_FILE_AGE_DAYS`, then asks for confirmation.

Recommended maintenance flow for aggressive cleanup:

1. Stop the runner service.
2. Run `scripts/manual_cleanup.py`.
3. Validate free space with `scripts/check_runner_storage.py`.
4. Start the runner service.
5. Verify `/runner/health` and `/runner/ping` on each instance.

## Transcription cache operations

For transcription-enabled runners, monitor cache directories:

- `CACHE_DIR`
- `WHISPER_MODELS_DIR` (default: `CACHE_DIR/whisper-models`)
- `HUGGINGFACE_MODELS_DIR` (default: `CACHE_DIR/huggingface`)
- `UV_CACHE_DIR` (default: `CACHE_DIR/uv`)

Operational notes:

- Keep enough free space before first model download or model upgrades.
- Use `uv run scripts/check_runner_storage.py` to validate free space and writability.
- If caches are intentionally purged, next transcription tasks will re-download model assets.

## Manager registration troubleshooting

If runner API is healthy but `registered=false`:

1. Check `MANAGER_URL` in `.env`.
2. Check `RUNNER_TOKEN` matches a manager `AUTHORIZED_TOKENS__*`.
3. Check runner/manager version compatibility (`MAJOR.MINOR` must match).
4. Inspect logs:
   - runner: `journalctl --user -u esup-runner-runner -n 200 --no-pager`
   - manager: corresponding manager service logs

### Completion callback rejected / pipeline test keeps waiting

If encoding outputs are complete but the Manager still reports `running`, look
for `Rejected completion callback` in the Runner log. Compare the Runner's
`MANAGER_URL` with either the Manager's private
`MANAGER_PROTOCOL://MANAGER_HOST:MANAGER_PORT` address or `MANAGER_PUBLIC_URL`.
A public reverse proxy must expose `/task/completion` and the other Manager API
routes, not just the administration interface.

With the updated Manager and Runner, both endpoints are advertised and checked
before processing. A mismatch immediately fails the Manager task with HTTP `422`
and an explanation showing the expected and advertised address components,
without URL credentials, queries or fragments. The pipeline test prints that
error instead of waiting for its deadline. After correcting configuration,
restart the affected service and rerun `manager/scripts/check_pipeline_tasks.py`.

## Backup checklist (before risky operations)

Before upgrades or large cleanup operations, back up at least:

- `/opt/esup-runner/runner/.env`
- `STORAGE_DIR`
- `LOG_DIR` (default `/var/log/esup-runner`)
- For transcription runners: `CACHE_DIR`

## Related docs

- Installation: [INSTALLATION.md](INSTALLATION.md)
- Configuration: [CONFIGURATION.md](CONFIGURATION.md)
- Parameters: [PARAMETERS.md](PARAMETERS.md)
- Upgrade and rollback: [UPGRADE.md](UPGRADE.md)
- Docker deployment: [DOCKER.md](DOCKER.md)
- FFmpeg setup: [FFMPEG_SETUP.md](FFMPEG_SETUP.md)

## Live streaming

See [LIVE.md](LIVE.md) for explicit `live` declarations in `runner/.env`, mixed or grouped instances, MediaMTX, adaptive transcoding and recovery.
