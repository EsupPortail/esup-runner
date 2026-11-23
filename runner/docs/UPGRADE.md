# Upgrade Guide — ESUP Runner (Runner)

This document describes a safe **production** upgrade procedure for the *Runner*.

> TL;DR
> - **PATCH**: you can usually upgrade Runner and Manager independently.
> - **MINOR/MAJOR**: a **coordinated** upgrade is required (the Manager enforces `MAJOR.MINOR`).

---

## 0) Prerequisites

- Installed under `/opt/esup-runner` (or equivalent).
- systemd user service installed: `esup-runner-runner`.
- Configuration file: `/opt/esup-runner/runner/.env`.

Useful docs:
- Installation: `INSTALLATION.md`
- Configuration: `CONFIGURATION.md`
- Parameters: `PARAMETERS.md`
- Operations: `OPERATIONS.md`
- Version management: `VERSION_MANAGEMENT.md`

---

## 1) Runner ⇄ Manager compatibility (important)

The Runner sends its version in `X-Runner-Version` during registration/heartbeats.

The Manager enforces **MAJOR + MINOR** compatibility:

- Runner `X.Y.*` is accepted only by a Manager `X.Y.*`
- `PATCH` versions may differ

Consequence:
- A **MINOR/MAJOR** bump must be done with a compatible Manager.

Recommended sequence:
- Prepare the new version on both Manager **and** Runner.
- Do a short cutover (stop services), switch version, resync, restart.

---

## 2) Recommended backups

Before any upgrade:

- Backup configuration:
  - `/opt/esup-runner/runner/.env`
- Backup “stateful” directories if you want to preserve artifacts:
  - `STORAGE_DIR` (default: `/tmp/esup-runner`)
  - `LOG_DIR` (default: `/var/log/esup-runner`)
- For transcription runners:
  - `CACHE_DIR` (contains `whisper-models`, `huggingface`, and `uv` caches)

Compatibility note: legacy variable `LOG_DIRECTORY` is still accepted.

When adopting a custom `SERVICE_USER`, remove or update explicit cache paths left
from older `.env` files (notably `CACHE_DIR`, `WHISPER_MODELS_DIR`,
`HUGGINGFACE_MODELS_DIR`, and `UV_CACHE_DIR`). Explicit paths always take precedence
over defaults derived from `SERVICE_USER`.

---

## 3) Update sources

This deployment uses a **monorepo clone**: the Git working tree is `/opt/esup-runner`, and the projects live in subdirectories (`manager/`, `runner/`).

If you use sparse-checkout, ensure the correct paths are included **depending on what is installed on this machine**:

- If **Runner + Manager are installed on the same machine**: include both.
- If **only Runner is installed**: include only `runner`.

```bash
cd /opt/esup-runner

# If you use sparse checkout and the automated helper script:
# - runner only host: git sparse-checkout set runner
# - manager + runner host: git sparse-checkout set manager runner

# Fetch history and tags
git fetch --tags

# Update the current branch (recommended in prod: ff-only)
git pull --ff-only

# Option: switch to a tagged Runner version
# git checkout runner-vX.Y.Z
```

---

## 4) Review configuration (.env)

Compare your `.env` with `.env.example` to pick up new variables.

```bash
cd /opt/esup-runner/runner

# Warning: .env contains secrets
diff -u .env.example .env || true
```

Sensitive settings to double-check after an upgrade:
- `MANAGER_URL`
- `RUNNER_TOKEN` (must match an `AUTHORIZED_TOKENS__*` on the Manager side)
- `RUNNER_TASK_TYPES` and the optional grouped syntax
- `STORAGE_DIR` and permissions / available disk space
- `ENCODING_TYPE` (CPU/GPU) and FFmpeg dependencies

---

## 5) Update Python dependencies

### If needed, update `uv` first

If `uv sync --locked` reports that `uv.lock` needs to be updated even though you already pulled the latest repository changes, first verify the installed `uv` version:

```bash
uv --version
```

Update `uv` for the current user with:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If this runner handles only **encoding** or **studio** tasks, install the default:
```bash
cd /opt/esup-runner/runner
make sync
```

If this runner must achieve **transcription** tasks on a **CPU-only** server, install the CPU transcription extra:

```bash
cd /opt/esup-runner/runner
make sync-transcription-cpu
```

If this runner must achieve **transcription** tasks on a **GPU** server, install the GPU transcription extra:

```bash
cd /opt/esup-runner/runner
make sync-transcription-gpu
```

Notes:
- Current transcription dependency support:
  - `transcription-cpu`: supported on Linux x86_64 and macOS Apple Silicon (`arm64`).
  - `transcription-gpu`: supported on Linux x86_64 GPU/CUDA hosts.
  - `transcription-gpu-cuda12`: CUDA 12.6 GPU profile, pinned to `torch==2.13.0+cu126`.
  - macOS Intel (`x86_64`) is not supported for transcription with the current `torch` stack because upstream wheels are no longer published for that platform.
- On Debian/Ubuntu GPU transcription hosts, ensure OS build headers are present:
  `sudo apt install -y build-essential python3-dev` (avoids `fatal error: Python.h: No such file or directory` when Triton JIT compiles helper modules).

### Optional: refresh `uv.lock` for GPU environments

Use these commands only when you intentionally update the lockfile during an upgrade.

- `make lock-upgrade-gpu-12`
  - Utility: regenerate the lockfile with a CUDA 12-compatible GPU stack.
  - Typical case: production servers still run CUDA 12.x / older NVIDIA compatibility constraints.

- `make lock-upgrade-gpu-latest`
  - Utility: regenerate the lockfile against the latest GPU torch/CUDA stack.
  - Typical case: production GPU servers are fully updated and you want the latest stack.

Then re-apply dependencies with:

```bash
cd /opt/esup-runner/runner
make sync-transcription-gpu
```

For the CUDA 12 profile, use:

```bash
cd /opt/esup-runner/runner
make sync-transcription-gpu-cuda12
```

---

## 6) Pre-start checks

From `/opt/esup-runner/runner`:

```bash
uv run scripts/check_config.py
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_gpu.py
uv run scripts/check_runner_resources.py
uv run scripts/check_runner_storage.py
```

- `check_config.py` validates the effective `.env` with the same schema used at Runner startup and reports all errors together without printing secrets.
- `check_ffmpeg.py` helps catch codec/build issues early.
- `check_gpu.py` validates that the current Python/Torch runtime can actually use CUDA for transcription.
- `check_runner_resources.py` validates CPU/RAM/GPU/config.
- `check_runner_storage.py` validates free space and permissions for configured storage directories, including the `uv` cache directory.

---

## 7) Re-initialize directories (if needed)

If new directories are required (logs/storage), run again:

```bash
cd /opt/esup-runner/runner
sudo make init
```

---

## 8) Update / redeploy the systemd service

> Warning
> `make create-service` **overwrites** `~/.config/systemd/user/esup-runner-runner.service` with `production/esup-runner-runner.service`.
> If you customized the unit, diff it first.

```bash
cd /opt/esup-runner/runner
make create-service
```

Run this as the service user (without `sudo`) so the unit is installed under `~/.config/systemd/user/`.

This performs: service copy → `systemctl --user daemon-reload` → `systemctl --user enable --now`.

Restart the service to load the updated code, dependencies and configuration,
including when the unit file did not change:

```bash
systemctl --user restart esup-runner-runner
```

---

## 9) Post-upgrade validation

### Service

```bash
systemctl --user status esup-runner-runner
journalctl --user -u esup-runner-runner -n 200 --no-pager
```

### Base endpoint

```bash
curl "http://127.0.0.1:<RUNNER_PORT>/"
```

### Registration on the Manager side

- Check Runner logs to confirm it registers successfully.
- Check Manager logs to confirm the runner is accepted (no `X-Runner-Version` compatibility errors).

---

## 10) Rollback procedure

1) Stop the service:

```bash
systemctl --user stop esup-runner-runner
```

2) Go back to a known tag/commit:

```bash
cd /opt/esup-runner
git fetch --tags
git checkout runner-vX.Y.Z
```

3) Resync dependencies with the same profile used before the rollback, then restart:

```bash
cd /opt/esup-runner/runner
make sync
# For transcription, replace make sync with the matching profile:
# make sync-transcription-cpu
# make sync-transcription-gpu
# make sync-transcription-gpu-cuda12
systemctl --user start esup-runner-runner
```

4) Check logs and registration to the Manager.

---

## 11) Automated stack update script

A helper script is available in monorepo deployments:

```bash
cd /opt/esup-runner
./update-stack.sh --help
```

Detection rules used by the script:
- Manager is considered installed when `/opt/esup-runner/manager/.env` exists.
- Runner is considered installed when `/opt/esup-runner/runner/.env` exists.

After `git pull`, the script reports changes to `.env.example` and configuration
code for updated components in the console and, when configured, the update
email. Review [parameters](PARAMETERS.md) and [configuration](CONFIGURATION.md)
before adapting your local `.env` manually. Email uses `SMTP_*` and
`MANAGER_EMAIL` from `runner/.env`; `--skip-email` disables it.

The comparison covers only changes brought by this pull and is skipped with
`--skip-git-update` or `--dry-run`. A comparison failure returns a non-zero exit
code but lets the remaining update steps continue.

The script also checks the Runner FFmpeg for CVE-2026-8461 (MagicYUV): below
8.1.2 or with an unreadable version, it adds `magicyuv` to `MEDIA_CODEC_DENYLIST`
in `runner/.env`; from 8.1.2, it reports that `magicyuv` can be removed from the
[media denylist](CONFIGURATION.md#media-denylist).

Examples:

```bash
cd /opt/esup-runner

# Update detected components (manager and/or runner)
./update-stack.sh

# Update runner only
./update-stack.sh --runner-only

# Force CPU transcription dependencies
./update-stack.sh --runner-only --runner-sync-mode transcription-cpu

# Force GPU transcription dependencies
./update-stack.sh --runner-only --runner-sync-mode transcription-gpu

# Keep the CUDA 12 profile without refreshing the lockfile
./update-stack.sh --runner-only --runner-sync-mode transcription-gpu-cuda12

# GPU update with lock refresh for CUDA 12 hosts
./update-stack.sh --runner-only --runner-sync-mode transcription-gpu --gpu-lock-profile cuda12

# GPU update with lock refresh for latest GPU stack
./update-stack.sh --runner-only --runner-sync-mode transcription-gpu --gpu-lock-profile latest

# Display commands without executing them
./update-stack.sh --runner-only --dry-run --skip-uv-update --skip-git-update
```

### Scheduled updates

As the service account (`esup-runner` by default), prepare the log directory,
enable the systemd user session at boot, and open its crontab:

```bash
sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/log/esup-runner
sudo loginctl enable-linger "$(id -un)"
crontab -e
```

The following cron entry is an example for Sunday at 03:00 (the host's cron
timezone). Adjust the schedule, checkout and `uv` paths, and `update-stack.sh`
options for your deployment:

```cron
0 3 * * 0 cd /opt/esup-runner && PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" XDG_RUNTIME_DIR="/run/user/$(id -u)" UV_CACHE_DIR=/tmp/esup-runner-uv-cache-$(id -un) ./update-stack.sh >> /var/log/esup-runner/update-stack.log 2>&1
```

For example, for GPU transcription with a lock refresh to the latest GPU stack,
replace `./update-stack.sh` in the cron entry with:

```bash
./update-stack.sh --runner-sync-mode transcription-gpu --gpu-lock-profile latest
```

Standard output and errors are appended to
`/var/log/esup-runner/update-stack.log`. Use one entry per host: the
script updates both components when both `.env` files exist. Reserve a maintenance
window with no active tasks or broadcasts; updates can restart the services.
Schedule [MediaMTX updates](../../manager/docs/MEDIAMTX_SETUP.md#scheduled-updates)
separately.

Notes:
- `--runner-sync-mode auto` (default) uses this heuristic:
  - if `RUNNER_TASK_TYPES` does not contain `transcription`: `make sync`
  - if CUDA 12 Torch (`+cu126`) is installed: `make sync-transcription-gpu-cuda12`
  - if transcription is enabled and GPU is detected/configured: `make sync-transcription-gpu`
  - otherwise: `make sync-transcription-cpu`
- Dependency profiles are selected by the update script, outside Runner configuration.
  Use `--runner-sync-mode` only for a one-time override. `--gpu-lock-profile latest`
  explicitly switches GPU transcription to the default GPU profile.
- By default, `make init` is skipped. Use `--with-init` only when directories/permissions changed.
- By default, restart policy is `if-changed` (restart only when `runner/` changed after git update, after a local GPU lock regeneration, or after a `MEDIA_CODEC_DENYLIST` change). You can force a restart with `--always-restart`, or disable it with `--no-restart`.
- If `make init` needs elevated privileges (for example when `.env` paths are under `/var`), run the script as `esup-runner` and configure restricted passwordless `sudo` for `make init`.
