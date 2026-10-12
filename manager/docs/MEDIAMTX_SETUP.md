# MediaMTX installation for Live streaming

If this is your first installation, start with the
[short Live installation path](LIVE_INSTALLATION.md). This page is the detailed
reference for native systemd, Docker, custom ports and troubleshooting.

This guide installs **MediaMTX 1.21.1** for Live on Linux, including Debian 12
and Debian 13. Run one Manager instance on the Manager host and one shared
Runner instance on each Runner server; all Runner processes and Live workers on
that server use the same MediaMTX service. It covers a native executable with
systemd user services and the official Docker image. MediaMTX is required only for Live.

Use the **same MediaMTX executable or image on CPU and GPU machines**. MediaMTX
receives and relays streams; FFmpeg on the Runner performs transcoding. MediaMTX
does not need CUDA, NVIDIA drivers or a GPU-enabled image. Configure the Runner's
FFmpeg separately using [FFmpeg setup](../../runner/docs/FFMPEG_SETUP.md) and
[Live Runner configuration](../../runner/docs/LIVE.md).

## 1) Choose an installation method

| Deployment | Steps |
| --- | --- |
| Native MediaMTX, managed by systemd --user | Check existing installations below, then sections [2](#2-install-the-native-executable), [3](#3-prepare-one-instance) and [4](#4-start-the-native-systemd-service) |
| MediaMTX in Docker, with Manager/Runner applications on the host | Sections [3](#3-prepare-one-instance) and [5](#5-run-mediamtx-in-docker) |
| Complete Manager/Runner stack in Docker Compose | Go directly to [Complete stack with Docker Compose](#complete-stack-with-docker-compose) |

### Already installed on this machine?

**There is no need to reinstall MediaMTX for each Manager or Runner.** One native
executable can serve all instances on a host. Docker containers can reuse the
same image. Check what is already available:

```bash
# Include the account-local binary directory in this shell.
export PATH="$HOME/.local/bin:$PATH"
# Native installation, if present:
command -v mediamtx
mediamtx --version

# Docker installation, if Docker is present:
docker image inspect bluenviron/mediamtx:1.21.1 --format '{{.Id}}'
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
```

If the executable reports `v1.21.1`, keep it. Run `install` in section 2 to
check it and prepare the path used by the user unit: an existing executable in
`PATH` is linked from `~/.local/bin/mediamtx`, without downloading or copying it.
If the Docker image already exists, skip the image download in section 5.

An existing service/container can also be retained when it is already dedicated
to the **same** Manager or Live Runner and its configuration matches this guide.
Inspect its configuration, version and ports before changing anything. If another
version is installed, plan its compatibility check or upgrade separately: the
tested reference for this integration is **1.21.1**, using API v3 and `tracks2`.

Reuse the **installation** and run one MediaMTX service for the Manager plus
one shared service on each Runner server. All Live instances declared in a
Runner's `RUNNER_TASK_TYPES` and all Runner processes on that server share its
MediaMTX service and credentials, using independent `live/<live_id>` relay
paths. For example, one Manager and a Runner server configured with
`[1x(encoding,studio),4x(live)]` need **two MediaMTX services**, one executable
or image, and one application `runner/.env`. Increasing the Live count or adding
Runner processes does not require another MediaMTX configuration or service.

The Manager's MediaMTX remains separate from the Runner's: it receives encoder
sources while the Runner server's shared instance hosts temporary pull relays.
Each additional Runner server has its own `runner` instance and local service.

### Prerequisites

- A Linux host and a login session for the service account (`esup-runner` in
  these examples). Run installation and operations as that account, without
  `sudo`. All example listener ports are above 1024.
- The `esup-runner` account and repository checkout from the
  [Manager installation](INSTALLATION.md) or
  [Runner installation](../../runner/docs/INSTALLATION.md), assumed below at
  `/opt/esup-runner`. Run the commands in Bash.
- Available ports for each instance, and network access from encoders to the
  Manager's RTMP listener and from Runners to that same listener.

Only initial OS provisioning needs an administrator if packages are missing.
On Debian 12/13 or Ubuntu, an administrator can install the prerequisites:

```bash
sudo apt update
sudo apt install -y python3 ca-certificates curl coreutils openssl
```

On other Linux distributions, use the equivalent packages. The upstream Linux
binary avoids a local compilation step. Native startup below requires systemd;
adapt service management on distributions using another init system. Docker
requires a supported Docker Engine installation, described in section 5.

Use the component-local installer: [Manager](../scripts/install_mediamtx.py)
or [Runner](../../runner/scripts/install_mediamtx.py). Each selects its own
`production/mediamtx-live.yml` template without a positional role argument.
It needs **Python 3.11 or newer** and uses only the standard library;
no application virtual environment or Python package installation is required.
The commands below use the operating system's Python as the logged-in service
account. New installations use `~/.local/bin/mediamtx`; configuration lives under
`~/.config/esup-runner/<instance>/`. MediaMTX configuration paths deliberately use
the account's home directory; `--config-root` selects another location. The user
unit files themselves follow systemd's configuration directory, including
`XDG_CONFIG_HOME` when set. Adapt custom configuration paths in the units below.

The scripts and YAML template are included in a component-only sparse checkout;
no root `scripts/` directory or other application is needed to install or configure
MediaMTX. Run commands from `manager/` on the Manager host or `runner/` on the
Runner host. Section 4 separately describes obtaining the shared systemd unit.

## 2) Install the native executable

Skip the native installation when using Docker. For a native service, run the
following once per host; it also checks and reuses an existing installation.

Install or reuse the native executable:

```bash
cd /opt/esup-runner/manager  # Use /opt/esup-runner/runner on a Runner host.
python3 scripts/install_mediamtx.py install
```

The script first looks for `~/.local/bin/mediamtx`, then for `mediamtx` in `PATH`,
and reuses a matching version. For a `PATH` installation, it creates a symbolic
link at `~/.local/bin/mediamtx` so the default user unit can find it. For example,
the link can point to `/usr/local/bin/mediamtx`; the existing executable stays
unchanged and there is no second installation. Repeated runs retain the link.
When neither location contains MediaMTX, it selects the Linux architecture, downloads
**1.21.1** and `checksums.sha256` from the
[official release](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.1),
verifies SHA-256, and installs only the executable in `~/.local/bin/mediamtx`.
The bundled default YAML is not installed; section 3 uses the project's templates.

An existing executable with a different version is left unchanged and reported
as an error. Use `--binary /absolute/path/to/mediamtx` to select an existing
installation outside `PATH`, or a separate destination for a deliberate version
change. An explicit `--binary` keeps that chosen location and does not create
the default link; adapt the systemd executable path in section 4 in that case.

Preview the operation or display the available options:

```bash
python3 scripts/install_mediamtx.py install --dry-run
python3 scripts/install_mediamtx.py --help
python3 scripts/install_mediamtx.py install --help
```

`--dry-run` performs no downloads or writes. It may run an existing executable
with `--version` to check compatibility. `--version` defaults to the fixed
`1.21.1` release; another explicit release is an operator choice requiring Live
validation. Avoid `latest` tags or MediaMTX's `--upgrade` command to retain the
pinned version.

### Install and configure with one command

`setup` combines `install` and `configure`. For a new native deployment, choose
one command below and then continue with section 4 and the authentication mapping
in section 3:

```bash
# Manager: choose a private interface instead of 0.0.0.0 where possible.
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py setup --instance manager \
  --rtmp-address 0.0.0.0:1935

# Live Runner on the same host or another machine:
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py setup --instance runner
```

Both commands use their component's fixed role and support `--dry-run`. The script
prepares files and reports their locations; service startup remains the explicit step in section 4. Existing
executables and complete instance configurations are reused without overwriting
files or regenerating passwords.

## 3) Prepare one instance

Use `configure` when the executable is already installed, or when MediaMTX runs
in Docker with host networking. Skip file creation if `setup` already prepared
this instance. The complete Compose stack retains its own procedure in section 5.

Run the commands from the selected component's directory, with its local
`scripts/` and `production/` directories present in the checkout. No native MediaMTX executable
is required for `configure`.

### Select the component and ports

For a **Manager**:

```bash
cd /opt/esup-runner/manager
python3 scripts/install_mediamtx.py configure --instance manager \
  --rtmp-address 0.0.0.0:1935
```

`0.0.0.0:1935` accepts RTMP connections on all host interfaces. Prefer a specific
private interface address where possible; restrict access to authorized encoders
and Runners with the firewall and MediaMTX `ips` settings. Use a private network
or configure RTMPS when transporting credentials across untrusted networks.
Without this override, the template's default is `127.0.0.1:1935`.

For a **Live Runner**:

```bash
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py configure --instance runner
```

The Runner pulls the Manager's stream; encoders do not connect to the Runner.
The Runner's RTMP listener and both roles' API/metrics listeners default to
loopback. The first Runner uses ports `2935`, `10997`, and `10998` respectively;
the Manager uses `1935`, `9997`, and `9998`.

Prepare only one Runner MediaMTX instance per Runner server. All Runner processes
and Live workers on that server use it; adding capacity does not require another
configuration or service. If the Manager and Runner share a host, keep their
separate role instances on the distinct default ports shown above.

### Files and options

The script reads the [Manager](../production/mediamtx-live.yml) or
[Runner](../../runner/production/mediamtx-live.yml) template and creates:

| File | Contents and permissions |
| --- | --- |
| `~/.config/esup-runner/<instance>/mediamtx.yml` | Role-specific MediaMTX configuration with separate generated passwords; mode `0600`, owned by the service account |
| `~/.config/esup-runner/<instance>/mediamtx.env` | The three listener addresses as `MTX_*ADDRESS` overrides; mode `0640`, readable by the service account's primary group |

The instance directory uses mode `0750`. Files belong to the logged-in account,
which also runs the user services. `--user` defaults to that account; selecting
a different owner requires appropriate administrative privileges. Regular
installation and configuration of your own instances need no ownership changes.
An existing configuration is retained verbatim: later `configure`/`setup` calls
**do not reapply the requested role, account or addresses**. Edit the files to
change an existing instance. Partial configurations and symlink destinations are
reported as errors and left unchanged.

| Option | Applies to | Purpose |
| --- | --- | --- |
| `--dry-run` | All commands | Preview without downloading, writing files or generating secrets |
| `--version 1.21.1` | `install`, `setup` | Pin the native executable version; does not change Compose image tags |
| `--binary /absolute/path/mediamtx` | `install`, `setup` | Select an executable to reuse or a destination for a new installation |
| `--instance NAME` | `configure`, `setup` | Required unique instance name, e.g. `manager` or `runner` |
| `--rtmp-address IP:PORT` | `configure`, `setup` | RTMP listener; defaults to the selected template |
| `--api-address IP:PORT` | `configure`, `setup` | Control API listener; defaults to the selected template |
| `--metrics-address IP:PORT` | `configure`, `setup` | Metrics listener; defaults to the selected template |
| `--user USER` | `configure`, `setup` | File owner and primary group; defaults to the current account |
| `--config-root PATH` | `configure`, `setup` | Absolute parent of the instance directory; defaults to `~/.config/esup-runner` |

Use `[::1]:PORT` for IPv6 loopback addresses. Run commands in the intended service
account's session. If changing the configuration root, also adapt the service
unit or Docker command in sections 4–5. Ensure the account can traverse all
parent directories.

### Connect authentication settings

New configurations already contain random passwords generated with Python's
`secrets` module. The script stores them in the protected YAML and does not print
them. Review that file locally and set `ips` lists for the actual client addresses;
an empty list permits any client IP with valid credentials:

```bash
# Replace manager with the selected instance name.
nano "$HOME/.config/esup-runner/manager/mediamtx.yml"
```

Retain the template's restricted permissions and path expressions. The
[connection helper in section 6](#6-connect-the-applications-and-verify) copies
application credentials automatically. Configure the publisher credentials in
the encoder itself:

| YAML user | Where the matching credentials are used |
| --- | --- |
| Manager `publisher` | Encoder RTMP publishing settings |
| Manager `relay` | Each associated Runner's `LIVE_UPSTREAM_RTMP_URL` |
| Manager `control` | Manager `LIVE_MEDIAMTX_API_USER` / `LIVE_MEDIAMTX_API_PASSWORD` |
| Runner `transcoder` | That Runner's `LIVE_LOCAL_RTMP_URL` |
| Runner `control` | That Runner's `LIVE_MEDIAMTX_API_USER` / `LIVE_MEDIAMTX_API_PASSWORD` |

The script configures MediaMTX only: the Manager/Runner application environments
remain under your control. Configure each encoder with a path allowed by the
template, such as `live/amphi-a`, where `amphi-a` is the public broadcaster name.
The Manager discovers accepted RTMP publications without a `LIVE_SOURCES`
declaration and uses the broadcaster name as `source_id`. That optional setting
declares sources before publication, for example
`LIVE_SOURCES={"amphi-a":"live/amphi-a"}`. The Manager template uses
`overridePublisher: true` so the next authenticated publisher on the same path
replaces the current one. Apply this setting manually to existing installations
before restarting MediaMTX. Follow
[Manager configuration](LIVE.md#manager-configuration) and the encoder's
authentication options. Keep the generated passwords private; broadcaster names
are public identifiers, not authentication secrets.

## 4) Start the native systemd service

Skip this section for Docker. Run these commands in the service account's login
session, on the host where the corresponding MediaMTX instance will run.

### 4.1) Understand the shared template and instance names

[`esup-runner-mediamtx@.service`](../../runner/production/esup-runner-mediamtx@.service) is a
**shared systemd template**. Install this file **once per account on each host**.
The standard setup uses `manager` on the Manager host and one `runner` instance
on each Runner server. All Runner processes and Live workers on that server
share the `runner` service and its configuration.

The name supplied to `--instance` in sections 2–3 determines both the
configuration directory and the service name:

| MediaMTX used by | Installer directory | `--instance` | Configuration directory | Service to start |
| --- | --- | --- | --- | --- |
| Manager | `manager/` | `manager` | `~/.config/esup-runner/manager/` | `esup-runner-mediamtx@manager.service` |
| All Runners on one Runner server | `runner/` | `runner` | `~/.config/esup-runner/runner/` | `esup-runner-mediamtx@runner.service` |

For example, `setup --instance manager` from `manager/` prepares the Manager's
configuration, while `configure --instance runner` from `runner/` prepares the
shared Runner configuration. **These installer commands prepare files; each role's
instance must then be started on its host.** Starting
`esup-runner-mediamtx@manager.service` starts only the Manager's MediaMTX;
`esup-runner-mediamtx@runner.service` starts the MediaMTX shared by that Runner
server. If both roles use the same host, start both instances there.

Keep the template filename `esup-runner-mediamtx@.service`: there is no need to copy or
rename it for each instance. In the template, systemd replaces `%i` with the name
between `@` and `.service`, and `%h` with the service account's home directory.
For `esup-runner-mediamtx@runner.service`, it therefore loads
`~/.config/esup-runner/runner/mediamtx.env` and
`~/.config/esup-runner/runner/mediamtx.yml`. The component-local installer selects the initial YAML
contents; the instance name selects which prepared files systemd loads.

Use the exact names chosen during configuration. `manager` and `runner` are the
recommended names; custom names are supported when supplied consistently to the
installer, helper and systemd service. If both roles run on one host, their
listener ports must remain distinct, as shown in section 3. On separate hosts,
follow this procedure for the local role instance on each host.

### 4.2) Install the shared template once

If a sparse checkout contains only `manager/`, first add the template directory:

```bash
cd /opt/esup-runner
git sparse-checkout add runner/production
```

This retrieves templates; the Manager does not need the Runner application.
Prepare the executable path and install the template once. If both are already
ready for this account, continue directly with the instance startup commands
below. Retain and review an existing customized unit instead of copying over it:

```bash
(
set -eu
cd /opt/esup-runner/manager  # Use /opt/esup-runner/runner on a Runner host.
python3 scripts/install_mediamtx.py install
LIVE_USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$LIVE_USER_UNIT_DIR"
install -m 0644 /opt/esup-runner/runner/production/esup-runner-mediamtx@.service \
  "$LIVE_USER_UNIT_DIR/esup-runner-mediamtx@.service"
systemctl --user daemon-reload
)
```

Existing installations are not renamed automatically. Before switching to the
new instance names, stop and disable the old MediaMTX user services, prepare and
validate the `manager` and `runner` configurations, and confirm their listener
ports are available. Existing configuration directories and service units are
left untouched by the installer.

The default executable is `~/.local/bin/mediamtx`, either the installed binary
or a link to an existing `PATH` installation. The `install` command ensures
that this path exists without reinstalling a matching executable or changing
instance configurations and passwords.

If you explicitly chose `--binary`, use the same option above and change both
`MEDIAMTX_BIN` in the startup examples and the installed unit's `ExecStart` to
that path. A custom `--config-root` also requires adapting `MTX_CONFIG_DIR`,
`EnvironmentFile` and the YAML argument in `ExecStart`. Run
`systemctl --user daemon-reload` after editing the installed template.

### 4.3) Start the Manager's MediaMTX (`manager`)

Run this block on the Manager's MediaMTX host, after preparing `manager` in
section 2 or 3. The subshell stops at the first failed command, so validation
must succeed before this block enables and starts the service:

```bash
MTX_INSTANCE=manager
MTX_CONFIG_DIR="$HOME/.config/esup-runner/$MTX_INSTANCE"
MEDIAMTX_BIN="$HOME/.local/bin/mediamtx"
(
set -eu
"$MEDIAMTX_BIN" --validate-conf="$MTX_CONFIG_DIR/mediamtx.yml"
systemctl --user enable --now "esup-runner-mediamtx@$MTX_INSTANCE.service"
systemctl --user status "esup-runner-mediamtx@$MTX_INSTANCE.service" --no-pager
)
```

### 4.4) Start the Runner's MediaMTX (`runner`)

Run this block on the Runner's MediaMTX host, after preparing `runner` in
section 2 or 3. **If `manager` and `runner` are on the same host, run both
startup blocks.** The shared template from section 4.2 is already installed;
only the instance name and its configuration directory change:

```bash
MTX_INSTANCE=runner
MTX_CONFIG_DIR="$HOME/.config/esup-runner/$MTX_INSTANCE"
MEDIAMTX_BIN="$HOME/.local/bin/mediamtx"
(
set -eu
"$MEDIAMTX_BIN" --validate-conf="$MTX_CONFIG_DIR/mediamtx.yml"
systemctl --user enable --now "esup-runner-mediamtx@$MTX_INSTANCE.service"
systemctl --user status "esup-runner-mediamtx@$MTX_INSTANCE.service" --no-pager
)
```

All Runner processes and Live application instances on this server share the
`runner` MediaMTX service. Do not repeat the block to add a process or Live
worker. A different Runner server prepares and starts its own local `runner`
instance.

These units run MediaMTX. Continue with section 6 for the application settings
and with [Live Runner service setup](../../runner/docs/LIVE.md#service-and-hls-exposure)
to start all application groups through `esup-runner-runner.service`, using
`RUNNER_TASK_TYPES` and `LIVE_*` in `runner/.env`. The name `runner` selects
MediaMTX files and ports; it does not enable Live in the Runner by itself.
There is no additional application `runner.env` file to create here.

### 4.5) Check logs and manage an instance

Always select the instance explicitly before inspecting or restarting it:

```bash
MTX_INSTANCE=runner  # Use manager for the Manager's MediaMTX.
systemctl --user status "esup-runner-mediamtx@$MTX_INSTANCE.service" --no-pager
journalctl --user -u "esup-runner-mediamtx@$MTX_INSTANCE.service" -n 50 --no-pager
```

The service restarts automatically after a failure. After configuration changes,
restart the selected instance outside an active Live session:

```bash
systemctl --user restart "esup-runner-mediamtx@$MTX_INSTANCE.service"
```

### 4.6) Keep user services running after logout and at boot

These commands need an active user service manager. Prefer logging in directly
as the service account (for example over SSH); a plain `su` shell may lack the
user bus. Check with `systemctl --user is-system-running`. A `degraded` result
means the bus is available but one or more user units have failed; inspect them.
`Failed to connect to bus` requires fixing the login session/user manager first.

To run at boot and remain active after logout, enable **lingering** once for the
service account. Check its status with:

```bash
loginctl show-user "$(id -un)" -p Linger
```

If it is disabled, an administrator can run `loginctl enable-linger esup-runner`
with administrative privileges (substitute the actual account). Some local
policies allow users to enable their own lingering. This is host provisioning;
routine MediaMTX start/stop/restart commands remain `systemctl --user`.
See the [systemd loginctl reference](https://github.com/systemd/systemd/blob/main/man/loginctl.xml).

The user units retain `NoNewPrivileges=true` and a private `UMask=0077`.
They allow access to home-local files and avoid filesystem namespace directives
that depend on privileged or enabled unprivileged user namespaces. See the
[systemd execution reference](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

Continue with section 6 to connect the application.

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

The official image is pinned to **`bluenviron/mediamtx:1.21.1`**, with no `v`
prefix in the tag. Download it once per Docker host if it is not already present:

```bash
docker pull bluenviron/mediamtx:1.21.1
docker run --rm bluenviron/mediamtx:1.21.1 --version
```

Expected version: `v1.21.1`. Neither a host MediaMTX executable nor GPU access is
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

Use a checkout containing `manager/`, `runner/` and `deploy/live/`. If using a
sparse checkout, add these paths without replacing the existing selection:

```bash
cd /opt/esup-runner
git sparse-checkout add manager runner deploy/live
```

Then follow the [Live Docker Compose procedure](LIVE.md#docker-compose) to copy
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

The second command assumes that the Manager's `manager` MediaMTX configuration
is on this same machine. `--instance` defaults to `manager` for the Manager and
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
RTMP/HTTP profile and its listener environment overrides. For external
authentication, hashed credentials, RTMPS/HTTPS, systemd environment overrides or
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
| Missing API fields or unsupported configuration | Confirm the running executable/image is the pinned `1.21.1` version. |
| MediaMTX works but no HLS is available | Check Live reservation/session state, Runner FFmpeg, HLS directory permissions and the HTTPS frontend. |

Upstream references: [installation](https://mediamtx.org/docs/kickoff/install),
[configuration](https://mediamtx.org/docs/features/configuration), and
[start on boot](https://mediamtx.org/docs/features/start-on-boot).
