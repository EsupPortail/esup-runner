# Install Manager, Runner, or both

## Choose an installation method

| Method | When to use it | Guides |
| --- | --- | --- |
| Guided installation | Set up one or both components on Debian/Ubuntu with systemd. The script installs dependencies, creates configuration, and starts the services. | [Guided setup](#guided-installation) or [unattended setup](#unattended-installation) |
| Manual installation | Set up the services directly on the host and configure each step yourself, including GPU support for the Runner if needed. | [Manager](manager/docs/INSTALLATION.md) · [Runner](runner/docs/INSTALLATION.md) |
| Docker | Run the components in containers. Pull the published images or build your own. | [Manager](manager/docs/DOCKER.md) · [Runner](runner/docs/DOCKER.md) |

For Live streaming, the [Live installation guide](manager/docs/LIVE_INSTALLATION.md)
covers native setup and Docker Compose.

## Guided installation

Use `install-stack.sh` on a Debian/Ubuntu server with systemd and Internet access.
It installs CPU services under `/opt/esup-runner`, running as `esup-runner`.
Run it with `sudo` or as root; application commands run as the service account.

Download the script, then start the assistant:

```bash
curl --fail --show-error --location \
  https://raw.githubusercontent.com/EsupPortail/esup-runner/main/install-stack.sh \
  --output install-stack.sh
sudo bash install-stack.sh
```

If you already have the repository in `/opt/esup-runner`, run its
`install-stack.sh` directly. The assistant asks which components to install,
their addresses, and the required passwords. Secret input is hidden.

You can also select a component directly:

```bash
# Manager and Runner on this server
sudo bash install-stack.sh --component both

# Manager for Runners on other servers
sudo bash install-stack.sh --component manager --manager-host manager.internal

# Runner connected to an existing Manager
sudo bash install-stack.sh --component runner \
  --manager-url http://manager.internal:8081 --runner-host runner.internal
```

Use DNS names or IPv4 addresses reachable by the other component. The default
`127.0.0.1` is suitable only when both components share a server. A remote Runner
needs an API token already authorized by its Manager; the script asks for it.
Manager and Runner versions must match at `MAJOR.MINOR`.

## What the script does

- Installs system packages, `uv`, Python, and the selected applications.
- Creates the service account and configuration files, generates API tokens,
  and asks for the `admin` account password when installing a Manager.
- Shares the API token automatically when both components are local.
- Creates runtime directories, installs systemd user services, enables startup
  at boot, and checks configuration and service health.

The Runner stores task files in `/tmp/esup-runner` by default. The host may clean
files under `/tmp`. The Manager stores its state in `/opt/esup-runner/manager/data`.
The service account uses `~/.cache/esup-runner/uv` as its uv cache.

New `.env` files, one per component, are readable only by the service account.
The final output gives the administration URL and useful service commands.
Generated API tokens stay in `.env`.

The script installs neither GPU drivers nor a GPU FFmpeg build. It does not
configure a firewall, reverse proxy, or HTTPS. Keep service ports private; use
the [Manager installation guide](manager/docs/INSTALLATION.md#41-production-hardening-reverse-proxy--https)
when publishing the administration interface.

## Optional capabilities

Add these options to the installation command:

| Option | Effect |
| --- | --- |
| `--with-transcription` | Installs CPU transcription dependencies and enables transcription on the Runner. |
| `--with-live` | Installs and connects MediaMTX; adds separate Live capacity on the Runner. |
| `--service-user NAME` | Uses another dedicated account; an existing checkout must belong to it. |
| `--dry-run` | Shows the plan without installing, downloading, or changing files. |
| `--help` | Lists all options. |

Live setup downloads the latest stable MediaMTX release (minimum `1.21.1`) when
no compatible executable is installed. For existing installations and scheduled
updates, see [MediaMTX updates](manager/docs/MEDIAMTX_SETUP.md#native-update-command).

For Live on separate servers, install the Manager first and supply its relay
password when installing the Runner. Follow [Manager quickstart, step 5](manager/docs/LIVE_QUICKSTART.md#5-publish-the-first-broadcast)
to publish a first stream. Public playback also needs the
[HLS reverse proxy](runner/docs/LIVE.md#hls-reverse-proxy).
For GPU installations, use the [Runner GPU guides](runner/docs/gpu/README.md).

## Unattended installation

Use `--non-interactive` and supply secrets through a file readable only by root
(mode `0600`). For example, create `/root/esup-runner-install.env` containing:

```dotenv
INSTALL_ADMIN_PASSWORD=replace-with-a-private-admin-password
# Required only for a Runner connected to a remote Manager:
INSTALL_RUNNER_TOKEN=replace-with-an-authorized-manager-token
# Required only for Live connected to a remote Manager:
INSTALL_LIVE_RELAY_PASSWORD=replace-with-the-manager-relay-password
```

Then run:

```bash
sudo bash install-stack.sh --component both --non-interactive \
  --secrets-file /root/esup-runner-install.env
```

The installer forwards the secrets privately to the service account. It does
not print them or place them in command arguments. Remove the input file when
you no longer need it.

## Rerun or add a component

After an interrupted installation, rerun the same command. Existing `.env`
files, service customizations, and task data are preserved. Running services
are not automatically restarted. You can add a local Runner later with
`--component runner`; the installer reuses the local Manager's connection settings.

Options configure new components. To change an existing component's task types,
addresses, or Live settings, use its configuration and Live guides. Use
[`update-stack.sh`](update-stack.sh) for subsequent updates; the installer does
not pull sources or reset an existing checkout.
