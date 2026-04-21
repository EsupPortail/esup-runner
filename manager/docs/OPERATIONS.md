# Operations Guide (Manager)

This document centralizes day-to-day production operations for the manager.

## Scope and assumptions

- Installation path: `/opt/esup-runner/manager`
- Service name: `esup-runner-manager` (systemd user service)
- Service user: `esup-runner`
- Configuration file: `/opt/esup-runner/manager/.env`

## Daily command quick reference

```bash
# Service state
systemctl --user status esup-runner-manager
systemctl --user is-active --quiet esup-runner-manager && echo "OK: service is running"

# Lifecycle
systemctl --user restart esup-runner-manager
systemctl --user stop esup-runner-manager
systemctl --user start esup-runner-manager

# Logs
journalctl --user -u esup-runner-manager -f
journalctl --user -u esup-runner-manager -n 200 --no-pager
```

## Health checks and validation

Use one valid manager token (`AUTHORIZED_TOKENS__*`) from `.env`.

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/manager/health"

curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/api/version"
```

Optional local validation helpers:

```bash
cd /opt/esup-runner/manager
uv run scripts/check_runtime.py
uv run scripts/check_version.py
```

## Admin UI runbook

- `/admin`: global dashboard (runners + recent tasks)
- `/tasks`: task browsing/search and bulk actions
- `/statistics`: usage analytics from `data/task_stats.csv`

Task operations from `/tasks`:

- Bulk restart: failed/timeout/warning/completed tasks
- Bulk delete: non-running tasks only

## Task retention behavior

Task JSON persistence (`data/YYYY-MM-DD/*.json`) is automatically cleaned based on:

- `CLEANUP_TASK_FILES_DAYS` in `.env` (default: `60`)

Important:

- This retention applies to persisted task JSON data.
- It does not purge `data/task_stats.csv` (statistics history CSV).

## Statistics maintenance (`data/task_stats.csv`)

The statistics dashboard reads an append-only CSV file:

- `data/task_stats.csv`

Important:

- Date filters in `/statistics` only affect display; they do not modify stored CSV data.
- There is currently no automatic retention/reset for `data/task_stats.csv`.
- To reset statistics or keep only a specific period, edit the CSV on the server.

Recommended procedure:

1. Back up the file:

```bash
cp data/task_stats.csv "data/task_stats.$(date +%F-%H%M%S).bak.csv"
```

2. Stop the manager before editing:

```bash
systemctl --user stop esup-runner-manager
```

3. Keep only one period (example: from `2026-01-01` to `2026-03-31`), preserving header:

```bash
awk -F, 'NR==1 || ($2 >= "2026-01-01" && $2 <= "2026-03-31")' \
  data/task_stats.csv > data/task_stats.filtered.csv
mv data/task_stats.filtered.csv data/task_stats.csv
```

4. Or reset all statistics (truncate the file):

```bash
: > data/task_stats.csv
```

After truncation, the header is recreated automatically on next task write.

5. Start the manager:

```bash
systemctl --user start esup-runner-manager
```

Notes:

- Keep CSV format and UTF-8 encoding when editing manually.
- Expected columns: `task_id,date,task_type,status,app_name,app_version,etab_name`.
- You can download the current CSV from `/statistics/task-stats.csv` before maintenance.

## Backup checklist (before risky operations)

Before upgrades or manual cleanup operations, back up at least:

- `/opt/esup-runner/manager/.env`
- `/opt/esup-runner/manager/data/`
- `LOG_DIR` defined in `.env` (default `/var/log/esup-runner`)
- Optional: `CACHE_DIR` from `.env`

## Related docs

- Installation: [INSTALLATION.md](INSTALLATION.md)
- Configuration: [CONFIGURATION.md](CONFIGURATION.md)
- Parameters: [PARAMETERS.md](PARAMETERS.md)
- Upgrade and rollback: [UPGRADE.md](UPGRADE.md)
- Docker deployment: [DOCKER.md](DOCKER.md)
