# Live quickstart: Manager

Get one automatic RTMP-to-HLS broadcast running with native Linux services.
**Order: Manager steps 1–4 → [Runner quickstart](../../runner/docs/LIVE_QUICKSTART.md)
→ Manager step 5.** Both roles can share a host; each needs its own MediaMTX service.

Before starting, complete [Manager installation](INSTALLATION.md#3-install-the-manager)
through **Synchronize Python dependencies**: `.env`, API tokens, an administrator
account and writable runtime/log directories must exist. Commands assume
`/opt/esup-runner` and the default MediaMTX ports.

Run commands as the service account (`esup-runner` by default), in a login
session where `systemctl --user` works. On an existing deployment, finish active
tasks and broadcasts before restarting services.

## 1. Install and start Manager MediaMTX

```bash
cd /opt/esup-runner/manager
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/install_mediamtx.py setup --instance manager \
  --rtmp-address 0.0.0.0:1935
uv run scripts/install_mediamtx.py start --instance manager
```

Allow TCP **1935** from encoders and Runner hosts over a private network or VPN.
`0.0.0.0` listens on all IPv4 interfaces; clients use the Manager's reachable
DNS name or IP. Keep the Control API (`9997`) and metrics (`9998`) on loopback.
Existing configurations are preserved, including addresses and passwords.
For a custom application `LOG_DIR`, pass the same `--log-dir` to `setup`.

## 2. Connect the Manager application

```bash
uv run scripts/configure_live.py --instance manager
uv run scripts/check_config.py
```

The helper updates `manager/.env`, enables `LIVE_ENABLED=true` and saves a
private backup. Keep `LIVE_AUTO_START=true` (the default). `LIVE_SOURCES` and Pod
integration are optional; neither is needed for this first broadcast.

## 3. Locate the passwords

```bash
nano "$HOME/.config/esup-runner/manager/mediamtx.yml"
```

Under `authInternalUsers`, read the `pass:` value directly below each `user:`:

| Account | Use its password for |
| --- | --- |
| `relay` | The Runner's `Manager MediaMTX relay password:` prompt |
| `publisher` | The encoder's RTMP publishing credentials |
| `control` | The local Control API check below |

Copy the **relay** password privately for the Runner setup. These MediaMTX
passwords are separate from the Runner API token and administrator password.
Close the editor without changing the file.

## 4. Start or restart the Manager

First installation only; skip this command if the application unit is installed:

```bash
make create-service
```

For both new and existing installations:

```bash
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemctl --user enable esup-runner-manager.service
systemctl --user restart esup-runner-manager.service
systemctl --user --no-pager status esup-runner-mediamtx@manager.service esup-runner-manager.service
curl --fail --show-error --user control http://127.0.0.1:9997/v3/paths/list
```

Enter the Manager MediaMTX **control** password when `curl` prompts. Both
services must be `active`; the API must return HTTP 200 with an `items` list.
Open `<MANAGER_PUBLIC_URL>/admin/live` with the administrator account.

## 5. Publish the first broadcast

Complete the [Runner quickstart](../../runner/docs/LIVE_QUICKSTART.md) now.
Once a Live Runner is available in `/admin/live`, configure your encoder:

| Encoder setting | Value |
| --- | --- |
| Video / audio | H.264 / AAC |
| RTMP server | `rtmp://manager.example.org:1935/live` |
| Stream key | `amphi-a?user=publisher&pass=YOUR_PUBLISHER_PASSWORD` |

Replace `manager.example.org` with the Manager address reachable from the
encoder and `YOUR_PUBLISHER_PASSWORD` with the **publisher** password from
step 3. URL-encode reserved characters in the password. If the encoder accepts
one URL, use `rtmp://manager.example.org:1935/live/amphi-a?user=publisher&pass=YOUR_PUBLISHER_PASSWORD`.
Give simultaneous broadcasts different names (`amphi-a`, `amphi-b`, etc.).

Start publishing. In `/admin/live`, wait for `running` and `hls_ready=true`, then
open the exact playback URL in an HLS player. Stop publishing: the session must
become `completed` and the Runner available again after cleanup. No `/live/start`
API call is needed.

For custom ports, publisher accounts or RTMPS (including port 443), see the
[MediaMTX reference](MEDIAMTX_SETUP.md). For Docker, use
[Live Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut).
For daily use and failures, see the [operations runbook](LIVE_OPERATIONS.md).
