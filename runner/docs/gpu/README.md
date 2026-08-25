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
| NVIDIA + CUDA | Debian 13 + CUDA 13.3 | [DEBIAN13_CUDA13_3.md](DEBIAN13_CUDA13_3.md) | Validated |
| FFmpeg source | Debian 11 + CUDA 12.4 | [FFMPEG_SOURCE_DEBIAN11_CUDA12_4.md](FFMPEG_SOURCE_DEBIAN11_CUDA12_4.md) | Historically validated; source revisions not recorded |
| FFmpeg source | Debian 12 + CUDA 13.2 | [FFMPEG_SOURCE_DEBIAN12_CUDA13_2.md](FFMPEG_SOURCE_DEBIAN12_CUDA13_2.md) | Historically validated; source revisions not recorded |
| FFmpeg source | Debian 13 + CUDA 13.3 | [FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md](FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md) | Validated with FFmpeg `n9.0.1` and `nv-codec-headers` `n13.1.15.0` |
| FFmpeg method | Install prebuilt package | [FFMPEG_PREBUILT.md](FFMPEG_PREBUILT.md) | Draft / to be validated |
| FFmpeg method | Docker wrapper binaries | [FFMPEG_DOCKER.md](FFMPEG_DOCKER.md) | Draft / to be validated |

## Recommended path

- Prefer the source-build profile matching the exact Debian/CUDA stack when you need full control of
  FFmpeg features. Record both upstream source revisions after validation.
- Prefer prebuilt package when you have a trusted internal package already aligned with your driver/CUDA stack.
- Prefer Docker wrapper only when host package management must stay untouched.
