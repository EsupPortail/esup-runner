# FFmpeg GPU source build on Debian 11 with CUDA 12.4

## Scope

Build and install the FFmpeg GPU binary historically used with Debian 11 and CUDA 12.4.

## Status

- Historically validated on a target host with the original source-build procedure.
- The exact FFmpeg and `nv-codec-headers` revisions used for that validation were not recorded.
- A new build from the current upstream branches must therefore be treated as a new validation.

## Prerequisites

- Complete [DEBIAN11_CUDA12_4.md](DEBIAN11_CUDA12_4.md).
- Confirm that `nvidia-smi` and `/usr/local/cuda-12.4/bin/nvcc` work.
- Run the build from the account that owns `~/ffmpeg_prerequisites`.

## 1) Create the build workspace

```bash
mkdir -p ~/ffmpeg_prerequisites
cd ~/ffmpeg_prerequisites
```

## 2) Install build dependencies

Install the toolchain before building `nv-codec-headers`:

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config nasm yasm cmake libtool unzip wget \
  libc6-dev libnuma1 libnuma-dev gobjc libpng-dev libopus-dev \
  libx264-dev libvpx-dev
```

## 3) Install NVIDIA codec headers

```bash
cd ~/ffmpeg_prerequisites
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
cd nv-codec-headers
git rev-parse HEAD
sudo make install
```

Keep the printed commit with the deployment record. For a reproducible rebuild, check out that exact
commit before running `make install`.

## 4) Install LAME 3.100

This step preserves the procedure that was historically validated on Debian 11:

```bash
cd ~/ffmpeg_prerequisites
wget https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz \
  -O lame-3.100.tar.gz
tar -xzf lame-3.100.tar.gz
cd lame-3.100
./configure
make -j"$(nproc)"
sudo make install
sudo ldconfig
```

## 5) Build and install FFmpeg

```bash
cd ~/ffmpeg_prerequisites
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg
git rev-parse HEAD

make distclean 2>/dev/null || true

./configure \
  --prefix=/usr/local \
  --enable-gpl \
  --enable-nonfree \
  --enable-cuda \
  --enable-cuda-nvcc \
  --enable-nvenc \
  --enable-nvdec \
  --enable-cuvid \
  --enable-libmp3lame \
  --enable-libopus \
  --enable-libx264 \
  --enable-libvpx \
  --extra-cflags=-I/usr/local/cuda-12.4/include \
  --extra-ldflags=-L/usr/local/cuda-12.4/lib64

make -j"$(nproc)"
sudo make install
sudo ldconfig
hash -r
```

Keep the printed FFmpeg commit with the deployment record. Do not move the installed binaries from
`/usr/local/bin` to `/usr/bin`.

## 6) Verification

Run the [common FFmpeg verification](FFMPEG_SOURCE.md#common-verification).
