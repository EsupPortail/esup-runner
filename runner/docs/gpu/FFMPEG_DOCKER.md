# FFmpeg GPU Via Docker Wrappers

## Scope

Provide `ffmpeg` and `ffprobe` through Docker wrapper scripts while runner stays on host/systemd.

## Status

- Draft / to be validated.

## Prerequisites

- Docker installed and usable by `esup-runner`.
- NVIDIA Container Toolkit installed and GPU access working in Docker.
- A GPU-enabled FFmpeg image available (`<ffmpeg-gpu-image>` placeholder below).

## Important behavior

Runner scripts call `ffmpeg`/`ffprobe` as local commands.
When runner runs on host (systemd), host wrapper scripts must proxy each call to Docker.

## 1) Create wrapper scripts

`/usr/local/bin/ffmpeg`:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec docker run --rm --gpus all \
  -u "$(id -u):$(id -g)" \
  -v /tmp/esup-runner:/tmp/esup-runner \
  -v /var/log/esup-runner:/var/log/esup-runner \
  <ffmpeg-gpu-image> \
  ffmpeg "$@"
```

`/usr/local/bin/ffprobe`:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec docker run --rm --gpus all \
  -u "$(id -u):$(id -g)" \
  -v /tmp/esup-runner:/tmp/esup-runner \
  -v /var/log/esup-runner:/var/log/esup-runner \
  <ffmpeg-gpu-image> \
  ffprobe "$@"
```

Set executable permissions:

```bash
sudo chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe
```

## 2) Verification

```bash
ffmpeg -version
ffprobe -version
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc"
ffmpeg -hide_banner -encoders | grep -E "png"
ffmpeg -hide_banner -decoders | grep -E "h264_cuvid"
ffmpeg -hide_banner -filters  | grep -E "scale_cuda|overlay_cuda|hwupload_cuda"

cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```

## Notes

- Mount every directory used by runner tasks into the FFmpeg container.
- This method adds container startup overhead for every FFmpeg call.
- For high-throughput production setups, running the whole runner in Docker is usually cleaner.
