# ESUP-Runner

<h1 align="center">
  <img src="manager/app/web/static/logo.png" alt="ESUP Runner logo" width="360" />
  <br>
  <br>

  [![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](https://github.com/EsupPortail/esup-runner/blob/main/LICENSE)
  [![Test](https://img.shields.io/github/actions/workflow/status/EsupPortail/esup-runner/ci.yml?branch=main&label=test)](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml?query=branch%3Amain)
  [![Lint](https://img.shields.io/github/actions/workflow/status/EsupPortail/esup-runner/ci.yml?branch=main&label=lint)](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml?query=branch%3Amain)
  [![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/EsupPortail/esup-runner/actions/workflows/ci.yml)
  [![Release](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fapi.github.com%2Frepos%2FEsupPortail%2Fesup-runner%2Ftags%3Fper_page%3D100&search=%22name%22%3A%22runner-v%28%5B0-9%5D%2B%5C.%5B0-9%5D%2B%5C.%5B0-9%5D%2B%29%22&replace=v%241&label=release&logo=github)](https://github.com/EsupPortail/esup-runner/tags)
  [![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](https://github.com/EsupPortail/esup-runner/blob/main/.github/workflows/ci.yml)
  [![Package Manager](https://img.shields.io/badge/package%20manager-uv-2E5E82)](https://docs.astral.sh/uv/)
  [![Docs](https://img.shields.io/badge/docs-manager%20%7C%20runner-informational)](https://github.com/EsupPortail/esup-runner#documentation)
</h1>

This GitHub repository contains **two distinct Python projects**:

- **Manager**: orchestration/admin service (source + packaging in `manager/`)
- **Runner**: execution agent/service (source + packaging in `runner/`)

Each project has its own `pyproject.toml`, documentation, release process, etc.

## Documentation

### Manager

- Docs home: [manager/docs/README.md](manager/docs/README.md)
- Installation: [manager/docs/INSTALLATION.md](manager/docs/INSTALLATION.md)
- Docker installation: [manager/docs/DOCKER.md](manager/docs/DOCKER.md)
- Upgrade: [manager/docs/UPGRADE.md](manager/docs/UPGRADE.md)
- Configuration: [manager/docs/CONFIGURATION.md](manager/docs/CONFIGURATION.md)
- Parameters: [manager/docs/PARAMETERS.md](manager/docs/PARAMETERS.md)
- Changelog: [manager/docs/CHANGELOG.md](manager/docs/CHANGELOG.md)
- Versioning: [manager/docs/VERSION_MANAGEMENT.md](manager/docs/VERSION_MANAGEMENT.md)

### Runner

- Docs home: [runner/docs/README.md](runner/docs/README.md)
- Installation: [runner/docs/INSTALLATION.md](runner/docs/INSTALLATION.md)
- Docker installation: [runner/docs/DOCKER.md](runner/docs/DOCKER.md)
- Upgrade: [runner/docs/UPGRADE.md](runner/docs/UPGRADE.md)
- Configuration: [runner/docs/CONFIGURATION.md](runner/docs/CONFIGURATION.md)
- Parameters: [runner/docs/PARAMETERS.md](runner/docs/PARAMETERS.md)
- Changelog: [runner/docs/CHANGELOG.md](runner/docs/CHANGELOG.md)
- Versioning: [runner/docs/VERSION_MANAGEMENT.md](runner/docs/VERSION_MANAGEMENT.md)

## License

This repository is licensed under the GPL-3.0 license. See [LICENSE](LICENSE).
