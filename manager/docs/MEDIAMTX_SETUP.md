# MediaMTX reference for Manager and Runner

For a first native deployment, use the [Manager quickstart](LIVE_QUICKSTART.md)
then the [Runner quickstart](../../runner/docs/LIVE_QUICKSTART.md).
For the complete Docker stack, use [Live Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut).
This shared reference covers custom ports, authentication, RTMPS, containers and upgrades.

MediaMTX receives and relays RTMP; **FFmpeg in the Runner generates adaptive HLS**.
MediaMTX installation is identical for CPU and GPU. Live requires **MediaMTX >= 1.21.1**;
the native installer defaults to the **latest stable release** and reuses compatible installations.

## Contents

- [Choose an installation method](#1-choose-an-installation-method)
- [Install the native executable](#2-install-the-native-executable)
- [Prepare one instance](#3-prepare-one-instance)
- [Listener overrides](#listener-addresses-and-environment-overrides)
- [Credentials](#connect-authentication-settings), [publisher accounts](#multiple-publisher-accounts), [trusted encoder IPs](#passwordless-publishing-from-trusted-encoder-ips)
- [RTMPS](#optional-rtmps-publishing) and [port 443](#system-service-for-port-443)
- [Native services](#4-start-the-native-systemd-service) and [logs](#log-files-and-rotation)
- [MediaMTX in Docker](#5-run-mediamtx-in-docker)
- [Application connections](#6-connect-the-applications-and-verify)
- [Upgrades](#manual-upgrades) and [troubleshooting](#troubleshooting)

## 1) Choose an installation method

| Deployment | Procedure |
| --- | --- |
| Standard Linux services | Start with the two quickstarts above |
| Custom native installation | Sections 2–4, then 6 |
| MediaMTX container + native applications | Section 3 (`configure`), then 5–6 |
| Complete Docker stack | [Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut) |

Use one Manager MediaMTX and **one shared Runner MediaMTX per Runner deployment**,
even on a shared host. Adding Live workers creates more temporary relay paths;
it needs no extra MediaMTX service. Both roles can reuse one executable or image.

### Already installed on this machine?

```bash
export PATH="$HOME/.local/bin:$PATH"
command -v mediamtx
mediamtx --version
```

Reuse a stable version >= 1.21.1 and any service already configured for the same
role. The installer links a supported `PATH` executable at the user unit's default
path. It leaves unsupported executables unchanged; use [upgrades](#manual-upgrades)
for an existing installation.

### Prerequisites

Complete the affected application's installation through Python dependency sync.
Run commands from `manager/` or `runner/` as its service account. Both installers
work in component-only checkouts. Native services need a working `systemctl --user`
session. Set this in each shell:

```bash
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
```

Default native ports (all initially on loopback):

| Role | RTMP | Control API | Metrics |
| --- | --- | --- | --- |
| Manager | 1935 | 9997 | 9998 |
| Runner | 2935 | 10997 | 10998 |

Make only the Manager RTMP listener reachable from encoders and Runner hosts.
Keep Control APIs and metrics private. Use a private network, VPN or RTMPS for
credentials crossing an untrusted network.

## 2) Install the native executable

Skip this section for Docker. Run once per host, from either component:

```bash
cd /opt/esup-runner/manager  # Use runner/ on a Runner host.
uv run scripts/install_mediamtx.py install
```

The installer reuses `~/.local/bin/mediamtx` or a supported executable in `PATH`.
Otherwise it downloads the selected Linux archive and `checksums.sha256` from the
[official release](https://github.com/bluenviron/mediamtx/releases),
checks SHA-256 and the exact binary version, and installs only the executable.
An unsupported existing executable is reported and left unchanged.

| Option | Effect |
| --- | --- |
| `--dry-run` | No release downloads or writes; `latest` still queries GitHub and existing binaries may be checked |
| `--version latest` | Default: resolve the latest stable release through the GitHub API if no supported binary is installed |
| `--version 1.21.4` | Exact release to download if absent; minimum: 1.21.1 |
| `--binary /absolute/path/mediamtx` | Existing binary or unused installation destination; no default-path link |
| `--skip-email` | Disable the optional [installer email report](#installer-email-reports) |

`install` and `setup` never replace an installed version. Use the explicit
[`update` command](#native-update-command) for an existing native deployment,
or a separate destination for a [manual upgrade](#manual-upgrades).
Run `uv run scripts/install_mediamtx.py --help` for all commands.

`latest` excludes drafts and prereleases and must satisfy the minimum version.
If the GitHub lookup fails, the command stops without falling back to an older release.
Use a numbered version for repeatable installations without a release lookup.

`setup` combines `install` and `configure` without starting services. For the
standard layout, follow the quickstarts; use the separate commands for custom deployments.

## 3) Prepare one instance

Skip creation if `setup` already prepared the instance. Use `configure` for
an existing installation or a MediaMTX-only container with host networking:

```bash
cd /opt/esup-runner/manager
uv run scripts/install_mediamtx.py configure --instance manager \
  --rtmp-address 0.0.0.0:1935

cd /opt/esup-runner/runner
uv run scripts/install_mediamtx.py configure --instance runner
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
addresses, logging or ownership. Partial configurations and symlink destinations fail.

| Option (`configure` / `setup`) | Purpose |
| --- | --- |
| `--instance NAME` | Required; standard names: `manager`, `runner` |
| `--rtmp-address IP:PORT` | RTMP bind address |
| `--api-address IP:PORT` | Control API bind address |
| `--metrics-address IP:PORT` | Metrics bind address |
| `--config-root /absolute/path` | Parent directory; default: `~/.config/esup-runner` |
| `--log-dir /absolute/path` | Log directory; default: exported `LOG_DIR` or `/var/log/esup-runner` |
| `--user USER` | Owner; defaults to caller; another owner requires privileges |
| `--dry-run` | Preview without writes or secret generation |

Use `[::1]:PORT` for IPv6. Custom binary/configuration paths require manual unit
adaptation; the `start` helper below manages the standard home-local layout.

### Listener addresses and environment overrides

Installer listener options are written to `mediamtx.env`; template addresses
remain in `mediamtx.yml`. **Loaded environment variables override YAML values.**

| Variable in `mediamtx.env` | YAML setting | Purpose |
| --- | --- | --- |
| `MTX_RTMPADDRESS` | `rtmpAddress` | Plain RTMP publishing and reading |
| `MTX_APIADDRESS` | `apiAddress` | HTTP Control API |
| `MTX_METRICSADDRESS` | `metricsAddress` | Monitoring |

Native services load this file through `EnvironmentFile`; Docker uses `--env-file`.
Running the binary with only a YAML argument does not load it. These settings are
local listening addresses; listener enablement, authentication and TLS stay in YAML.

| Address | Effect |
| --- | --- |
| `127.0.0.1:9997` | IPv4 loopback; clients must share the host/network namespace |
| `192.0.2.10:1935` | One interface; replace the example with an assigned local IP |
| `0.0.0.0:1935` | All IPv4 interfaces; clients use a reachable IP or DNS name |
| `[::1]:9997` | IPv6 loopback |

`MTX_RTMPADDRESS` does not affect RTMPS: set `rtmpsAddress` in YAML for
[RTMPS publishing](#optional-rtmps-publishing).

To change an existing instance, edit `mediamtx.env` directly. Rerunning `setup`
or `configure` preserves it, even with new options. Restart the service or
recreate the container between broadcasts; YAML reload does not reload the environment.
Update the affected application's connection URLs too.

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
separately with H.264/AAC and an allowed path such as `live/amphi-a`. The default
template also accepts nested paths such as `live/amphi-a/amphi-a`. The Manager
discovers accepted publications; the final segment (`amphi-a`) is the public
`source_id`, not a secret.
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
        path: "~^live/([A-Za-z0-9_-]+/)*[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
  - user: publisherMedecine
    pass: "YOUR_MEDECINE_PASSWORD"
    ips: []
    permissions:
      - action: publish
        path: "~^live/([A-Za-z0-9_-]+/)*[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
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
        path: "~^live/([A-Za-z0-9_-]+/)*[A-Za-z0-9][A-Za-z0-9_-]{0,99}$"
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

Run from the component directory, using `manager` or `runner` as appropriate:

```bash
uv run scripts/install_mediamtx.py start --instance manager
```

`start` validates the YAML with its listener overrides, installs the component-local
user unit, reloads systemd and enables/starts the instance. It preserves configuration
and never restarts an active service. Add `--dry-run` to preview. Customized units,
symlinks and unsupported environment overrides are refused without replacement.

The unit runs `~/.local/bin/mediamtx` with files from
`~/.config/esup-runner/<instance>/`. It is installed in
`${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`; the MediaMTX configuration root
stays home-local. Custom paths require [manual unit setup](#custom-units-and-paths).
Enable boot/logout persistence once:

```bash
sudo loginctl enable-linger "$(id -un)"
```

Then [verify the Control API](#verify-the-connection) and connect the application.
For the Runner application unit, follow [Runner quickstart step 5](../../runner/docs/LIVE_QUICKSTART.md#5-configure-and-restart-the-runner-service).

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
diagnostics as its account; if access is unavailable, clear
`LIVE_MEDIAMTX_LOG_UNIT` to read `LOG_DIR/mediamtx_manager.log` instead. Ensure
that file is readable by the Manager account.

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

### Log files and rotation

New configurations use `logDestinations: [stdout, file]`. Native defaults are:

| MediaMTX instance | Log file |
| --- | --- |
| `manager` | `/var/log/esup-runner/mediamtx_manager.log` |
| `runner` | `/var/log/esup-runner/mediamtx_runner.log` |
| Another instance | `/var/log/esup-runner/mediamtx_<instance>.log` |

`setup` and `configure` accept `--log-dir /absolute/path`, defaulting to exported
`LOG_DIR` or `/var/log/esup-runner`. The installer does not load application
`.env` files: pass their `LOG_DIR` explicitly when it differs from the default.
It writes the absolute `logFile` into YAML without creating the log directory.
Prepare that directory with the application's `make init` procedure and make
it writable by the MediaMTX account before starting the service. For example:

```bash
uv run scripts/install_mediamtx.py configure --instance manager --log-dir /srv/esup/logs
```

All Live workers in a Runner deployment share `mediamtx_runner.log`. Separate
MediaMTX processes sharing a log directory need distinct instance names.
The Manager's file reader expects `LOG_DIR/mediamtx_manager.log`; use the
journal unit for a differently named native instance, or expose its file under
that conventional name in the Manager's filesystem.

Keep `logLevel: info` and `logStructured: false` for administration diagnostics.
Stdout remains available through `journalctl` and `docker logs`. Rotate only
`mediamtx_*.log` with `copytruncate`, following the
[Manager](INSTALLATION.md#5-bonus-production-log-rotation) or
[Runner](../../runner/docs/INSTALLATION.md#6-bonus-production-log-rotation)
`logrotate` rule; Python application logs already rotate internally. Docker named
volumes require a host rotation rule targeting their actual log location, or
bind-mounted log directories managed by the host's `logrotate`.

### Logs, restarts and boot persistence

Use the [operations runbook](LIVE_OPERATIONS.md#native-services) for status and
journal commands. After intentional configuration changes, restart the affected
unit between broadcasts:

```bash
systemctl --user restart esup-runner-mediamtx@manager.service
```

The user unit restarts after failures, with `NoNewPrivileges=true` and `UMask=0077`.
`start` and `enable --now` do not reload an active service's environment.
If the user bus is unavailable, open a service-account login session and check
`loginctl show-user "$(id -un)" -p Linger`. A plain `su` shell may lack the bus.
A `degraded` user manager means the bus works but a unit has failed.

## 5) Run MediaMTX in Docker

### Docker prerequisites

Reuse Docker Engine or install it with the official [distribution guide](https://docs.docker.com/engine/install/).
The commands require access to the daemon; the complete stack also needs the Compose plugin.
Access through the `docker` group grants administrative powers. See
[post-installation](https://docs.docker.com/engine/install/linux-postinstall/) or
[rootless mode](https://docs.docker.com/engine/security/rootless/) for account setup.

```bash
docker version
docker compose version  # Complete stack only.
docker pull bluenviron/mediamtx:1.21.1
docker run --rm bluenviron/mediamtx:1.21.1 --version
```

Use an explicit stable tag >= 1.21.1, without a `v` prefix. The MediaMTX container
needs neither a host executable nor GPU access; transcoding belongs to the Runner.

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
MTX_LOG_DIR=/var/log/esup-runner  # Match logFile in this instance YAML.
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env-file "$MTX_CONFIG_DIR/mediamtx.env" \
  --mount "type=bind,src=$MTX_CONFIG_DIR/mediamtx.yml,dst=/mediamtx.yml,readonly" \
  --mount "type=bind,src=$MTX_LOG_DIR,dst=$MTX_LOG_DIR" \
  bluenviron/mediamtx:1.21.1 --validate-conf=/mediamtx.yml

docker run -d \
  --name "esup-mediamtx-$MTX_INSTANCE" \
  --restart unless-stopped \
  --network host \
  --user "$(id -u):$(id -g)" \
  --env-file "$MTX_CONFIG_DIR/mediamtx.env" \
  --mount "type=bind,src=$MTX_CONFIG_DIR/mediamtx.yml,dst=/mediamtx.yml,readonly" \
  --mount "type=bind,src=$MTX_LOG_DIR,dst=$MTX_LOG_DIR" \
  bluenviron/mediamtx:1.21.1

docker logs --tail 50 "esup-mediamtx-$MTX_INSTANCE"
```

The YAML remains on the host and is mounted read-only. The container runs under
the service account's numeric UID/GID so that it can read the private YAML.
The log directory is mounted writable at its configured absolute path and must
already exist with permissions for that UID/GID. API-created Runner relay paths
are temporary and do not require writing the YAML.

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

Follow [Live Compose installation](LIVE_INSTALLATION.md#docker-compose-shortcut).
It uses the YAML files in `deploy/live/`, a private Docker network and two separate
MediaMTX containers. The home-local configurations and host-network commands above
are unnecessary. Use a checkout containing `manager/`, `runner/` and `deploy/live/`;
for a sparse checkout, add them with `git sparse-checkout add manager runner deploy/live`.

For internal addresses, volumes and additional workers, see the
[Compose reference](LIVE.md#docker-compose).

## 6) Connect the applications and verify

### Automatic connection for native applications

With the application dependencies and `.env` already prepared, run each component's
helper as the account owning its configuration:

```bash
cd /opt/esup-runner/manager
uv run scripts/configure_live.py --instance manager
cd /opt/esup-runner/runner
uv run scripts/configure_live.py --instance runner
```

The Runner command above assumes both MediaMTX configurations share a host and
service account. For a remote Manager, use the next section. Instance options can
be omitted for the standard names; `--manager-instance NAME` selects a different
local Manager instance.

The helpers read `mediamtx.yml` and `mediamtx.env`, URL-encode RTMP credentials
and update only application connection keys. Defaults are omitted. The Manager
helper enables Live and selects its journal unit if none is configured.
Runner task groups, hardware and public HLS URLs remain operator choices; see the
[Runner configuration](../../runner/docs/LIVE.md#instance-configuration).

| Option | Use |
| --- | --- |
| `--dry-run` | Preview key names without writes or password prompts |
| `--config-root /absolute/path` | Select the MediaMTX configuration parent |
| `--env-file /absolute/path` | Select the application `.env` |
| Manager `--no-journal` | Do not add a journal source, e.g. for a MediaMTX container; does not clear an existing choice |
| Manager `--service-mode user\|system` | Select the installed MediaMTX scope for diagnostics |

A write creates a private `.env.live-backup-*` and replaces `.env` atomically with
mode `0600`. Repeating the same configuration is a no-op. Files must be regular,
owned files, without symlinked paths. Backups contain secrets; retain only those
needed. The helpers never modify MediaMTX files or start/restart services.

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

Run on the MediaMTX host; enter that instance's **control** password at the prompt:

```bash
# Manager; for Runner use port 10997. Adapt custom addresses/ports.
curl --fail --show-error --user control http://127.0.0.1:9997/v3/paths/list
```

Expect HTTP 200 and an `items` list, which can be empty before publication.
For Compose, keep the APIs internal and use application status plus
`docker compose -p live-a logs manager-media runner-media` from `deploy/live/`.

Restart applications after `.env` changes. Continue with the
[Manager first broadcast](LIVE_QUICKSTART.md#5-publish-the-first-broadcast), or use
the [manual validation](../../runner/docs/LIVE.md#manual-validation) and
[CPU smoke test](LIVE.md#reproducible-test-without-streaming-hardware) for broader checks.

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
uv run --no-sync python scripts/install_mediamtx.py update --version latest --dry-run
uv run --no-sync python scripts/install_mediamtx.py update --version latest
```

`--version` accepts `latest` or an exact stable version such as `1.21.4` (minimum
`1.21.1`). `latest` resolves once before planning or changing services; the
download and binary checks then use that exact version.

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

`--dry-run` inspects the installation and resolves `latest` through GitHub;
it does not download releases, write, validate with the new executable or restart
services. An already installed target version
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

#### Installer email reports

`install`, `setup`, user-mode `start` and `update` send an optional report to `MANAGER_EMAIL`,
using the same branded text/HTML format as stack update emails. Reports include
the outcome, previous and installed versions, host and date. Reusing an installed
executable sends a `No updates` report; configuration or service setup may still
have run. Failed commands send a failure report without private diagnostics.

The installer reads `SMTP_*`, `MANAGER_EMAIL` and optional `MANAGER_PUBLIC_URL`
or `MANAGER_URL` from the component's `.env`; exported variables take precedence.
The Manager can fall back to `runner/.env` when its own SMTP configuration is
incomplete. Dotenv files require `python-dotenv` from the application environment;
values are read without interpolation or shell evaluation.

Missing SMTP settings skip delivery. SMTP errors produce a warning without
changing the installation result or undoing an update. `--skip-email` disables
reports; `--dry-run` and `configure` never send email.

#### Scheduled updates

As the service account, prepare the log directory once:

```bash
sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/log/esup-runner
```

For a standard user service, add this to the service account's crontab to check
for updates every Sunday at 03:00 (the host's cron timezone):

```cron
0 3 * * 0 cd /opt/esup-runner/manager && XDG_RUNTIME_DIR="/run/user/$(id -u)" UV_CACHE_DIR=/tmp/esup-runner-uv-cache "$HOME/.local/bin/uv" run --no-sync python scripts/install_mediamtx.py update --version latest >> /var/log/esup-runner/mediamtx-update.log 2>&1
```

Use `runner/` on Runner-only hosts and adjust the checkout and `uv` paths.
The account needs a working systemd user session; enable lingering as described
in [service startup](#4-start-the-native-systemd-service). System services also
require unattended `sudo` validation and service-control permissions. Schedule
a maintenance window: an available update restarts active MediaMTX services and
interrupts broadcasts. An already installed version causes no restart.

### Native executable with systemd user services

For customized units or paths unsupported by `update`:

1. Identify every unit using the executable with `systemctl --user cat` (or
   `sudo systemctl cat` for system units). Privately back up the executable,
   YAML/environment files and units, preserving credentials and permissions.
2. Read the selected release's [release notes](https://github.com/bluenviron/mediamtx/releases).
   Download to an unused path from the affected component, for example:

   ```bash
   uv run scripts/install_mediamtx.py install \
     --version 1.21.3 --binary "$HOME/.local/bin/mediamtx-1.21.3"
   "$HOME/.local/bin/mediamtx-1.21.3" --version
   ```

3. Validate **every** affected YAML with the candidate's
   `--validate-conf=/absolute/path/mediamtx.yml`, loading the same `MTX_*`
   environment as each service. Continue only after checksum, version and
   configuration validation succeed.
4. During maintenance, stop every affected unit. Replace a regular executable
   atomically on the same filesystem; for symlinks or system installations,
   use the original installation method or deliberately update `ExecStart`.
   Reload systemd if units changed, then start and inspect all affected units.
5. Check the running version with authenticated `GET /v3/info`, repeat the
   [Control API check](#verify-the-connection) and test RTMP-to-HLS playback.
   The executable's `--version` alone does not verify the running service.

On failure, stop the affected units, restore the saved executable and any changed
configuration/units, then start and verify again. Keep backups until validation
passes. Rollback does not restore broadcasts or temporary relay paths.

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

| Symptom | Check |
| --- | --- |
| `203/EXEC` or missing executable | Rerun `install` to link a supported `PATH` executable; check custom `ExecStart`. Then `systemctl --user reset-failed esup-runner-mediamtx@manager.service` and restart the correct instance. Inspect its journal. |
| `address already in use` | Reuse the intended service/container or choose distinct ports. |
| Service cannot read YAML or write logs | File ownership, permissions, configured paths and Docker UID/GID/mounts. |
| Unsupported configuration or API fields | Check the **running** version (>= 1.21.1) and release compatibility; roll back if Live validation fails. |

For API authentication, RTMP publication, missing HLS, session states and recovery,
use [troubleshooting by symptom](LIVE_OPERATIONS.md#troubleshooting-by-symptom).
For sanitized authentication events, use [Live diagnostics](LIVE.md#live-diagnostics-in-the-log-viewer).
Remote MediaMTX logs require log shipping or a shared mount; its Control API does
not supply the log file.

Upstream references: [installation](https://mediamtx.org/docs/kickoff/install),
[configuration](https://mediamtx.org/docs/features/configuration), and
[start on boot](https://mediamtx.org/docs/features/start-on-boot).
