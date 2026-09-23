# FFmpeg 8 on Debian 13 with CUDA 13.3

## Recommendation

ESUP-Runner remains on the latest stable FFmpeg 8 release for production encoding. As of
2026-09-23, use:

- FFmpeg `n8.1.3`, commit `1041abdc962f4cc4f394aa8de9dc5236c0c3b9e7`;
- `nv-codec-headers` `n13.1.15.0`;
- the existing Debian 13, CUDA 13.3 and NVIDIA driver stack.

Do not install FFmpeg `9.0.1` for runner encoding. A scheduler regression can stop an audio stream
before the end while video encoding continues. FFmpeg may still exit with status `0`, which makes
the incomplete output look successful unless every stream duration is checked with `ffprobe`.

FFmpeg `9.0.2` appears to fix this regression: its changelog includes the revert of
`fftools/ffmpeg_sched: allow throttling decoder outputs`, which caused premature stream termination.
This release has not yet been validated with ESUP-Runner. Stay on stable FFmpeg 8 for now; moving
to FFmpeg 9 still requires the GPU checks and a long encoding test described below.

No CUDA, NVIDIA driver or DKMS downgrade is needed when replacing FFmpeg 9.0.1 with FFmpeg 8.1.3.

Upstream tracking:

- https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/23988
- https://code.ffmpeg.org/FFmpeg/FFmpeg/issues/24008
- https://code.ffmpeg.org/FFmpeg/FFmpeg/pulls/24118
- https://mail-archive.com/ffmpeg-devel@ffmpeg.org/msg190282.html (revert backported to the 9.0 branch)
- https://raw.githubusercontent.com/FFmpeg/FFmpeg/n9.0.2/Changelog
- https://ffmpeg.org/download.html (latest stable releases)
- https://github.com/FFmpeg/FFmpeg/releases/tag/n8.1.3

## Prerequisites

Complete [DEBIAN13_CUDA13_3.md](DEBIAN13_CUDA13_3.md) first. Run the commands below as the
`esup-runner` service account.

Check the installed driver and CUDA toolkit:

```bash
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
/usr/local/cuda-13.3/bin/nvcc --version
```

The NVIDIA driver must be version `610.43.02` or newer.

## 1) Install build dependencies

```bash
sudo apt update
sudo apt install -y \
  git build-essential pkg-config nasm clang \
  libmp3lame-dev libopus-dev libx264-dev libvpx-dev

mkdir -p ~/ffmpeg_prerequisites
```

## 2) Install the NVIDIA codec headers

```bash
cd ~/ffmpeg_prerequisites
git clone --branch n13.1.15.0 --depth 1 \
  https://code.ffmpeg.org/FFmpeg/nv-codec-headers.git \
  nv-codec-headers-n13.1.15.0

cd nv-codec-headers-n13.1.15.0
test "$(git describe --tags --exact-match)" = "n13.1.15.0"
sudo make install
sudo ldconfig

test "$(pkg-config --modversion ffnvcodec)" = "13.1.15.0"
```

If the source directory already exists, reuse it after checking that it is clean and points to the
expected tag instead of cloning it again.

## 3) Build FFmpeg 8.1.3

```bash
cd ~/ffmpeg_prerequisites
git clone --branch n8.1.3 --depth 1 \
  https://code.ffmpeg.org/FFmpeg/FFmpeg.git \
  ffmpeg-n8.1.3

cd ffmpeg-n8.1.3
test "$(git rev-parse HEAD)" = "1041abdc962f4cc4f394aa8de9dc5236c0c3b9e7"

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
```

Do not add `--enable-cuda-nvcc` or `--enable-libnpp` to this profile.

## 4) Install FFmpeg

Wait until the server has no active encoding task. Stop the runner before replacing the binaries:

```bash
systemctl --user stop esup-runner-runner

cd ~/ffmpeg_prerequisites/ffmpeg-n8.1.3
sudo make install
sudo ldconfig
hash -r
```

FFmpeg and `ffprobe` must both come from `/usr/local/bin` and use the same version.

## 5) Verify before restarting the runner

```bash
command -v ffmpeg ffprobe
ffmpeg -hide_banner -version | sed -n '1p'
ffprobe -hide_banner -version | sed -n '1p'
pkg-config --modversion ffnvcodec

ffmpeg -hide_banner -encoders | grep -E "h264_nvenc|png"
ffmpeg -hide_banner -decoders | grep "h264_cuvid"
ffmpeg -hide_banner -filters | grep -E "scale_cuda|overlay_cuda|hwupload_cuda"
ffmpeg -hide_banner -buildconf | grep -E -- "--enable-cuda-llvm|--enable-libvpx|--nvcc=clang"

cd /opt/esup-runner/runner
UV_CACHE_DIR=/tmp/esup-runner-uv-cache uv run scripts/check_ffmpeg.py --mode gpu
```

The version lines must start with `ffmpeg version n8.1.3` and `ffprobe version n8.1.3`. The runner
check performs real NVENC and `scale_cuda` smoke tests.

If every check succeeds, restart the runner:

```bash
systemctl --user start esup-runner-runner
systemctl --user is-active --quiet esup-runner-runner
journalctl --user -u esup-runner-runner -n 200 --no-pager
```

Before updating a second production server, encode a representative long source on the first one
and confirm that all audio and video stream durations reach the expected end. The runner performs
this output validation with `ffprobe` after encoding.

## Moving back to FFmpeg 9 later

Although FFmpeg `9.0.2` appears to fix the scheduler regression, ESUP-Runner stays on stable
FFmpeg 8 until a FFmpeg 9 release has passed validation. Validate the exact candidate release
on Debian 13/CUDA 13.3 with:

1. `scripts/check_ffmpeg.py --mode gpu`;
2. the scheduler regression test associated with the upstream correction;
3. one of the long sources previously truncated by FFmpeg 9.0.1;
4. `ffprobe` checks of every audio and video output stream.

Record the exact release and commit in this document before production deployment. Do not deploy an
unpinned development branch or a locally backported FFmpeg 9.0.1 build.
