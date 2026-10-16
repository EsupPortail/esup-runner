# Live installation

Install Live on top of a working [Manager](INSTALLATION.md) and
[Runner](../../runner/docs/INSTALLATION.md). This guide uses the standard native
layout and CPU transcoding; see [MediaMTX setup](MEDIAMTX_SETUP.md) for custom
ports/paths and [Compose](#docker-compose-shortcut) for a container deployment.

## Before you start

- Linux, Python 3.11+, `uv`, the application's dependencies (`make sync`), and
  the service account's checkout at `/opt/esup-runner` on each host.
- Normal application `.env` files and user service units already prepared,
  with matching Manager/Runner tokens and an administrator account.
- FFmpeg/ffprobe with libx264 and AAC on the Runner; see
  [FFmpeg setup](../../runner/docs/FFMPEG_SETUP.md).
- A working `systemctl --user` login session. Enable
  [lingering](MEDIAMTX_SETUP.md#logs-restarts-and-boot-persistence) for startup
  at boot and after logout.
- Encoder and Runner access to the Manager's RTMP port, and a private
  Manager/Runner application network. Keep Control APIs private.

Two MediaMTX services are needed, even on one host: the Manager's receives RTMP;
the Runner's relays it to FFmpeg. Every Live worker on a Runner server shares
that server's MediaMTX. One worker handles one broadcast until cleanup finishes.
Remote viewers also need an HTTPS route to each worker's `/hls/` endpoint.

## Recommended native installation

Run commands as the service account, without `sudo`. MediaMTX defaults to
**1.21.1**, the minimum supported version; newer stable installations are reused.
Existing complete configurations and passwords are preserved.

### 1. Prepare and start MediaMTX

On the Manager host (prefer a specific private interface where possible):

```bash
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py setup --instance manager \
  --rtmp-address 0.0.0.0:1935
python3 scripts/install_mediamtx.py start --instance manager
```

On each Runner host:

```bash
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py setup --instance runner
python3 scripts/install_mediamtx.py start --instance runner
```

On a shared host, run both pairs. `setup` installs/reuses the executable and
prepares private YAML/environment files. `start` validates the prepared
configuration, installs the user unit, enables/starts it and checks its state.
Add `--dry-run` to preview. It preserves customized units and does not restart
an already active service; see [service management](MEDIAMTX_SETUP.md#4-start-the-native-systemd-service).

### 2. Connect the applications

On the Manager host:

```bash
cd /opt/esup-runner/manager
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --instance manager
```

On the Runner host:

```bash
cd /opt/esup-runner/runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --instance runner \
  --manager-rtmp-host manager.example.org
```

Replace the hostname with the Manager's RTMP address reachable from the Runner.
Enter the Manager MediaMTX `relay` password from its private YAML when prompted.
On a shared host, omit `--manager-rtmp-host` to read it locally.
The helpers update connection settings and save private `.env` backups.
The Manager helper enables Live; retain the default `LIVE_AUTO_START=true`.

Set capacity and public routing in `runner/.env`, for example:

```dotenv
RUNNER_TASK_TYPES=[1x(encoding,studio),2x(live)]
RUNNER_HOST=runner.example.org
LIVE_PUBLIC_HLS_BASE_URL=https://live.example.org/runner-{instance_id}/hls
```

This starts VOD at instance 0 and Lives at instances 1 and 2, normally ports
8082, 8083 and 8084. Route `/runner-1/hls/` and `/runner-2/hls/` to the matching
workers using the [proxy examples](../../runner/docs/LIVE.md#service-and-hls-exposure).
For direct local HTTP testing, omit `LIVE_PUBLIC_HLS_BASE_URL` and use
`RUNNER_HOST=127.0.0.1`; the application derives each URL from its actual port.
HLS defaults to `/dev/shm/hls`, writable by the service account.

### 3. Start the applications

Add the [Runner service drop-in](../../runner/docs/LIVE.md#use-the-main-runner-service-with-a-mixed-or-grouped-env)
so the main Runner service depends on MediaMTX and stops all workers/FFmpeg
together. Run `systemctl --user daemon-reload` after editing units.
Start each normal application service on its host:

```bash
systemctl --user enable --now esup-runner-manager.service  # Manager host
systemctl --user enable --now esup-runner-runner.service   # Runner host
```

For applications already running, finish active tasks/broadcasts and use
`systemctl --user restart <service>` to load the edited `.env`.
`enable --now` does not reload an active application's environment.

### 4. Check readiness

On the relevant hosts, check each application and its MediaMTX service:

```bash
systemctl --user --no-pager status esup-runner-mediamtx@manager.service esup-runner-manager.service
systemctl --user --no-pager status esup-runner-mediamtx@runner.service esup-runner-runner.service
```

Check each local Control API using that instance's `control` password:

```bash
curl --fail --show-error --user control http://127.0.0.1:9997/v3/paths/list
curl --fail --show-error --user control http://127.0.0.1:10997/v3/paths/list
```

Both should return HTTP 200 with an `items` list. Log into the Manager's
`/admin/live` and confirm at least one available Live Runner.

### 5. Perform the first Live test

Configure the encoder for H.264/AAC, the Manager's RTMP address, the `publisher`
credentials from its private YAML, and an allowed path, for example:

```text
rtmp://manager.example.org:1935/live/amphi-a?user=publisher&pass=...
```

URL-encode reserved characters in credentials. Publishing starts the session
automatically. Within the next discovery passes, `/admin/live` should show
`running`, `hls_ready=true` and a playback URL. Use that exact URL in the player.
Stop publishing and confirm the Runner becomes available after cleanup.
For a repeatable CPU-only test, use the
[smoke test](LIVE.md#reproducible-test-without-streaming-hardware).

## Docker Compose shortcut

For a self-contained evaluation or a small deployment, use the supplied
[Compose stack](../../deploy/live/compose.yml). The example starts one Live
Runner and uses loopback addresses for a first test on the Docker host.

### 1. Prepare private configuration files and build the images

For a new deployment, copy the examples below. For an existing deployment,
retain and review its configured files instead of copying over them:

```bash
cd /opt/esup-runner/deploy/live
umask 077
cp manager.env.example manager.env
cp runner.env.example runner.env
cp ../../manager/production/mediamtx-live.yml manager-mediamtx.yml
cp ../../runner/production/mediamtx-live.yml runner-mediamtx.yml
docker compose -p live-a build
```

### 2. Configure matching credentials and an administrator

Replace all active `CHANGE_ME_*` values in the four files. Use different secrets
for each role, with matching values only where the same credential is used:

| Credential | Matching locations |
| --- | --- |
| Runner API token | `manager.env` `AUTHORIZED_TOKENS__runners` and `runner.env` `RUNNER_TOKEN` |
| Client API token | `manager.env` `AUTHORIZED_TOKENS__pod` and the client application's token |
| Manager `publisher` password | `manager-mediamtx.yml` and the encoder's publishing credentials |
| Manager `relay` password | `manager-mediamtx.yml` and `runner.env` `LIVE_UPSTREAM_RTMP_URL` |
| Manager `control` password | `manager-mediamtx.yml` and `manager.env` `LIVE_MEDIAMTX_API_PASSWORD` |
| Runner `control` password | `runner-mediamtx.yml` and `runner.env` `LIVE_MEDIAMTX_API_PASSWORD` |
| Runner `transcoder` password | `runner-mediamtx.yml` and `runner.env` `LIVE_LOCAL_RTMP_URL` |

URL-encode reserved characters in RTMP URL credentials. For administration,
generate a bcrypt hash using the built Manager image; no host Python environment
is required. This command prompts privately for the password and prints its hash:

```bash
docker compose -p live-a run --rm --no-deps --entrypoint python manager -c \
  'from getpass import getpass; from app.core.passwords import BcryptPasswordContext; print(BcryptPasswordContext().hash(getpass("Admin password: ")))'
```

Replace `CHANGE_ME_BCRYPT_HASH` in `manager.env` with that hash, keeping the
single quotes around the `ADMIN_USERS__admin` value so Compose preserves its
dollar signs. Use the entered password to log in as `admin`; store only the hash
in the file. If the Manager's host Python environment is already installed,
`uv run scripts/generate_password.py` from `manager/` is an alternative; copy its
hash into the same single-quoted assignment. Without an `ADMIN_USERS__*` entry,
the administration interface is inaccessible even when API tokens work.

### 3. Set browser-facing URLs and encoder access

The examples work locally with the default published ports:

| File and setting | Local test value | Change it when |
| --- | --- | --- |
| `manager.env`: `MANAGER_PUBLIC_URL` | `http://127.0.0.1:8081` | The browser uses another address, port or HTTPS frontend; use that exact public URL for administration and CSRF validation |
| `runner.env`: `LIVE_PUBLIC_HLS_BASE_URL` | `http://127.0.0.1:8082/hls` | Viewers use another address, port or HTTPS frontend; use the URL routed to this Runner's `/hls/` endpoint |

Loopback URLs work only for a browser/player on the Docker host. For remote
viewers, configure the public HTTPS route before starting and set the HLS base URL
to it, for example `https://live.example.org/hls`. Follow
[Runner HLS exposure](../../runner/docs/LIVE.md#service-and-hls-exposure);
Compose does not create the reverse proxy, DNS record or TLS certificate.

Set `RTMP_BIND_IP` to an encoder-reachable host interface for a remote encoder;
it defaults to loopback for local testing. Export it in the shell running Compose,
or put it in `deploy/live/.env`. The same applies to host-port overrides such as
`MANAGER_HTTP_PORT` and `RUNNER_HTTP_PORT`; update the browser-facing URLs to match.
`manager.env` and `runner.env` configure applications inside their containers;
they do not supply variables for Compose host-port interpolation.

Keep internal container URLs using `manager-media` and `runner-media`; do not
replace these names with `127.0.0.1`.

### 4. Validate and start

Run from `deploy/live/`. The subshell stops before startup if any active
placeholder remains or Compose validation fails:

```bash
(
set -eu
# Ignore disabled optional settings; print only filenames, never secret values.
if awk '!/^[[:space:]]*#/ && /CHANGE_ME/ {
  if (!seen[FILENAME]++) print FILENAME
  found = 1
} END { exit !found }' manager.env runner.env manager-mediamtx.yml runner-mediamtx.yml; then
  echo "Replace placeholders in the listed files before starting Compose" >&2
  exit 1
else
  echo "No MediaMTX or application placeholders remain"
fi
docker compose -p live-a config --quiet
docker compose -p live-a up -d
docker compose -p live-a ps
)
```

The `awk` check ignores comment lines and prints filenames only. It does not
validate password matching or public routing. `config --quiet` validates the
Compose model without displaying the expanded environment. The Manager's
writable application logs stay at `/var/log/esup-runner`; it reads MediaMTX
diagnostics from the separate read-only mount `/var/log/mediamtx/mediamtx.log`.

### 5. Verify the first session

Open `http://127.0.0.1:8081/admin/live` for the default local test, or the configured
Manager public URL followed by `/admin/live`. Log in with the `admin` account and
confirm that the Live Runner is available. Inspect startup logs with:

```bash
docker compose -p live-a logs --tail=100 manager runner manager-media runner-media
```

Perform the [first Live test](#5-perform-the-first-live-test), using the Docker
host's reachable RTMP address and published port for the encoder. For a local
publisher, these default to `127.0.0.1:1935`. Wait for `running` and `hls_ready=true`,
then use the exact displayed `hls_url` in the player.

Before adding workers or publishing production traffic, follow the
[Compose reference](LIVE.md#docker-compose) for networking, persistent volumes
and additional Runner identities.

## Next step

Once the first stream works, read the
[Live operations runbook](LIVE_OPERATIONS.md). It explains the administration
page, logs, normal stops, restarts, capacity, and the recovery path for each
common failure.
