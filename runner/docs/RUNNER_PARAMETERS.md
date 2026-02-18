# Runner Parameters

This page summarizes the environment variables consumed by the runner. Values are parsed in [app/core/config.py](../app/core/config.py) and validated at startup.

## Quick start (.env snippet)
```properties
DEBUG=False
RUNNER_PROTOCOL=http
RUNNER_HOST=127.0.0.1
RUNNER_BASE_PORT=8082
RUNNER_BASE_NAME=my-runner
RUNNER_TASK_TYPES=[2x(encoding,studio,transcription)]
RUNNER_TOKEN=change-me-runner-token
MANAGER_URL=http://127.0.0.1:8081
STORAGE_DIR=/tmp/esup-runner/storage
EXTERNAL_SCRIPT_TIMEOUT_SECONDS=18000
LOG_DIRECTORY=/var/log/esup-runner
LOG_LEVEL=INFO
ENCODING_TYPE=CPU
GPU_CUDA_PATH=/usr/local/cuda-13.2
WHISPER_MODEL=turbo
WHISPER_LANGUAGE=auto
```

## Core runner
- `DEBUG` (bool, default `False`): Enables verbose logging and debug flags passed to scripts.
- `RUNNER_PROTOCOL` (default `http`), `RUNNER_HOST` (default `localhost`), `RUNNER_BASE_PORT` (default `8081`), `RUNNER_BASE_NAME` (default `default-runner`): Base URL components. The launcher offsets the port per instance.
- `RUNNER_INSTANCES` (int, default `1`): Number of instances when using legacy task-type syntax.
- `RUNNER_TASK_TYPES` (CSV or grouped syntax): Task types handled. Legacy: `encoding,studio,transcription`. Grouped: `[2x(encoding,studio,transcription),1x(encoding,studio)]` (preferred for per-instance mapping).
- `RUNNER_MONITORING` (bool, default `False`): If true, the launcher monitors and restarts instances.

## Networking and auth
- `RUNNER_TOKEN` (required): Token used to authenticate runner <-> manager calls in both directions.
- `MANAGER_URL` (default `http://localhost:8000`): Manager base URL for registration and callbacks.

## Storage and cleanup
- `STORAGE_DIR` (default `/tmp/esup-runner/storage`): Root workspace for task data.
- `MAX_VIDEO_SIZE_GB` (int, default `0` = unlimited): Reject downloads above this size.
- `MAX_FILE_AGE_DAYS` (int, default `0` = keep forever): Cleanup threshold.
- `CLEANUP_INTERVAL_HOURS` (int, default `24`): Periodic cleanup interval.
- `EXTERNAL_SCRIPT_TIMEOUT_SECONDS` (int, default `18000`): Timeout (in seconds) for external scripts run by `encoding`, `studio`, and `transcription` handlers.

## Encoding / hardware
- `ENCODING_TYPE` (`CPU` | `GPU`, default `CPU`): Selects CPU or GPU path for encoding tasks.
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
- `WHISPER_MODELS_DIR` (default `/tmp/esup-runner/whisper-models`): Cache directory.
- `WHISPER_LANGUAGE` (default `auto`): Default language; can be overridden per task.

## Notifications
- `SMTP_SERVER`, `SMTP_PORT` (default `25`), `SMTP_SENDER`, `MANAGER_EMAIL`: Optional email settings for failure notifications.

## Logging
- `LOG_DIRECTORY` (default `/var/log/esup-runner/`): Log base path; trailing slash is added if missing.
- `LOG_LEVEL` (default `INFO`): `DEBUG|INFO|WARNING|ERROR|CRITICAL`.

## Completion callbacks
- `COMPLETION_NOTIFY_MAX_RETRIES` (int, default `5`): Max attempts to notify the Manager.
- `COMPLETION_NOTIFY_RETRY_DELAY_SECONDS` (int, default `60`): Delay between retries.
- `COMPLETION_NOTIFY_BACKOFF_FACTOR` (float, default `1.5`): Multiplier for retry backoff.

## Validation notes
- At startup, the runner checks that at least one instance and task type are defined, ports are in the 80–65535 range, and tokens are non-default. In GPU mode it also checks `GPU_CUDA_PATH` exists.
- When `RUNNER_TASK_TYPES` uses grouped syntax, the computed instance count overrides `RUNNER_INSTANCES` (a warning is logged if they differ).

## Related docs
- Task type grouping and multi-instance details: [docs/RUNNER_CONFIGURATION.md](RUNNER_CONFIGURATION.md)
- Cut parameter specifics for encoding tasks: [docs/TYPE_ENCODING.md](TYPE_ENCODING.md)
