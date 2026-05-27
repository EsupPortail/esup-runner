"""FFmpeg probing and runtime capability helpers for studio pipeline selection."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

_WEBM_VIDEO_CODECS = {"vp8", "vp9", "av1"}


def has_encoder(encoder: str, *, subprocess_module=subprocess) -> bool:
    """Return True if ffmpeg exposes the given encoder."""
    try:
        result = subprocess_module.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.DEVNULL,
            text=True,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == encoder:
                return True
    except Exception:
        return False
    return False


def has_decoder(decoder: str, *, subprocess_module=subprocess) -> bool:
    """Return True if ffmpeg exposes the given decoder."""
    try:
        result = subprocess_module.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.DEVNULL,
            text=True,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == decoder:
                return True
    except Exception:
        return False
    return False


def probe_codec(source: str, *, subprocess_module=subprocess) -> str:
    """Probe the primary video codec name for a source file."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        source,
    ]
    result = subprocess_module.run(
        cmd, stdout=subprocess_module.PIPE, stderr=subprocess_module.STDOUT
    )
    try:
        info = json.loads(result.stdout.decode("utf-8"))
        if info.get("streams"):
            return str(info["streams"][0].get("codec_name") or "")
    except Exception:
        pass
    return ""


def choose_cuda_decoder_for(
    source: str,
    *,
    probe_codec_fn: Callable[[str], str] = probe_codec,
    has_decoder_fn: Callable[[str], bool] = has_decoder,
) -> str | None:
    """Return the best *_cuvid decoder for the given source, or None."""
    codec = (probe_codec_fn(source) or "").lower()
    # CUVID decode for VP8/VP9/AV1 is less stable across builds: keep CPU decode.
    if codec in _WEBM_VIDEO_CODECS:
        return None
    mapping = {
        "h264": "h264_cuvid",
        "hevc": "hevc_cuvid",
        "mpeg2video": "mpeg2_cuvid",
    }
    decoder_name = mapping.get(codec)
    if decoder_name and has_decoder_fn(decoder_name):
        return decoder_name
    return None


def choose_h264_encoder(*, has_encoder_fn: Callable[[str], bool] = has_encoder) -> tuple[str, str]:
    """Prefer libx264, fallback to builtin h264 with a warning message."""
    if has_encoder_fn("libx264"):
        return "libx264", ""
    return "h264", "libx264 missing; using h264 fallback\n"


def nvenc_preflight(*, subprocess_module=subprocess) -> tuple[bool, str]:
    """Return (ok, details) for NVENC availability on this host."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=640x360:r=30",
        "-t",
        "0.1",
        "-an",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        out = subprocess_module.run(
            cmd, stdout=subprocess_module.PIPE, stderr=subprocess_module.STDOUT, text=True
        )
        if out.returncode == 0:
            return True, ""
        details = "NVENC preflight failed (ffmpeg exit %s)\n" % out.returncode
        if out.stdout:
            details += out.stdout
        return False, details
    except FileNotFoundError:
        return False, "ffmpeg command not found; cannot use NVENC\n"
    except Exception as exc:
        return False, f"NVENC preflight exception: {exc}\n"


def probe_height(source: str, *, subprocess_module=subprocess) -> int:
    """Probe the primary video height for a source file."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "json",
        source,
    ]
    result = subprocess_module.run(
        cmd, stdout=subprocess_module.PIPE, stderr=subprocess_module.STDOUT
    )
    try:
        info = json.loads(result.stdout.decode("utf-8"))
        if info.get("streams"):
            return int(info["streams"][0].get("height") or 0)
    except Exception:
        pass
    return 0


def filter_available(name: str, *, subprocess_module=subprocess) -> bool:
    """Return whether FFmpeg exposes a given filter."""
    try:
        result = subprocess_module.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.STDOUT,
        )
        text = result.stdout.decode(errors="ignore")
        return name in text
    except Exception:
        return False


def set_cuda_env(args: Any, *, os_module=os) -> None:
    """Populate CUDA-related environment variables from CLI arguments."""
    if args.cuda_visible_devices:
        os_module.environ["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices
    if args.cuda_device_order:
        os_module.environ["CUDA_DEVICE_ORDER"] = args.cuda_device_order
    if args.cuda_path:
        os_module.environ["CUDA_PATH"] = args.cuda_path
