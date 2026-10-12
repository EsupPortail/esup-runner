# Live installation: the short path

This page is the operator's shortest route to a working RTMP-to-HLS Live
deployment. It assumes that the Manager and Runner applications are installed
or that you are following their [Manager installation](INSTALLATION.md) and
[Runner installation](../../runner/docs/INSTALLATION.md) guides.

For the detailed MediaMTX options, Docker variants and custom networks, keep
[MediaMTX setup](MEDIAMTX_SETUP.md) as the reference. For day-to-day use and
incident handling, use the [Live operations runbook](LIVE_OPERATIONS.md).

## The target architecture

You need two MediaMTX instances, even when Manager and Runner share a host:

| Component | Role | Must be reachable by |
| --- | --- | --- |
| Manager MediaMTX | Receives the encoder's authenticated RTMP publication | Encoder and Runner |
| Runner MediaMTX | Creates a temporary pull relay for each Live session | Runner only |
| Manager application | Discovers publications and assigns Live Runners | Administrators, clients and Runner |
| Runner application | Runs FFmpeg and serves temporary HLS playlists | Manager and the HLS frontend |

MediaMTX does not transcode. FFmpeg runs in the Runner environment. One
Runner instance handles one Live session; several Live instances share the
same Runner MediaMTX service.

## Before you start

- Linux host(s), the service account used by the Manager and Runner, and a
  checkout at `/opt/esup-runner`.
- One RTMP port reachable from the encoder to the Manager host.
- One private application/control network between Manager and Runner.
- FFmpeg with H.264/AAC support on the Runner. GPU/NVENC is optional; CPU is
  the simplest first installation.
- A public HTTPS route to the Runner's `/hls/` endpoint if viewers are outside
  the Runner host. The HLS URL returned by the Manager is the source of truth.

Do not expose MediaMTX Control APIs, Runner task APIs or credentials to viewers.
Use long, different passwords for publisher, relay, control and transcoder
accounts.

## Recommended native installation

The commands below default to MediaMTX `1.21.1`, the minimum accepted version,
and the standard instance names `manager` and `runner`. Run them as the service account. If
the Manager checkout is sparse, also include `runner/production/` for the
shared systemd unit template. The installation and connection scripts themselves
are contained in each application directory.

Any newer stable version may be reused or selected with `--version` for a new
installation. See [version selection and manual upgrades](MEDIAMTX_SETUP.md#manual-upgrades)
before replacing a running MediaMTX service; active broadcasts are interrupted.

Include the shared systemd template in a sparse Manager checkout:

```bash
cd /opt/esup-runner
git sparse-checkout add runner/production
```

### 1. Prepare MediaMTX

On the Manager host:

```bash
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py setup --instance manager \
  --rtmp-address 0.0.0.0:1935
```

On the Runner host:

```bash
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py setup --instance runner
```

The installer reuses a matching existing binary, generates private credentials
and writes the YAML under `~/.config/esup-runner/<instance>/`. It does not start
anything and never overwrites a complete existing configuration.

Install the shared user unit once on each host, then start the corresponding
instance. The complete procedure, including user lingering for boot, is in
[MediaMTX systemd setup](MEDIAMTX_SETUP.md#4-start-the-native-systemd-service).
Follow its YAML validation and unit installation steps before running the
essential start commands:

```bash
systemctl --user enable --now esup-runner-mediamtx@manager.service  # Manager host
systemctl --user enable --now esup-runner-mediamtx@runner.service     # Runner host
```

### 2. Connect the applications

Prepare the normal `manager/.env` and `runner/.env` first, including the
Manager/Runner token and the Runner task declaration. Install each application's
Python dependencies with `make sync` or its usual development/transcription
sync target; the connection helper uses that component's environment and `uv.lock`.
Then run the helper:

On the Manager host:

```bash
cd /opt/esup-runner/manager
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --instance manager
```

On the Runner host, with the Manager RTMP address reachable from that host:

```bash
cd /opt/esup-runner/runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --instance runner \
  --manager-rtmp-host manager.example.org
```

The Runner command prompts only for the Manager MediaMTX `relay` password. It
updates connection settings and creates a private `.env` backup; it does not
start or restart services.

In `runner/.env`, declare the capacity you need. For example, two simultaneous
Live sessions and one VOD worker:

```dotenv
RUNNER_TASK_TYPES=[1x(encoding,studio),2x(live)]
RUNNER_HOST=runner.example.org
LIVE_PUBLIC_HLS_BASE_URL=https://live.example.org/runner-{instance_id}/hls
```

Keep `LIVE_AUTO_START=true` in the Manager for the normal workflow: publishing
RTMP starts a session automatically. The final HLS URL must be routed by the
frontend to the owning Runner instance; never construct it from a guessed port.

### 3. Start the applications

Start MediaMTX before the applications. If the Runner uses a grouped
configuration, use the existing `esup-runner-runner.service`; do not also enable
the old templated Live service. Add the MediaMTX dependency drop-in described in
[Runner Live service setup](../../runner/docs/LIVE.md#use-the-main-runner-service-with-a-mixed-or-grouped-env)
and run `systemctl --user daemon-reload` before starting the application.

For a first application installation, enable and start the normal user services
after their `.env` files are complete. Run each command on the corresponding host:

```bash
systemctl --user enable --now esup-runner-manager.service
systemctl --user enable --now esup-runner-runner.service
```

For applications that are already running, finish active tasks and broadcasts,
then restart the affected services to load the updated `.env`:

```bash
systemctl --user restart esup-runner-manager.service  # Manager host
systemctl --user restart esup-runner-runner.service   # Runner host
```

`enable --now` alone does not restart an active service or apply its new environment.

### 4. Verify before involving the encoder

Check all four services on the relevant hosts:

```bash
systemctl --user --no-pager status esup-runner-mediamtx@manager.service
systemctl --user --no-pager status esup-runner-mediamtx@runner.service
systemctl --user --no-pager status esup-runner-manager.service
systemctl --user --no-pager status esup-runner-runner.service
```

Check the private MediaMTX Control APIs from their local hosts. A `200` with
an `items` list is enough before a publisher connects:

```bash
curl --fail --show-error --user control \
  http://127.0.0.1:9997/v3/paths/list
curl --fail --show-error --user control \
  http://127.0.0.1:10997/v3/paths/list
```

Open `/admin/live` in the Manager. A healthy empty system shows no active
session and at least one available Live Runner.

### 5. Perform the first Live test

Configure the encoder with the Manager RTMP address, the `publisher` account,
an allowed path such as `live/amphi-a`, and H.264 video plus AAC audio. A
typical URL is:

```text
rtmp://manager.example.org:1935/live/amphi-a?user=publisher&pass=...
```

Publishing is the start command. Wait for the next Manager scan, then open
**Live** and wait for `running`, `hls_ready=true` and a non-empty playback URL.
Use that exact URL in the player. Stop publishing to end the session; the
Runner is released only after FFmpeg, the relay and HLS files are cleaned up.

For a repeatable test without an encoder, follow the
[CPU smoke test](LIVE.md#reproducible-test-without-streaming-hardware).

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
