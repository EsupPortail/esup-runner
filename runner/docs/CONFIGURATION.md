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
DEBUG=False

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
RUNNER_MONITORING=False

# Manager URL configuration
MANAGER_URL=http://127.0.0.1:8081

# API token authentication
RUNNER_TOKEN=change-me-runner-token

# Logs
LOG_DIR=/var/log/esup-runner
LOG_LEVEL=INFO

# Workspace and storage configuration
STORAGE_DIR=/tmp/esup-runner
MAX_VIDEO_SIZE_GB=0
MAX_FILE_AGE_DAYS=7
CLEANUP_INTERVAL_HOURS=24
EXTERNAL_SCRIPT_TIMEOUT_SECONDS=18000

# Encoding type (CPU or GPU)
ENCODING_TYPE=CPU

# Transcription (Whisper) settings
WHISPER_MODEL=turbo
CACHE_DIR=/home/esup-runner/.cache/esup-runner
WHISPER_LANGUAGE=auto
```

`WHISPER_LANGUAGE` now expresses the final subtitle language:
- `auto`: keep the detected spoken language
- explicit `fr` or `en`: request subtitles in that language; when it differs from the detected spoken language, the runner transcribes first and then translates the VTT

The built-in subtitle translation currently supports:
- `fr -> en`
- `en -> fr`

`CACHE_DIR` controls the shared cache root used by Whisper models (`CACHE_DIR/whisper-models`),
local translation models (`CACHE_DIR/huggingface`), and uv (`CACHE_DIR/uv`).

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

## Related docs
- Full environment variable reference: [docs/PARAMETERS.md](PARAMETERS.md)
