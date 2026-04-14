# Installation Guide (Runner)

This guide describes how to install and run the **Esup-Runner Runner** service on a Debian/Ubuntu server.
For Docker-based deployment, see [DOCKER.md](DOCKER.md).

## Version compatibility note (Runner & Manager)

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

Install `uv` as `esup-runner`:

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
  See [FFMPEG_SETUP.md](FFMPEG_SETUP.md) for setup options:
  source compilation, prebuilt package, or Docker container.

## 3) Install the runner

### Create the installation directory

```bash
sudo mkdir -p /opt/esup-runner
sudo chown esup-runner:esup-runner /opt/esup-runner
```

### Fetch the sources

As `esup-runner`, clone the repository into `/opt/esup-runner`, then use sparse checkout to materialize `runner/` plus the root `update-stack.sh` helper.

```bash
cd /opt/esup-runner
git clone --filter=blob:none --sparse https://github.com/EsupPortail/esup-runner.git .
git sparse-checkout set runner
cd runner
```

Notes:

- The `.` destination means the clone happens *in-place* in `/opt/esup-runner` (so you do **not** end up with `/opt/esup-runner/esup-runner`).
- If `/opt/esup-runner` is not empty, `git clone … .` will fail. In that case, choose another directory (e.g. `/opt/esup-runner-src`) or clean the existing one.
- `git sparse-checkout set …` defines which paths (directories/files) are checked out. Running it again will replace the previous selection.

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
- Full configuration reference: see [CONFIGURATION.md](CONFIGURATION.md) and [PARAMETERS.md](PARAMETERS.md).

### Initialize required directories

This creates required directories based on your `.env` values (storage, logs, etc.).

```bash
sudo make init
```

### Install Python dependencies

From `/opt/esup-runner/runner`:

- If this runner achieve only **encoding** ot **studio** tasks, install the default:

```bash
make sync
```

- If this runner must achieve **transcription** tasks on a **CPU-only** server, install the CPU transcription extra:

```bash
make sync-transcription-cpu
```

- If this runner must achieve **transcription** tasks on a **GPU** server, install the GPU transcription extra:

```bash
make sync-transcription-gpu
```

Notes:
- `sync-transcription-cpu` installs a CPU-only torch profile on Linux x86_64, which avoids `nvidia-*` packages.
- `sync-transcription-gpu` keeps the default `torch` resolution, intended for GPU/CUDA environments.
- Current transcription dependency support:
  - `transcription-cpu`: supported on Linux x86_64 and macOS Apple Silicon (`arm64`).
  - `transcription-gpu`: supported on Linux x86_64 GPU/CUDA hosts.
  - macOS Intel (`x86_64`) is not supported for transcription with the current `torch` stack because upstream wheels are no longer published for that platform.

### Optional: GPU lock strategy (when maintaining `uv.lock`)

These commands are useful only when you need to regenerate or upgrade `uv.lock` for GPU transcription environments.

- `make lock-upgrade-gpu-12`
  - Utility: forces a CUDA 12-compatible lock resolution for GPU transcription.
  - Use this when: your production hosts are pinned to CUDA 12.x / older NVIDIA stacks and you want to avoid drift to newer CUDA stacks.

- `make lock-upgrade-gpu-latest`
  - Utility: upgrades the lock against the latest available GPU torch/CUDA stack.
  - Use this when: your GPU hosts are up to date and you want to follow latest supported GPU dependencies.

After either command, apply the lockfile on the target host with the appropriate sync command (for example `make sync-transcription-gpu`).

### Verification checks

Before starting the service, you can run a few built-in checks from `/opt/esup-runner/runner`.

```bash
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_runner_resources.py
uv run scripts/check_runner_storage.py
```

Notes:
- These scripts may read your configuration from `.env`, so make sure it is present and correctly configured.
- A successful check should exit with code `0`; any non-zero exit code indicates something to fix (missing binary, wrong permissions, insufficient disk/RAM, etc.).
- `check_runner_storage.py` validates free space in `LOG_DIR`, `STORAGE_DIR`, `HUGGINGFACE_MODELS_DIR`, `WHISPER_MODELS_DIR`, and `UV_CACHE_DIR` (which defaults to `CACHE_DIR/uv`).
- Compatibility note: legacy variable `LOG_DIRECTORY` is still accepted.

## 4) Run

### Foreground (manual run)

```bash
make run
# or: uv run esup-runner-runner
```

The runner exposes an OpenAPI UI at `/docs`.

## 5) Production (systemd user service)

Warning: the generated service uses `/opt/esup-runner` by default.
If your installation lives in another directory, edit `production/esup-runner-runner.service` before running `make create-service`.

Install and start the service:

```bash
make create-service
```

Do not run this target with `sudo`: it must install the unit in the service user's home.

If this service must start at boot without an interactive login session, enable lingering once (as `root`):

```bash
sudo loginctl enable-linger esup-runner
```

Check status and logs:

```bash
systemctl --user status esup-runner-runner
systemctl --user restart esup-runner-runner
journalctl --user -u esup-runner-runner -f
```

Quick check:

```bash
systemctl --user is-active --quiet esup-runner-runner && echo "OK: service is running"
```

The provided unit sets a `PATH` that includes `/home/esup-runner/.local/bin` and starts `uv` via `/usr/bin/env`.
If your `uv` is installed elsewhere, edit the unit accordingly:

- Service template: `production/esup-runner-runner.service`
- Installed to: `~/.config/systemd/user/esup-runner-runner.service`

## 6) Bonus (production): log rotation

If you write logs under `/var/log/esup-runner`, configure `logrotate`.

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
