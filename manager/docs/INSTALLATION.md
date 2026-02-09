# Installation Guide (Manager)

This document describes how to install and run the **ESUP Runner Manager** on a Debian/Ubuntu-like system.

## 1) Create a dedicated system user

As `root`:

```bash
adduser esup-runner
adduser esup-runner sudo
# (or /usr/sbin/adduser esup-runner sudo)
```

Then switch to that user:

```bash
su - esup-runner
```

## 2) Prerequisites

### System packages

As `root`:

```bash
apt update
apt install -y curl ca-certificates git make
```

### Install `uv`

Install `uv` using the official installer:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Notes:

- Run the installer for **both** `esup-runner` and `root` if you intend to run some steps with `sudo`.
- Re-open your shell session (or source your profile) so that `~/.local/bin` is in your `PATH`.

Verify:

```bash
uv --version
uv python list
```

## 3) Install the manager

### Create the source directory

As `root`:

```bash
mkdir -p /opt/esup-runner
chown esup-runner:esup-runner /opt/esup-runner/
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
- If `/opt/esup-runner` is not empty, `git clone ... .` will fail. In that case, choose another directory (e.g. `/opt/esup-runner-src`) or clean the existing one.
- `git sparse-checkout set ...` defines which subdirectories are checked out. Running it again will replace the previous selection.

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
- `LOG_DIRECTORY`

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

- Generate tokens:

  ```bash
  uv run scripts/generate_tokens.py
  ```

- Generate bcrypt password hashes for admin users:

  ```bash
  uv run scripts/generate_passwords.py
  ```

### Initialize required directories

This creates directories from `.env` (notably `LOG_DIRECTORY` and `RUNNERS_STORAGE_PATH` if set) and assigns ownership to the invoking user.

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

Replace:

- `<AUTHORIZED_TOKEN>` with one of your `AUTHORIZED_TOKENS__*` values from `.env`.
- `<MANAGER_PORT>` with the value of `MANAGER_PORT` from `.env`.

## 4) Production: systemd service

Install and start the systemd service:

```bash
sudo make create-service
```

Useful commands:

```bash
systemctl status esup-runner-manager
systemctl restart esup-runner-manager
systemctl reload esup-runner-manager
journalctl -u esup-runner-manager -f
```

Quick check:

```bash
systemctl is-active --quiet esup-runner-manager && echo "OK: service is running"
```

Notes:

- The service loads environment variables from `/opt/esup-runner/manager/.env`.
- The service runs as the `esup-runner` user.

## 5) Bonus (production): log rotation

If the manager writes log files under `/var/log/esup-runner`, configure `logrotate`.

Create `/etc/logrotate.d/esup-runner` as `root`:

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

Make sure `LOG_DIRECTORY` in `.env` matches this path.
