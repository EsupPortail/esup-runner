# Live quickstart: Runner

Complete **steps 1–4** of the [Manager quickstart](../../manager/docs/LIVE_QUICKSTART.md) first.
This guide configures **one CPU Live instance** using native Linux services.
The first test uses HTTP on a private network; public playback uses an HTTPS
frontend.

Complete [Runner installation](INSTALLATION.md#3-install-the-runner) through
**Install Python dependencies** first (`make sync` is sufficient for Live).
FFmpeg with libx264/AAC, `.env` and writable runtime/log directories must exist.
Keep the configured API token and persistent storage directories.

Run commands as the service account (`esup-runner` by default), in a login
session where `systemctl --user` works. On an existing deployment, finish active
tasks and broadcasts before changing task groups or restarting services.

## 1. Install and start Runner MediaMTX

```bash
cd /opt/esup-runner/runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache-$(id -un)
uv run scripts/install_mediamtx.py setup --instance runner
uv run scripts/install_mediamtx.py start --instance runner
```

Run this even on a shared Manager/Runner host: each role has its own MediaMTX
service. Keep Runner MediaMTX ports `2935`, `10997` and `10998` on loopback.
For a custom application `LOG_DIR`, pass the same `--log-dir` to `setup`.

## 2. Connect to the Manager's RTMP service

Replace `manager.internal` with the Manager's reachable DNS name or IP:

```bash
uv run scripts/configure_live.py --instance runner \
  --manager-rtmp-host manager.internal
```

Use only a DNS name or IP, without `rtmp://`, port or path. Use `127.0.0.1`
when both roles share a host. At `Manager MediaMTX relay password:`, enter the
**relay** password saved in [Manager step 3](../../manager/docs/LIVE_QUICKSTART.md#3-locate-the-passwords).
Input is hidden; enter the raw password, separate from `RUNNER_TOKEN`.

The helper fills the connection settings in `runner/.env` and saves a private
backup. It reads the Runner's local control/transcoder credentials automatically.

## 3. Set capacity and addresses

```bash
nano .env
```

Set the following values, replacing the example hostnames. Edit existing
assignments rather than adding duplicates:

```dotenv
RUNNER_TASK_TYPES=[1x(live)]
RUNNER_INSTANCES=1
RUNNER_HOST=runner.internal
RUNNER_BASE_PORT=8082
MANAGER_URL=http://manager.internal:8081
LIVE_TRANSCODER=cpu
LIVE_PUBLIC_HLS_BASE_URL=http://runner.internal:8082/hls
```

`RUNNER_HOST` must be reachable from the Manager. `MANAGER_URL` is the Manager's
private **HTTP API** address, distinct from its RTMP endpoint. Keep `RUNNER_TOKEN`
equal to an authorized Manager token (for example `AUTHORIZED_TOKENS__runners`).
Reserve port `8082` so the launcher does not need a fallback. Keep task status
and persistent storage outside `/dev/shm`.

## 4. Choose the playback address

For a **first private-network test**, keep the HTTP HLS URL above and allow the
test player to reach port `8082`. Keep access limited to the Manager and test
machines. An HTTPS web page needs an HTTPS HLS URL for browser playback.

For **public playback**, set:

```dotenv
LIVE_PUBLIC_HLS_BASE_URL=https://live.example.org/hls
```

Configure DNS, TLS and the [HLS reverse proxy](LIVE.md#hls-reverse-proxy) before
starting. Expose only `/hls/`; keep the Runner API private. CORS is configured
on the Runner through `CORS_ALLOW_ORIGINS`.

## 5. Configure and restart the Runner service

Check the configuration, FFmpeg and HLS space before starting:

```bash
uv run scripts/check_config.py
uv run scripts/check_ffmpeg.py --mode cpu
df -h /dev/shm
```

First installation only; skip this command if the application unit is installed:

```bash
make create-service
```

Add the following drop-in (keep any existing settings):

```bash
systemctl --user edit esup-runner-runner.service
```

```ini
[Unit]
After=esup-runner-mediamtx@runner.service
Wants=esup-runner-mediamtx@runner.service

[Service]
KillMode=control-group
TimeoutStopSec=45
```

Then run:

```bash
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable esup-runner-runner.service
systemctl --user restart esup-runner-runner.service
systemctl --user --no-pager status esup-runner-mediamtx@runner.service esup-runner-runner.service
curl --fail --show-error --user control http://127.0.0.1:10997/v3/paths/list
```

Enter the **Runner's control password** from
`~/.config/esup-runner/runner/mediamtx.yml` when `curl` prompts. Both services
must be `active`; the API must return HTTP 200 with an `items` list.
HLS uses `/dev/shm/hls` by default; leave room for the stream's segments.

## 6. Continue with the first broadcast

Open the Manager's `/admin/live`: one Live Runner must be available.
Return to [Manager step 5](../../manager/docs/LIVE_QUICKSTART.md#5-publish-the-first-broadcast)
to configure the encoder and test playback.

For VOD alongside Live, more workers, GPU or HAProxy, see
the [Runner Live reference](LIVE.md). For local credential detection or custom
RTMP ports, see the [connection reference](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify).
For failures, use the [Live operations runbook](../../manager/docs/LIVE_OPERATIONS.md#troubleshooting-by-symptom).
