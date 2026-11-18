# MediaMTX for Live Runners

For a first native installation, complete Manager steps 1–4 in the
[Manager quickstart](../../manager/docs/LIVE_QUICKSTART.md), then follow the
[Runner quickstart](LIVE_QUICKSTART.md). It includes MediaMTX installation,
credentials, the application service and a first HLS test.

Each Runner deployment uses **one shared MediaMTX service** for all its Live
workers, separate from the Manager's service even on a shared host. Adding
workers does not require another MediaMTX service.

The [shared MediaMTX reference](../../manager/docs/MEDIAMTX_SETUP.md) applies
to both components. Run its Runner commands from `runner/`; the local installer
and template also work in a Runner-only checkout.

| Need | Reference |
| --- | --- |
| Reuse or install an executable | [Native installation](../../manager/docs/MEDIAMTX_SETUP.md#2-install-the-native-executable) |
| Custom ports, configuration or log paths | [Prepare an instance](../../manager/docs/MEDIAMTX_SETUP.md#3-prepare-one-instance) |
| Local/remote Manager credentials | [Application connections](../../manager/docs/MEDIAMTX_SETUP.md#6-connect-the-applications-and-verify) |
| MediaMTX container with a native Runner | [Docker](../../manager/docs/MEDIAMTX_SETUP.md#5-run-mediamtx-in-docker) |
| Complete Manager/Runner Docker stack | [Compose installation](../../manager/docs/LIVE_INSTALLATION.md#docker-compose-shortcut) |
| Upgrade an existing installation | [Upgrades](../../manager/docs/MEDIAMTX_SETUP.md#manual-upgrades); use the Manager installer on shared hosts, especially for system services |
| More workers, HLS routing or GPU | [Runner Live configuration](LIVE.md) |

MediaMTX needs no GPU access. For GPU transcoding, configure FFmpeg in the
Runner environment and set `LIVE_TRANSCODER=nvenc`; see [FFmpeg setup](FFMPEG_SETUP.md).
