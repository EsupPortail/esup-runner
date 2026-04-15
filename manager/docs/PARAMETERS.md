# Manager Parameters

This page summarizes the environment variables consumed by the manager. Values are parsed in [app/core/config.py](../app/core/config.py) and validated at startup.

## Quick start (.env snippet)
```properties
MANAGER_PROTOCOL=http
MANAGER_HOST=0.0.0.0
MANAGER_BIND_HOST=
MANAGER_PORT=8081
ENVIRONMENT=production
UVICORN_WORKERS=2
AUTHORIZED_TOKENS__runners=change-me-runners-token
ADMIN_USERS__admin="$2b$12$change-me-bcrypt-hash"
LOG_DIR=/var/log/esup-runner
RUNNERS_STORAGE_ENABLED=false
RUNNERS_STORAGE_DIR=/tmp/esup-runner
CACHE_DIR=/home/esup-runner/.cache/esup-runner
API_DOCS_VISIBILITY=public
OPENAPI_ALLOW_QUERY_TOKEN=false
NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false
RUNNER_URL_ALLOW_PRIVATE_NETWORKS=true
```

## Environment loading
- `CONFIG_ENV_PATH` (optional): Absolute path to the `.env` file.
- `ENV_FILE` (legacy-compatible optional override): Alternate key for the `.env` path.
- If no override is set, the default file is `manager/.env`.

## Core manager
- `MANAGER_PROTOCOL` (default `http`), `MANAGER_HOST` (default `0.0.0.0`), `MANAGER_PORT` (default `8081`): Base URL components.
- `MANAGER_BIND_HOST` (default computed): Socket bind host used by Uvicorn/Gunicorn.
  - If unset and `MANAGER_HOST` is an IP literal, bind uses `MANAGER_HOST`.
  - If unset and `MANAGER_HOST` is a DNS hostname, bind uses `0.0.0.0`.
- `MANAGER_URL` is computed automatically as `MANAGER_PROTOCOL://MANAGER_HOST:MANAGER_PORT`.
- `ENVIRONMENT` (default `development`): Environment name used by runtime/deployment wrappers.
- `UVICORN_WORKERS` (int, default `4`): Worker count for production process managers.
- `CLEANUP_TASK_FILES_DAYS` (int, default `30`): Retention for task files managed by cleanup services.

## Authentication and admin access
- `AUTHORIZED_TOKENS__*`: Defines accepted API tokens (headers: `Authorization: Bearer <token>` or `X-API-Token: <token>`).
- `ADMIN_USERS__*`: Defines admin users for `/admin` with bcrypt hashes.
- If no `AUTHORIZED_TOKENS__*` is configured, the manager logs a warning and protected API access is effectively blocked.
- If no `ADMIN_USERS__*` is configured, the manager logs a warning and admin login is unavailable.

## OpenAPI visibility
- `API_DOCS_VISIBILITY` (default `public`): `public` or `private`.
- `OPENAPI_ALLOW_QUERY_TOKEN` (bool, default `false`): Allows `?token=...` on docs/OpenAPI routes.

## Logging and cache
- `LOG_DIR` (default `/var/log/esup-runner/`): Log directory; trailing slash is normalized automatically.
- Legacy alias: `LOG_DIRECTORY`.
- `LOG_LEVEL` (default `INFO`): `DEBUG|INFO|WARNING|ERROR|CRITICAL`.
- `CACHE_DIR` (default `/home/esup-runner/.cache/esup-runner`): Shared cache root.
- `UV_CACHE_DIR` (default `CACHE_DIR/uv`): uv package cache directory.

## Shared storage
- `RUNNERS_STORAGE_ENABLED` (bool, default `false`): Enables manager-side shared storage reads.
- `RUNNERS_STORAGE_DIR` (default `/tmp/esup-runner`): Shared storage root.
- Legacy alias: `RUNNERS_STORAGE_PATH`.

## Domain-based priorities
- `PRIORITIES_ENABLED` (bool, default `false`): Enables priority-domain scheduling logic.
- `PRIORITY_DOMAIN` (default empty): Domain (and subdomains) treated as priority.
- `MAX_OTHER_DOMAIN_TASK_PERCENT` (int, default `100`): Non-priority quota percentage, clamped in `[0, 100]`.

## URL hardening
- `NOTIFY_URL_ALLOWED_HOSTS` (CSV, default empty): Optional callback-host allowlist.
- `NOTIFY_URL_ALLOW_PRIVATE_NETWORKS` (bool, default `false`): Allows/rejects private/loopback callback destinations.
- `RUNNER_URL_ALLOWED_HOSTS` (CSV, default empty): Optional runner registration-host allowlist.
- `RUNNER_URL_ALLOW_PRIVATE_NETWORKS` (bool, default `true`): Allows private-network runner URLs (useful for internal deployments).

## CORS
- `CORS_ALLOW_ORIGINS` (CSV, default `*`): Allowed origins.
- `CORS_ALLOW_CREDENTIALS` (bool, default `false`): Credentialed browser requests.
- `CORS_ALLOW_METHODS` (CSV, default `*`): Allowed HTTP methods.
- `CORS_ALLOW_HEADERS` (CSV, default `*`): Allowed request headers.

## Completion callback retries
- `COMPLETION_NOTIFY_MAX_RETRIES` (int, default `5`): Maximum callback retry attempts.
- `COMPLETION_NOTIFY_RETRY_DELAY_SECONDS` (int, default `60`): Delay between retries.
- `COMPLETION_NOTIFY_BACKOFF_FACTOR` (float, default `1.5`): Retry backoff multiplier.

## SMTP notifications
- `SMTP_SERVER` (default empty), `SMTP_PORT` (default `25`), `SMTP_USE_TLS` (default `false`),
  `SMTP_USERNAME` (default empty), `SMTP_PASSWORD` (default empty), `SMTP_SENDER` (default empty),
  `MANAGER_EMAIL` (default empty).

## Validation notes
- `CORS_ALLOW_CREDENTIALS=true` is invalid when `CORS_ALLOW_ORIGINS=*` (startup error).
- `RUNNERS_STORAGE_ENABLED=true` requires a non-empty `RUNNERS_STORAGE_DIR`/`RUNNERS_STORAGE_PATH`.
- If `PRIORITIES_ENABLED=true` but `PRIORITY_DOMAIN` is empty, priorities are disabled with a warning.
- Boolean values accept common forms: `true/false`, `1/0`, `yes/no`, `on/off`.

## Related docs
- Manager runtime behavior and security hardening: [docs/CONFIGURATION.md](CONFIGURATION.md)
- Installation flow: [docs/INSTALLATION.md](INSTALLATION.md)
