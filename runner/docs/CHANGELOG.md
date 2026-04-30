# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Updated `scripts/generate_tree_diagram.py` default ignore patterns to exclude cache/build artifacts and local data directories from rendered trees (`.uv-cache`, `data`, `*.egg-info`, `htmlcov`).
- Hardened source media download in `app/task_handlers/base_handler.py`: downloads now stream to a temporary `.part` file and are atomically moved to the final path only after full validation.
- Added defensive download checks in the shared handler: reject empty payloads, verify byte count against `Content-Length` when present, and retry transient failures with exponential backoff.
- Added a transcription input pre-check in `app/task_handlers/transcription/transcription_handler.py` using `ffprobe` before launching Whisper, with explicit failure messages when media is unreadable.
- Updated encoding input validation in `app/task_handlers/encoding/scripts/encoding.py` to raise `EncodingValidationError` as soon as the source media is missing or empty, aligning failure handling with CLI exit behavior.
- Expanded runner test coverage for streamed download retries, transcription `ffprobe` pre-check branches, and invalid-input encoding CLI paths.

### Fixed

- Fixed intermittent transcription failures caused by partially downloaded or zero-byte source files being treated as successful downloads.
- Fixed encoding failure reporting so missing or empty input media now produces an explicit non-zero error path instead of a silent early return.
- Fixed studio base generation failure when one mediapackage source is audio-only: mapping now follows the effective pipeline and no longer references `[vout]` when no mixed-video filter graph is built.

## [1.2.0] - 2026-04-22

### Added

- Added `GET /task/status/{task_id}` in `app/api/routes/task.py` so the manager can reconcile task state after outages.
- Added runner-side in-memory task-status tracking in `app/core/state.py` (`running`, `completed`, `failed`, `timeout`) keyed by `task_id`.
- Added an internal shared check-output helper in `app/core/_check_output.py` for consistent runner script status rendering.
- Added rendition bitrate fields (`video_bitrate`, `audio_bitrate`) with input validation in `encoding.py`.
- Added dynamic rendition heights (e.g. `2160`) for CPU/GPU FFmpeg commands and `info_video.json`.
- Added bitrate auto-inference when `video_bitrate`/`audio_bitrate` are omitted, including custom renditions.
- Added targeted encoding tests for validation/inference, dynamic selection, and thumbnail size limits.
- Added a download link for manager statistics CSV (`task_stats.csv`) with a date-stamped filename (`task_stats_YYYYMMDD.csv`) to avoid ambiguity.
- Added a dedicated runner operations runbook (`docs/OPERATIONS.md`) covering service runbook, health/readiness checks, multi-instance validation, storage/cache maintenance, and documentation cross-links.

### Changed

- Updated `STORAGE_DIR` to always default to `/tmp/esup-runner` when not explicitly set.
- Updated task execution flow to record status transitions during `run`/`process_task`, normalize `script_output` payloads, and expose optional `error_message`/`script_output` in status responses.
- Standardized `script_output` rendering across task types to the log-style sections `[info_script.log]` and `[error_script.log]` (including contextual labels for nested payloads such as studio/encoding stages).
- Updated encoding/studio task handlers to preserve meaningful `script_output.stdout` when external script stdout is empty by falling back to `encoding.log` content.
- Updated transcription `script_output` classification so non-error Whisper progress lines (`Loading weights:`) are moved from `stderr` to `stdout` and appear under `[info_script.log]`.
- Startup recovery now reconciles in-flight tasks after runner restart.
- Startup recovery now requalifies initially failed tasks as `completed` when workspace evidence confirms completion (manifest or final artifacts).
- Startup recovery now automatically restarts tasks that are genuinely failed.
- Runner availability is now preserved as `busy` during startup reconciliation/restart, including at manager registration.
- Unified runner check-script text output (`check_ffmpeg.py`, `check_gpu.py`, `check_runner_resources.py`, `check_runner_storage.py`, `check_version.py`) to the shared `✓ INFO` / `⚠ WARNING` / `✗ ERROR` format and aligned final conclusions.
- Moved check output formatting logic out of `scripts/` into the internal application module (`app/core/_check_output.py`).
- Updated runner metadata license reference from `LGPL 3.0` to `GPL 3.0` in `app/__version__.py`.
- Updated monorepo `update-stack.sh` with clearer step-based CLI output, concrete usage examples, and automatic `check_pipeline_tasks.py --with-transcription-translation` execution when runner sync mode targets transcription (`transcription-cpu`/`transcription-gpu`).
- Capped thumbnail extraction at `1280x720` (no upscale), including 1080p/4K sources.
- Updated `docs/TYPE_ENCODING.md` and `docs/TYPE_STUDIO.md` with bitrate fields and optional auto-inference.

### Fixed

- Improved recovery after manager unavailability by enabling post-restart status reconciliation from runner state.
- Adjusted `scripts/check_gpu.py` severity/exit behavior for CPU deployments: CUDA runtime unavailability is now non-blocking (`warning`, exit code `0`) when `ENCODING_TYPE=CPU`, while remaining blocking for `ENCODING_TYPE=GPU` (exit code `1`).
- Fixed 1080p HLS profile to use 1080 bitrate values (not 720).
- Improved transcription dependency failure reporting in `app/task_handlers/transcription/scripts/transcription.py`: missing `torch`/`whisper` now prints actionable remediation steps (`make sync-transcription-cpu|gpu`, service restart, runtime checks) instead of exposing a raw traceback.
- Added focused regression tests for the new transcription dependency-resolution messages and fallback branches, closing remaining coverage gaps in that script section.

## [1.1.1] - 2026-04-15

### Changed

- Renamed documentation pages `docs/RUNNER_CONFIGURATION.md` -> `docs/CONFIGURATION.md` and `docs/RUNNER_PARAMETERS.md` -> `docs/PARAMETERS.md`.
- Updated runner documentation links (`README.md`, `INSTALLATION.md`, `UPGRADE.md`, `PARAMETERS.md`) to follow the new naming.
- Created `scripts/check_gpu.py` to replace the former `make check-gpu` command.
- Added focused transcription-script regression tests for HF Hub warning filtering and CUDA runtime environment alignment (`_apply_runtime_cuda_environment`).

### Fixed

- Closed remaining coverage gaps in `app/task_handlers/transcription/scripts/transcription.py` for warning-filter fallback and CUDA env-application branches.

## [1.1.0] - 2026-04-13

### Added

- Added a shared cache-root configuration (`CACHE_DIR`) with explicit `UV_CACHE_DIR` support for uv package cache handling.
- Added aggregated cache checks in `scripts/check_runner_storage.py` when Whisper/Hugging Face/uv caches are grouped under `CACHE_DIR`.
- Added `UV_CACHE_DIR` storage validation behavior for missing-on-disk uv cache directories (validated against writable parent + free space).
- Added explicit `uv` extra conflicts between `transcription-cpu` and `transcription-gpu` in `pyproject.toml`.
- Documented the monorepo `update-stack.sh` automation workflow in the upgrade guide.

### Changed

- Introduced `LOG_DIR` as the preferred logging variable while keeping `LOG_DIRECTORY` as a backward-compatible alias.
- Updated runner configuration defaults so Whisper and Hugging Face model cache directories derive from `CACHE_DIR` unless explicitly overridden.
- Updated transcription CLI defaults to follow `CACHE_DIR` for cache subdirectories when specific cache env vars are not set.
- Updated `scripts/init.py` to provision `CACHE_DIR` and derived subdirectories (`whisper-models`, `huggingface`, `uv`) with de-duplicated directory creation.
- Consolidated Docker cache mounts from separate Whisper/Hugging Face volumes to a single cache volume/path model.
- Updated `Makefile` to export `UV_CACHE_DIR`, support `UV_LINK_MODE` during `uv sync`, and align Docker permission helpers with `CACHE_DIR`.
- Switched `create-service` and the shipped unit to `systemd --user` scope (`~/.config/systemd/user/esup-runner-runner.service`).
- Updated documentation for new env naming and current transcription platform support notes.
- Refreshed dependency locks in `runner/uv.lock`.

## [1.0.1] - 2026-04-10

### Security

- Upgraded `transformers` to a non-vulnerable range for transcription extras: `>=5.0.0rc3,<6.0.0` (for both `transcription-cpu` and `transcription-gpu`).
- Resolved Dependabot alert related to arbitrary code execution risk in `Trainer._load_rng_state()` when loading malicious checkpoint RNG files (e.g. `rng_state.pth`) in affected versions.
- Regenerated `runner/uv.lock` and updated resolved dependencies, including `transformers` to `5.5.1` and compatible transitive packages.
- Hardened task result filesystem access in `runner/app/api/routes/task.py` by validating `task_id` and result relative paths before file resolution.
- Added strict path boundary checks for manifest/result retrieval and deletion flows to prevent traversal/symlink escape patterns from user-controlled inputs.
- Refactored task result path resolution to traverse filesystem entries from trusted base directories (instead of composing paths from user input), improving robustness against `Uncontrolled data used in path expression` CodeQL alerts.
- Extended storage route security coverage with targeted regression tests in `runner/tests/test_storage_routes_coverage.py`.

## [1.0.0] - 2026-04-09

### Added

- Initial release of the ESUP Runner.
