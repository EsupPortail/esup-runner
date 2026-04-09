# Version Management Guide — Runner

## 📌 Overview

This project follows **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

- **MAJOR**: incompatible changes
- **MINOR**: backward-compatible feature additions
- **PATCH**: backward-compatible bug fixes

## 🤝 Compatibility with the Manager

The Runner sends its version in the `X-Runner-Version` header when registering and sending heartbeats to the Manager.

The Manager enforces **MAJOR + MINOR** compatibility:

- Runner `X.Y.*` is accepted only by a Manager `X.Y.*`
- `PATCH` versions may differ

If you bump Runner `MAJOR` or `MINOR`, deploy a compatible Manager `MAJOR.MINOR` too.

## 📁 Version files

### Primary files

- `app/__version__.py` — single source of truth at runtime
- `VERSION` — plain text version number
- `CHANGELOG.md` — release notes history

### Configuration files

- `pyproject.toml` — package metadata (`[project].version`) kept in sync by `manage_version.py`

### API metadata source

- OpenAPI version is derived from `app/__version__.py` via `OpenAPIConfig.VERSION`

## 🛠️ Using the management script

### Show current version

```bash
uv run scripts/manage_version.py show
```

### Bump the version

#### Patch (1.0.0 → 1.0.1)

For bug fixes:

```bash
uv run scripts/manage_version.py bump patch
```

#### Minor (1.0.0 → 1.1.0)

For backward-compatible features:

```bash
uv run scripts/manage_version.py bump minor
```

#### Major (1.0.0 → 2.0.0)

For breaking changes:

```bash
uv run scripts/manage_version.py bump major
```

### Set a specific version

```bash
uv run scripts/manage_version.py set 2.1.3
```

### Without `uv`

```bash
python3 scripts/manage_version.py show
```

## 📝 Release process

### 1. Update the version

```bash
# Example for a patch release
uv run scripts/manage_version.py bump patch
```

### 2. Update CHANGELOG.md

Document the release with the usual sections (Added, Changed, Deprecated, Removed, Fixed, Security).

### 3. Commit changes

```bash
git add app/__version__.py VERSION pyproject.toml CHANGELOG.md
git commit -m "chore: bump version to X.Y.Z"
```

### 4. Create a Git tag

```bash
git tag -a runner-vX.Y.Z -m "Esup-Runner Runner release X.Y.Z"
```

### 5. Push changes

```bash
git push origin main
git push origin runner-vX.Y.Z
```

## 🔍 Version checks

### From Python

```python
from app import __version__
print(__version__)
```

### From the API

The Runner does **not** expose a `GET /api/version` endpoint.
Default local port is `8082` (`RUNNER_BASE_PORT` in `.env.example`).

Use one of these instead:

```bash
curl http://localhost:8082/
```

```bash
curl -s http://localhost:8082/openapi.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
```

### From the VERSION file

```bash
cat VERSION
```

## 📊 Version endpoints

### GET /

Root endpoint returns the current version in the response payload.

### GET /openapi.json

OpenAPI `info.version` reflects `app/__version__.py`.

### GET /api/version

Not available in the Runner API.

## 🔐 Authentication notes (API calls)

- `GET /` and `GET /runner/*` endpoints are public
- `/task/*` endpoints require the runner token

Supported auth headers for protected endpoints:

- `X-API-Token: <RUNNER_TOKEN>`
- `Authorization: Bearer <RUNNER_TOKEN>`

Example:

```bash
curl -H "X-API-Token: $RUNNER_TOKEN" http://localhost:8082/task/status/some-task-id
```

## ⚠️ Best practices

1. **Always** update `CHANGELOG.md` before tagging
2. **Never** edit version values manually in multiple files
3. **Use** `manage_version.py` for every version bump
4. **Create** a Git tag for each release
5. **Keep** Runner and Manager `MAJOR.MINOR` compatibility in mind

## 🆘 Troubleshooting

### The displayed version is inconsistent

```bash
python3 scripts/check_version.py
```

This checks consistency between:

- `app/__version__.py`
- `VERSION`
- `pyproject.toml`
- OpenAPI version (`/openapi.json`)

### Reset to a specific version

```bash
uv run scripts/manage_version.py set 1.0.0
```

## 🔗 References

- https://semver.org/
- https://git-scm.com/book/en/v2/Git-Basics-Tagging
