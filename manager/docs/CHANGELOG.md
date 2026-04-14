# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added a dedicated manager runtime configuration guide in `docs/MANAGER_CONFIGURATION.md`, including `.env` variable behavior, security hardening options, and a full copy/paste configuration example.

### Changed

- Updated documentation navigation to reference the new manager configuration guide from `manager/docs/README.md` and the repository root `README.md`.

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
