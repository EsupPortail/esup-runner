# FFmpeg setup for Runner GPU mode

This document provides three ways to deploy a GPU-capable FFmpeg for the runner.

Use this document for `ENCODING_TYPE=GPU`.
For CPU-only mode, install the distribution package:

```bash
sudo apt install -y ffmpeg
```

## Requirements (all methods)

- NVIDIA driver installed and GPU visible with `nvidia-smi`.
- `ffmpeg` and `ffprobe` available in `PATH` for the `esup-runner` user.
- Runner configured for GPU mode in `.env`. For example:

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
- Filter: `scale_npp`
- Optional (studio GPU overlay): `overlay_cuda`

Quick validation commands:

```bash
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc"
ffmpeg -hide_banner -encoders | grep -E "png"
ffmpeg -hide_banner -decoders | grep -E "h264_cuvid"
ffmpeg -hide_banner -filters  | grep -E "scale_npp|overlay_cuda|hwupload_cuda"
```

Additional quick checks for common failures:

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

## 1) Install FFmpeg by compiling sources

This gives full control over FFmpeg/CUDA options.

Install build dependencies (Debian/Ubuntu example):

### NVIDIA driver and CUDA installation

Follow the official NVIDIA documentation for Debian:

- https://docs.nvidia.com/cuda/cuda-installation-guide-linux/#debian
- https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html

#### Install CUDA 12.4 (or newer)

Debian 11 example using the package manager:

https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=Debian&target_version=11&target_type=deb_network

```bash
sudo apt-get install locales
sudo apt-get install -y software-properties-common

sudo add-apt-repository contrib
sudo apt-key del 7fa2af80
wget https://developer.download.nvidia.com/compute/cuda/repos/debian11/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb

sudo apt-get update
sudo apt-get -y install cuda

# Before reboot, update .bashrc as shown below
sudo reboot
```

#### Enable persistence mode

Enable the NVIDIA persistence daemon:

```bash
sudo systemctl enable nvidia-persistenced
```

#### Enable CUDA 12.4 in the shell

Reference:
https://docs.nvidia.com/cuda/cuda-installation-guide-linux/index.html#post-installation-actions

Update `.bashrc` for the service account (for example `esup-runner`) to enable CUDA:

```bash
cd
nano .bashrc

############
# Manually added to enable CUDA:
export LD_LIBRARY_PATH=/usr/local/cuda-12.4/lib64:$LD_LIBRARY_PATH
export LIBRARY_PATH=/usr/local/cuda-12.4/lib64:$LIBRARY_PATH
export PATH=/usr/local/cuda-12.4/bin${PATH:+:${PATH}}
export CUDA_PATH=/usr/local/cuda-12.4
export CUDA_VISIBLE_DEVICES=0,1
export CUDA_DEVICE_ORDER=PCI_BUS_ID
############

source .bashrc
```

`CUDA_VISIBLE_DEVICES=0,1` and `CUDA_DEVICE_ORDER=PCI_BUS_ID` are useful on servers with two GPUs.

#### Install CUDA toolkit

```bash
sudo apt-get install cuda-toolkit
```

#### Remove old packages (optional)

```bash
sudo apt autoremove
```

#### Final verification

```bash
# Verify CUDA driver package installation
sudo apt-get install cuda-drivers
# Verify persistence daemon state
sudo systemctl status nvidia-persistenced
# Verify GPU detection
nvidia-detect
# Verify NVIDIA CLI tool
nvidia-smi
# Optional final reboot
sudo reboot
```

No errors should be reported.


### FFmpeg prerequisite: LAME

Download the latest stable LAME release (currently 3.100):
https://sourceforge.net/projects/lame/files/lame/3.100/

Extract and install it:
```bash
cd
tar -zxvf lame-3.100.tar.gz
cd lame-3.100
./configure
make
sudo make install
```

### FFmpeg prerequisites

```bash
sudo apt update
sudo apt-get install git pkg-config -y

# Install nv-codec-headers (required for NVENC/NVDEC headers)
git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git
cd nv-codec-headers && sudo make install && cd ..

sudo apt-get install build-essential yasm cmake libtool libc6 libc6-dev unzip wget libnuma1 libnuma-dev
# Additional dependency for recent builds
sudo apt install -y gobjc
# PNG
sudo apt install -y libpng-dev
# libopus
sudo apt-get install -y libopus-dev
# libx264
sudo apt install -y libx264-dev pkg-config
# libvpx
sudo apt install -y nasm yasm
sudo apt install -y libvpx-dev pkg-config
```

### Build and install FFmpeg

Adjust the CUDA path if needed:

```bash
cd /tmp
git clone https://git.ffmpeg.org/ffmpeg.git ffmpeg
cd ffmpeg

# Clean previous build artifacts (if any)
make distclean 2>/dev/null || true

# Configure build options
./configure --enable-cuda --enable-nonfree --enable-cuda-nvcc --enable-cuvid --enable-nvenc --enable-nvdec --enable-libnpp --enable-libmp3lame --extra-cflags=-I/usr/local/cuda/include --extra-ldflags=-L/usr/local/cuda/lib64 --enable-libopus --enable-gpl --enable-libx264 --enable-libvpx

# Build and install
make -j"$(nproc)" && sudo make install

# Move ffmpeg and ffprobe binaries
sudo mv /usr/local/bin/ffmpeg /usr/bin
sudo mv /usr/local/bin/ffprobe /usr/bin

# Verification
ffmpeg -version
```

Validate with the commands from the Requirements section and run:

```bash
cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```

## 2) Install FFmpeg from a prebuilt package (Documentation to be reviewed / NOT TESTED)

This is the fastest approach when a trusted package built with CUDA/NVENC is available.

Install a local `.deb` package (example):

```bash
sudo apt install -y ./ffmpeg_<version>_amd64.deb
```

If dependency fixes are needed:

```bash
sudo apt -f install -y
```

Notes:

- Ensure the package is built with NVIDIA GPU support (`h264_nvenc`, `h264_cuvid`, `scale_npp`...).
- Ensure the installed binary is the one used by systemd (`which ffmpeg`).
- Keep package version aligned with the NVIDIA driver/CUDA stack.

After installation, run the same validation commands and:

```bash
cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```

## 3) Install FFmpeg with a Docker container (Documentation to be reviewed / NOT TESTED)

Use this approach when FFmpeg binaries must stay outside the host system.

Important: runner scripts call `ffmpeg`/`ffprobe` as local commands.
When the runner stays on the host (systemd), wrapper scripts must invoke Docker.

Example wrapper for `/usr/local/bin/ffmpeg`:

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

And `/usr/local/bin/ffprobe`:

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

Notes:

- Docker + NVIDIA Container Toolkit must be installed and working.
- Mount every directory used by runner tasks (storage/logs/temp) into the container.
- This approach adds container startup overhead on every FFmpeg call.
- For heavy production workloads, running the whole runner in a container is usually cleaner.

Finally, validate:

```bash
cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```
