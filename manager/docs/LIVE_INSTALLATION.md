# Live installation

## Native installation

For the standard Linux/systemd layout with one CPU Live instance, follow this
order:

1. Complete **steps 1–4** of the [Manager quickstart](LIVE_QUICKSTART.md) on the Manager host.
2. Complete the [Runner quickstart](../../runner/docs/LIVE_QUICKSTART.md) on the Runner host, even if both roles share a machine.
3. Return to [Manager step 5](LIVE_QUICKSTART.md#5-publish-the-first-broadcast) to publish and test the first broadcast.

For custom ports, a MediaMTX-only container or upgrades, use the
[MediaMTX reference](MEDIAMTX_SETUP.md).

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

Configure the encoder for H.264/AAC and publish using the `publisher` password
from `manager-mediamtx.yml`:

```text
rtmp://127.0.0.1:1935/live/amphi-a?user=publisher&pass=YOUR_PUBLISHER_PASSWORD
```

Replace `127.0.0.1:1935` with the Docker host's reachable RTMP address and published
port for a remote encoder. URL-encode reserved characters in the password.
Wait for `running` and `hls_ready=true`, then use the exact displayed `hls_url`
in the player. Stop publishing and confirm that the Runner becomes available
after cleanup.

Before adding workers or publishing production traffic, follow the
[Compose reference](LIVE.md#docker-compose) for networking, persistent volumes
and additional Runner identities.

## Next step

Once the first stream works, read the
[Live operations runbook](LIVE_OPERATIONS.md). It explains the administration
page, logs, normal stops, restarts, capacity, and the recovery path for each
common failure.
