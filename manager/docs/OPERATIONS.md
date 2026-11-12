# Operations Guide (Manager)

This document centralizes day-to-day production operations for the manager.

For RTMP-to-HLS Live operations, use the dedicated
[Live operations runbook](LIVE_OPERATIONS.md). For a new native Live deployment,
start with the [Manager quickstart](LIVE_QUICKSTART.md). For Docker, use
[Live Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut).

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

This token can be provisioned either from the admin UI (`/admin/credentials`) or with the CLI helper script (`uv run scripts/generate_token.py`).

```bash
curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/api/health"

curl -H "X-API-Token: <AUTHORIZED_TOKEN>" \
  "http://127.0.0.1:<MANAGER_PORT>/api/version"
```

Optional local validation helpers:

```bash
cd /opt/esup-runner/manager
uv run scripts/check_config.py
uv run scripts/check_runtime.py
uv run scripts/check_version.py
uv run scripts/check_pipeline_tasks.py
# Optional: include transcription + translation smoke checks
uv run scripts/check_pipeline_tasks.py --with-transcription-translation
```

Run `check_config.py` first after editing `.env` and before restarting the
service. It validates the effective configuration, reports all detected errors
together without printing secrets, and returns `2` when invalid.

`check_pipeline_tasks.py` reads the private `MANAGER_URL` and manager tokens from `.env` by default; `RUNNER_API_TOKEN` and `RUNNER_MANAGER_URL` remain available as optional overrides. The optional client callback is disabled by default; set `RUNNER_NOTIFY_URL` only when you want the smoke test to exercise a reachable `notify_url`.

## Automatic MediaMTX monitoring

With `LIVE_ENABLED=true`, the Manager checks its own MediaMTX Control API at
startup and every `LIVE_MEDIAMTX_HEALTH_CHECK_INTERVAL_HOURS` hours (default
`12`; `0` disables). Only one worker performs the checks to avoid duplicate
alerts. Each failure emails `MANAGER_EMAIL` using the existing SMTP settings
and the same format as Runner GPU alerts. Restart the Manager after
configuration changes.

## Admin UI runbook

- `/admin`: global dashboard (runners + recent tasks)
- `/tasks`: task browsing/search and bulk actions
- `/statistics`: usage analytics from `data/task_stats.csv`
- `/admin/credentials`: credentials management for `AUTHORIZED_TOKENS__*` and `ADMIN_USERS__*`

From `/admin/credentials`, operators can:

- Generate and store API tokens directly in `.env`
- Generate and store admin bcrypt password hashes directly in `.env`
- Copy or delete existing token/admin entries
- Reload runtime configuration after updates

Equivalent CLI workflow remains available with:

- `uv run scripts/generate_token.py`
- `uv run scripts/generate_password.py`

Task operations from `/tasks`:

- Bulk restart: failed/timeout/warning/completed tasks
- Bulk delete: non-running tasks only

### Acknowledge a task incident

After handling a failed, warning or timed-out task, click **Acknowledge** in
**Needs attention** or on its detail page. This clears the dashboard alert; the
task stays in `/tasks` with an **acknowledged** badge, preserving its status,
history and statistics. Acknowledgments survive Manager restarts and are cleared
when the task is restarted. With an updated Runner, acknowledged incidents also
prevent automatic task recovery after Runner service restarts, without sending
new failure emails. Tasks can still be restarted manually from the Manager.

### Actions fail with `Invalid request origin`

In `manager/.env`, set `MANAGER_PUBLIC_URL` to the browser-facing base URL,
including the public port and proxy prefix if needed, but without `/admin` or
`/tasks`. Check the value suggested by the error message and add any missing
prefix. Example:

```properties
MANAGER_PUBLIC_URL=https://server.univ.fr/runner-manager
```

Validate the configuration as described above, restart the Manager with
`systemctl --user restart esup-runner-manager`, then reload the browser page.

## Diagnosing OOM kills

If systemd reports `Failed with result 'oom-kill'`, compare the service's memory
usage with its configured limits. Check kernel logs for the killed process and
whether the host or a cgroup ran out of memory, then correlate the incident time
with Manager activity (requests, downloads, and concurrent tasks):

```bash
systemctl --user show esup-runner-manager \
  -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax -p MemorySwapMax
journalctl --user -u esup-runner-manager --since '1 hour ago' --no-pager
# Kernel logs may require elevated read permissions.
journalctl -k --since '1 hour ago' --no-pager
```

For a system service, omit `--user` from the service commands. Adjust `--since`
to cover the incident; `MemoryCurrent` reflects usage after any restart.

## Running task reconciliation

The Manager checks running file-based tasks with their Runner at startup and
every hour, and saves terminal statuses. Results from previous execution
attempts are ignored with updated Runners.

If the assigned Runner returns `not_found` for a task last updated at least
60 seconds ago, the Manager checks again after 10 seconds. A second matching
`not_found` response marks the unchanged task as `failed`, allowing **Restart**
in the task interface. Network errors, HTTP errors and missing Runners do not
count as confirmation. This does not restart the encoding automatically or
change the Runner's availability. Restarting the Manager triggers this check;
restarting a Runner can only recover tasks from its retained state and outputs.

See [Runner periodic verification](../../runner/docs/OPERATIONS.md#periodic-running-task-verification)
for the Runner's local checks.

## Task retention behavior

Task JSON persistence (`data/YYYY-MM-DD/*.json`) is automatically cleaned based on:

- `CLEANUP_TASK_FILES_DAYS` in `.env` (default: `30`; set `0` to disable age-based cleanup)

Use a short, non-zero retention to limit task accumulation and memory usage.

Important:

- This retention applies to persisted task JSON data.
- It does not purge `data/task_stats.csv` (statistics history CSV).
- It does not delete Runner outputs in `STORAGE_DIR`; see [Runner storage maintenance](../../runner/docs/OPERATIONS.md#storage-and-retention-operations).

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
- When date filters are active on `/statistics`, the CSV download link exports only the selected period.

## Disk space maintenance

### Diagnose usage

Adapt these paths to `SERVICE_USER`, `LOG_DIR`, `STORAGE_DIR` and `CACHE_DIR` in
`.env`; omit paths absent from this host:

```bash
df -h                         # Filesystem space
df -i                         # Inodes (too many small files can exhaust them)
du -xhd1 /opt/esup-runner/manager/data /var/log/esup-runner \
  /tmp/esup-runner /home/esup-runner/.cache
```

If space remains occupied after deletion, use `sudo lsof +L1` to find deleted
files still open. Restart their owning service during maintenance to release space.

### Log retention

Manager/Runner application logs in `LOG_DIR` already rotate at 10 MiB with
10 backups per file. Systemd journals have separate retention:

```bash
journalctl --user --disk-usage
# Optional: remove archived journals older than 14 days across the whole host
sudo journalctl --rotate --vacuum-time=14d
```

Configure ongoing journal limits in `journald.conf`. Rotate MediaMTX file logs
separately; see [MediaMTX diagnostics](MEDIAMTX_SETUP.md#authentication-diagnostics-in-administration).

### Docker images and build cache

Run on each Docker host, using the same daemon as the deployment:

```bash
docker system df -v            # Images, build cache and volumes
docker builder prune          # Remove dangling build cache
docker image prune            # Remove dangling images
```

These commands affect all projects on that daemon and preserve volumes.
Keep application data and model-cache volumes; avoid volume pruning.
See Docker's [build cache](https://docs.docker.com/reference/cli/docker/builder/prune/)
and [image cleanup](https://docs.docker.com/reference/cli/docker/image/prune/) references.

## Free disk space: uv cache

Run as `SERVICE_USER` (default: `esup-runner`) on each host. Check `CACHE_DIR`
and `UV_CACHE_DIR` in each component's `.env` and any shell/service overrides.
Common cache locations are:

- `/home/esup-runner/.cache/uv`: uv's default Linux cache (`$XDG_CACHE_HOME/uv` if set).
- `/home/esup-runner/.cache/esup-runner/uv`: the project's default (`UV_CACHE_DIR`, otherwise `CACHE_DIR/uv`).

Adapt the paths to your configuration. Wait for active Runner tasks to finish,
then stop the services sharing these caches. Omit services not installed on this
host and restart only those stopped:

```bash
systemctl --user stop esup-runner-manager
systemctl --user stop esup-runner-runner

uv cache prune --cache-dir /home/esup-runner/.cache/uv
uv cache prune --cache-dir /home/esup-runner/.cache/esup-runner/uv

systemctl --user start esup-runner-manager
systemctl --user start esup-runner-runner
```

`prune` removes unused cache entries. To clear all uv cache entries, replace
`prune` with `clean`; subsequent dependency operations may download packages
again. Existing `.venv` environments and Runner model caches are preserved.
Do not remove the entire `CACHE_DIR`, which also contains transcription models.

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

## Live streaming

With Live enabled, RTMP publications accepted by MediaMTX start HLS automatically
by default. Keep the Manager, Runner and MediaMTX services running; no start API
call is needed from an encoder. The Manager waits for available Live capacity,
stops the session after encoder disconnection, and creates a new session when
the encoder republishes. Read the playback URL from **Live** in administration
or `GET /live/sources/{source_id}` with an API token. List identifiers with
`GET /live/sources`; source IDs are the broadcaster names at the end of the RTMP
paths. `LIVE_SOURCES` is only needed to declare sources before publication.

An explicit stop or terminal failure suppresses automatic restart for the same
RTMP connection. Fix failures, then republish or use **Restart session** after
cleanup when the encoder stays connected.
Use **Live diagnostics** in the Logs page to filter `[LIVE]` events, including
MediaMTX authentication refusals when a diagnostic log source is configured.
See [Live diagnostics](LIVE.md#live-diagnostics-in-the-log-viewer) for native and Docker setup.
Preserve `.live-publication-*.json` checkpoints and the private `.live-sources.json`
registry when backing up `MANAGER_DATA_DIR`. The registry contains RTMP paths/keys;
keep its owner-only permissions. Adding publishers requires no Manager restart.
For manual API orchestration only, set `LIVE_AUTO_START=false` and restart the
Manager. See [automatic operation](LIVE.md#automatic-rtmp-to-hls-operation).

See [LIVE.md](LIVE.md) for Live Runner reservations, MediaMTX, the dynamic `hls_url` contract for Pod and multi-instance deployments.

## Live monitoring

Use **Live** on the administration dashboard to inspect sessions, HLS readiness,
source measurements, stale reports and available runners, or request a stop.
See [Live administration](LIVE.md#administration-and-statistics) for refresh,
retention and statistics semantics.
