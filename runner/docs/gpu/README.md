# Runner GPU Documentation Matrix

This directory contains installation documentation for NVIDIA driver/CUDA and GPU-capable FFmpeg stacks used by the runner when `ENCODING_TYPE=GPU`.

## How to use this documentation

1. Choose the platform page matching your OS/CUDA stack.
2. Choose one FFmpeg deployment method.
3. Run the common validation checks from [../FFMPEG_SETUP.md](../FFMPEG_SETUP.md).

## Compatibility matrix

| Layer | Target | Documentation | Current status |
|---|---|---|---|
| NVIDIA + CUDA | Debian 11 + CUDA 12.4 | [DEBIAN11_CUDA12_4.md](DEBIAN11_CUDA12_4.md) | Validated |
| NVIDIA + CUDA | Debian 12 + CUDA 13.2 | [DEBIAN12_CUDA13_2.md](DEBIAN12_CUDA13_2.md) | Validated |
| FFmpeg method | Build from source | [FFMPEG_SOURCE.md](FFMPEG_SOURCE.md) | Validated |
| FFmpeg method | Install prebuilt package | [FFMPEG_PREBUILT.md](FFMPEG_PREBUILT.md) | Draft / to be validated |
| FFmpeg method | Docker wrapper binaries | [FFMPEG_DOCKER.md](FFMPEG_DOCKER.md) | Draft / to be validated |

## Recommended path

- Prefer source build when you need full control and reproducibility of FFmpeg features.
- Prefer prebuilt package when you have a trusted internal package already aligned with your driver/CUDA stack.
- Prefer Docker wrapper only when host package management must stay untouched.
