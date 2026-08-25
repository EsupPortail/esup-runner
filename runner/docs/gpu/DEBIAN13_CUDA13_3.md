# CUDA 13.3 on Debian 13

## Scope

Install NVIDIA drivers and CUDA on Debian 13 for runner GPU mode.

## Status

- Targets CUDA 13.3 Update 1, the latest release published by NVIDIA at the time of writing.
- Validated on a Debian 13 target host on 2026-08-25.
- Validation included the runner NVENC and `scale_cuda` GPU preflight checks.

## Prerequisites

- Debian 13 host with sudo access.
- NVIDIA GPU supported by current NVIDIA drivers.
- Runner installed under `/opt/esup-runner/runner`.

## Reference links

- NVIDIA Linux installation guide: https://docs.nvidia.com/cuda/cuda-installation-guide-linux/#debian
- NVIDIA driver installation guide for Debian: https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/debian.html
- CUDA downloads (Debian 13):
  https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=Debian&target_version=13&target_type=deb_network

## 1) Prepare Debian and add the NVIDIA CUDA repository

```bash
sudo apt update
sudo apt install -y wget ca-certificates gnupg software-properties-common linux-headers-$(uname -r) build-essential python3-dev

# Enable the contrib component if needed
sudo add-apt-repository contrib
sudo apt update

# NVIDIA repository for Debian 13 x86_64
wget https://developer.download.nvidia.com/compute/cuda/repos/debian13/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update
```

## 2) Install NVIDIA driver packages (headless server variant)

On Debian, NVIDIA documents two driver families:

- Open kernel modules (recommended for Turing and newer GPUs)
- Proprietary kernel modules (required for older GPUs)

For a headless server with a Turing or newer GPU, use:

```bash
sudo apt -V install -y nvidia-driver-cuda nvidia-kernel-open-dkms
```

For an older GPU that does not support the open kernel modules, use:

```bash
sudo apt -V install -y nvidia-driver-cuda nvidia-kernel-dkms
```

Do not install both kernel module variants.

## 3) Install CUDA 13.3 toolkit

To keep a version-pinned CUDA 13.3 line, install `cuda-toolkit-13-3`:

```bash
sudo apt install -y cuda-toolkit-13-3
```

Then reboot:

```bash
sudo reboot
```

## 4) Enable persistence mode

```bash
sudo systemctl enable nvidia-persistenced
```

## 5) Configure CUDA environment for the service user

As `esup-runner` (or your service account), update shell profile:

```bash
cd
nano .bashrc
```

Add:

```bash
export LD_LIBRARY_PATH=/usr/local/cuda-13.3/lib64:$LD_LIBRARY_PATH
export LIBRARY_PATH=/usr/local/cuda-13.3/lib64:$LIBRARY_PATH
export PATH=/usr/local/cuda-13.3/bin${PATH:+:${PATH}}
export CUDA_PATH=/usr/local/cuda-13.3
export CUDA_VISIBLE_DEVICES=0,1
export CUDA_DEVICE_ORDER=PCI_BUS_ID
```

_The lines `CUDA_VISIBLE_DEVICES=0,1` and `CUDA_DEVICE_ORDER=PCI_BUS_ID` are useful for a server with two GPUs._

Apply changes:

```bash
source .bashrc
```

## 6) Verification

```bash
nvidia-smi
nvidia-smi -L
nvcc --version

ffmpeg -hwaccels
ffmpeg -encoders | egrep 'nvenc'
ffmpeg -decoders | egrep 'cuvid|nvdec'
ffmpeg -filters  | egrep 'cuda|npp'

cd /opt/esup-runner/runner
uv run scripts/check_gpu.py
```

FFmpeg note: `-init_hw_device cuda:1` can be used to target the second CUDA GPU.
Transcription note: if `uv run scripts/check_gpu.py` fails or reports `cuda_available=False`, run
`make sync-transcription-gpu` in `/opt/esup-runner/runner`, then restart the runner service.
If transcription logs contain `fatal error: Python.h: No such file or directory` (often followed by Triton fallback warnings), install missing build headers then resync:

```bash
sudo apt install -y build-essential python3-dev
cd /opt/esup-runner/runner
make sync-transcription-gpu
```

## 7) Runner `.env` alignment

Use consistent values in `/opt/esup-runner/runner/.env`:

```properties
ENCODING_TYPE=GPU
GPU_HWACCEL_DEVICE=0
GPU_CUDA_VISIBLE_DEVICES=0
GPU_CUDA_DEVICE_ORDER=PCI_BUS_ID
GPU_CUDA_PATH=/usr/local/cuda-13.3
```

Then proceed with one FFmpeg method:

- [FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md](FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md)
- [FFMPEG_PREBUILT.md](FFMPEG_PREBUILT.md)
- [FFMPEG_DOCKER.md](FFMPEG_DOCKER.md)
