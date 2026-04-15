# FFmpeg GPU Install From Prebuilt Package

## Scope

Install a prebuilt FFmpeg package that already includes NVIDIA GPU capabilities.

## Status

- Draft / to be validated.

## Prerequisites

- NVIDIA driver and CUDA stack installed and validated.
- A trusted `.deb` package built with required GPU features.

## Required FFmpeg capabilities

- Encoders: `h264_nvenc`, `png`
- Decoder: `h264_cuvid`
- Filter: `scale_cuda`
- Optional for studio overlay: `overlay_cuda`

## Installation

```bash
sudo apt install -y ./ffmpeg_<version>_amd64.deb
```

If dependency fixes are required:

```bash
sudo apt -f install -y
```

## Verification

```bash
which ffmpeg
ffmpeg -version
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc"
ffmpeg -hide_banner -encoders | grep -E "png"
ffmpeg -hide_banner -decoders | grep -E "h264_cuvid"
ffmpeg -hide_banner -filters  | grep -E "scale_cuda|overlay_cuda|hwupload_cuda"
ffmpeg -hide_banner -buildconf | grep -E -- "--enable-libvpx"
ffmpeg -hide_banner -encoders | grep -E "libx264|h264"

cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```

## Notes

- Keep package version aligned with your NVIDIA driver/CUDA stack.
- Ensure systemd resolves the same binary path as your interactive shell.
