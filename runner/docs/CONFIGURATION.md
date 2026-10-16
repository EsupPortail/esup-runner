# Runner Configuration

This document describes the Runner runtime configuration with a focus on **multi-instance** deployments and how to assign **task types per instance**.

## Multi-instance basics

- The Runner can start multiple FastAPI/Uvicorn processes ("instances") from the launcher.
- Each instance gets its own `RUNNER_INSTANCE_ID` (0-based) and `RUNNER_PORT` (derived from `RUNNER_BASE_PORT`).
- Each instance registers itself to the Manager with its own URL and supported `task_types`.

## Task types

Task types are configured via `RUNNER_TASK_TYPES`.

### Legacy syntax (same task types for all instances)

```properties
RUNNER_INSTANCES=2
RUNNER_TASK_TYPES=encoding,studio,transcription
```

Behavior:
- The launcher starts `RUNNER_INSTANCES` processes.
- All instances expose the **same** task types.

### Grouped syntax (different task types per instance)

```properties
RUNNER_TASK_TYPES=[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]
```

Behavior:
- The number of instances is computed from the sum of multipliers (here: 2 + 1 + 1 = 4).
- The mapping is positional and follows the expansion order:
  - instance 0: `encoding, studio, transcription`
  - instance 1: `encoding, studio, transcription`
  - instance 2: `encoding, studio`
  - instance 3: `transcription`

Notes:
- Multipliers must be `>= 1`.
- Each group must contain at least one task type.
- Whitespace is allowed.
- Brackets `[…]` are optional (both forms are accepted).

## RUNNER_INSTANCES interaction

When `RUNNER_TASK_TYPES` uses the grouped syntax, `RUNNER_INSTANCES` is effectively **ignored** at runtime (the computed total is used).

- If `RUNNER_INSTANCES` is set and does not match the computed total, a warning is emitted.
- Recommended: either omit `RUNNER_INSTANCES` or keep it consistent with the grouped total.

## Validate before startup

After changing `.env`, run the configuration preflight before restarting the
Runner:

```bash
uv run scripts/check_config.py
```

The command uses the same loader and validators as the Runner, reports all
detected errors together, excludes secrets from its summary, and returns exit
code `0` when valid or `2` when invalid.

## HAProxy and other reverse proxies

When publishing the Runner through HAProxy, explicitly configure the trusted
proxy addresses in `runner/.env`. Configure `manager/.env` too if the Manager
is published through the proxy:

```dotenv
# Replace with the HAProxy source IP as seen by this application.
FORWARDED_ALLOW_IPS=10.0.0.10
```

`FORWARDED_ALLOW_IPS` is a native Uvicorn/Gunicorn environment setting. The
ESUP-Runner launchers load `.env` before starting these servers, so this setting
works without an application configuration field or code change. It controls
which peers may supply `X-Forwarded-Proto` and `X-Forwarded-For`, allowing the
application to recognize HTTPS and the original client IP, including for HLS
viewer tracking.

Use the proxy's source IP after any NAT, rather than the public DNS name or
the Runner's destination address. For redundant proxies, use a comma-separated
list such as `10.0.0.10,10.0.0.11`. IP addresses and CIDR networks are supported;
prefer individual proxy IPs. The server default is `127.0.0.1,::1`, which covers
loopback connections only. Avoid `*`, which trusts forwarding headers from any
peer, and restrict direct access to the application ports.

HAProxy must set the forwarding headers and preserve the public `Host` header;
see the [Live HAProxy example](LIVE.md#haproxy-with-one-rule-for-multiple-instances).
Restart the affected services completely after changing this setting; an
application configuration reload does not reconfigure the HTTP servers.
See the [Uvicorn HTTP settings](https://uvicorn.dev/settings/#http).

## Example configuration

```properties
RUNNER_PROTOCOL=http
RUNNER_HOST=127.0.0.1
RUNNER_BASE_PORT=8082
RUNNER_BASE_NAME=my-runner

RUNNER_TASK_TYPES=[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]
```

This starts 4 runner instances on ports 8082..8085 with the task-type mapping shown above.

## Full .env example (copy/paste)

```properties
# DEBUG mode
DEBUG=false

# Runner/Multi-instance configuration
RUNNER_PROTOCOL=http
RUNNER_HOST=127.0.0.1
RUNNER_BASE_PORT=8082
RUNNER_BASE_NAME=my-runner

# Task types managed by this runner
# Grouped syntax example (total instances = 2 + 1 + 1 = 4)
RUNNER_TASK_TYPES=[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]

# Optional: keep consistent with grouped total to avoid a warning
RUNNER_INSTANCES=4

# Monitor instances and automatically restart failed ones
RUNNER_MONITORING=false

# Manager API URL used by this Runner.
# Match the Manager's MANAGER_PROTOCOL/HOST/PORT or MANAGER_PUBLIC_URL.
# Prefer the private API; a public proxy must expose the Manager API too.
MANAGER_URL=http://127.0.0.1:8081
# Optional public Manager base URL for the "Open manager" button in emails.
# /admin is appended unless already present; defaults to MANAGER_URL.
# MANAGER_PUBLIC_URL=https://admin.example.org/runner-manager

# API token authentication
RUNNER_TOKEN=change-me-runner-token

# Optional service account (default: esup-runner)
SERVICE_USER=esup-runner

# Logs
LOG_DIR=/var/log/esup-runner
LOG_LEVEL=INFO

# Workspace and storage configuration
STORAGE_DIR=/tmp/esup-runner
MAX_VIDEO_SIZE_GB=0
MAX_FILE_AGE_DAYS=7
CLEANUP_INTERVAL_HOURS=24
EXTERNAL_SCRIPT_TIMEOUT_SECONDS=18000
TASK_PROCESS_CHECK_INTERVAL_SECONDS=3600
MEDIA_CODEC_DENYLIST=magicyuv

# Encoding type (CPU or GPU)
ENCODING_TYPE=CPU

# Transcription (Whisper) settings
WHISPER_MODEL=turbo
# Optional; defaults to /home/{SERVICE_USER}/.cache/esup-runner
# CACHE_DIR=/srv/esup-runner/cache
WHISPER_LANGUAGE=auto
# HF_TOKEN=hf_xxx
```

The Manager advertises both its private and public completion endpoints. The
Runner accepts tasks when either endpoint matches its own `MANAGER_URL` followed
by `/task/completion`. It always sends notifications to its own configured
endpoint and does not follow redirects. A mismatch returns HTTP `422` before
execution, with the expected and
advertised addresses and the configuration variables to check. This error is
visible in the Manager task and the pipeline smoke-test output.

`WHISPER_LANGUAGE` now expresses the final subtitle language:
- `auto`: keep the detected spoken language
- explicit `fr` or `en`: request subtitles in that language; when it differs from the detected spoken language, the runner transcribes first and then translates the VTT

The built-in subtitle translation currently supports:
- `fr -> en`
- `en -> fr`

`SERVICE_USER` selects the Unix account used by `make init`, `make create-service`, and
`update-stack.sh`. It defaults to `esup-runner`. `CACHE_DIR` defaults to
`/home/{SERVICE_USER}/.cache/esup-runner` and controls the shared cache root used by Whisper models (`CACHE_DIR/whisper-models`),
local translation models (`CACHE_DIR/huggingface`), and uv (`CACHE_DIR/uv`).
`HF_TOKEN` is optional but recommended on production runners: it enables authenticated Hugging Face downloads, which reduces rate-limit warnings and can speed up model retrieval.

Legacy alias for logs is still supported: `LOG_DIRECTORY`.

The translation backend is selected automatically from the available hardware:
- CPU mode: lighter Marian translation models
- GPU mode: larger `tc-big` Marian translation models

Because of that, translation quality may vary slightly between CPU and GPU deployments. The runner writes the effective languages and translation model used for each task into `info_video.json`.

For target languages outside the dedicated local `fr <-> en` translation pipeline, the runner keeps backward compatibility by falling back to Whisper's legacy best-effort multilingual behavior. This broadens language coverage, but quality is generally less predictable than the dedicated local FR/EN translation path.

For `WHISPER_CHUNK_THRESHOLD_SECONDS`, the runner uses a hardware-aware default when the variable is unset:
- `800` on CPU
- `1800` on GPU

Set `WHISPER_CHUNK_THRESHOLD_SECONDS` only if you explicitly need to override that behavior.

## Media denylist

`MEDIA_CODEC_DENYLIST` is a comma-separated runner-level codec denylist. The default is `magicyuv`.

When `magicyuv` is present, the runner performs a small binary-signature check immediately after a media file is downloaded and before any FFmpeg, ffprobe, or Whisper processing touches that file. Matching media is rejected with an explicit message asking the user to convert it to H.264/H.265/VP9/AV1 first.

Set `MEDIA_CODEC_DENYLIST=` only if you deliberately want to disable this protection.

## Related docs
- Full environment variable reference: [docs/PARAMETERS.md](PARAMETERS.md)
- Operations runbook: [docs/OPERATIONS.md](OPERATIONS.md)

## Live streaming

See [LIVE.md](LIVE.md) for explicit `live` declarations in `runner/.env`, mixed or grouped instances, MediaMTX, adaptive transcoding and recovery.
See [TYPE_LIVE.md](TYPE_LIVE.md) for the task parameters, request examples and HLS outputs.

### Simplified Live setup

After preparing MediaMTX, run the [connection helper](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify)
to populate the installation-specific credentials in the existing application
`.env`. Keep only operator choices that differ from defaults. Technical Live
timeouts and media tuning are internal defaults, documented in
[PARAMETERS.md](PARAMETERS.md#internal-live-defaults), and are no longer read from
`LIVE_*` environment overrides. No additional application environment file is needed.
