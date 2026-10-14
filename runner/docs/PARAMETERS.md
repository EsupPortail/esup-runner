# Runner Parameters

This page summarizes the environment variables consumed by the runner. Values are parsed in [app/core/config.py](../app/core/config.py) and validated at startup.

## Quick start (.env snippet)
```properties
SERVICE_USER=esup-runner
DEBUG=false
RUNNER_PROTOCOL=http
RUNNER_HOST=127.0.0.1
RUNNER_BASE_PORT=8082
RUNNER_BASE_NAME=my-runner
RUNNER_TASK_TYPES=[2x(encoding,studio,transcription)]
RUNNER_TOKEN=change-me-runner-token
MANAGER_URL=http://127.0.0.1:8081
STORAGE_DIR=/tmp/esup-runner
EXTERNAL_SCRIPT_TIMEOUT_SECONDS=18000
LOG_DIR=/var/log/esup-runner
LOG_LEVEL=INFO
MEDIA_CODEC_DENYLIST=magicyuv
ENCODING_TYPE=CPU
GPU_CUDA_PATH=/usr/local/cuda-13.2
WHISPER_MODEL=turbo
WHISPER_LANGUAGE=auto
```

## Core runner
- `SERVICE_USER` (optional, default `esup-runner`): Unix account that owns runtime directories and runs the systemd user service. It also defines the default cache root.
- `DEBUG` (bool, default `False`): Enables verbose logging and debug flags passed to scripts.
- `RUNNER_PROTOCOL` (default `http`), `RUNNER_HOST` (default `localhost`), `RUNNER_BASE_PORT` (default `8082`), `RUNNER_BASE_NAME` (default `default-runner`): Base URL components. The launcher offsets the port per instance.
- `RUNNER_INSTANCES` (int, default `1`): Number of instances when using legacy task-type syntax.
- `RUNNER_TASK_TYPES` (CSV or grouped syntax): Task types handled. Legacy: `encoding,studio,transcription`. Grouped: `[2x(encoding,studio,transcription),1x(encoding,studio)]` (preferred for per-instance mapping).
- `RUNNER_MONITORING` (bool, default `False`): If true, the launcher monitors and restarts instances.

## Networking and auth
- `RUNNER_TOKEN` (required): Token used to authenticate runner <-> manager calls in both directions.
- `MANAGER_URL` (default `http://127.0.0.1:8081`): Manager API URL used for registration, health checks and callbacks. It must match either the Manager's `MANAGER_PROTOCOL`, `MANAGER_HOST` and `MANAGER_PORT`, or its `MANAGER_PUBLIC_URL`. Prefer the private API; a public reverse proxy must also expose the API. Route paths such as `/api/health` are appended automatically.
- `MANAGER_PUBLIC_URL` (optional, default empty): Browser-facing Manager HTTP(S) base URL used for email links. Set it in the Runner environment to the same public address as the Manager, including any reverse-proxy path prefix. The "Open manager" button appends `/admin` unless the URL already ends with it. When unset or blank, links use `MANAGER_URL`. This setting does not change API calls or completion callbacks.

The Manager sends `completion_callback` (private endpoint) and
`completion_callback_public` (public endpoint). At least one must match the
Runner's `MANAGER_URL` followed by `/task/completion`. The accepted callback is
canonicalized before persistence. Notifications use only this configured
endpoint, including after task recovery, and never follow redirects. A mismatch returns HTTP `422` before
execution, with a redacted diagnostic visible in the Manager task and CLI test.

## Storage and cleanup
- `STORAGE_DIR` (runtime default `/tmp/esup-runner`): Root workspace for task data.
- `RUNNER_TASK_STATUS_FILE` (optional, default `STORAGE_DIR/runner_task_statuses.json`): Persistent JSON state used to restore in-flight tasks after runner restart (status, callback URL, process metadata). When `RUNNER_INSTANCE_ID` is set, the effective file is instance-scoped with suffix `.instance-<id>` to avoid cross-instance collisions.
- `MAX_VIDEO_SIZE_GB` (int, default `0` = unlimited): Reject downloads above this size.
- `MAX_FILE_AGE_DAYS` (int, default `0` = keep forever): Cleanup threshold.
- `CLEANUP_INTERVAL_HOURS` (int, default `24`): Periodic cleanup interval.
- `EXTERNAL_SCRIPT_TIMEOUT_SECONDS` (int, default `18000`): Timeout (in seconds) for external scripts run by `encoding`, `studio`, and `transcription` handlers.
- `TASK_PROCESS_CHECK_INTERVAL_SECONDS` (non-negative int, default `3600`): Periodically verify running file-based tasks within each Runner instance. Use `600` for ten-minute checks or `0` to disable. This is independent of script timeouts and launcher monitoring. See [periodic running-task verification](OPERATIONS.md#periodic-running-task-verification).

## Task verification

`TASK_PROCESS_CHECK_INTERVAL_SECONDS` applies independently to each instance's
file-based tasks. The check preserves active Python work and surviving external
processes, and rechecks abandoned executions after ten seconds. Process birth
ticks distinguish reused PIDs; zombies do not count as active work.

Completion requires a matching manifest or successful metadata, plus nonempty
recorded deliverables contained in the workspace. A missing manifest can be
rebuilt from successful metadata; partial files alone never establish success.
Explicit failure and user cancellation take precedence. New executions carry
an identifier in recovery state, metadata and manifests; the Manager also uses
it to reject stale callbacks and polling responses. Startup retries skip attempts
already replaced on the Manager. Existing unmarked recovery records retain
their startup behavior. Update both components for these protections.

The check does not diagnose stalled Python workers or surviving processes.
`EXTERNAL_SCRIPT_TIMEOUT_SECONDS` separately bounds external script execution.

## Media safety
- `MEDIA_CODEC_DENYLIST` (CSV, default `magicyuv`): Codec denylist checked with lightweight binary signatures immediately after download and before FFmpeg, ffprobe, or Whisper inspect the media. The built-in `magicyuv` rule rejects MagicYUV signatures because of CVE-2026-8461 / PixelSmash. Set this to an empty value only if you accept that risk.

## Encoding / hardware
- `ENCODING_TYPE` (`CPU` | `GPU`, default `CPU`): Selects CPU or GPU path for encoding tasks.
- `GPU_HEALTH_CHECK_INTERVAL_HOURS` (non-negative int, default `12`): When `ENCODING_TYPE=GPU`, instance 0 checks `nvidia-smi` at startup and every 12 hours, independently of tasks. Use `24` for daily checks or `0` to disable. Each failed check sends an administrator warning using the existing SMTP settings; command execution times out after 10 seconds. See [automatic NVIDIA driver monitoring](OPERATIONS.md#automatic-nvidia-driver-monitoring).
- `GPU_HWACCEL_DEVICE` (int, default `0`): Device index passed to scripts.
- `GPU_CUDA_VISIBLE_DEVICES` (CSV, default `0,1`): Exported when GPU mode is enabled.
- `GPU_CUDA_DEVICE_ORDER` (default `PCI_BUS_ID`): CUDA device ordering.
- `GPU_CUDA_PATH` (default `/usr/local/cuda-13.2`): Used to prepend CUDA bin to `PATH`.

## Studio defaults
- `STUDIO_DEFAULT_CRF` (default `23`): CRF passed to studio encoding.
- `STUDIO_DEFAULT_PRESET` (default `medium`): FFmpeg preset for studio outputs.
- `STUDIO_DEFAULT_AUDIO_BITRATE` (default `128k`): Audio bitrate for studio outputs.

## Transcription / Whisper
- `WHISPER_MODEL` (default `small`): Logical whisper model (`small|medium|large|turbo`). Turbo recommended.
- `CACHE_DIR` (default `/home/{SERVICE_USER}/.cache/esup-runner`): Shared cache root.
- `WHISPER_MODELS_DIR` (default `CACHE_DIR/whisper-models`): Optional override for Whisper model cache directory.
- `HUGGINGFACE_MODELS_DIR` (default `CACHE_DIR/huggingface`): Optional override for Hugging Face translation model cache.
- `HF_TOKEN` (optional): Hugging Face access token used for authenticated model downloads (recommended to reduce rate-limit warnings and improve download reliability/speed).
- `UV_CACHE_DIR` (default `CACHE_DIR/uv`): Optional override for uv package cache directory.
- `WHISPER_LANGUAGE` (default `auto`): Default final subtitle language; `auto` keeps the detected spoken language, while an explicit `fr`/`en` target can trigger subtitle translation after transcription.
- `WHISPER_CHUNK_THRESHOLD_SECONDS` (optional override): If unset, the runner chooses a hardware-aware default: `800` on CPU and `1800` on GPU.

An explicit `CACHE_DIR`, `WHISPER_MODELS_DIR`, `HUGGINGFACE_MODELS_DIR`, or
`UV_CACHE_DIR` remains authoritative. Remove or update old `/home/esup-runner/...`
overrides when changing `SERVICE_USER`.

Current built-in translation support:
- `fr -> en`
- `en -> fr`

Translation model selection is hardware-aware:
- CPU runners use lighter Marian models (`opus-mt-en-fr` / `opus-mt-fr-en`)
- GPU runners use larger `tc-big` Marian models (`opus-mt-tc-big-en-fr` / `opus-mt-tc-big-fr-en`)

As a result, translation quality can depend slightly on available resources, especially whether the runner is operating in GPU mode.

Compatibility note:
- For target languages outside the dedicated local `fr <-> en` translation pipeline, the runner falls back to Whisper's historical best-effort multilingual behavior.
- This keeps broader language coverage, but quality is usually less predictable than the dedicated local FR/EN translation models.


## Notifications
- `SMTP_SERVER`, `SMTP_PORT` (default `25`), `SMTP_SENDER`, `MANAGER_EMAIL`: Optional email settings for task failures and GPU health warnings. Set `MANAGER_EMAIL` to an administrator address or mailing list.

## Logging
- `LOG_DIR` (default `/var/log/esup-runner/`): Log base path; trailing slash is added if missing.
- Legacy alias: `LOG_DIRECTORY`.
- `LOG_LEVEL` (default `INFO`): `DEBUG|INFO|WARNING|ERROR|CRITICAL`.

## Completion callbacks
- `COMPLETION_NOTIFY_MAX_RETRIES` (int, default `5`): Max attempts to notify the Manager.
- `COMPLETION_NOTIFY_RETRY_DELAY_SECONDS` (int, default `60`): Delay between retries.
- `COMPLETION_NOTIFY_BACKOFF_FACTOR` (float, default `1.5`): Multiplier for retry backoff.

## Validation notes
- Explicit invalid boolean, integer, and floating-point values are rejected instead of being silently replaced or clamped.
- At startup, the runner validates required names and paths, HTTP(S) URLs, supported task types, complete instance port ranges, tokens, runtime modes, numeric limits, CORS compatibility, and Studio defaults. In GPU mode it also checks that `GPU_CUDA_PATH` exists.
- Run `uv run scripts/check_config.py` after changing `.env` to see all validation errors together. The summary intentionally excludes secrets such as `RUNNER_TOKEN`; exit code `0` means valid and `2` means invalid.
- When `RUNNER_TASK_TYPES` uses grouped syntax, the computed instance count overrides `RUNNER_INSTANCES` (a warning is logged if they differ).

## Related docs
- Task type grouping and multi-instance details: [docs/CONFIGURATION.md](CONFIGURATION.md)
- Transcription-specific behavior and exit codes: [docs/TYPE_TRANSCRIPTION.md](TYPE_TRANSCRIPTION.md)
- Cut parameter specifics for encoding tasks: [docs/TYPE_ENCODING.md](TYPE_ENCODING.md)
- Operations runbook: [docs/OPERATIONS.md](OPERATIONS.md)

## Live parameters

See [LIVE.md](LIVE.md) for the lifecycle, profiles and examples, and
[TYPE_LIVE.md](TYPE_LIVE.md) for the task request and output contract.
Time values are in seconds and bitrates are in bit/s.

| Parameter | Default |
| --- | --- |
| `LIVE_MEDIAMTX_API_URL` | `http://127.0.0.1:10997` |
| `LIVE_MEDIAMTX_API_USER` | `control` |
| `LIVE_MEDIAMTX_API_PASSWORD` | `` |
| `LIVE_POD_URL` | `` |
| `LIVE_POD_USERNAME` | `` |
| `LIVE_POD_PASSWORD` | `` |
| `LIVE_POD_ESTABLISHMENT_NAME` | `` (empty: all establishments; otherwise exact `task.etab_name` match) |
| `LIVE_UPSTREAM_RTMP_URL` | `` |
| `LIVE_LOCAL_RTMP_URL` | `rtmp://127.0.0.1:2935` |
| `LIVE_PUBLIC_HLS_BASE_URL` | Derived from `RUNNER_PROTOCOL://RUNNER_HOST:{port}/hls` |
| `LIVE_HLS_DIRECTORY` | `/dev/shm/hls` |
| `LIVE_TRANSCODER` | `cpu` (`cpu` for libx264, `nvenc` for NVIDIA NVENC) |
| `LIVE_CPU_FALLBACK` | `true` |
| `LIVE_NVDEC` | `false` |
| `LIVE_VARIANTS` | `HIGH 1280×720 1100000/128000 ; MID 720×406 560000/96000 ; LOW 480×270 280000/64000` |

`LIVE_TRANSCODER` selects Live encoding independently of `ENCODING_TYPE`.
The former `auto` value is no longer accepted. Set `LIVE_TRANSCODER=nvenc`
explicitly when upgrading a deployment that relied on `ENCODING_TYPE=GPU` to
select Live NVENC. `LIVE_NVDEC` enables hardware decoding only in NVENC mode;
it is ignored in CPU mode and after CPU fallback. Scaling remains on CPU.

Declare Live in `RUNNER_TASK_TYPES`, for example
`[1x(encoding,studio,transcription,live)]` for a mixed instance, or
`[1x(encoding,studio),2x(live)]` for VOD plus two concurrent Live instances.
All Live instances in this `.env` share MediaMTX, credentials, encoder settings,
variants and timeouts. `LIVE_PUBLIC_HLS_BASE_URL` accepts `{port}` (the actual
API port) and `{instance_id}` (the zero-based index including VOD groups);
resolved public URLs must differ. `LIVE_HLS_DIRECTORY` accepts `{instance_id}`; use `/dev/shm/hls` on Linux
for temporary HLS output in memory (see [storage and sizing](LIVE.md#hls-storage-in-memory-on-linux));
a static root is also valid because each session owns a UUID subdirectory.
Age cleanup is disabled for all groups sharing an environment with Live.
See [LIVE.md](LIVE.md) for direct and reverse-proxy examples.

### Internal Live defaults

These values are fixed in `app/core/live_config.py` and, for deferred Pod retries,
`app/services/pod_sync.py`; they are not environment
parameters. Remove their former `LIVE_*` assignments when upgrading: they are
no longer read. Keep deployment, hardware and rendition overrides from the
operator table above only when they differ from the defaults.

| Behavior | Fixed value | Former environment names (remove) |
| --- | --- | --- |
| Video/audio codecs | H.264 / AAC | `LIVE_VIDEO_CODEC`, `LIVE_AUDIO_CODEC` |
| NVENC / CPU preset | `p4` / `veryfast` | `LIVE_NVENC_PRESET`, `LIVE_CPU_PRESET` |
| Frame rate / GOP | 25 fps / 50 frames | `LIVE_FPS`, `LIVE_GOP` |
| HLS segment / playlist | 2 s / 6 segments | `LIVE_SEGMENT_SECONDS`, `LIVE_PLAYLIST_SIZE` |
| Source / startup timeout | 120 s / 60 s | `LIVE_SOURCE_TIMEOUT`, `LIVE_STARTUP_TIMEOUT` |
| Stall / stop timeout | 20 s / 10 s | `LIVE_STALL_TIMEOUT`, `LIVE_STOP_TIMEOUT` |
| Monitoring interval | 2 s | `LIVE_POLL_INTERVAL` |
| FFmpeg restart budget | 3 retries after the initial attempt; enabled CPU fallback switches to CPU after the first NVENC failure | `LIVE_RESTART_ATTEMPTS` |
| MediaMTX control timeout | 5 s | `LIVE_MEDIAMTX_TIMEOUT` |
| Deferred Pod status retries | 10 s pause between scans; at most 2 PATCHes per cycle | None (no environment override) |

Use [the connection helper](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify)
to fill the three installation-specific connection secrets. HLS storage and direct
playback URLs then work without additional Live assignments. A reverse proxy
or Docker port mapping may still need `LIVE_PUBLIC_HLS_BASE_URL`.

`LIVE_POD_URL`, `LIVE_POD_USERNAME` and `LIVE_POD_PASSWORD` are optional and must
be configured together to synchronize an Esup-Pod broadcaster. Set
`LIVE_POD_ESTABLISHMENT_NAME` to the exact `task.etab_name` for the Pod when the
Runner serves multiple establishments. A different establishment causes no Pod
client or request; leaving it empty preserves the single-establishment behavior.
This integration requires Pod 4.5.0's broadcaster `source_id` field ("Live source
identifier") and its REST filter. The Runner looks up
`GET <LIVE_POD_URL>/rest/broadcasters/?source_id=<identifier>`, checks every result
page for exactly one matching `source_id`, then sends
`PATCH <LIVE_POD_URL>/rest/broadcasters/<resolved-slug>/` with `{"status":true}`
when the source appears and `{"status":false}` during cleanup. The configured
account needs permission to read broadcasters and update their status. A missing
or ambiguous match causes no PATCH; there is no fallback to slug matching.
Pod API failures are logged without blocking MediaMTX cleanup or Runner
availability.

The Manager derives the internal `broadcaster_id` from the final component of
the RTMP source path and forwards it as the Live source identifier and
`source_id`. For automatic discovery,
the optional `equipment` publication parameter is separate metadata and is not
used as the Pod source identifier. The final RTMP path component must exactly
match the broadcaster's `source_id` in Pod; its slug can be different.
