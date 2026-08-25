# FFmpeg GPU source build on Debian 13 with CUDA 13.3

## Scope

Build and install a GPU-capable FFmpeg binary on Debian 13 with CUDA 13.3.

## Status

- Validated on a Debian 13/CUDA 13.3 target host on 2026-08-25.
- Validated source pair: FFmpeg `n9.0.1` and `nv-codec-headers` `n13.1.15.0`.
- Validation covered NVENC, CUVID, `scale_cuda`, `hwupload_cuda` and `overlay_cuda`.

## Reference links

- NVIDIA FFmpeg GPU compilation guide:
  https://docs.nvidia.com/video-technologies/video-codec-sdk/13.1/ffmpeg-with-nvidia-gpu/index.html
- CUDA 13.3 release notes:
  https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html
- FFmpeg releases: https://ffmpeg.org/releases/
- NVIDIA codec header releases: https://github.com/FFmpeg/nv-codec-headers/releases

## Prerequisites

- Complete [DEBIAN13_CUDA13_3.md](DEBIAN13_CUDA13_3.md).
- Use a Turing or newer NVIDIA GPU for the currently documented Video Codec SDK stack.
- Confirm that the NVIDIA driver is version `610.43.02` or newer.
- Confirm that `nvidia-smi` and `/usr/local/cuda-13.3/bin/nvcc` work.
- Run the build from the account that owns `~/ffmpeg_prerequisites`.

Check the driver before compiling:

```bash
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
```

## 1) Create the build workspace

```bash
mkdir -p ~/ffmpeg_prerequisites
cd ~/ffmpeg_prerequisites
```

## 2) Install build dependencies

Debian 13 provides LAME 3.100 as a development package, so a separate LAME source build is not needed:

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config nasm clang \
  libmp3lame-dev libopus-dev libx264-dev libvpx-dev
```

## 3) Install the pinned NVIDIA codec headers

```bash
cd ~/ffmpeg_prerequisites
git clone --branch n13.1.15.0 --depth 1 \
  https://code.ffmpeg.org/FFmpeg/nv-codec-headers.git
cd nv-codec-headers
git rev-parse HEAD
sudo make install
```

## 4) Build and install the pinned FFmpeg release

The CUDA filters are compiled with Clang, following the current NVIDIA recommendation. Do not add
`--enable-cuda-nvcc` or `--enable-libnpp` to this profile.

```bash
cd ~/ffmpeg_prerequisites
git clone --branch n9.0.1 --depth 1 \
  https://code.ffmpeg.org/FFmpeg/FFmpeg.git ffmpeg
cd ffmpeg
git rev-parse HEAD

./configure \
  --prefix=/usr/local \
  --enable-gpl \
  --enable-nonfree \
  --enable-cuda \
  --enable-cuda-llvm \
  --enable-nvenc \
  --enable-nvdec \
  --enable-cuvid \
  --enable-libmp3lame \
  --enable-libopus \
  --enable-libx264 \
  --enable-libvpx \
  --nvcc=clang \
  --extra-cflags=-I/usr/local/cuda-13.3/include \
  --extra-ldflags=-L/usr/local/cuda-13.3/lib64

make -j"$(nproc)"
sudo make install
sudo ldconfig
hash -r
```

Keep FFmpeg and `ffprobe` under `/usr/local/bin`. This directory is already present in the runner
systemd service `PATH`.

## 5) Verification

Check that the intended revisions and CUDA compiler mode were used:

```bash
pkg-config --modversion ffnvcodec
ffmpeg -hide_banner -buildconf | grep -E -- "--enable-cuda-llvm|--nvcc=clang"
```

Then run the [common FFmpeg verification](FFMPEG_SOURCE.md#common-verification).

For future revalidations, record the GPU model, driver version, FFmpeg commit,
`nv-codec-headers` commit and runner check output.
