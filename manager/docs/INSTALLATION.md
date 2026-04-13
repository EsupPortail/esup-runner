# Installation Guide (Manager)

This document describes how to install and run the **ESUP Runner Manager** on a Debian/Ubuntu-like system.
For Docker-based deployment, see [DOCKER.md](DOCKER.md).

## Version compatibility note (Runner & Manager)

The Runner sends its version to the Manager in the `X-Runner-Version` header during registration/heartbeats.
The Manager enforces compatibility at **MAJOR + MINOR** level:

- Runner `X.Y.*` can register only to a Manager `X.Y.*`
- `PATCH` versions may differ

> Assumptions
>
> - You have root access (or sudo).
> - You are installing under `/opt/esup-runner`.

## 1) Create a dedicated system user

As `root`:

```bash
adduser esup-runner
adduser esup-runner sudo
# (alternative)
/usr/sbin/adduser esup-runner sudo
```

Then switch to that user:

```bash
su - esup-runner
```

## 2) Prerequisites

### System packages

```bash
sudo apt update
sudo apt install -y curl ca-certificates git make
```

### Install `uv`

Install `uv` as `esup-runner`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Re-open your shell session (recommended) or reload your shell init files, then verify:

```bash
uv --version
uv python list
```

## 3) Install the manager

### Create the source directory

```bash
sudo mkdir -p /opt/esup-runner
sudo chown esup-runner:esup-runner /opt/esup-runner/
```

### Fetch sources

As `esup-runner`, clone the repository into `/opt/esup-runner`, then use sparse checkout to only materialize `manager/`.

Example:

```bash
cd /opt/esup-runner
git clone --filter=blob:none --sparse https://github.com/EsupPortail/esup-runner.git .
git sparse-checkout set manager
```

Notes:

- The `.` destination means the clone happens *in-place* in `/opt/esup-runner` (so you do **not** end up with `/opt/esup-runner/esup-runner`).
- If `/opt/esup-runner` is not empty, `git clone … .` will fail. In that case, choose another directory (e.g. `/opt/esup-runner-src`) or clean the existing one.
- `git sparse-checkout set …` defines which subdirectories are checked out. Running it again will replace the previous selection.

If you plan to install **both** the manager and the runner on the same machine (recommended layout: `/opt/esup-runner/manager` and `/opt/esup-runner/runner`), use:

```bash
git sparse-checkout set manager runner
```

### Configure `.env`

```bash
cd /opt/esup-runner/manager/
cp .env.example .env
nano .env
```

At minimum, review:

- `MANAGER_HOST`, `MANAGER_PORT`
- `AUTHORIZED_TOKENS__*` (clients and runners)
- `ADMIN_USERS__*` (for the `/admin` dashboard)
- `LOG_DIR`
- `RUNNERS_STORAGE_ENABLED` and `RUNNERS_STORAGE_DIR` (if you use shared storage mode)
- `CACHE_DIR` and `UV_CACHE_DIR`
- `NOTIFY_URL_ALLOWED_HOSTS`, `NOTIFY_URL_ALLOW_PRIVATE_NETWORKS` (if tasks use `notify_url`)

Compatibility note: legacy names `LOG_DIRECTORY` and `RUNNERS_STORAGE_PATH` are still accepted.

#### `notify_url` callback restrictions

If task requests use `notify_url`, the manager validates the callback target before sending any outbound request.

By default, `NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=false`, so the manager rejects callback targets whose hostname resolves to any of the following:

- Invalid IP addresses, for example `999.999.999.999`, `abc`, or `2001:db8:::1`
- Private addresses, for example `10.0.0.1`, `172.16.0.5`, `192.168.1.10`, or `fd00::1`
- Loopback addresses, for example `127.0.0.1` or `::1`
- Link-local addresses, for example `169.254.1.1` or `fe80::1`
- Multicast addresses, for example `224.0.0.1` or `ff02::1`
- Reserved addresses, for example `240.0.0.1`
- Unspecified addresses, for example `0.0.0.0` or `::`

Additional notes:

- `localhost` is also rejected before DNS resolution.
- `NOTIFY_URL_ALLOWED_HOSTS` can be used to restrict callbacks to a specific hostname allowlist.
- Set `NOTIFY_URL_ALLOW_PRIVATE_NETWORKS=true` only if you intentionally allow callbacks to internal/private network destinations that you control.

#### CORS configuration (`CORS_ALLOW_*`)

If you access the manager API / web UI from a **browser** hosted on another origin (different scheme/host/port), you must configure CORS so that the browser is allowed to call the API.

In the normal ESUP Runner setup (runner processes and external applications calling the manager API server-to-server, and the manager UI served from the same origin as the API), this is typically **optional**. In that case, the simplest and recommended choice is to keep the default values from `.env.example`.

The manager uses FastAPI/Starlette `CORSMiddleware`, driven by the following `.env` variables:

- `CORS_ALLOW_ORIGINS`: Comma-separated list of allowed origins (no spaces), or `*` to allow any origin.
  - Example: `CORS_ALLOW_ORIGINS=https://runner-ui.example.edu,http://localhost:5173`
- `CORS_ALLOW_CREDENTIALS`: `true`/`false`.
  - Set to `true` only if you need cookies / HTTP auth across origins.
  - Important: `CORS_ALLOW_CREDENTIALS=true` is **not compatible** with `CORS_ALLOW_ORIGINS=*` (the manager will refuse to start with that combination).
- `CORS_ALLOW_METHODS`: Comma-separated list (e.g. `GET,POST,OPTIONS`) or `*`.
- `CORS_ALLOW_HEADERS`: Comma-separated list (e.g. `Content-Type,Authorization,X-API-Token`) or `*`.

Notes:

- If your browser app calls the API with `X-API-Token`, make sure it is included in `CORS_ALLOW_HEADERS` (or keep `*`).
- If you run everything on the same origin (no cross-origin browser calls), CORS settings usually do not matter.

Typical production examples:

1) Web UI on a separate domain, API auth with `X-API-Token` (recommended default, no cookies):

```dotenv
# Allow only the UI origin(s)
CORS_ALLOW_ORIGINS=https://esup-runner-ui.example.edu

# No cross-site cookies needed
CORS_ALLOW_CREDENTIALS=false

# Keep explicit (or use "*")
CORS_ALLOW_METHODS=GET,POST,PUT,PATCH,DELETE,OPTIONS

# Include the custom token header used by the browser
CORS_ALLOW_HEADERS=Content-Type,Authorization,X-API-Token
```

2) If you really need cookies / browser credentials across origins:

```dotenv
# Must be explicit origins (no "*")
CORS_ALLOW_ORIGINS=https://esup-runner-ui.example.edu
CORS_ALLOW_CREDENTIALS=true
CORS_ALLOW_METHODS=GET,POST,OPTIONS
CORS_ALLOW_HEADERS=Content-Type
```

Helpers:

- Generate one API token entry (`AUTHORIZED_TOKENS__…`):

  ```bash
  uv run scripts/generate_token.py
  # Example:
  # Token label (letters, numbers, underscores): runners
  # Add this line to your .env file:
  # AUTHORIZED_TOKENS__runners=s3cr3t_token_value
  ```

- Generate one admin bcrypt hash entry (`ADMIN_USERS__…`):

  ```bash
  uv run scripts/generate_password.py
  # Example:
  # Admin username (letters, numbers, underscores): admin
  # Password:
  # Confirm password:
  # Add this line to your .env file:
  # ADMIN_USERS__admin="s3cr3t_bcrypt_value"
  ```

### Initialize required directories

This creates directories from `.env` (notably `LOG_DIR`, `RUNNERS_STORAGE_DIR`, `CACHE_DIR`, and `UV_CACHE_DIR` when set) and assigns ownership to the invoking user.

```bash
sudo make init
```

### Synchronize Python dependencies

```bash
make sync
```

### Verification checks

After dependencies are installed, run the built-in version consistency checks:

```bash
uv run scripts/check_version.py
```

Expected outcome: the script prints a summary with all checks passing and exits with status code `0`.

Optional runtime checks (recommended):

1) Start the manager in the foreground:

```bash
make run
# or: uv run esup-runner-manager
```

2) From another terminal, call the health endpoint (token required):

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/manager/health"
```

3) Check the API version endpoint:

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/api/version"
```

4) Optional end-to-end task test with the example async client:

```bash
cd /opt/esup-runner/manager
RUNNER_API_TOKEN="<AUTHORIZED_TOKEN>" \
RUNNER_MANAGER_URL="http://127.0.0.1:<MANAGER_PORT>" \
uv run scripts/example_async_client.py
```

Replace:

- `<AUTHORIZED_TOKEN>` with one of your `AUTHORIZED_TOKENS__*` (for example `AUTHORIZED_TOKENS__runners`) values from `.env`.
- `<MANAGER_PORT>` with the value of `MANAGER_PORT` from `.env`.

The expected result is as follows: `{"status":"healthy","timestamp":"XXXX","runners":0,"tasks":0}`.

## 4) Production: systemd user service

Warning: the generated service uses `/opt/esup-runner` by default.
If your installation lives in another directory, edit `production/esup-runner-manager.service` before running `make create-service`.

Install and start the systemd user service:

```bash
make create-service
```

Do not run this target with `sudo`: it must install the unit in the service user's home.

If this service must start at boot without an interactive login session, enable lingering once (as `root`):

```bash
sudo loginctl enable-linger esup-runner
```

Useful commands:

```bash
systemctl --user status esup-runner-manager
systemctl --user restart esup-runner-manager
journalctl --user -u esup-runner-manager -f
```

Quick check:

```bash
systemctl --user is-active --quiet esup-runner-manager && echo "OK: service is running"
```

Installed unit path: `~/.config/systemd/user/esup-runner-manager.service`.

## 4.1) Production hardening: reverse proxy + HTTPS

For security, prefer exposing the manager (API and admin UI) behind a reverse proxy such as HAProxy, Nginx, or Traefik.

Recommendations:

- Expose only the reverse proxy publicly on `443` (HTTPS).
- Keep the manager service on a private interface, or localhost only when possible.
- Forward admin endpoints (`/admin`, `/tasks`…) and API routes through the proxy.
- Avoid direct public access to the manager process on `MANAGER_PORT`.

Notes:

- The service loads environment variables from `/opt/esup-runner/manager/.env`.
- The service runs as the `esup-runner` user.

## 5) Bonus (production): log rotation

If the manager writes log files under `/var/log/esup-runner`, configure `logrotate`.

Create `/etc/logrotate.d/esup-runner`:

```bash
sudo nano /etc/logrotate.d/esup-runner
```

With content:

```conf
/var/log/esup-runner/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

Make sure `LOG_DIR` in `.env` matches this path.
