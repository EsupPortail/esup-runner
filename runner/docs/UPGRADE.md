# Upgrade Guide — ESUP Runner (Runner)

This document describes a safe **production** upgrade procedure for the *Runner*.

> TL;DR
> - **PATCH**: you can usually upgrade Runner and Manager independently.
> - **MINOR/MAJOR**: a **coordinated** upgrade is required (the Manager enforces `MAJOR.MINOR`).

---

## 0) Prerequisites

- Installed under `/opt/esup-runner` (or equivalent).
- systemd service installed: `esup-runner-runner`.
- Configuration file: `/opt/esup-runner/runner/.env`.

Useful docs:
- Installation: `INSTALLATION.md`
- Configuration: `RUNNER_CONFIGURATION.md`
- Parameters: `RUNNER_PARAMETERS.md`
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
  - `LOG_DIRECTORY` (default: `/var/log/esup-runner`)
- For transcription runners:
  - `WHISPER_MODELS_DIR` (model cache)

---

## 3) Update sources

This deployment uses a **monorepo clone**: the Git working tree is `/opt/esup-runner`, and the projects live in subdirectories (`manager/`, `runner/`).

If you use sparse-checkout, ensure the correct directories are included **depending on what is installed on this machine**:

- If **Runner + Manager are installed on the same machine**: include both.
- If **only Runner is installed**: include only `runner`.

```bash
cd /opt/esup-runner

git fetch --tags
git pull --ff-only

# Option: switch to a tagged version
# git checkout vX.Y.Z

# Sparse-checkout selection:
# - same machine (runner + manager):
#   git sparse-checkout set manager runner
# - runner only:
#   git sparse-checkout set runner
git sparse-checkout set runner
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

If this runner achieve only **encoding** ot **studio** tasks, install the default:
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

---

## 6) Pre-start checks

From `/opt/esup-runner/runner`:

```bash
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_runner_resources.py
```

- `check_ffmpeg.py` helps catch codec/build issues early.
- `check_runner_resources.py` validates disk/RAM/config.

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
> `make create-service` **overwrites** `/etc/systemd/system/esup-runner-runner.service` with `production/esup-runner-runner.service`.
> If you customized the unit, diff it first.

```bash
cd /opt/esup-runner/runner
sudo make create-service
```

This performs: service copy → `daemon-reload` → `enable` → `restart`.

---

## 9) Post-upgrade validation

### Service

```bash
sudo systemctl status esup-runner-runner
sudo journalctl -u esup-runner-runner -n 200 --no-pager
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
sudo systemctl stop esup-runner-runner
```

2) Go back to a known tag/commit:

```bash
cd /opt/esup-runner
git fetch --tags
git checkout vX.Y.Z
```

3) Resync dependencies and restart:

```bash
cd /opt/esup-runner/runner
make sync
sudo systemctl start esup-runner-runner
```

4) Check logs and registration to the Manager.
