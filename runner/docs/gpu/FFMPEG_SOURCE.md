# FFmpeg GPU build from source

## Scope

Choose and validate the FFmpeg source-build procedure matching the host Debian and CUDA stack.

There is no single globally validated source-build procedure. Compiler selection, CUDA paths,
NVIDIA codec headers and minimum driver requirements vary between stacks.

## Compatibility matrix

| Host stack | Procedure | Status |
|---|---|---|
| Debian 11 + CUDA 12.4 | [FFMPEG_SOURCE_DEBIAN11_CUDA12_4.md](FFMPEG_SOURCE_DEBIAN11_CUDA12_4.md) | Historically validated; source revisions were not recorded |
| Debian 12 + CUDA 13.2 | [FFMPEG_SOURCE_DEBIAN12_CUDA13_2.md](FFMPEG_SOURCE_DEBIAN12_CUDA13_2.md) | Historically validated; source revisions were not recorded |
| Debian 13 + CUDA 13.3 | [FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md](FFMPEG_SOURCE_DEBIAN13_CUDA13_3.md) | Validated with FFmpeg `n9.0.1` and `nv-codec-headers` `n13.1.15.0` |

## Common requirements

- Complete the matching NVIDIA driver and CUDA installation page first.
- Run the build from the service account workspace.
- Keep manually installed binaries under `/usr/local/bin`. Do not move or overwrite Debian-managed
  files under `/usr/bin`.
- Record the exact FFmpeg and `nv-codec-headers` revisions used for every validated production build.
- Revalidate the complete GPU pipeline after changing FFmpeg, `nv-codec-headers`, CUDA or the NVIDIA
  driver.

The build enables GPL and non-free components. The resulting binary must not be redistributed without
a separate license review.

## Required FFmpeg capabilities

Runner GPU mode expects:

- encoders: `h264_nvenc` and `png`;
- decoder: `h264_cuvid`;
- filters: `scale_cuda` and `hwupload_cuda`;
- optional Studio GPU overlay filter: `overlay_cuda`;
- CPU fallback encoder: `libx264` or `h264`;
- WebM support built with `libvpx`.

## Common verification

Run these commands as the `esup-runner` service account:

```bash
command -v ffmpeg ffprobe
ffmpeg -version
ffmpeg -hide_banner -encoders | grep -E "h264_nvenc|png"
ffmpeg -hide_banner -decoders | grep -E "h264_cuvid"
ffmpeg -hide_banner -filters  | grep -E "scale_cuda|overlay_cuda|hwupload_cuda"
ffmpeg -hide_banner -buildconf | grep -E -- "--enable-libvpx"

cd /opt/esup-runner/runner
uv run scripts/check_ffmpeg.py --mode gpu
```

The runner check is authoritative: it performs NVENC and `scale_cuda` smoke tests instead of only
checking that component names are listed.
