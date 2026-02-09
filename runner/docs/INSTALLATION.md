# Installation Guide (Runner)

This guide describes how to install and run the **Esup-Runner Runner** service on a Debian/Ubuntu server.

## Version compatibility note (Runner  Manager)

The Runner sends its version to the Manager in the `X-Runner-Version` header during registration/heartbeats.
The Manager enforces compatibility at **MAJOR + MINOR** level:

- Runner `X.Y.*` can register only to a Manager `X.Y.*`
- `PATCH` versions may differ

> Assumptions
> - You have root access (or sudo).
> - You are installing under `/opt/esup-runner`.
> - You will run in **CPU mode** unless you explicitly install a GPU-enabled FFmpeg build.

## 1) Create a dedicated service user

As `root`:

```bash
adduser esup-runner
adduser esup-runner sudo
# (alternative)
/usr/sbin/adduser esup-runner sudo
```

Then switch to the service user:

```bash
su - esup-runner
```

## 2) Prerequisites

### System packages

```bash
sudo apt update
sudo apt install -y curl ca-certificates git make time
```

Optional (only if you hit FFmpeg/PNG fallbacks that require ImageMagick):

```bash
sudo apt install -y imagemagick
```

### Install `uv`

Install `uv` **as both `root` and `esup-runner`** (this matches the usual layout where each user gets `~/.local/bin/uv`).

As `root`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

As `esup-runner`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Re-open your shell session (recommended) or reload your shell init files, then verify:

```bash
uv --version
uv python list
```

### FFmpeg (CPU vs GPU)

- **CPU mode (recommended when no GPU is available):**

```bash
sudo apt install -y ffmpeg
```

- **GPU mode:** requires a GPU-capable FFmpeg build (specific prebuilt package or a custom compilation).
  The project documentation for this will be added later.

## 3) Install the runner

### Create the installation directory

```bash
sudo mkdir -p /opt/esup-runner
sudo chown esup-runner:esup-runner /opt/esup-runner
```

### Fetch the sources

As `esup-runner`, clone the repository into `/opt/esup-runner`, then use sparse checkout to only materialize `runner/`.

```bash
cd /opt/esup-runner
git clone --filter=blob:none --sparse https://github.com/EsupPortail/esup-runner.git .
git sparse-checkout set runner
cd runner
```

Notes:

- The `.` destination means the clone happens *in-place* in `/opt/esup-runner` (so you do **not** end up with `/opt/esup-runner/esup-runner`).
- If `/opt/esup-runner` is not empty, `git clone ... .` will fail. In that case, choose another directory (e.g. `/opt/esup-runner-src`) or clean the existing one.
- `git sparse-checkout set ...` defines which subdirectories are checked out. Running it again will replace the previous selection.

If you plan to install **both** the runner and the manager on the same machine (recommended layout: `/opt/esup-runner/runner` and `/opt/esup-runner/manager`), use:

```bash
git sparse-checkout set manager runner
```

### Configure environment (.env)

Create your configuration file from the example:

```bash
cp .env.example .env
nano .env
```

Notes:
- `.env` contains secrets (tokens, URLs): keep it readable only by administrators and do not share it.
- Full configuration reference: see [RUNNER_CONFIGURATION.md](RUNNER_CONFIGURATION.md) and [RUNNER_PARAMETERS.md](RUNNER_PARAMETERS.md).

### Initialize required directories

This creates required directories based on your `.env` values (storage, logs, etc.).

```bash
sudo make init
```

### Install Python dependencies

From `/opt/esup-runner/runner`:

- For **encoding** or **studio** runners:

```bash
make sync
```

- For **transcription** runners:

```bash
make sync-transcription
```

### Verification checks

Before starting the service, you can run a few built-in checks from `/opt/esup-runner/runner`.

```bash
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_runner_resources.py
```

Notes:
- These scripts may read your configuration from `.env`, so make sure it is present and correctly configured.
- A successful check should exit with code `0`; any non-zero exit code indicates something to fix (missing binary, wrong permissions, insufficient disk/RAM, etc.).

## 4) Run

### Foreground (manual run)

```bash
make run
# or: uv run esup-runner-runner
```

The runner exposes an OpenAPI UI at `/docs`.

## 5) Production (systemd service)

Install and start the service:

```bash
sudo make create-service
```

Check status and logs:

```bash
sudo systemctl status esup-runner-runner
sudo journalctl -u esup-runner-runner -f
```

The provided unit sets a `PATH` that includes `/home/esup-runner/.local/bin` and starts `uv` via `/usr/bin/env`.
If your `uv` is installed elsewhere, edit the unit accordingly:

- Service template: `production/esup-runner-runner.service`
- Installed to: `/etc/systemd/system/esup-runner-runner.service`

## 6) Bonus (production): log rotation

If you write logs under `/var/log/esup-runner`, configure `logrotate`.

Create `/etc/logrotate.d/esup-runner`:

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
