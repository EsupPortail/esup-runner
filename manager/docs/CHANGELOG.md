# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Updated OpenAPI documentation references (`docs/CONFIGURATION.md`, `docs/PARAMETERS.md`) with detailed `OPENAPI_COOKIE_SECRET` behavior: signature purpose (integrity), non-encryption note, empty-value fallback, multi-instance recommendation, and secure secret generation example.
- Updated `scripts/generate_tree_diagram.py` default ignore patterns to exclude cache/build artifacts and local data directories from rendered trees (`.uv-cache`, `data`, `*.egg-info`, `htmlcov`).

### Fixed

- Fixed `scripts/check_pipeline_tasks.py` encoding smoke-test payload so `rendition` is sent as a valid JSON string (not a Python dict string), preventing runner warnings (`Failed to parse rendition parameter`) and unintended fallback to default rendition configuration.
- Fixed admin/runtime config reload propagation across Gunicorn workers: `/admin/reload-config` now publishes a shared reload marker so updated `.env` `ADMIN_USERS__*` / `AUTHORIZED_TOKENS__*` are picked up without restarting the manager service.
- Fixed private OpenAPI admin access flow to avoid clear-text token leakage in `/admin/docs` links and to authenticate docs via secure HttpOnly cookie (while keeping `?token=...` as optional fallback when `OPENAPI_ALLOW_QUERY_TOKEN=true`).
- Fixed private OpenAPI cookie hardening: auth cookie is now signed/opaque (no raw token value), with configurable TTL (`OPENAPI_COOKIE_MAX_AGE_SECONDS`) and optional per-request rotation (`OPENAPI_COOKIE_ROTATE_EACH_REQUEST`).
- Fixed `test_task_service_unit.py` persistence side effects by mocking `save_tasks()` in that test module, preventing orphan `run` tasks from being written to `data/YYYY-MM-DD/run.json` during test runs.

## [1.2.0] - 2026-04-22

### Added

- Added an hourly running-task reconciliation background service that polls each assigned runner via `GET /task/status/{task_id}`.
- Added an internal shared check-output helper in `app/core/_check_output.py` for consistent manager script status rendering.
- Added date-range filtering on the statistics dashboard so usage data can be analyzed over a selected period (`app/api/routes/statistics.py`, `app/web/templates/statistics.html`).
- Added a dedicated manager operations runbook (`docs/OPERATIONS.md`) and moved statistics maintenance guidance there (backup/edit/reset of `data/task_stats.csv`, period-based pruning examples), with README navigation updated accordingly.

### Changed

- Changed task-retention defaults from `30` to `60` days for `CLEANUP_TASK_FILES_DAYS` (`app/core/config.py`, `.env.example`, docs).
- Changed task cleanup semantics to apply retention to all task statuses (including `running`, `pending`, `warning`, `timeout`), not only `completed`/`failed` (`app/services/task_service.py`).
- Changed cleanup persistence behavior so expired tasks are deleted through persistence tombstones across day directories instead of memory-only removal (`app/core/state.py`, `app/services/task_service.py`).
- Re-enabled the admin config reload control in `app/web/templates/admin.html` and updated its status text to report the number of authorized tokens after reload.
- Updated task lifecycle handling so manager-side `running` tasks are reconciled from runner-reported statuses (`running`, `completed`, `failed`, `timeout`) and persisted automatically.
- Unified manager check-script text output (`check_runtime.py`, `check_version.py`, `check_pipeline_tasks.py`) to the shared `✓ INFO` / `⚠ WARNING` / `✗ ERROR` format and aligned final conclusions where applicable.
- Updated manager metadata/documentation license references from `LGPL 3.0` to `GPL 3.0` (`app/__version__.py`, `docs/README.md`, `docs/VERSION_MANAGEMENT.md`).
- Updated monorepo `update-stack.sh` with clearer step-based CLI output, concrete usage examples, and automatic `check_pipeline_tasks.py --with-transcription-translation` execution when runner sync mode targets transcription (`transcription-cpu`/`transcription-gpu`).
- Updated task-route test fixtures for completion/CSV scenarios to use explicit `test-task-*` identifiers instead of ambiguous `t1` IDs.
- Refreshed dependency lockfile in `manager/uv.lock`.

### Fixed

- Fixed stale historical tasks reappearing in admin/task snapshots by filtering production local-only entries using `CLEANUP_TASK_FILES_DAYS` retention on task age (`app/core/state.py`).
- Fixed recurring re-persistence of expired tasks into current-day directories by deleting expired task IDs from persistence (tombstone + file deletion) during retention cleanup (`app/services/task_service.py`, `app/core/persistence.py` integration via state helpers).
- Prevented stale `running` tasks after manager downtime by adding periodic reconciliation against runner task status.
- Improved `scripts/check_version.py` portability by gracefully skipping the OpenAPI assertion with a warning when `fastapi` is unavailable in minimal environments.
- Prevented `pytest`/`make ci` test runs from polluting `data/task_stats.csv` by skipping CSV appends in test execution context (`app/api/routes/task.py`).
- Excluded update smoke-test tasks (`etab_name = Quick manual test`) from `data/task_stats.csv` statistics so manual validation traffic is not counted as production usage (`app/api/routes/task.py`).
- Fixed PyLance typing error in persistence lock helpers by annotating returned/stored locks as `BaseFileLock` (instead of `FileLock`) to match `filelock` runtime aliasing on current platforms (`app/core/persistence.py`).

## [1.1.1] - 2026-04-15

### Security

- Hardened `scripts/check_runtime.py` report output to avoid clear-text logging findings from CodeQL (`Clear-text logging of sensitive information`) by hiding runtime configuration values and token content.

### Added

- Added a dedicated manager runtime configuration guide in `docs/CONFIGURATION.md`, including `.env` variable behavior, security hardening options, and a full copy/paste configuration example.
- Added a dedicated manager parameter reference in `docs/PARAMETERS.md`, aligned with the runner documentation style.

### Changed

- Updated documentation navigation to reference `docs/CONFIGURATION.md` and `docs/PARAMETERS.md` from manager README/installation/upgrade docs and the repository root `README.md`.
- Updated `scripts/generate_password.py` to accept admin labels containing `.`, `-`, and `@` (email-compatible), and aligned the installation guide example accordingly.
- Kept runtime report diagnostics useful by showing minimal non-sensitive status (`configured`/`missing`) while preserving hidden values.
- Preserved compatibility for existing runtime-check tests and helper contracts (`_mask_secret`, context keys) while applying the logging hardening.

## [1.1.0] - 2026-04-13

### Added

- Added support for `CACHE_DIR` and `UV_CACHE_DIR` in configuration/bootstrap flows so cache directories can be managed explicitly.
- Documented the monorepo `update-stack.sh` automation workflow in the upgrade guide.

### Changed

- Introduced `LOG_DIR` and `RUNNERS_STORAGE_DIR` as the preferred environment variable names while keeping `LOG_DIRECTORY` and `RUNNERS_STORAGE_PATH` as backward-compatible aliases.
- Updated shared storage path resolution in task result routes to accept both legacy/new names and emit clearer configuration errors.
- Updated `scripts/init.py` to create directories from the new env naming (`LOG_DIR`, `RUNNERS_STORAGE_DIR`, `CACHE_DIR`, `UV_CACHE_DIR`) with legacy alias support.
- Updated Docker/installation documentation and examples to use the new env variable names and compatibility notes.
- Updated `Makefile` to export `UV_CACHE_DIR` and support `UV_LINK_MODE` during `uv sync`.
- Switched `create-service` and the shipped unit to `systemd --user` scope (`~/.config/systemd/user/esup-runner-manager.service`), including manager service helper scripts.
- Refreshed dependency locks in `manager/uv.lock`.

## [1.0.1] - 2026-04-10

### Security

- Removed token content from unauthorized authentication logs in `manager/app/core/auth.py` to avoid clear-text exposure of sensitive API/Bearer token values.
- Kept existing authentication behavior (constant-time token comparison and HTTP 401 responses) while hardening log hygiene.

## [1.0.0] - 2026-04-09

### Added

- Initial release of the ESUP Runner Manager.
