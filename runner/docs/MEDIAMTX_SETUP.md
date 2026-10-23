# MediaMTX setup for Live Runners

For a first native deployment, start with the
[Manager quickstart](../../manager/docs/LIVE_QUICKSTART.md), then follow the
[Runner quickstart](LIVE_QUICKSTART.md). For Docker, use
[Live Compose installation](../../manager/docs/LIVE_INSTALLATION.md#docker-compose-shortcut).
This page keeps the Runner-specific MediaMTX details and validation links.

A Runner server needs one **MediaMTX >= 1.21.1** service, shared by all its Live
instances and separate from the Manager's MediaMTX. Reuse a supported executable
or Docker image already installed on the host. For native or Docker installation,
follow the [shared guide](../../manager/docs/MEDIAMTX_SETUP.md).

## Installation path

| Method | Shared instructions |
| --- | --- |
| Reuse an existing installation | [Check the installed version and instances](../../manager/docs/MEDIAMTX_SETUP.md#already-installed-on-this-machine) |
| Native executable | [Install a supported binary](../../manager/docs/MEDIAMTX_SETUP.md#2-install-the-native-executable), [configure it from the Runner directory](../../manager/docs/MEDIAMTX_SETUP.md#3-prepare-one-instance), then [start systemd](../../manager/docs/MEDIAMTX_SETUP.md#4-start-the-native-systemd-service) |
| MediaMTX in Docker with a native Runner | [Configure it from the Runner directory](../../manager/docs/MEDIAMTX_SETUP.md#3-prepare-one-instance), then [start the container](../../manager/docs/MEDIAMTX_SETUP.md#5-run-mediamtx-in-docker) |
| Complete Docker stack | [Manager/Runner Compose deployment](../../manager/docs/MEDIAMTX_SETUP.md#complete-stack-with-docker-compose) |

The Runner [`scripts/install_mediamtx.py`](../scripts/install_mediamtx.py)
utility can install a selected stable release >= 1.21.1 and prepare this Runner in one step.
It uses only files inside `runner/`, including in a Runner-only sparse checkout.
Run it as the service account, without `sudo`:

```bash
cd /opt/esup-runner/runner
python3 scripts/install_mediamtx.py setup --instance runner
python3 scripts/install_mediamtx.py start --instance runner
```

The defaults are `~/.local/bin/mediamtx` and
`~/.config/esup-runner/runner/`. When reusing an executable from `PATH`, the
installer creates a link at the default binary path without copying or
reinstalling it. Rerun `install` if an earlier setup reused the executable
without creating this link. An explicit `--binary` instead requires adapting
the unit's `ExecStart`, as described in the shared guide.
`start` validates YAML with the listener overrides, installs the component-local
user unit, reloads systemd and enables/starts the prepared instance. A working
`systemctl --user` login session is required. Existing customized units are
retained and require manual management; active services are never restarted.
The unit directory follows `XDG_CONFIG_HOME`, defaulting to
`~/.config/systemd/user/`. See the shared guide for boot/logout persistence.
Use `configure` instead of `setup` for Docker. Add `--dry-run` to preview either
operation. `setup`/`configure` only prepare files and preserve passwords.

### Version selection and manual upgrades

**1.21.1 is the minimum accepted version and the default download.** Any newer
stable installed version is reused. To download a chosen release at an unused
destination, pass `--version 1.21.2` to `install` or `setup`; the downloaded
binary must report that exact release. Existing executables are never replaced.

Follow the shared [manual upgrade procedure](../../manager/docs/MEDIAMTX_SETUP.md#manual-upgrades)
for native services or Docker, including backups, configuration validation and
rollback. **A MediaMTX restart interrupts all Live workers sharing it**, drops
temporary relay paths and can require encoder reconnection/session recovery.
Perform the change between broadcasts and verify the current HLS URL afterwards.
Manager/Runner updates and `update-stack.sh` do not upgrade MediaMTX automatically.

The local installer selects the [Runner YAML template](../production/mediamtx-live.yml).
The example ports are RTMP `2935`, Control API `10997`, and metrics `10998`, all on loopback for a
native application. These ports and credentials are shared by all Live groups
in the same `.env`. Do not create another MediaMTX instance on this server to
increase Runner or Live capacity. A different server prepares its own `runner`
instance with its local ports.

## CPU and GPU

MediaMTX installation is identical for both. It does not need CUDA or GPU access,
including in Docker. FFmpeg performs transcoding in the **Runner** environment:

- CPU: use FFmpeg with libx264/AAC and `LIVE_TRANSCODER=cpu` (the default).
- GPU: follow [FFMPEG_SETUP.md](FFMPEG_SETUP.md) for the NVIDIA driver and suitable
  FFmpeg build, then set `LIVE_TRANSCODER=nvenc`. The supplied complete Compose stack
  uses CPU FFmpeg unless the Runner image/runtime is adapted for GPU use.

Live backend selection is independent of `ENCODING_TYPE`; the former `auto`
value is no longer accepted. See [Live Runner configuration](LIVE.md#instance-configuration)
for GPU selection, optional NVDEC and CPU fallback.

## Connect and validate

Follow the shared guide's [automatic application connection](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify):

Install the Runner's application dependencies first with `make sync` or the
appropriate transcription/development sync target. The helper uses the Runner
environment, with PyYAML and python-dotenv managed by `pyproject.toml` and `uv.lock`.

```bash
cd /opt/esup-runner/runner
export UV_CACHE_DIR=/tmp/esup-runner-uv-cache
# Manager and Runner MediaMTX configurations on this machine:
uv run scripts/configure_live.py
# Or, when the Manager is remote (prompts privately for its relay password):
# uv run scripts/configure_live.py --manager-rtmp-host manager.example.org
```

The helper fills this Runner's control/transcoder credentials and the Manager's
relay connection directly in `runner/.env`, with a private backup. Add `--dry-run`
to preview key names. It never writes MediaMTX files or restarts services.

Declare `RUNNER_TASK_TYPES="[1x(encoding,studio),2x(live)]"` in that same `.env`.
The main service starts all three instances. Direct HLS URLs are derived from the
Runner address and actual worker ports, and HLS files default to `/dev/shm/hls`.
Override the public URL for a reverse proxy as explained in
[LIVE.md](LIVE.md#shared-settings-and-automatic-instance-values).
`runner` names the MediaMTX configuration only; no `runner.env` is needed there.

Then complete [LIVE.md](LIVE.md) for the application service, instance identities,
HLS storage, HTTPS exposure and the [manual validation](LIVE.md#manual-validation).
