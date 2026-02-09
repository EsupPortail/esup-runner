<p align="center">
	<img src="manager/app/web/static/logo.png" alt="ESUP Runner logo" width="360" />
</p>

# ESUP Runner

[![CI](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml/badge.svg)](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/github/actions/workflow/status/EsupPortail/esup-runner/ci.yml?branch=main&label=coverage)](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml)

This GitHub repository contains **two distinct Python projects**:

- **Manager**: orchestration/admin service (source + packaging in `manager/`)
- **Runner**: execution agent/service (source + packaging in `runner/`)

Each project has its own `pyproject.toml`, documentation, release process, etc.

## Documentation

### Manager

- Docs home: [manager/docs/README.md](manager/docs/README.md)
- Installation: [manager/docs/INSTALLATION.md](manager/docs/INSTALLATION.md)
- Upgrade: [manager/docs/UPGRADE.md](manager/docs/UPGRADE.md)
- Changelog: [manager/docs/CHANGELOG.md](manager/docs/CHANGELOG.md)
- Versioning: [manager/docs/VERSION_MANAGEMENT.md](manager/docs/VERSION_MANAGEMENT.md)

### Runner

- Docs home: [runner/docs/README.md](runner/docs/README.md)
- Installation: [runner/docs/INSTALLATION.md](runner/docs/INSTALLATION.md)
- Upgrade: [runner/docs/UPGRADE.md](runner/docs/UPGRADE.md)
- Configuration: [runner/docs/RUNNER_CONFIGURATION.md](runner/docs/RUNNER_CONFIGURATION.md)
- Versioning: [runner/docs/VERSION_MANAGEMENT.md](runner/docs/VERSION_MANAGEMENT.md)

## Contributors (development)

Prerequisites: Python >= 3.11, `uv`, `make`.

Recommended workflow uses the project Makefiles (they wrap `uv` and handle extras):

- Manager:
	- Install deps: `cd manager && make sync-dev`
	- Run tests: `cd manager && make test` (or `make coverage`)
	- Run in dev: `cd manager && uv run esup-runner-manager-dev`

- Runner:
	- Install deps: `cd runner && make sync-dev`
	- (Optional) transcription deps: `cd runner && make sync-all` (or `make sync EXTRAS=transcription`)
	- Run tests: `cd runner && make test`
	- Run in dev: `cd runner && uv run esup-runner-runner-dev`

If you need fully reproducible installs, you can call `uv` directly with the lockfile:

- Manager: `cd manager && uv sync --locked --extra dev`
- Runner: `cd runner && uv sync --locked --extra dev`

Build/test commands and production setup details are documented in the pages above.

## License

This repository is licensed under the GPL-3.0 license. See [LICENSE](LICENSE).
