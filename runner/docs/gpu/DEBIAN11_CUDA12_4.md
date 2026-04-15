# CUDA 12.4 on Debian 11

## Scope

Install NVIDIA drivers and CUDA on Debian 11 for runner GPU mode.

## Status

- Validated with the current installation procedure.

## Prerequisites

- Debian 11 host with sudo access.
- NVIDIA GPU supported by current NVIDIA drivers.
- Runner installed under `/opt/esup-runner/runner`.

## Reference links

- NVIDIA Linux installation guide: https://docs.nvidia.com/cuda/cuda-installation-guide-linux/#debian
- CUDA downloads (Debian 11):
  https://developer.nvidia.com/cuda-downloads?target_os=Linux&target_arch=x86_64&Distribution=Debian&target_version=11&target_type=deb_network

## 1) Prepare Debian and add the NVIDIA CUDA repository

```bash
sudo apt update
sudo apt install -y locales software-properties-common wget build-essential python3-dev

# Enable the contrib component if needed
sudo add-apt-repository contrib

# Optional: remove legacy NVIDIA key if present
sudo apt-key del 7fa2af80 || true

# NVIDIA repository for Debian 11 x86_64
wget https://developer.download.nvidia.com/compute/cuda/repos/debian11/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update
```

## 2) Install CUDA 12.4 packages

```bash
sudo apt install -y cuda-toolkit-12-4
sudo apt install -y cuda-drivers
```

Then reboot:

```bash
sudo reboot
```

## 3) Enable persistence mode

```bash
sudo systemctl enable nvidia-persistenced
```

## 4) Configure CUDA environment for the service user

As `esup-runner` (or your service account), update shell profile:

```bash
cd
nano .bashrc
```

Add:

```bash
export LD_LIBRARY_PATH=/usr/local/cuda-12.4/lib64:$LD_LIBRARY_PATH
export LIBRARY_PATH=/usr/local/cuda-12.4/lib64:$LIBRARY_PATH
export PATH=/usr/local/cuda-12.4/bin${PATH:+:${PATH}}
export CUDA_PATH=/usr/local/cuda-12.4
export CUDA_VISIBLE_DEVICES=0,1
export CUDA_DEVICE_ORDER=PCI_BUS_ID
```
_The lines `CUDA_VISIBLE_DEVICES=0,1` and `CUDA_DEVICE_ORDER=PCI_BUS_ID` are useful for a server with two GPUs._

Apply changes:

```bash
source .bashrc
```

## 5) Optional cleanup

```bash
sudo apt autoremove -y
```

## 6) Verification

```bash
sudo systemctl status nvidia-persistenced
nvidia-smi
nvcc --version

cd /opt/esup-runner/runner
uv run scripts/check_gpu.py
```

If transcription logs contain `fatal error: Python.h: No such file or directory` (often followed by Triton fallback warnings), install missing build headers then resync:

```bash
sudo apt install -y build-essential python3-dev
cd /opt/esup-runner/runner
make sync-transcription-gpu
```

Optional reboot:

```bash
sudo reboot
```

## 7) Runner `.env` alignment

Use consistent values in `/opt/esup-runner/runner/.env`:

```properties
ENCODING_TYPE=GPU
GPU_HWACCEL_DEVICE=0
GPU_CUDA_VISIBLE_DEVICES=0
GPU_CUDA_DEVICE_ORDER=PCI_BUS_ID
GPU_CUDA_PATH=/usr/local/cuda-12.4
```

Then proceed with one FFmpeg method:

- [FFMPEG_SOURCE.md](FFMPEG_SOURCE.md)
- [FFMPEG_PREBUILT.md](FFMPEG_PREBUILT.md)
- [FFMPEG_DOCKER.md](FFMPEG_DOCKER.md)
