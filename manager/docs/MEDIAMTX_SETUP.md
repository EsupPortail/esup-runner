# MediaMTX installation for Live streaming

For a first native deployment, start with the [Manager quickstart](LIVE_QUICKSTART.md).
For Docker, use [Live Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut).
This reference covers alternative ports, Docker, service management and upgrades.

Live requires **MediaMTX >= 1.21.1** on Linux (including Debian 12/13).
The installer defaults to **1.21.1** and reuses newer stable installations.
MediaMTX receives/relays RTMP; FFmpeg in the Runner performs CPU or GPU transcoding.
The MediaMTX executable/image is identical for CPU and GPU hosts.

## Contents

- [Choose an installation method](#1-choose-an-installation-method)
- [Install the native executable](#2-install-the-native-executable)
- [Prepare one instance](#3-prepare-one-instance)
- [Listener addresses and environment overrides](#listener-addresses-and-environment-overrides)
- [Passwordless publishing from trusted encoder IPs](#passwordless-publishing-from-trusted-encoder-ips)
- [Optional RTMPS publishing](#optional-rtmps-publishing)
- [Start the native systemd service](#4-start-the-native-systemd-service)
- [System service for port 443](#system-service-for-port-443)
- [Run MediaMTX in Docker](#5-run-mediamtx-in-docker)
- [Connect the applications and verify](#6-connect-the-applications-and-verify)
- [Manual upgrades](#manual-upgrades)
- [Troubleshooting](#troubleshooting)

## 1) Choose an installation method

| Deployment | Procedure |
| --- | --- |
| Native executable + systemd user service | Sections 2–4, then 6 |
| MediaMTX container + native applications | Section 3 (`configure`), then 5–6 |
| Complete Docker Compose stack | [Live installation: Compose](LIVE_INSTALLATION.md#docker-compose-shortcut) |

Run one Manager MediaMTX and **one shared Runner MediaMTX per Runner server**,
even when both roles share a host. Adding Live workers needs no extra MediaMTX
service: each session creates its own temporary relay path through the API.
One native executable or Docker image can serve both roles.

### Already installed on this machine?

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v mediamtx
mediamtx --version
```

Reuse a stable executable **>= 1.21.1**. Section 2 links an existing `PATH`
installation to the user unit's default path without copying or replacing it.
For Docker, inspect existing images/containers before pulling another image.
Retain a service already dedicated to the same role if its ports, credentials
and configuration match. Upgrade older versions [manually](#manual-upgrades).

### Prerequisites

Use the service account and checkout from the [Manager](INSTALLATION.md) or
[Runner](../../runner/docs/INSTALLATION.md) installation, assumed at
`/opt/esup-runner`. Run commands without `sudo`, from the relevant component.
Both installers are self-contained in component-only sparse checkouts.

The installer uses Python **3.11+**, its standard library and CA certificates.
Native services require a working `systemctl --user` session. Missing OS
packages require an administrator; on Debian 12/13 or Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 ca-certificates curl
```

Reserve distinct listener ports. Encoders and Runners must reach the Manager's
RTMP listener; Control APIs and metrics remain private. Default ports:

| Role | RTMP | Control API | Metrics |
| --- | --- | --- | --- |
| Manager | 1935 | 9997 | 9998 |
| Runner | 2935 | 10997 | 10998 |

## 2) Install the native executable

Skip this section for Docker. Run once per host, from either component:

```bash
cd /opt/esup-runner/manager  # Use runner/ on a Runner host.
python3 scripts/install_mediamtx.py install
```

The installer reuses `~/.local/bin/mediamtx` or a supported executable in `PATH`.
Otherwise it downloads the selected Linux archive and `checksums.sha256` from the
[official release](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.1),
checks SHA-256 and the exact binary version, and installs only the executable.
An unsupported existing executable is reported and left unchanged.

| Option | Effect |
| --- | --- |
| `--dry-run` | No downloads or writes; an existing binary may be checked with `--version` |
| `--version 1.21.2` | Exact release to download if absent; minimum/default: 1.21.1 |
| `--binary /absolute/path/mediamtx` | Existing binary or unused installation destination; no default-path link |

`install` and `setup` never replace an installed version. Use the explicit
[`update` command](#native-update-command) for an existing native deployment,
or a separate destination for a [manual upgrade](#manual-upgrades).
Run `python3 scripts/install_mediamtx.py --help` for all commands.

### Install and configure with one command

`setup` combines `install` and `configure`, preserving existing binaries and
complete configurations. Run the relevant command on each host:

```bash
# Manager: prefer a specific private interface where possible.
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py setup --instance manager \
  --rtmp-address 0.0.0.0:1935

# Runner:
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py setup --instance runner
```

Continue with section 4; `setup` does not start services.

## 3) Prepare one instance

Skip creation if `setup` already prepared the instance. Use `configure` for
an existing installation or a MediaMTX-only container with host networking:

```bash
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py configure --instance manager \
  --rtmp-address 0.0.0.0:1935

cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py configure --instance runner
```

Each component selects its own `production/mediamtx-live.yml` template.
All listeners default to loopback; the Manager example opens RTMP on all
interfaces. Prefer a private interface and restrict firewall access and YAML
`ips` lists to authorized clients. Use RTMPS or a private network for credentials
crossing untrusted networks.

### Files and options

| Generated path | Permissions / purpose |
| --- | --- |
| `~/.config/esup-runner/<instance>/` | `0750`; one directory per role |
| `mediamtx.yml` | `0600`; separate random account passwords |
| `mediamtx.env` | `0640`; RTMP/API/metrics listener overrides |

Files belong to the service account. Existing complete configurations are
retained verbatim: rerunning `configure`/`setup` does not change passwords,
addresses or ownership. Partial configurations and symlink destinations fail.

| Option (`configure` / `setup`) | Purpose |
| --- | --- |
| `--instance NAME` | Required; standard names: `manager`, `runner` |
| `--rtmp-address IP:PORT` | RTMP bind address |
| `--api-address IP:PORT` | Control API bind address |
| `--metrics-address IP:PORT` | Metrics bind address |
| `--config-root /absolute/path` | Parent directory; default: `~/.config/esup-runner` |
| `--user USER` | Owner; defaults to caller; another owner requires privileges |
| `--dry-run` | Preview without writes or secret generation |

Use `[::1]:PORT` for IPv6. Custom binary/configuration paths require manual unit
adaptation; the `start` helper below manages the standard home-local layout.

### Listener addresses and environment overrides

The installer writes `--rtmp-address`, `--api-address` and `--metrics-address`
values to `~/.config/esup-runner/<instance>/mediamtx.env`. It keeps the template's
addresses in `mediamtx.yml`, so the two files can show different values.
**A loaded environment variable takes precedence over its matching YAML setting.**
If the variable is absent, the YAML value applies.

| Variable in `mediamtx.env` | Replaces in `mediamtx.yml` | Listener |
| --- | --- | --- |
| `MTX_RTMPADDRESS` | `rtmpAddress` | Plain RTMP publishing and reading |
| `MTX_APIADDRESS` | `apiAddress` | HTTP Control API used by the Manager or Runner |
| `MTX_METRICSADDRESS` | `metricsAddress` | HTTP metrics endpoint for monitoring |

Both native service modes load this file through systemd's `EnvironmentFile`;
the Docker commands below pass it with `--env-file`. Starting the MediaMTX binary
with only a YAML argument does not automatically load `mediamtx.env`.
These variables change addresses only: enabling listeners, authentication and
TLS remain configured in the YAML (`rtmp`, `api`, `metrics`, etc.).

These are **local listening addresses**, not remote destinations or application
URLs. Use literal `IP:PORT` values for installer-managed overrides:

| Address example | Effect |
| --- | --- |
| `127.0.0.1:9997` | IPv4 loopback only; clients must be on the same host/network namespace |
| `192.0.2.10:1935` | Only that IPv4 address; it must be assigned to the MediaMTX host |
| `0.0.0.0:1935` | All local IPv4 interfaces; clients connect to a reachable host IP or DNS name |
| `[::1]:9997` | IPv6 loopback only |

For example, with this Manager `mediamtx.env` (replace the example IP with the
server's address):

```dotenv
MTX_RTMPADDRESS=192.0.2.10:1935
MTX_APIADDRESS=127.0.0.1:9997
MTX_METRICSADDRESS=127.0.0.1:9998
```

RTMP listens on `192.0.2.10:1935`, even if the YAML still says
`rtmpAddress: 127.0.0.1:1935`. The API and metrics stay on loopback. Keep them
private; the applications use the Control API, while encoders publish through
RTMP or RTMPS. The Runner's default ports are `2935`, `10997` and `10998`.

`MTX_RTMPADDRESS` does **not** change the RTMPS listener. With
`rtmpEncryption: optional`, plain RTMP uses the effective `rtmpAddress` and
RTMPS uses `rtmpsAddress` from YAML, for example `:443`. Configure RTMPS in
[the RTMPS section](#optional-rtmps-publishing); the installer and connection
helpers support only the three environment overrides listed above.

For an existing instance, edit `mediamtx.env` directly: rerunning
`configure`/`setup` preserves its contents, even with new address options.
Restart the appropriate MediaMTX user/system service, or recreate its container
with the updated `--env-file`, between broadcasts. YAML hot reload does not
reload environment variables. If an address or port changes, also update the
affected application `LIVE_*` connection settings, manually or with
[`scripts/configure_live.py`](#6-connect-the-applications-and-verify).

### Connect authentication settings

Passwords are stored only in the private YAML; the installer does not print them.
Review credentials and client `ips` lists locally:

```bash
nano "$HOME/.config/esup-runner/manager/mediamtx.yml"
```

| YAML account | Matching application/client credential |
| --- | --- |
| Manager `publisher` | Encoder publishing settings |
| Manager `relay` | Runner `LIVE_UPSTREAM_RTMP_URL` |
| Manager `control` | Manager `LIVE_MEDIAMTX_API_PASSWORD` |
| Runner `transcoder` | Runner `LIVE_LOCAL_RTMP_URL` |
| Runner `control` | Runner `LIVE_MEDIAMTX_API_PASSWORD` |

Section 6 copies application credentials automatically. Configure the encoder
separately with H.264/AAC and an allowed path such as `live/amphi-a`. The Manager
discovers accepted publications; `amphi-a` is the public `source_id`, not a secret.
`LIVE_SOURCES` optionally declares sources before publication; it is not an
allowlist. Keep the template's `overridePublisher: true`
(new authenticated publishers on the same path replace the current publisher).

### Multiple publisher accounts

In the Manager's active `mediamtx.yml`, add one entry per account under
`authInternalUsers`. Keep `relay` and `control`; no Manager or Runner settings
need changing:

```yaml
  - user: publisherDroit
    pass: "YOUR_DROIT_PASSWORD"
    ips: []
    permissions:
      - action: publish
        path: "~^live/[A-Za-z0-9_-]+$"
  - user: publisherMedecine
    pass: "YOUR_MEDECINE_PASSWORD"
    ips: []
    permissions:
      - action: publish
        path: "~^live/[A-Za-z0-9_-]+$"
```

Use each account in the encoder's `user` and `pass` URL parameters, for example
`droit-amphi-a?user=publisherDroit&pass=YOUR_DROIT_PASSWORD` as the stream key.
The shared regex allows both accounts to publish on any matching path. To
separate permissions, use `~^live/droit-[A-Za-z0-9_-]+$` for `publisherDroit`
and `~^live/medecine-[A-Za-z0-9_-]+$` for `publisherMedecine`.
Keep `source: publisher` in `paths`: it is a MediaMTX source type, not an account
name. MediaMTX [reloads the file automatically](https://mediamtx.org/docs/features/configuration).

### Passwordless publishing from trusted encoder IPs

To let selected encoders publish without a username or password, add this entry
to the existing `authInternalUsers` list in the Manager's active
`~/.config/esup-runner/manager/mediamtx.yml`. Keep the existing `publisher`,
`relay` and `control` entries:

```yaml
  - user: any
    pass: ""
    ips:
      - "192.0.2.10/32"
      - "198.51.100.20/32"
    permissions:
      - action: publish
        path: "~^live/[A-Za-z0-9_-]+$"
```

Replace the example IPs with the encoders' source IPs **as seen by MediaMTX**.
Behind NAT, use the outgoing IP; all devices sharing it receive this permission.
`user: any` includes anonymous clients and ignores the password. Keep `ips`
restricted: `ips: []` would allow publishing without credentials from any IP.
Only publication is granted; reading and Control API access keep their existing
authentication. Other IPs still need a valid publisher account.

Set the encoder server to `rtmp://manager.example.org:1935/live`, the stream key
to `amphi-a`, and leave username/password empty, without `user` or `pass` query
parameters. To restrict an encoder to one stream, use `path: "live/amphi-a"`
with a separate entry for each IP/path pair.

No Manager or Runner `.env` changes are needed. MediaMTX
[reloads the YAML automatically](https://mediamtx.org/docs/features/configuration).

### Optional RTMPS publishing

**Not all encoders support RTMPS. Check the encoder's capabilities first.**

Point a DNS name such as `live.example.org` to the Manager's ingress address.
Use a trusted certificate covering that name. Make `fullchain.pem` (server certificate
and intermediates) and `privkey.pem` readable by the MediaMTX service account;
keep the private key owned by that account with mode `0600`.

For the default **user service on port 1936**, add these top-level settings to
`~/.config/esup-runner/manager/mediamtx.yml`, adapting the absolute paths and
keeping existing accounts and `paths`:

```yaml
rtmpEncryption: optional
rtmpsAddress: :1936
rtmpServerKey: /home/esup-runner/.config/esup-runner/manager/privkey.pem
rtmpServerCert: /home/esup-runner/.config/esup-runner/manager/fullchain.pem
```

`optional` keeps plain RTMP enabled for the Runners' relay, on port `1935` by
default. Its effective address comes from
[`MTX_RTMPADDRESS`](#listener-addresses-and-environment-overrides); use a private
address reachable by the Runners and restrict access to that network.

Allow TCP `1936` from encoders. Set the encoder server to
`rtmps://live.example.org:1936/live` and keep the stream key, for example
`amphi-a?user=publisher&pass=YOUR_PUBLISHER_PASSWORD` (URL-encode the password).

Restart the user service between broadcasts, then publish from the encoder:

```bash
systemctl --user restart esup-runner-mediamtx@manager.service
```

For **direct RTMPS on port 443**, use the
[system service below](#system-service-for-port-443) and set `rtmpsAddress: :443`.
Encoders connect directly to `rtmps://live.example.org:443/live`; **no port
redirection is needed**. Omit `:443` if the encoder uses it by default.

Keep MediaMTX's certificate files updated when renewing the certificate.
For Docker, mount them read-only and expose the chosen RTMPS port.

## 4) Start the native systemd service

Skip this section for Docker. Run as the service account in its login session.

### Standard startup

On the Manager host:

```bash
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py start --instance manager
```

On each Runner host:

```bash
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py start --instance runner
```

Run both on a shared host. The helper:

1. Checks the prepared files and reuses/installs the default executable.
2. Validates YAML with the listener overrides from `mediamtx.env`.
3. Checks the user bus, then installs the component-local shared unit once.
4. Reloads systemd, enables/starts the instance and checks that it is active.

Add `--dry-run` to preview. A failure stops subsequent operations. Existing
passwords/configurations are preserved; an active service is never restarted.
Custom units, symlinks and unsupported environment overrides are refused without
replacement. After startup, verify the API in section 6 as well.

The identical template shipped in each component uses `%h` for the account's
home and `%i` for the instance name. It reads
`~/.config/esup-runner/<instance>/mediamtx.env` and `mediamtx.yml` and runs
`~/.local/bin/mediamtx`. Units are installed in
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`.
No other component's checkout is needed.

### System service for port 443

This optional **Manager-only** mode runs MediaMTX as the existing service account
with `CAP_NET_BIND_SERVICE`. It can bind privileged ports without running as root.
The Manager application and Runner services keep their current user-service setup.

Prepare the standard home-local executable and configuration as the service
account first. Configure the DNS name and certificates in the
[RTMPS section](#optional-rtmps-publishing), then set `rtmpsAddress: :443` in the
Manager's YAML. Keep `rtmpEncryption: optional`. Allow TCP `443` from encoders
and check that no other listener uses it on the same address.

In `mediamtx.env`, `MTX_RTMPADDRESS` sets the private RTMP listener used to relay
streams to Runners.

Finish active broadcasts before migrating. Run these commands from `manager/`
as the service account (`esup-runner` here; adapt the account name):

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --service-mode system
# Existing user-service installations only:
systemctl --user disable --now esup-runner-mediamtx@manager.service
# Preview; omit --dry-run below to install and start:
sudo env UV_CACHE_DIR=/tmp/esup-runner-uv-cache "$(command -v uv)" run --no-sync python \
  scripts/install_mediamtx.py start --instance manager --service-mode system \
  --user esup-runner --dry-run
```

The installer requires root for actual installation and an explicit **non-root**
`--user`. It reuses that account's `~/.local/bin/mediamtx` and
`~/.config/esup-runner/manager/` files. It validates the existing executable and
YAML as that account, without downloads or configuration changes. It creates an
instance unit in `/etc/systemd/system/esup-runner-mediamtx@manager.service`, with
explicit paths, `User=esup-runner`, `AmbientCapabilities=CAP_NET_BIND_SERVICE`,
`CapabilityBoundingSet=CAP_NET_BIND_SERVICE`, `NoNewPrivileges=true` and boot
target `multi-user.target`. The capability permits privileged ports generally,
not only 443. Other instance names must be unique across accounts on the host.

An enabled or active counterpart blocks startup; stop and disable it explicitly.
Customized units, drop-ins and symlink destinations are retained/refused. An
already active service is not restarted. Custom configuration roots or user
systemd directories require manual service management. Executable paths cannot
contain quotes, backslashes or `$`.

After installation, restart the Manager to load the selected diagnostic scope:

```bash
systemctl --user restart esup-runner-manager.service
sudo systemctl status esup-runner-mediamtx@manager.service --no-pager
sudo journalctl -u esup-runner-mediamtx@manager.service -n 50 --no-pager
```

`configure_live.py --service-mode system` writes
`LIVE_MEDIAMTX_SERVICE_MODE=system` to `manager/.env`, preserving the existing log
source. Without the option, the helper preserves this choice; the default is
`user`. Changing `.env` alone never installs, stops or migrates a service.
The Manager reads only the selected unit's accessible journal entries. Check
diagnostics as its account; if access is unavailable, use the existing
`LIVE_MEDIAMTX_LOG_FILE` alternative rather than granting broad journal access.

Publish to `rtmps://live.example.org:443/live` with the usual stream key, omitting
`:443` if the encoder uses it by default. Verify automatic Live/HLS operation.
Later MediaMTX restarts use `sudo systemctl restart esup-runner-mediamtx@manager.service`;
the Manager itself still uses `systemctl --user` and needs its existing boot setup.

To roll back, stop/disable the system unit with
`sudo systemctl disable --now esup-runner-mediamtx@manager.service`, restore a
non-privileged RTMPS port such as `1936`, then run as the service account:

```bash
uv run scripts/configure_live.py --service-mode user
uv run scripts/install_mediamtx.py start --instance manager
systemctl --user restart esup-runner-manager.service
```

### Custom units and paths

For an existing customized deployment, retain its unit. For new custom paths,
copy the component's `production/esup-runner-mediamtx@.service` to the user unit
directory and adapt `ExecStart` and `EnvironmentFile`. Validate with the same
`MTX_*` environment as the service before `systemctl --user daemon-reload` and
`systemctl --user enable --now esup-runner-mediamtx@<instance>.service`.
Do not replace an active deployment's executable/unit to add another role.
Before changing old instance names, finish broadcasts, stop/disable the old
units, and check that the replacement ports are available.

### Logs, restarts and boot persistence

```bash
MTX_INSTANCE=runner  # Use manager on the Manager host.
systemctl --user status "esup-runner-mediamtx@$MTX_INSTANCE.service" --no-pager
journalctl --user -u "esup-runner-mediamtx@$MTX_INSTANCE.service" -n 50 --no-pager
# After deliberate configuration changes, between broadcasts:
systemctl --user restart "esup-runner-mediamtx@$MTX_INSTANCE.service"
```

The unit restarts automatically after failures, with `NoNewPrivileges=true` and
`UMask=0077`. `enable --now` does not reload an active service's environment.
For boot/logout persistence, check `loginctl show-user "$(id -un)" -p Linger`.
If disabled, an administrator enables lingering once with
`loginctl enable-linger esup-runner` (adapt the account).

`Failed to connect to bus` requires a working login/user-manager session; a plain
`su` shell may lack it. A `degraded` result from
`systemctl --user is-system-running` means the bus works but a unit has failed.
See [systemd lingering](https://github.com/systemd/systemd/blob/main/man/loginctl.xml).
Continue with section 6 to connect the applications and with
[Runner service setup](../../runner/docs/LIVE.md#service-and-hls-exposure)
for the application groups; starting MediaMTX alone does not enable Live.

## 5) Run MediaMTX in Docker

### Docker prerequisites

If Docker Engine is already installed, reuse it. Otherwise follow the official
[Debian Docker Engine installation](https://docs.docker.com/engine/install/debian/)
for Debian 12/13, or the appropriate
[Linux distribution instructions](https://docs.docker.com/engine/install/).
Install the Compose plugin as well if using the complete stack below.

The `docker` commands assume the account already has access to the selected
Docker daemon without `sudo`. If `docker version` reports a socket permission
error, ask the administrator to configure access according to the
[Docker post-installation guide](https://docs.docker.com/engine/install/linux-postinstall/).
Access to a system daemon through the `docker` group grants administrative powers;
removing `sudo` from a command does not make that daemon unprivileged.
For a daemon running under your account, see
[Docker rootless mode](https://docs.docker.com/engine/security/rootless/).

```bash
docker version
# Required only for the complete Compose stack:
docker compose version
```

The examples use **`bluenviron/mediamtx:1.21.1`**, with no `v` prefix in the tag.
You may substitute another explicit stable tag **>= 1.21.1** in all commands and
in the Compose file. Download it once per Docker host if it is not already present:

```bash
docker pull bluenviron/mediamtx:1.21.1
docker run --rm bluenviron/mediamtx:1.21.1 --version
```

Expected version: the selected release (`v1.21.1` for this example).
Neither a host MediaMTX executable nor GPU access is
required for this container, including on GPU machines. The standard image is
sufficient; transcoding and any NVIDIA container setup belong to the Runner.

### MediaMTX container with native Manager or Runner

Prepare the role, instance name, ports and configuration with `configure` in
section 3, then select that instance below (use `runner` for the Runner example).
This method uses **Linux host networking**, so the application can access
MediaMTX through the loopback URLs used by native services. Host networking uses
the configured listener ports directly and needs no `-p` port mappings.

This particular command assumes a conventional local Docker daemon without UID
remapping: the container runs with the host account's UID/GID to read its private
YAML. A rootless daemon has different UID mappings and may have different host
network behavior; adapt the user and networking options using the
[rootless networking reference](https://docs.docker.com/engine/security/rootless/troubleshoot/)
instead of copying this host-network command unchanged. The native user-service
method above runs entirely under the service account without Docker.

Validate the configuration first, then start the container:

```bash
MTX_INSTANCE=manager
MTX_CONFIG_DIR="$HOME/.config/esup-runner/$MTX_INSTANCE"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env-file "$MTX_CONFIG_DIR/mediamtx.env" \
  --mount "type=bind,src=$MTX_CONFIG_DIR/mediamtx.yml,dst=/mediamtx.yml,readonly" \
  bluenviron/mediamtx:1.21.1 --validate-conf=/mediamtx.yml

docker run -d \
  --name "esup-mediamtx-$MTX_INSTANCE" \
  --restart unless-stopped \
  --network host \
  --user "$(id -u):$(id -g)" \
  --env-file "$MTX_CONFIG_DIR/mediamtx.env" \
  --mount "type=bind,src=$MTX_CONFIG_DIR/mediamtx.yml,dst=/mediamtx.yml,readonly" \
  bluenviron/mediamtx:1.21.1

docker logs --tail 50 "esup-mediamtx-$MTX_INSTANCE"
```

The YAML remains on the host and is mounted read-only. The container runs under
the service account's numeric UID/GID so that it can read the private YAML.
API-created Runner relay paths are temporary and do not require writing this file.

Choose either this container or the native MediaMTX service for an instance;
starting both would occupy the same ports. When the application uses `esup-runner-runner.service`, omit the native
MediaMTX dependency from the [Live service drop-in](../../runner/docs/LIVE.md#service-and-hls-exposure).
If that dependency was already added, remove its `After=` and `Wants=` entries
from the drop-in, then run `systemctl --user daemon-reload`. Start the MediaMTX
container before the Runner application.

Each Runner server needs one local Runner MediaMTX container. Do not start an
additional container on the same server for more Runner processes or Live
workers. Continue with section 6.

### Complete stack with Docker Compose

The [Live Compose file](../../deploy/live/compose.yml) starts the Manager, one
dedicated Live Runner, and their two separate MediaMTX containers. Both MediaMTX
services already use `bluenviron/mediamtx:1.21.1`. The provided Runner image uses
CPU FFmpeg; GPU transcoding requires a suitable Runner image/runtime as described
in the [GPU documentation](../../runner/docs/gpu/README.md).

Choose another stable MediaMTX release >= 1.21.1 by editing both media image
tags; application image updates do not update these separate containers.

Use a checkout containing `manager/`, `runner/` and `deploy/live/`. If using a
sparse checkout, add these paths without replacing the existing selection:

```bash
cd /opt/esup-runner
git sparse-checkout add manager runner deploy/live
```

Then follow the [Live Docker Compose procedure](LIVE_INSTALLATION.md#docker-compose-shortcut) to copy
and customize the two application environments and two MediaMTX YAML files.
This method uses Docker's private network: the applications reach the control
APIs at `http://manager-media:9997` and `http://runner-media:9997`, respectively.
Do not substitute `127.0.0.1` inside those application containers: that would
refer to the application container itself. MediaMTX APIs and metrics are not
published on the host. Select an encoder-accessible `RTMP_BIND_IP` for the
Manager's RTMP input; it defaults to host loopback.
Compose interpolation reads the invoking shell or `deploy/live/.env`, not the
application `manager.env` / `runner.env` service environment files. For example,
export `RTMP_BIND_IP` in the shell before running the Compose commands.

Run `docker compose -p live-a config --quiet` after filling the local files and
before `up`. The Manager has separate mounts for writable application logs
(`/var/log/esup-runner`) and read-only MediaMTX diagnostics (`/var/log/mediamtx`).
`LIVE_MEDIAMTX_LOG_FILE` points to `/var/log/mediamtx/mediamtx.log` in the Manager,
while `MTX_LOGFILE` is `/var/log/esup-runner/mediamtx.log` in `manager-media`.
Both paths refer to the same file in the `manager-media-logs` volume.

The `~/.config/esup-runner/` files and host-network containers from sections 3–5 are
unnecessary for this Compose method. A second Manager/Runner stack uses another
Compose project name and different published host ports. Additional independent
Runner deployments can use their own Runner/MediaMTX pair; grouped workers share
the existing Runner MediaMTX container. See the linked procedure for task groups
and their API port mappings.

## 6) Connect the applications and verify

### Automatic connection for native applications

Prepare each application's usual `.env` first (Manager address, Runner address,
shared Runner token and task groups). Then run each component's connection helper
from its directory, as the account that owns the application and MediaMTX files:

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
cd /opt/esup-runner/manager
uv run scripts/configure_live.py --instance manager
cd /opt/esup-runner/runner
uv run scripts/configure_live.py --instance runner
```

The second command assumes that both roles share the same machine and service
account, with the Manager's `manager` MediaMTX configuration in that account's
configuration directory. `--instance` defaults to `manager` for the Manager and
`runner` for the Runner, so both options can be omitted for the standard setup.
Use `--manager-instance another-name` when the local Manager has another name.

The connection helper uses the component's Python environment. Install the
application dependencies with `make sync` (or the usual development/transcription
sync target) before running it. PyYAML and python-dotenv are declared in each
component's `pyproject.toml` and locked in its `uv.lock`; `uv run` uses those
project dependencies. No separate script environment or runtime config file is
needed. The MediaMTX installer itself still uses only the Python standard library.
The helper reads the existing `mediamtx.yml` and `mediamtx.env`, including custom
listener ports, URL-encodes RTMP credentials and edits only the relevant connection
assignments in `manager/.env` or `runner/.env`. It does not regenerate passwords.
API URLs and the `control` username are omitted when they match application defaults.

It enables `LIVE_ENABLED=true` on the Manager and, when no diagnostic source is
already selected, adds its systemd journal unit. It preserves Runner task groups,
hardware choices, proxy URLs, other settings and comments. Keep Live explicit:

```dotenv
# runner/.env
RUNNER_TASK_TYPES="[1x(encoding,studio),2x(live)]"
```

That declaration starts one VOD worker and two Live workers from the same Runner
service. With the connection secrets filled, native defaults provide local
MediaMTX API ports **9997 / 10997**, HLS in **`/dev/shm/hls`**, and public HLS URLs
computed from **`RUNNER_PROTOCOL://RUNNER_HOST:{port}/hls`**. `RUNNER_HOST` must be
reachable by viewers; set `LIVE_PUBLIC_HLS_BASE_URL` explicitly for a reverse proxy
or translated container ports. Live encoding uses `LIVE_TRANSCODER=cpu` (default)
or `nvenc`, independently of `ENCODING_TYPE`. Optional NVDEC and CPU fallback
remain configurable; the former `auto` value is no longer accepted.

Preview the affected **key names**, select a different configuration root or
target `.env`, or disable automatic systemd diagnostics for a host-network container:

```bash
cd /opt/esup-runner/manager
uv run scripts/configure_live.py --dry-run
uv run scripts/configure_live.py --no-journal
cd /opt/esup-runner/runner
uv run scripts/configure_live.py --config-root /srv/esup-media --env-file /opt/esup-runner/runner/.env --dry-run
```

A successful write creates a private `.env.live-backup-*` beside the target and
atomically replaces it with mode **0600**. Repeating the same operation is a no-op.
Backups contain secrets: keep them private and remove old backups when no longer
needed. Existing files must be regular files, not symbolic links. A dry run never
writes or requests a remote password. No values or credentials are printed.
The helper never changes MediaMTX configuration, starts or restarts services.
It does not remove an already configured log source when `--no-journal` is used.

### Runner on another machine

Run the Manager command on its own machine. On the Runner machine, prepare the
Runner's MediaMTX instance, then provide the Manager's reachable RTMP address:

```bash
cd /opt/esup-runner/runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run scripts/configure_live.py --manager-rtmp-host manager.example.org
```

The helper privately prompts for the Manager MediaMTX **relay** password. Only
this read credential is needed: do not copy the Manager's whole YAML or its
publisher/control passwords to the Runner. For a nonstandard RTMP port, add
`--manager-rtmp-port 1936`. For automation, `--relay-password-stdin` reads one line
from standard input; keep the secret out of command arguments and shell history.
The local Runner's control and transcoder credentials are read from its own files.

### Custom deployments and upgrades

Automatic connection supports the installer's internal-authentication, plain
RTMP/HTTP profile and its listener environment overrides. The **Manager** helper
also accepts `rtmpEncryption: optional` with an HTTP Control API, retaining the
Runners' plain RTMP relay. Select its installed service scope with `--service-mode
user|system`. For external authentication, hashed credentials, strict RTMPS,
HTTPS APIs, systemd environment overrides or
a full Docker private network, use explicit application settings from the table
below. The helper cannot infer remote hosts, container DNS or proxy routing.
These remain supported operator choices; no fixed password is built into the code.

| Application setting | Manager default / value | Live Runner default / value |
| --- | --- | --- |
| `LIVE_MEDIAMTX_API_URL` | `http://127.0.0.1:9997` | `http://127.0.0.1:10997` |
| `LIVE_MEDIAMTX_API_USER` | `control` | `control` |
| `LIVE_MEDIAMTX_API_PASSWORD` | Manager MediaMTX `control` password | Runner MediaMTX `control` password |
| `LIVE_UPSTREAM_RTMP_URL` | Not applicable | `rtmp://relay:<password>@manager.internal:1935` |
| `LIVE_LOCAL_RTMP_URL` | Not applicable | `rtmp://transcoder:<password>@127.0.0.1:2935` |

For Compose, retain the explicit container hostnames supplied by `compose.yml`
and follow its separate deployment procedure. With manual settings, URL-encode
reserved characters in RTMP credentials and adapt URLs to the listener ports.

For an existing `.env`, remove technical timing/codec/preset assignments listed
in the [Manager](PARAMETERS.md#internal-live-defaults) and
[Runner](../../runner/docs/PARAMETERS.md#internal-live-defaults) migration tables:
they are no longer read. Other optional assignments can be omitted when they match
defaults. The helper updates only connection keys; it preserves custom operator
settings and does not migrate old tuning values. Apply configuration changes at
a planned application restart; avoid restarting a Runner during a broadcast.

### Verify the connection

Check the selected instance's Control API from the host. `curl --user control`
prompts for that instance's control password, keeping it out of the command:

```bash
MTX_INSTANCE=manager  # Use runner for the Runner's MediaMTX.
MTX_CONFIG_DIR="$HOME/.config/esup-runner/$MTX_INSTANCE"
MTX_API_ADDRESS=$(sed -n 's/^MTX_APIADDRESS=//p' "$MTX_CONFIG_DIR/mediamtx.env")
curl --fail --show-error --user control \
  "http://$MTX_API_ADDRESS/v3/paths/list"
```

Expect HTTP 200 and JSON containing an `items` list. An empty list is normal
before an encoder publishes or a Runner session creates its relay. The shipped
YAML uses `logLevel: info`; existing installations retain their configured level.
For Compose, use the application's Live status endpoints and
`docker compose -p live-a logs manager-media runner-media` from `deploy/live/`
(adapt the project name); its control ports intentionally remain internal.

Complete [Manager Live configuration](LIVE.md#manager-configuration) and
[Runner Live configuration](../../runner/docs/LIVE.md#instance-configuration),
including source keys, Runner task groups and identities, HLS storage and HTTPS exposure.
Restart the affected applications after editing their environments. MediaMTX
alone does not generate this integration's adaptive HLS output. With
`LIVE_ENABLED=true` and `LIVE_AUTO_START=true` on the Manager, publishing RTMP
to an allowed MediaMTX path automatically reserves a Live Runner and
starts HLS; the encoder does not call `/live/start`. Keep all application and
media services running. See [automatic operation](LIVE.md#automatic-rtmp-to-hls-operation)
for discovering the playback URL and handling disconnects. Validate the
complete path with the [manual Live test](../../runner/docs/LIVE.md#manual-validation)
or the [CPU smoke test](LIVE.md#reproducible-test-without-streaming-hardware).

## Manual upgrades

MediaMTX version changes are an operator decision. `install`, `setup`, `start`
and `update-stack.sh` leave an existing executable unchanged. Use the explicit
`update` command below or the manual procedures.

**Plan a maintenance window: restarting or recreating MediaMTX disconnects
active RTMP publishers and readers and interrupts the Live RTMP-to-HLS path.**
All Live workers sharing the affected Runner MediaMTX can be interrupted.
Runner relay paths created through the API are temporary and are lost on a
MediaMTX restart. Stop ongoing broadcasts and wait for session cleanup before
the change. Encoder reconnection and session recovery may be necessary; verify
the resulting session and its current HLS URL in administration. A replacement
session can have a new URL. See
[Restart and outage behavior](LIVE_OPERATIONS.md#restart-and-outage-behavior).

### Native update command

Run as the non-root service account, with the application environment already
installed. **Prefer `manager/` whenever the Manager is installed on the host;
use `runner/` on Runner-only hosts.**

```bash
cd /opt/esup-runner/manager  # Use runner/ on a Runner-only host.
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
uv run --no-sync python scripts/install_mediamtx.py update --version 1.21.3 --dry-run
uv run --no-sync python scripts/install_mediamtx.py update --version 1.21.3
```

The command discovers `esup-runner-mediamtx@*.service` units belonging to that
account in both systemd scopes, including configured instances that are stopped.
It downloads and verifies the exact release, validates each existing YAML with
its `mediamtx.env` listener overrides, and saves
the old executable, configurations and units in a private
`~/.local/bin/mediamtx-backup-*` directory. It then replaces the executable
atomically and restarts only services that were active. It checks that their
processes remain active and verifies the executable where `/proc` access permits.
Configuration files, passwords, certificates and units are preserved. A detected
activation failure triggers restoration of the previous executable and recovery
of the active services;
failed recovery reports the backup path for manual intervention.

Update once per service account: both installers handle all detected user
MediaMTX services sharing its executable. The **Manager installer** is required
when that account has a system MediaMTX service, including RTMPS on port 443.
It requests `sudo` authentication for service control only; downloads and binary
replacement run as the service account. A working systemd user session is required.

`--dry-run` only inspects the installation; it does not download, write, validate
with the new executable or restart services. An already installed target version
is a no-op; downgrades are refused. Concurrent updates for the account are refused.

An account-owned `~/.local/bin/mediamtx` symlink, including one created when reusing
a `PATH` installation, is replaced with a local executable. Its target is left
unchanged; the backup and rollback preserve the original link. Broken links and
symlinked parent directories are refused.

Customized units/drop-ins, nonstandard paths and custom environment syntax
require the manual procedure below. After an update or rollback,
repeat the [Control API checks](#verify-the-connection), check `/v3/info`, and verify
a test broadcast and its current HLS URL. Temporary relays and broadcasts are not
restored by a rollback.

### Native executable with systemd user services

1. Choose a stable release >= **1.21.1** and review its
   [upstream release notes](https://github.com/bluenviron/mediamtx/releases).
   Identify the actual executable/configuration with
   `systemctl --user cat esup-runner-mediamtx@runner.service` (adapt the instance
   name). Save the old executable, YAML/environment files and customized units
   in a private backup directory. Preserve existing credentials and settings.
2. Download the chosen release at a **separate, unused path**. For example,
   under the service account on a Runner host with its application environment
   already installed:

   ```bash
   cd /opt/esup-runner/runner  # Use manager/ on a Manager host.
   export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
   uv run --no-sync python scripts/install_mediamtx.py install \
     --version 1.21.2 --binary "$HOME/.local/bin/mediamtx-1.21.2"
   "$HOME/.local/bin/mediamtx-1.21.2" --version
   "$HOME/.local/bin/mediamtx-1.21.2" \
     --validate-conf="$HOME/.config/esup-runner/runner/mediamtx.yml"
   ```

   Check that the binary reports the selected release. Validate **every**
   configuration using it, including the Manager configuration if both roles
   share the executable, with the same `MTX_*` overrides as the services.
   Continue only when the checksum, version and configuration checks succeed.
3. During maintenance, stop all MediaMTX units using that executable. For the
   default account-local regular file, replace `~/.local/bin/mediamtx` with the
   prepared executable using an atomic rename on the same filesystem. For a
   symbolic link, system installation or custom `ExecStart`, use its original
   installation method or deliberately update the unit path; do not
   unintentionally replace a link or another deployment's executable.
   Run `systemctl --user daemon-reload` if a unit changed, then start the affected
   units and inspect their status/journal. For the standard Runner instance:

   ```bash
   systemctl --user start esup-runner-mediamtx@runner.service
   systemctl --user status esup-runner-mediamtx@runner.service --no-pager
   journalctl --user -u esup-runner-mediamtx@runner.service -n 50 --no-pager
   ```

   On a host with a shared Manager/Runner binary, upgrade it once and restart
   **both** MediaMTX units. On separate hosts, perform the procedure on each.
4. Check the running release with `GET /v3/info` and the authenticated
   `GET /v3/paths/list` check in section 6, using the actual Control API URL.
   Republish a test source and verify relay creation and HLS playback. The file's
   `--version` alone does not prove that the running service has loaded it.
   If startup or Live validation fails, stop the affected units, restore the
   saved executable and any deliberately changed units/configuration, start
   them again, and repeat the checks. Keep the backup until validation succeeds.
   A rollback does not transparently restore broadcasts or temporary relays.

### Docker and Docker Compose

Keep the configured YAML, environment, ports, mounts and service identity.
Pull an explicit image tag >= **1.21.1** and run its `--version` and
`--validate-conf` with the existing mounts/environment. For a standalone
container, recreate it with the selected image and its original options;
restarting the old container keeps its old image.

For the supplied Compose stack, save the current tags and edit **both**
`manager-media` and `runner-media` image tags in `deploy/live/compose.yml`.
Use the deployment's existing project name (here `live-a`):

```bash
cd /opt/esup-runner/deploy/live
docker compose -p live-a config --quiet
docker compose -p live-a pull manager-media runner-media
docker compose -p live-a run --rm --no-deps manager-media --version
docker compose -p live-a run --rm --no-deps runner-media --version
docker compose -p live-a run --rm --no-deps manager-media --validate-conf=/mediamtx.yml
docker compose -p live-a run --rm --no-deps runner-media --validate-conf=/mediamtx.yml
```

After successful validation, recreate the media containers during maintenance:

```bash
docker compose -p live-a up -d --no-deps manager-media runner-media
docker compose -p live-a logs --tail 50 manager-media runner-media
```

Verify the actual running versions, the applications' Live status and an
RTMP-to-HLS test; keep the Control APIs private. To roll back, restore the old
explicit tags and recreate the media containers, preserving their data/log
volumes, then repeat those checks. See the official
[Compose recreation behavior](https://docs.docker.com/reference/cli/docker/compose/up/).

## Troubleshooting

### Authentication diagnostics in administration

The supplied templates use `logLevel: info` and `logStructured: false`, since
MediaMTX logs RTMP authentication refusals at info level. For existing installations,
review these two values in `mediamtx.yml` **between broadcasts**: changing them
reloads MediaMTX listeners and disconnects active encoders, which must reconnect.
Do not overwrite existing credentials by copying a fresh template.

For the native Manager, add `LIVE_MEDIAMTX_LOG_UNIT=esup-runner-mediamtx@manager.service`
to `manager/.env`, using its actual instance name, then restart the Manager.
It must run under the account that owns that user service. The installation
script preserves existing configuration files, so their old `warn` level is
not changed by rerunning `configure`.

The complete Compose stack instead writes a MediaMTX log file into a dedicated
volume, mounts it read-only at `/var/log/mediamtx` in the Manager and sets
`LIVE_MEDIAMTX_LOG_FILE=/var/log/mediamtx/mediamtx.log`. The Manager's own
`/var/log/esup-runner` volume remains writable and separate.
For an independently deployed container, configure `MTX_LOGDESTINATIONS=stdout,file`,
set `MTX_LOGFILE` to a writable mounted file and expose that directory read-only
to the Manager. Arrange log rotation for file-based deployments.

Open **Logs → Live diagnostics** to see sanitized authentication and source events.
See [Live diagnostics](LIVE.md#live-diagnostics-in-the-log-viewer) for limits and markers.

| Symptom | Check |
| --- | --- |
| `203/EXEC` or missing `~/.local/bin/mediamtx` | Run the current `install` command in section 2 to create the link to an existing `PATH` executable, then validate the YAML. Check `ExecStart` when using `--binary`. After correcting the path, run `systemctl --user reset-failed esup-runner-mediamtx@manager.service` and restart that instance (adapt its name). A status captured immediately after startup can precede the execution failure; inspect the journal too. |
| `address already in use` | Another service/container uses the same port. Reuse the intended instance or allocate distinct ports to the new one. |
| API connection refused | Service/container status, selected API port and listener address. Container loopback and host loopback are different unless using host networking. |
| API HTTP 401 | The application's control username/password must match this instance's YAML and allowed client IPs. |
| Encoder cannot publish | Manager RTMP bind address, firewall, `publisher` credentials, allowed IPs and the `live/<key>` path. |
| Runner cannot read the source | Manager address reachable from the Runner, `relay` credentials and allowed Runner IPs. |
| Service cannot read YAML | File ownership/permissions and the service account; for Docker, check the bind mount and numeric UID/GID. |
| Missing API fields or unsupported configuration | Confirm the running version is >= `1.21.1`, then check the chosen release's API v3 / `tracks2` and configuration compatibility. Roll back if the newer release fails Live validation. |
| MediaMTX works but no HLS is available | Check Live reservation/session state, Runner FFmpeg, HLS directory permissions and the HTTPS frontend. |

Upstream references: [installation](https://mediamtx.org/docs/kickoff/install),
[configuration](https://mediamtx.org/docs/features/configuration), and
[start on boot](https://mediamtx.org/docs/features/start-on-boot).
