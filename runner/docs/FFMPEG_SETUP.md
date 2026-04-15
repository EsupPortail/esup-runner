# FFmpeg setup for Runner GPU mode

This document is the entry point for deploying a GPU-capable FFmpeg stack for the runner.

Use this for `ENCODING_TYPE=GPU`.
For CPU-only mode, install distribution FFmpeg:

```bash
sudo apt install -y ffmpeg
```

## Documentation map

- GPU matrix and navigation: [gpu/README.md](gpu/README.md)

NVIDIA/CUDA platform pages:
- Debian 11 + CUDA 12.4: [gpu/DEBIAN11_CUDA12_4.md](gpu/DEBIAN11_CUDA12_4.md)
- Debian 12 + CUDA 13.2: [gpu/DEBIAN12_CUDA13_2.md](gpu/DEBIAN12_CUDA13_2.md)

FFmpeg deployment methods:
- Build from source: [gpu/FFMPEG_SOURCE.md](gpu/FFMPEG_SOURCE.md)
- Install prebuilt package: [gpu/FFMPEG_PREBUILT.md](gpu/FFMPEG_PREBUILT.md)
- Use Docker wrappers: [gpu/FFMPEG_DOCKER.md](gpu/FFMPEG_DOCKER.md)

## Common requirements (all GPU methods)

- NVIDIA driver installed and GPU visible with `nvidia-smi`.
- `ffmpeg` and `ffprobe` available in `PATH` for the `esup-runner` user.
- Runner configured for GPU mode in `.env`.

Example `.env` values:

```properties
ENCODING_TYPE=GPU
GPU_HWACCEL_DEVICE=0
GPU_CUDA_VISIBLE_DEVICES=0
GPU_CUDA_DEVICE_ORDER=PCI_BUS_ID
GPU_CUDA_PATH=/usr/local/cuda-13.2
```

Expected FFmpeg GPU capabilities for runner scripts:

- Encoders: `h264_nvenc` and `png`
- Decoder: `h264_cuvid`
- Filter: `scale_cuda`
- Optional (studio GPU overlay): `overlay_cuda`

## Common validation checklist

Quick validation commands:

```bash
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc"
ffmpeg -hide_banner -encoders | grep -E "png"
ffmpeg -hide_banner -decoders | grep -E "h264_cuvid"
ffmpeg -hide_banner -filters  | grep -E "scale_cuda|overlay_cuda|hwupload_cuda"
```

Additional checks for common failures:

```bash
# Detect: buildconf:libvpx -> Missing --enable-libvpx
ffmpeg -hide_banner -buildconf | grep -E -- "--enable-libvpx"

# Detect: preflight:cpu -> No CPU H.264 encoder found (libx264 or h264)
ffmpeg -hide_banner -encoders | grep -E "libx264|h264"
```

Runner-level check:

```bash
cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```
