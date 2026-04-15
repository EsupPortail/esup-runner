# Manager Configuration

This document describes the Manager runtime configuration (`manager/.env`) with a focus on authentication, security hardening, callback policies, and shared storage behavior.

## Configuration loading

- Configuration is read from environment variables (usually from `manager/.env`).
- Default `.env` path is `manager/.env` (project root + `manager/`).
- You can override the file path with:
  - `CONFIG_ENV_PATH=/path/to/.env`
  - `ENV_FILE=/path/to/.env` (legacy-compatible override)
- If no `.env` file is found, built-in defaults are used.

## Core manager settings

```properties
MANAGER_PROTOCOL=http
MANAGER_HOST=0.0.0.0
MANAGER_BIND_HOST=
MANAGER_PORT=8081
ENVIRONMENT=production
UVICORN_WORKERS=2
CLEANUP_TASK_FILES_DAYS=7
```

Behavior:
- `MANAGER_URL` is computed automatically as `MANAGER_PROTOCOL://MANAGER_HOST:MANAGER_PORT`.
- `MANAGER_BIND_HOST` controls the socket bind interface.
  - If unset and `MANAGER_HOST` is an IP (`127.0.0.1`, `10.x.x.x`, `::1`, etc.), manager binds on that IP.
  - If unset and `MANAGER_HOST` is a DNS hostname, manager binds on `0.0.0.0` for reliability.
- `CLEANUP_TASK_FILES_DAYS` controls cleanup retention for completed/failed task files.
- `UVICORN_WORKERS` is used in production process setups (Gunicorn/Uvicorn workers).

## Authentication

### API tokens (`AUTHORIZED_TOKENS__*`)

The Manager accepts API tokens from:
- `Authorization: Bearer <token>`
- `X-API-Token: <token>`

Configure tokens with environment variables prefixed by `AUTHORIZED_TOKENS__`:

```properties
AUTHORIZED_TOKENS__runners=CHANGE_ME_RUNNERS_TOKEN
AUTHORIZED_TOKENS__app=CHANGE_ME_APP_TOKEN
```

Notes:
- Suffix (for example `runners`, `app`) is just a label.
- If no token is configured, the manager logs a warning and protected API access will fail.

### Admin users (`ADMIN_USERS__*`)

`/admin` uses HTTP Basic auth with bcrypt hashes:

```properties
ADMIN_USERS__admin="$2b$12$CHANGE_ME_BCRYPT_HASH"
```

If no admin user is configured, the manager logs a warning and admin login is unavailable.

## OpenAPI docs visibility

OpenAPI/docs can be public or token-protected:

```properties
API_DOCS_VISIBILITY=public
OPENAPI_ALLOW_QUERY_TOKEN=false
```

Behavior:
- `API_DOCS_VISIBILITY=public`: `/docs`, `/redoc`, `/openapi.json` are publicly accessible.
- `API_DOCS_VISIBILITY=private`: OpenAPI routes require a valid API token.
- In private mode, tokens are read from headers first; query token (`?token=...`) is accepted only if `OPENAPI_ALLOW_QUERY_TOKEN=true`.
- Query tokens are not recommended for production because they can leak via logs/history.

## Logging and cache directories

```properties
LOG_DIR=/var/log/esup-runner
LOG_LEVEL=INFO
CACHE_DIR=/home/esup-runner/.cache/esup-runner
UV_CACHE_DIR=/home/esup-runner/.cache/esup-runner/uv
```

Notes:
- Legacy alias `LOG_DIRECTORY` is still supported.
- `LOG_DIR` is normalized with a trailing slash internally.
- If `UV_CACHE_DIR` is not set, default is `CACHE_DIR/uv`.

## Shared storage mode

```properties
RUNNERS_STORAGE_ENABLED=false
RUNNERS_STORAGE_DIR=/tmp/esup-runner
```

Behavior:
- `RUNNERS_STORAGE_ENABLED=false` (default): manager proxies result access via runners.
- `RUNNERS_STORAGE_ENABLED=true`: manager reads manifests/files from shared storage.
- Expected manifest location: `<RUNNERS_STORAGE_DIR>/<task_id>/manifest.json`.
- Legacy alias `RUNNERS_STORAGE_PATH` is still supported.
- If shared storage is enabled and directory is empty, startup validation raises an error.

## Domain-based priorities

```properties
PRIORITIES_ENABLED=false
PRIORITY_DOMAIN=example.org
MAX_OTHER_DOMAIN_TASK_PERCENT=25
```

When enabled, the manager can reserve runner capacity for a priority domain:
- A task is considered priority when its `notify_url` hostname is exactly `PRIORITY_DOMAIN` or one of its subdomains.
- Non-priority quota is computed from registered runner capacity:
  - `floor(capacity * MAX_OTHER_DOMAIN_TASK_PERCENT / 100)`
  - If `capacity > 0` and percentage `> 0`, at least `1` non-priority task is still allowed.
- `MAX_OTHER_DOMAIN_TASK_PERCENT` is clamped between `0` and `100`.
- If `PRIORITIES_ENABLED=true` but `PRIORITY_DOMAIN` is empty, priorities are automatically disabled with a warning.

## URL hardening policies

### Task callback URLs (`notify_url`)

```properties
NOTIFY_URL_ALLOWED_HOSTS=
NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false
```

Behavior:
- `NOTIFY_URL_ALLOWED_HOSTS` is an optional comma-separated allowlist.
- `localhost` is always blocked for `notify_url`.
- If `NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false` (default), callback targets resolving to private/loopback/link-local/multicast/reserved/unspecified IPs are rejected.

### Runner registration URLs

```properties
RUNNER_URL_ALLOWED_HOSTS=
RUNNER_URL_ALLOW_PRIVATE_NETWORKS=true
```

Behavior:
- `RUNNER_URL_ALLOWED_HOSTS` is an optional comma-separated allowlist.
- `RUNNER_URL_ALLOW_PRIVATE_NETWORKS=true` by default (common for internal runner networks).
- Set it to `false` to require runner URLs resolving to public IPs.

## CORS settings

```properties
CORS_ALLOW_ORIGINS=*
CORS_ALLOW_CREDENTIALS=false
CORS_ALLOW_METHODS=*
CORS_ALLOW_HEADERS=*
```

Notes:
- These settings apply to browser-origin calls (FastAPI `CORSMiddleware`).
- `CORS_ALLOW_ORIGINS` is a comma-separated list, for example:
  - `CORS_ALLOW_ORIGINS=https://ui.example.org,http://localhost:5173`
- Validation rule: `CORS_ALLOW_CREDENTIALS=true` is not allowed with `CORS_ALLOW_ORIGINS=*` (startup error).

## Completion notify retry tuning

```properties
COMPLETION_NOTIFY_MAX_RETRIES=5
COMPLETION_NOTIFY_RETRY_DELAY_SECONDS=60
COMPLETION_NOTIFY_BACKOFF_FACTOR=1.5
```

These values control retries for outbound completion callbacks to task `notify_url`.

## Optional SMTP notifications

```properties
SMTP_SERVER=smtp.example.org
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_SENDER=esup-runner@example.org
MANAGER_EMAIL=admin@example.org
```

Email notifications are active only when required SMTP fields are configured.

## Full `.env` example (copy/paste)

```properties
# Manager URL configuration
MANAGER_PROTOCOL=http
MANAGER_HOST=0.0.0.0
MANAGER_BIND_HOST=
MANAGER_PORT=8081

# Production/development settings
ENVIRONMENT=production
UVICORN_WORKERS=2

# Remove task files older than specified number of days
CLEANUP_TASK_FILES_DAYS=7

# Logs
# Legacy alias still supported: LOG_DIRECTORY
LOG_DIR=/var/log/esup-runner
LOG_LEVEL=INFO

# Shared runner storage
RUNNERS_STORAGE_ENABLED=false
# Legacy alias still supported: RUNNERS_STORAGE_PATH
RUNNERS_STORAGE_DIR=/tmp/esup-runner

# Shared cache directories
CACHE_DIR=/home/esup-runner/.cache/esup-runner
UV_CACHE_DIR=/home/esup-runner/.cache/esup-runner/uv

# Optional domain-based priorities
PRIORITIES_ENABLED=false
PRIORITY_DOMAIN=example.org
MAX_OTHER_DOMAIN_TASK_PERCENT=25

# OpenAPI visibility and token handling
API_DOCS_VISIBILITY=public
OPENAPI_ALLOW_QUERY_TOKEN=false

# CORS
CORS_ALLOW_ORIGINS=*
CORS_ALLOW_CREDENTIALS=false
CORS_ALLOW_METHODS=*
CORS_ALLOW_HEADERS=*

# Tokens accepted by manager API
AUTHORIZED_TOKENS__runners=CHANGE_ME_RUNNERS_TOKEN
AUTHORIZED_TOKENS__app=CHANGE_ME_APP_TOKEN

# Admin users (/admin): bcrypt hashes only
ADMIN_USERS__admin="CHANGE_ME_BCRYPT_HASH"

# Completion notify retry settings
COMPLETION_NOTIFY_MAX_RETRIES=5
COMPLETION_NOTIFY_RETRY_DELAY_SECONDS=60
COMPLETION_NOTIFY_BACKOFF_FACTOR=1.5

# Optional SMTP/email notifications
# SMTP_SERVER=smtp.example.org
# SMTP_PORT=587
# SMTP_USE_TLS=true
# SMTP_USERNAME=
# SMTP_PASSWORD=
# SMTP_SENDER=esup-runner@example.org
# MANAGER_EMAIL=admin@example.org

# Optional notify_url callback hardening
NOTIFY_URL_ALLOWED_HOSTS=
NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false

# Optional runner registration URL hardening
RUNNER_URL_ALLOWED_HOSTS=
RUNNER_URL_ALLOW_PRIVATE_NETWORKS=true
```

Boolean values accept common forms: `true/false`, `1/0`, `yes/no`, `on/off`.

## Related docs
- Full environment variable reference: [docs/PARAMETERS.md](PARAMETERS.md)
