# Version Management Guide — Runner

This page explains where the Runner version is defined, how to bump it, and how to retrieve it from the API.

## Overview

The Runner follows **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`

- **MAJOR**: incompatible changes
- **MINOR**: backward-compatible feature additions
- **PATCH**: backward-compatible bug fixes

## Compatibility with the Manager

The Runner includes its version in the `X-Runner-Version` header when registering and sending heartbeats to the Manager.

The Manager enforces **MAJOR + MINOR** compatibility:

- Runner `X.Y.*` is accepted only by a Manager `X.Y.*`
- `PATCH` versions may differ

If you bump the Runner `MAJOR` or `MINOR`, you must deploy a compatible Manager `MAJOR.MINOR` as well.

## Version sources (must stay in sync)

The project keeps the version in a few places:

- `app/__version__.py`: runtime version (imported by the app)
- `VERSION`: plain-text version (useful for packaging/deploy scripts)
- `pyproject.toml` (`[project].version`): Python package version

The API/OpenAPI version is derived from `app/__version__.py` via `OpenAPIConfig.VERSION`.

## Using the version management script

The helper script updates all required version files for you.

Show current version:

```bash
uv run scripts/manage_version.py show
```

Bump patch/minor/major:

```bash
uv run scripts/manage_version.py bump patch
uv run scripts/manage_version.py bump minor
uv run scripts/manage_version.py bump major
```

Set a specific version:

```bash
uv run scripts/manage_version.py set 2.1.3
```

If you don't use `uv`, the script also works with Python:

```bash
python3 scripts/manage_version.py show
```

## Release workflow (suggested)

1. Bump version (choose patch/minor/major):

   ```bash
   uv run scripts/manage_version.py bump patch
   ```

2. Update your release notes/changelog (if you keep one).
3. Commit:

   ```bash
   git add app/__version__.py VERSION pyproject.toml
   git commit -m "chore: bump version to X.Y.Z"
   ```

4. Tag and push:

   ```bash
   git tag -a vX.Y.Z -m "Release X.Y.Z"
   git push && git push --tags
   ```

## Version checks

### From Python

```python
from app import __version__

print(__version__)
```

### From the API

The Runner does **not** expose a `GET /api/version` endpoint.

Use one of these instead:

1) Root endpoint:

```bash
curl http://localhost:8081/
```

Expected response (example):

```json
{
  "message": "Runner API",
  "version": "1.0.0",
  "documentation": {
    "swagger": "/docs",
    "redoc": "/redoc",
    "openapi": "/openapi.json"
  },
  "health_check": "/runner/health"
}
```

2) OpenAPI document:

```bash
curl -s http://localhost:8081/openapi.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
```

### From the VERSION file

```bash
cat VERSION
```

## Authentication notes (for API calls)

- `GET /` and `GET /runner/*` endpoints are currently public.
- `/task/*` endpoints are protected and require the runner token.

Supported auth headers for protected endpoints:

- `X-API-Token: <RUNNER_TOKEN>`
- `Authorization: Bearer <RUNNER_TOKEN>`

Example:

```bash
curl -H "X-API-Token: $RUNNER_TOKEN" http://localhost:8081/task/status/some-task-id
```

## Troubleshooting

If the displayed version looks inconsistent, run:

```bash
python3 scripts/check_version.py
```

It checks consistency between:

- `app/__version__.py`
- `VERSION`
- `pyproject.toml`
- OpenAPI version (`/openapi.json`)

## References

- https://semver.org/
- https://git-scm.com/book/en/v2/Git-Basics-Tagging
