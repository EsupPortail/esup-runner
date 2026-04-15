# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
