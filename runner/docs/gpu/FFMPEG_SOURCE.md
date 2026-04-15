# FFmpeg GPU Build From Source

## Scope

Build and install a GPU-capable FFmpeg binary on the host system.

## Status

- Validated with the current build-from-source procedure.

## Prerequisites

- NVIDIA driver and CUDA installed and working (`nvidia-smi`).
- One validated CUDA stack page completed:
  - [DEBIAN11_CUDA12_4.md](DEBIAN11_CUDA12_4.md)
  - [DEBIAN12_CUDA13_2.md](DEBIAN12_CUDA13_2.md)

## 1) Create a dedicated prerequisites directory

Use a dedicated workspace directory to keep all FFmpeg prerequisites and sources in one place:

```bash
cd
mkdir -p ffmpeg_prerequisites
cd ffmpeg_prerequisites
```

## 2) Install build dependencies

```bash
sudo apt update
sudo apt-get install -y git pkg-config

# Install nv-codec-headers (required for NVENC/NVDEC headers)
cd ~/ffmpeg_prerequisites
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
cd nv-codec-headers && sudo make install && cd ..

sudo apt-get install -y build-essential yasm cmake libtool libc6 libc6-dev unzip wget libnuma1 libnuma-dev
sudo apt install -y gobjc
sudo apt install -y libpng-dev
sudo apt-get install -y libopus-dev
sudo apt install -y libx264-dev pkg-config
sudo apt install -y nasm yasm
sudo apt install -y libvpx-dev pkg-config
```

## 3) Install LAME dependency

Download latest stable LAME source archive, [3.100](https://sourceforge.net/projects/lame/files/lame/3.100/), then:

```bash
cd ~/ffmpeg_prerequisites
wget https://downloads.sourceforge.net/project/lame/lame/3.100/lame-3.100.tar.gz -O lame-3.100.tar.gz
tar -zxvf lame-3.100.tar.gz
cd lame-3.100
./configure
make
sudo make install
```

## 4) Build and install FFmpeg

Adjust CUDA path flags if your target stack differs.

```bash
cd ~/ffmpeg_prerequisites
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg

# Clean previous build artifacts (if any)
make distclean 2>/dev/null || true

# Configure build options
./configure \
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
  --extra-cflags=-I/usr/local/cuda/include \
  --extra-ldflags=-L/usr/local/cuda/lib64

# Build and install
make -j"$(nproc)"
sudo make install

# Ensure runner/systemd resolves expected binaries
sudo mv /usr/local/bin/ffmpeg /usr/bin
sudo mv /usr/local/bin/ffprobe /usr/bin
```

## 5) Verification

```bash
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
