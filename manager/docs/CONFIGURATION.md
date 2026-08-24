# Manager Configuration

This document describes the Manager runtime configuration (`manager/.env`) with a focus on authentication, security hardening, callback policies, and shared storage behavior.

## Configuration loading

- Configuration is read from environment variables (usually from `manager/.env`).
- Default `.env` path is `manager/.env` (project root + `manager/`).
- You can override the file path with:
  - `CONFIG_ENV_PATH=/path/to/.env`
  - `ENV_FILE=/path/to/.env` (legacy-compatible override)
- If no `.env` file is found, built-in defaults are used.

## Validate before startup

After changing `.env`, run the configuration preflight before starting or
restarting the Manager:

```bash
uv run scripts/check_config.py
```

The command uses the same loader and validators as the Manager, reports all
detected errors together, excludes credential values from its summary, and
returns exit code `0` when valid or `2` when invalid. The same validation runs at
startup and before a hot-reloaded configuration replaces the live one.

## Core manager settings

```properties
MANAGER_PROTOCOL=http
MANAGER_HOST=127.0.0.1
MANAGER_PUBLIC_URL=http://127.0.0.1:8081
MANAGER_BIND_HOST=0.0.0.0
MANAGER_PORT=8081
ENVIRONMENT=production
UVICORN_WORKERS=2
CLEANUP_TASK_FILES_DAYS=60
```

The five addressing variables deliberately describe three different network
views of the same Manager:

| Variable | Purpose | How to choose it |
| --- | --- | --- |
| `MANAGER_PROTOCOL` | Scheme of the private Manager API URL (`http` or `https`). | Use the protocol through which Runners actually reach the private API. This setting only builds URLs; it does not enable TLS in Uvicorn/Gunicorn. |
| `MANAGER_HOST` | Private DNS name or IP advertised for Runner-to-Manager communication. | Use a value resolvable and reachable from every Runner, such as `manager.internal` or a private IP. Never advertise `0.0.0.0`; use `127.0.0.1` only when every Runner is on the same host. |
| `MANAGER_PORT` | Port of the private API and local Uvicorn/Gunicorn listening socket. | Use the port on which the Manager process listens, normally `8081`. A public reverse proxy can expose another port, commonly `443`. |
| `MANAGER_BIND_HOST` | Local interface on which Uvicorn/Gunicorn accepts connections. | Use `127.0.0.1` for same-host access only, a specific local interface address to restrict listening, or `0.0.0.0` for every IPv4 interface. This value is never advertised to Runners. |
| `MANAGER_PUBLIC_URL` | Complete browser-facing URL of the administration interface. | Set it to the external reverse-proxy URL, including its scheme, optional non-default port and optional path prefix. Runners never use this URL. |

`MANAGER_PROTOCOL`, `MANAGER_HOST` and `MANAGER_PORT` are combined into the
computed private `MANAGER_URL`. The Manager puts
`MANAGER_URL/task/completion` in dispatched tasks; Runners must be able to call
that address. A Runner's own `MANAGER_URL` must identify the same private API so
that registration, heartbeats and health checks use the private network.

`MANAGER_PUBLIC_URL` does not change the listening socket. If it is absent, it
falls back to `MANAGER_PROTOCOL://MANAGER_HOST:MANAGER_PORT`. Set it explicitly
whenever administrators use a different origin, protocol, port or reverse-proxy
path. Its optional path is decoded and used as FastAPI's `root_path`; for
example, `https://example.org/manager` publishes `/admin` as
`https://example.org/manager/admin` through a proxy that removes `/manager`
before forwarding the request. Credentials, query strings and fragments are
rejected, one trailing slash is normalized away, and changing the value
requires a Manager restart.

The important distinction is that `MANAGER_HOST` answers “where can a Runner
reach the Manager?”, while `MANAGER_BIND_HOST` answers “on which local network
interface does the process listen?”. They can be equal, but do not need to be.

- `CLEANUP_TASK_FILES_DAYS` controls cleanup retention for all task files (all statuses). Set `0` to disable age-based cleanup.
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
- Token entries can be generated/managed from `/admin/credentials` (UI) or via `scripts/generate_token.py` (CLI).

### Admin users (`ADMIN_USERS__*`)

`/admin` uses HTTP Basic auth with bcrypt hashes:

```properties
ADMIN_USERS__admin="$2b$12$CHANGE_ME_BCRYPT_HASH"
```

If no admin user is configured, the manager logs a warning and admin login is unavailable.

Admin password hashes can be generated/managed from `/admin/credentials` (UI) or via `scripts/generate_password.py` (CLI).

## OpenAPI docs visibility

OpenAPI/docs can be public or token-protected:

```properties
API_DOCS_VISIBILITY=private
OPENAPI_ALLOW_QUERY_TOKEN=false
OPENAPI_COOKIE_SECRET=
```

Behavior:
- `API_DOCS_VISIBILITY=public`: `/docs`, `/redoc`, `/openapi.json` are publicly accessible.
- `API_DOCS_VISIBILITY=private`: OpenAPI routes require a valid API token.
- In private mode, tokens are read from headers first, then from the secure OpenAPI cookie used by `/admin/docs`; query token (`?token=...`) is accepted only if `OPENAPI_ALLOW_QUERY_TOKEN=true`.
- Query tokens are not recommended for production because they can leak via logs/history.
- Set `OPENAPI_COOKIE_SECRET` in production to use an explicit cookie-signing secret.

### Advanced OpenAPI cookie tuning (optional)

These parameters are available but usually do not need to be changed:

```properties
OPENAPI_COOKIE_MAX_AGE_SECONDS=900
OPENAPI_COOKIE_ROTATE_EACH_REQUEST=true
```

- `OPENAPI_COOKIE_MAX_AGE_SECONDS` controls cookie TTL (default: 900s).
- `OPENAPI_COOKIE_ROTATE_EACH_REQUEST=true` refreshes cookie value/TTL on each protected docs request.
- They are intentionally omitted from the default `.env.example` to keep the base config focused on commonly adjusted settings.

How `OPENAPI_COOKIE_SECRET` works:
- The OpenAPI auth cookie is **signed** (HMAC) with `OPENAPI_COOKIE_SECRET` so tampering is detected.
- The cookie is not encrypted; this setting ensures integrity/authenticity, not confidentiality.
- If `OPENAPI_COOKIE_SECRET` is empty, the manager derives a fallback secret from current configured tokens/admin hashes.
- The documented `change-me-with-a-long-random-secret` value triggers a warning but does not block startup; replace it in production or leave it empty to use the fallback.
- For stable behavior across restarts and multi-instance deployments, set an explicit long random secret and keep it identical on all manager instances.

Example secret generation:
```bash
openssl rand -hex 32
```

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
- When enabled, `RUNNERS_STORAGE_DIR` on the Manager and `STORAGE_DIR` on every
  Runner must point to the same generated-files workspace. On separate hosts or
  in containers, expose the same shared filesystem or volume to both services.
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
- `MAX_OTHER_DOMAIN_TASK_PERCENT` must be between `0` and `100`.
- If `PRIORITIES_ENABLED=true`, `PRIORITY_DOMAIN` is required; an inconsistent configuration is rejected instead of silently disabling priorities.

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
MANAGER_HOST=127.0.0.1
MANAGER_PUBLIC_URL=http://127.0.0.1:8081
MANAGER_BIND_HOST=0.0.0.0
MANAGER_PORT=8081

# Production/development settings
ENVIRONMENT=production
UVICORN_WORKERS=2

# Remove task files older than specified number of days
CLEANUP_TASK_FILES_DAYS=60

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
API_DOCS_VISIBILITY=private
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
- Operations runbook: [docs/OPERATIONS.md](OPERATIONS.md)
