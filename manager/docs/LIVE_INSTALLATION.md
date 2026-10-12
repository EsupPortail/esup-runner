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
The essential start commands are:

```bash
systemctl --user enable --now esup-runner-mediamtx@manager.service  # Manager host
systemctl --user enable --now esup-runner-mediamtx@runner.service     # Runner host
```

Validate the selected YAML with `mediamtx --validate-conf` before starting it.

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

Start MediaMTX before the applications, then start or restart the normal user
services after their `.env` files are complete:

```bash
systemctl --user enable --now esup-runner-manager.service
systemctl --user enable --now esup-runner-runner.service
```

If the Runner uses a grouped configuration, use the existing
`esup-runner-runner.service`; do not also enable the old templated Live service.
Add the MediaMTX dependency drop-in described in
[Runner Live service setup](../../runner/docs/LIVE.md#use-the-main-runner-service-with-a-mixed-or-grouped-env).

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
[Compose stack](../../deploy/live/compose.yml):

```bash
cd /opt/esup-runner/deploy/live
umask 077
cp manager.env.example manager.env
cp runner.env.example runner.env
cp ../../manager/production/mediamtx-live.yml manager-mediamtx.yml
cp ../../runner/production/mediamtx-live.yml runner-mediamtx.yml
# Replace every CHANGE_ME_* value with a unique secret.
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
docker compose -p live-a up -d --build
docker compose -p live-a ps
```

The `grep` command must return no placeholder before the stack is used. Keep
the Manager `control` password aligned with its application environment, the
Runner `control` and `transcoder` passwords aligned with the Runner environment,
the Manager `relay` password aligned with `LIVE_UPSTREAM_RTMP_URL`, and the
Runner `transcoder` password aligned with `LIVE_LOCAL_RTMP_URL`. Set
`RTMP_BIND_IP` to an encoder-reachable interface; it defaults to loopback.
Export it in the shell running Compose, or put it in `deploy/live/.env`.
`manager.env` and `runner.env` configure the applications inside their containers;
they do not supply variables for Compose host-port interpolation.

The placeholder check ignores comment lines and prints filenames only, keeping configured secrets out of
its output. `config --quiet` validates the Compose model without displaying
the expanded environment. The Manager's writable application logs stay at
`/var/log/esup-runner`; it reads MediaMTX diagnostics from the separate
read-only mount `/var/log/mediamtx/mediamtx.log`.

Compose uses internal names (`manager-media`, `runner-media`) in container
URLs. Do not replace them with `127.0.0.1`. Use
`docker compose -p live-a logs --tail=100 manager runner manager-media runner-media`
for the first verification. This shortcut exposes one Live Runner; follow the
[Compose details](MEDIAMTX_SETUP.md#complete-stack-with-docker-compose) before
adding workers or publishing production traffic.

## Next step

Once the first stream works, read the
[Live operations runbook](LIVE_OPERATIONS.md). It explains the administration
page, logs, normal stops, restarts, capacity, and the recovery path for each
common failure.
