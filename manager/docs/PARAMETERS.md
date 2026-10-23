# Manager Parameters

This page summarizes the environment variables consumed by the manager. Application settings are parsed in [app/core/config.py](../app/core/config.py) and validated at startup; native HTTP server settings are identified separately below.

## Quick start (.env snippet)
```properties
SERVICE_USER=esup-runner
MANAGER_PROTOCOL=http
MANAGER_HOST=127.0.0.1
MANAGER_PUBLIC_URL=http://127.0.0.1:8081
MANAGER_BIND_HOST=0.0.0.0
MANAGER_PORT=8081
ENVIRONMENT=production
UVICORN_WORKERS=2
AUTHORIZED_TOKENS__runners=CHANGE_ME_RUNNERS_TOKEN
ADMIN_USERS__admin="CHANGE_ME_BCRYPT_HASH"
LOG_DIR=/var/log/esup-runner
RUNNERS_STORAGE_ENABLED=false
RUNNERS_STORAGE_DIR=/tmp/esup-runner
API_DOCS_VISIBILITY=private
OPENAPI_ALLOW_QUERY_TOKEN=false
NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false
RUNNER_URL_ALLOW_PRIVATE_NETWORKS=true
```

## Environment loading
- `CONFIG_ENV_PATH` (optional): Absolute path to the `.env` file.
- `ENV_FILE` (legacy-compatible optional override): Alternate key for the `.env` path.
- If no override is set, the default file is `manager/.env`.

## Core manager
- `SERVICE_USER` (optional, default `esup-runner`): Unix account that owns runtime directories and runs the systemd user service. It also defines the default cache root.
- `MANAGER_PROTOCOL` (default `http`): Scheme used to build the private Manager API URL. Allowed values are `http` and `https`. It does not enable TLS in Uvicorn/Gunicorn; choose `https` only when the private network path terminates TLS for this URL.
- `MANAGER_HOST` (default `127.0.0.1`): Private DNS name or IP through which Runners reach the Manager. It becomes the host in the computed `MANAGER_URL` and is also used as a fallback domain for manager notification senders. It must be reachable from every Runner; `0.0.0.0` is a bind wildcard, not a reachable destination.
- `MANAGER_PORT` (default `8081`): Uvicorn/Gunicorn listening port and port included in the computed private `MANAGER_URL`. It is not necessarily the public reverse-proxy port recorded in `MANAGER_PUBLIC_URL`.
- `MANAGER_BIND_HOST` (default computed from `MANAGER_HOST`): Local socket interface used by Uvicorn/Gunicorn. `127.0.0.1` restricts access to the local host, a specific local IP restricts it to that interface, and `0.0.0.0` listens on every IPv4 interface. It never becomes part of an advertised URL.
- `MANAGER_PUBLIC_URL` (default computed from `MANAGER_PROTOCOL://MANAGER_HOST:MANAGER_PORT`): Complete browser-facing HTTP(S) URL of the administration interface, also advertised as an alternative task completion address. It does not configure the socket. Runners may use it when their own `MANAGER_URL` matches and the proxy exposes the Manager API. Set it explicitly when a reverse proxy changes the public scheme, host, port or path. Credentials, query strings and fragments are rejected. One trailing slash is normalized away; the optional path becomes FastAPI's `root_path`. Changing it requires a Manager restart.
- `MANAGER_URL` (computed, not an environment variable of the Manager): Private API base URL assembled from `MANAGER_PROTOCOL`, `MANAGER_HOST` and `MANAGER_PORT`. It is advertised alongside `MANAGER_PUBLIC_URL` for task completion callbacks. The `MANAGER_URL` configured in each Runner must match one of these base addresses.
- `MANAGER_ROOT_PATH` (computed, not an environment variable): FastAPI deployment prefix derived from the path in `MANAGER_PUBLIC_URL`.
- `FORWARDED_ALLOW_IPS` (native Gunicorn/Uvicorn setting, default `127.0.0.1,::1`): Trusted proxy IPs or CIDR networks, comma-separated. Recommended behind HAProxy: add its source IPs as seen by the application to `manager/.env` (and `runner/.env` if proxied) so HTTPS and client IP forwarding headers are honored. Avoid `*`; restart the affected services completely. This setting is read by the HTTP server, outside application configuration validation. See [proxy configuration](CONFIGURATION.md#trust-haproxy-forwarding-headers).
- `ENVIRONMENT` (default `development`): Environment name used by runtime/deployment wrappers.
- `UVICORN_WORKERS` (int, default `4`): Worker count for production process managers.
- `CLEANUP_TASK_FILES_DAYS` (int, default `30`): Retention for all tasks managed by cleanup services, regardless of status. Set `0` to disable age-based cleanup. Keeping this value low and non-zero is recommended to limit task accumulation and Manager memory usage; choose the shortest retention that meets your task-history needs.

## Authentication and admin access
- `AUTHORIZED_TOKENS__*`: Defines accepted API tokens (headers: `Authorization: Bearer <token>` or `X-API-Token: <token>`).
- `ADMIN_USERS__*`: Defines admin users for `/admin` with bcrypt hashes.
- If no `AUTHORIZED_TOKENS__*` is configured, the manager logs a warning and protected API access is effectively blocked.
- If no `ADMIN_USERS__*` is configured, the manager logs a warning and admin login is unavailable.

## OpenAPI visibility
- `API_DOCS_VISIBILITY` (default `public`): `public` or `private`.
- `OPENAPI_ALLOW_QUERY_TOKEN` (bool, default `false`): Allows `?token=...` on docs/OpenAPI routes (legacy/fallback; admin docs uses a secure cookie instead).
- `OPENAPI_COOKIE_MAX_AGE_SECONDS` (int, default `900`): Advanced cookie tuning; TTL of the signed OpenAPI auth cookie used by admin/docs flow.
- `OPENAPI_COOKIE_ROTATE_EACH_REQUEST` (bool, default `true`): Advanced cookie tuning; rotates OpenAPI auth cookie on each protected docs request.
- `OPENAPI_COOKIE_SECRET` (string, default empty): Optional explicit signing secret for OpenAPI auth cookie (recommended in production). Cookie is signed (integrity), not encrypted. If empty, manager derives a fallback secret from configured tokens/admin hashes. Use a long random shared value on every manager instance (example: `openssl rand -hex 32`).

## Logging and cache
- `LOG_DIR` (default `/var/log/esup-runner/`): Log directory; trailing slash is normalized automatically.
- Legacy alias: `LOG_DIRECTORY`.
- `LOG_LEVEL` (default `INFO`): `DEBUG|INFO|WARNING|ERROR|CRITICAL`.
- `CACHE_DIR` (default `/home/{SERVICE_USER}/.cache/esup-runner`): Shared cache root.
- `UV_CACHE_DIR` (default `CACHE_DIR/uv`): uv package cache directory.

An explicit `CACHE_DIR` or `UV_CACHE_DIR` remains authoritative. Remove or update
old `/home/esup-runner/...` overrides when changing `SERVICE_USER`.

## Shared storage
- `RUNNERS_STORAGE_ENABLED` (bool, default `false`): Enables manager-side shared storage reads.
- `RUNNERS_STORAGE_DIR` (default `/tmp/esup-runner`): Shared storage root. When
  shared storage is enabled, it must point to the same generated-files workspace
  as `STORAGE_DIR` on every Runner (the same shared filesystem or volume).
- Legacy alias: `RUNNERS_STORAGE_PATH`.

## Domain-based priorities
- `PRIORITIES_ENABLED` (bool, default `false`): Enables priority-domain scheduling logic.
- `PRIORITY_DOMAIN` (default empty): Domain (and subdomains) treated as priority.
- `MAX_OTHER_DOMAIN_TASK_PERCENT` (int, default `100`): Non-priority quota percentage; must be in `[0, 100]`.

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
- Explicit invalid boolean, integer, and floating-point values are rejected instead of being silently replaced or clamped.
- At startup, the manager validates URL components, supported environment/log/docs modes, numeric bounds, non-empty required paths, credential entries (including bcrypt hash structure), CORS compatibility, and priority settings.
- `CORS_ALLOW_CREDENTIALS=true` is invalid when `CORS_ALLOW_ORIGINS=*` (startup error).
- `RUNNERS_STORAGE_ENABLED=true` requires a non-empty
  `RUNNERS_STORAGE_DIR`/`RUNNERS_STORAGE_PATH` pointing to the same
  generated-files workspace as each Runner's `STORAGE_DIR`.
- `PRIORITIES_ENABLED=true` requires a non-empty hostname-only `PRIORITY_DOMAIN`.
- Documented `CHANGE_ME_*` token/admin values are rejected. The optional `change-me-with-a-long-random-secret` OpenAPI cookie value emits a warning only; replace it in production or leave it empty to use the derived fallback. Empty token/admin collections remain allowed with an explicit warning because they intentionally disable the corresponding access.
- Boolean values accept common forms: `true/false`, `1/0`, `yes/no`, `on/off`.
- Run `uv run scripts/check_config.py` after changing `.env` to see all validation errors together. Its summary reports credential counts, never credential values; exit code `0` means valid and `2` means invalid.

## Related docs
- Manager runtime behavior and security hardening: [docs/CONFIGURATION.md](CONFIGURATION.md)
- Installation flow: [docs/INSTALLATION.md](INSTALLATION.md)
- Operations runbook: [docs/OPERATIONS.md](OPERATIONS.md)

## Live parameters

See [LIVE.md](LIVE.md) for the complete configuration and Esup-Pod contract.

| Parameter | Default |
| --- | --- |
| `MANAGER_DATA_DIR` | `data` (tasks, registry, locks and statistics; separate path per Manager) |
| `LIVE_ENABLED` | `false` |
| `LIVE_AUTO_START` | `true` (discover accepted RTMP publications and automatically start/stop sessions when Live is enabled) |
| `LIVE_AUTO_ETAB_NAME` | `unknown` (fallback establishment for automatic sessions; 1–200 characters) |
| `LIVE_SOURCES` | `{}` |
| `LIVE_MEDIAMTX_API_URL` | `http://127.0.0.1:9997` |
| `LIVE_MEDIAMTX_API_USER` | `control` |
| `LIVE_MEDIAMTX_API_PASSWORD` | `` |
| `LIVE_MEDIAMTX_HEALTH_CHECK_INTERVAL_HOURS` | `12` (non-negative whole hours; `0` disables monitoring) |
| `LIVE_MEDIAMTX_LOG_UNIT` | `` (optional systemd user service, e.g. `esup-runner-mediamtx@manager.service`) |
| `LIVE_MEDIAMTX_LOG_FILE` | `` (alternative absolute path to the Manager MediaMTX plain log) |

With `LIVE_ENABLED=true`, one worker checks the Manager MediaMTX at startup and
at this interval, emailing `MANAGER_EMAIL` on each failure. See
[automatic MediaMTX monitoring](OPERATIONS.md#automatic-mediamtx-monitoring).

Other time values are in seconds. `LIVE_SOURCES` optionally declares broadcaster names and private MediaMTX paths before publication, for example `{"amphi-a":"live/amphi-a"}`. Each identifier must equal the final path component: the public broadcaster name, containing at most 100 letters, digits, underscores or hyphens and starting with a letter or digit. Other accepted RTMP publications are discovered automatically using their broadcaster names as `source_id`; this setting is not an allowlist. Bindings persist in `MANAGER_DATA_DIR/.live-sources.json`, even if declarations are removed. The Manager does not transcode and has no Live slot settings. The Manager MediaMTX template allows a new publisher to replace the previous publisher on the same path; see [Live configuration](LIVE.md#manager-configuration).
Paths must be unique when automatic startup is enabled. Reconciliation and
RTMP discovery run every 5 seconds (internal default). See
[automatic operation](LIVE.md#automatic-rtmp-to-hls-operation) for capacity,
disconnect, republication and stop behavior.
Configure only one MediaMTX log source. These diagnostics supplement `manager.log`
in administration; they require MediaMTX `logLevel: info` and `logStructured: false`.
See [Live diagnostics](LIVE.md#live-diagnostics-in-the-log-viewer).

### Internal Live defaults

MediaMTX request timeout: **5 s**; reconciliation/publication scan interval:
**5 s**; Runner control timeout: **15 s**. These are code defaults, not environment
parameters. Remove the former `LIVE_MEDIAMTX_TIMEOUT`, `LIVE_POLL_INTERVAL` and
`LIVE_RUNNER_TIMEOUT` entries when upgrading; they are no longer read.

Use [the connection helper](MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify)
to populate the installation password and log source. With the standard native
ports/account, only `LIVE_ENABLED=true`, that password and the optional log
source need to appear in the Manager's Live block. No password has a shared default.
