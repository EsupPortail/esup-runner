# Operations Guide (Runner)

This document centralizes day-to-day production operations for the runner.

## Scope and assumptions

- Installation path: `/opt/esup-runner/runner`
- Service name: `esup-runner-runner` (systemd user service)
- Service user: `esup-runner`
- Configuration file: `/opt/esup-runner/runner/.env`

## Daily command quick reference

```bash
# Service state
systemctl --user status esup-runner-runner
systemctl --user is-active --quiet esup-runner-runner && echo "OK: service is running"

# Lifecycle
systemctl --user restart esup-runner-runner
systemctl --user stop esup-runner-runner
systemctl --user start esup-runner-runner

# Logs
journalctl --user -u esup-runner-runner -f
journalctl --user -u esup-runner-runner -n 200 --no-pager
```

## Task handling after service restart

When you restart the service:

```bash
systemctl --user restart esup-runner-runner
```

the runner automatically checks tasks that were in progress before restart.

In most cases, no manual action is needed:

- tasks that are still running continue to be tracked
- tasks that already produced final files are marked as completed
- tasks in `failed`/`timeout` with a persisted request are automatically restarted
- tasks that cannot be recovered are marked as failed (or timeout)

The runner then sends updated task status back to the manager.
During this startup reconciliation window, runner availability is kept `false`
to avoid accepting new work too early.

### Quick operator check

After a restart, verify only these points:

1. Service is up.
2. API health/readiness endpoints respond.
3. Logs do not show repeated recovery errors.

```bash
systemctl --user status esup-runner-runner
journalctl --user -u esup-runner-runner -n 200 --no-pager
journalctl --user -u esup-runner-runner -n 300 --no-pager | grep -E "Recovering|Inspecting|Scheduled automatic restart|Failed to recover|Skipping automatic restart"
```

## API health and readiness checks

The runner exposes:

- root endpoint: `/`
- health endpoint: `/runner/health`
- readiness endpoint: `/runner/ping`
- status endpoint: `/runner/status`

Example checks:

```bash
curl "http://127.0.0.1:<RUNNER_PORT>/"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/health"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/ping"
curl "http://127.0.0.1:<RUNNER_PORT>/runner/status"
```

Expected behavior:

- `/runner/health` should report `status=healthy`.
- `/runner/ping` should report `available=true` when the instance is idle.
- `/runner/ping` and `/runner/status` should report `registered=true` after successful manager registration.

## Multi-instance checks

For grouped task-type deployments, check every port from `RUNNER_BASE_PORT` to
`RUNNER_BASE_PORT + instance_count - 1`.

Example for ports `8082..8085`:

```bash
for p in 8082 8083 8084 8085; do
  echo "---- port ${p} ----"
  curl -s "http://127.0.0.1:${p}/runner/ping"
  echo
done
```

## Operational validation scripts

From `/opt/esup-runner/runner`:

```bash
uv run scripts/check_version.py
uv run scripts/check_ffmpeg.py
uv run scripts/check_gpu.py
uv run scripts/check_runner_resources.py
uv run scripts/check_runner_storage.py
```

Use these checks:

- after installation/upgrade
- after changing `.env`
- after changing FFmpeg/GPU drivers
- before and after maintenance windows

## Storage and retention operations

Task outputs/manifests are stored under `STORAGE_DIR`.

Automatic cleanup behavior:

- `MAX_FILE_AGE_DAYS`:
  - `0` means unlimited retention (no age-based deletion)
  - `> 0` enables age-based deletion
- `CLEANUP_INTERVAL_HOURS` controls periodic cleanup cadence

Manual cleanup helper:

```bash
cd /opt/esup-runner/runner
uv run scripts/manual_cleanup.py
```

The script reads `.env`, previews items older than `MAX_FILE_AGE_DAYS`, then asks for confirmation.

Recommended maintenance flow for aggressive cleanup:

1. Stop the runner service.
2. Run `scripts/manual_cleanup.py`.
3. Validate free space with `scripts/check_runner_storage.py`.
4. Start the runner service.
5. Verify `/runner/health` and `/runner/ping` on each instance.

## Transcription cache operations

For transcription-enabled runners, monitor cache directories:

- `CACHE_DIR`
- `WHISPER_MODELS_DIR` (default: `CACHE_DIR/whisper-models`)
- `HUGGINGFACE_MODELS_DIR` (default: `CACHE_DIR/huggingface`)
- `UV_CACHE_DIR` (default: `CACHE_DIR/uv`)

Operational notes:

- Keep enough free space before first model download or model upgrades.
- Use `uv run scripts/check_runner_storage.py` to validate free space and writability.
- If caches are intentionally purged, next transcription tasks will re-download model assets.

## Manager registration troubleshooting

If runner API is healthy but `registered=false`:

1. Check `MANAGER_URL` in `.env`.
2. Check `RUNNER_TOKEN` matches a manager `AUTHORIZED_TOKENS__*`.
3. Check runner/manager version compatibility (`MAJOR.MINOR` must match).
4. Inspect logs:
   - runner: `journalctl --user -u esup-runner-runner -n 200 --no-pager`
   - manager: corresponding manager service logs

## Backup checklist (before risky operations)

Before upgrades or large cleanup operations, back up at least:

- `/opt/esup-runner/runner/.env`
- `STORAGE_DIR`
- `LOG_DIR` (default `/var/log/esup-runner`)
- For transcription runners: `CACHE_DIR`

## Related docs

- Installation: [INSTALLATION.md](INSTALLATION.md)
- Configuration: [CONFIGURATION.md](CONFIGURATION.md)
- Parameters: [PARAMETERS.md](PARAMETERS.md)
- Upgrade and rollback: [UPGRADE.md](UPGRADE.md)
- Docker deployment: [DOCKER.md](DOCKER.md)
- FFmpeg setup: [FFMPEG_SETUP.md](FFMPEG_SETUP.md)
