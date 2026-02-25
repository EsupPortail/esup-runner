#!/usr/bin/env python3
"""
Video encoding script using FFmpeg.
Handles H.264 encoding with configurable quality and presets.
Supports both CPU and GPU encoding.
"""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import hashlib
import json
import os
import shlex
import subprocess
import time
import unicodedata
import urllib.parse
import urllib.request
from functools import lru_cache
from json.decoder import JSONDecodeError
from timeit import default_timer as timer
from typing import Any, Dict, Optional

# =============================================================================
# INITIAL CONFIGURATION
# =============================================================================
_DEBUG = True
_VIDEOS_DIR = "/tmp/esup-runner/task01"
_VIDEOS_OUTPUT_DIR = "/tmp/esup-runner/task01/output"
_ENCODING_TYPE = "CPU"
_HWACCEL_DEVICE = 0

# Video renditions configuration
_RENDITION = {"360": "640x360", "720": "1280x720", "1080": "1920x1080"}

# Video renditions encoding configuration (which formats to encode)
_RENDITION_CONFIG = {
    "360": {"resolution": "640x360", "encode_mp4": True},
    "720": {"resolution": "1280x720", "encode_mp4": True},
    "1080": {"resolution": "1920x1080", "encode_mp4": False},
}

# Supported image codecs for thumbnail detection
_IMAGE_CODEC = ["jpeg", "gif", "png", "bmp", "jpg"]

# Supported video codecs for hardware acceleration
_LIST_CODEC = ("h264", "hevc", "mjpeg", "mpeg1", "mpeg2", "mpeg4", "vc1", "vp8", "vp9")

# Overview configuration (sprite sheet for video navigation)
_OVERVIEW_CONFIG = {
    "enabled": True,
    "thumbnail_width": 160,
    "thumbnail_height": 90,
    "interval": 1,  # Generate one thumbnail per second
    # Keep sprite dimensions conservative for broad FFmpeg/PNG compatibility.
    "max_sprite_width": 16384,
    "max_sprite_height": 16384,
}

# =============================================================================
# FFMPEG COMMAND TEMPLATES
# =============================================================================

# Audio encoding templates
MP3 = (
    "time ffmpeg -i {input} -hide_banner -y -c:a libmp3lame -q:a 2 "
    '-ar 44100 -vn -threads 0 "{output_dir}/audio_192k_{output}.mp3"'
)

M4A = (
    "time ffmpeg -i {input} -hide_banner -y -c:a aac -ar 44100 "
    '-q:a 2 -vn -threads 0 "{output_dir}/audio_192k_{output}.m4a"'
)

# Thumbnail extraction templates (3 thumbnails at 25%, 50%, 75% of video duration)
EXTRACT_THUMBNAIL_0 = "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y -vframes 1 {output_dir}/{filename}_0.{ext}"
EXTRACT_THUMBNAIL_1 = "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y -vframes 1 {output_dir}/{filename}_1.{ext}"
EXTRACT_THUMBNAIL_2 = "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y -vframes 1 {output_dir}/{filename}_2.{ext}"

# CPU encoding base command
CPU = "time ffmpeg -hide_banner -y -i {input} "

# GPU encoding base command (using CUDA)
GPU = (
    "time ffmpeg -y -hwaccel_device {hwaccel_device} "
    "-hwaccel cuda -hwaccel_output_format cuda -c:v {codec}_cuvid -i {input} "
)

# Common encoding parameters
# NOTE: For GPU pipelines (CUDA frames + scale_npp + NVENC), forcing a *software* pix_fmt
# (like yuv420p) can make FFmpeg try to insert swscale (auto_scale), which cannot consume
# CUDA hardware frames. Keep pix_fmt enforcement only for CPU pipelines.
COMMON_CPU = (
    " -c:a aac -ar 48000 -strict experimental -profile:v high "
    '-pix_fmt yuv420p -force_key_frames "expr:gte(t,n_forced*2)" '
    "-fps_mode passthrough -preset slow -qmin 20 -qmax 50 "
)

COMMON_GPU = (
    " -c:a aac -ar 48000 -strict experimental -profile:v high "
    '-force_key_frames "expr:gte(t,n_forced*2)" '
    "-fps_mode passthrough -preset p4 -qmin 20 -qmax 50 "
)

# GPU scaling filter
scale_gpu = (
    COMMON_GPU
    + '-vf "scale_npp=-2:{height}" -c:v h264_nvenc -sc_threshold 0 -bf 0 -rc-lookahead 0 '
)

# CPU scaling filter (libx264 preferred; fallback decided at runtime)
scale_cpu = COMMON_CPU + '-vf "scale=-2:{height}" -c:v {encoder} -sc_threshold 0 '

# Bitrate configurations for different resolutions
rate_360 = "-b:a 96k -minrate 500k -b:v 750k -maxrate 1000k -bufsize 1500k "
rate_720 = "-b:a 128k -minrate 1000k -b:v 2000k -maxrate 3000k -bufsize 4000k "
rate_1080 = "-b:a 192k -minrate 2M -b:v 3M -maxrate 4500k -bufsize 6M "

# Output format templates
end_360_m3u8 = (
    rate_360
    + "-max_muxing_queue_size 9999 -hls_playlist_type vod -hls_list_size 0 "
    + "-hls_time 2 -hls_flags single_file+independent_segments "
    + "{output_dir}/360p_{output}.m3u8 "
)

end_360_mp4 = (
    rate_360 + "-max_muxing_queue_size 9999 -movflags faststart "
    '-write_tmcd 0 "{output_dir}/360p_{output}.mp4" '
)

end_720_m3u8 = (
    rate_720
    + "-max_muxing_queue_size 9999 -hls_playlist_type vod -hls_list_size 0 "
    + "-hls_time 2 -hls_flags single_file+independent_segments "
    + "{output_dir}/720p_{output}.m3u8 "
)

end_720_mp4 = (
    rate_720 + "-max_muxing_queue_size 9999 -movflags faststart "
    '-write_tmcd 0 "{output_dir}/720p_{output}.mp4" '
)

end_1080_m3u8 = (
    rate_720
    + "-max_muxing_queue_size 9999 -hls_playlist_type vod -hls_list_size 0 "
    + "-hls_time 2 -hls_flags single_file+independent_segments "
    + "{output_dir}/1080p_{output}.m3u8 "
)

end_1080_mp4 = (
    rate_1080 + "-max_muxing_queue_size 9999 -movflags faststart "
    '-write_tmcd 0 "{output_dir}/1080p_{output}.mp4" '
)

# Global variable for subtime (seek position)
SUBTIME = " "

# Global variable for effective duration (after cut)
EFFECTIVE_DURATION = 0

# Global variable for dressing configuration
_DRESSING_CONFIG: dict = {}

# Global variable for cut configuration
_CUT_CONFIG: dict = {}

# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


def timestamp_to_seconds(timestamp: str) -> int:
    """
    Convert timestamp in format HH:MM:SS to seconds.

    Args:
        timestamp: Time in format HH:MM:SS

    Returns:
        int: Time in seconds
    """
    try:
        parts = timestamp.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
        else:
            return int(parts[0])
    except (ValueError, AttributeError):
        return 0


# =============================================================================
# CORE ENCODING FUNCTIONS
# =============================================================================


def encode_with_gpu(format: str, codec: str, height: int, file: str) -> bool:
    """
    Attempt GPU encoding first, fall back to CPU if GPU fails.

    Args:
        format: Output format (m3u8 or mp4)
        codec: Input video codec
        height: Video height in pixels
        file: Input filename

    Returns:
        bool: True if encoding succeeded, False otherwise
    """
    msg = "--> encode_with_gpu \n"
    return_value = False

    if encode("gpu", format, codec, height, file):
        msg += "Encode GPU %s ok \n" % format
        return_value = True
    else:
        # Fallback to CPU encoding if GPU fails
        if encode("cpu", format, codec, height, file):
            msg += "Encode CPU %s ok \n" % format
            return_value = True

    if not return_value:
        msg += 20 * "////" + "\n"
        msg += "ERROR ENCODING %s FOR FILE %s \n" % (format, file)

    encode_log(msg)
    return return_value


def encode_without_gpu(format: str, codec: str, height: int, file: str) -> bool:
    """
    Encode using CPU only.

    Args:
        format: Output format (m3u8 or mp4)
        codec: Input video codec
        height: Video height in pixels
        file: Input filename

    Returns:
        bool: True if encoding succeeded, False otherwise
    """
    msg = "--> encode_without_gpu \n"
    return_value = False

    if encode("cpu", format, codec, height, file):
        msg += "Encode CPU %s ok \n" % format
        return_value = True
    else:
        msg += 20 * "////" + "\n"
        msg += "ERROR ENCODING %s FOR FILE %s \n" % (format, file)

    encode_log(msg)
    return return_value


def get_cmd_gpu(format: str, codec: str, height: int, file: str) -> str:
    """
    Generate GPU encoding command for FFmpeg.

    Args:
        format: Output format (m3u8 or mp4)
        codec: Input video codec
        height: Video height in pixels
        file: Input filename

    Returns:
        str: Complete FFmpeg command string
    """
    # Start with GPU base command
    ffmpeg_cmd = GPU.format(
        hwaccel_device=_HWACCEL_DEVICE, codec=codec, input=os.path.join(_VIDEOS_DIR, file)
    )

    # Remove extension and sanitize
    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename(filename)

    # Add 360p rendition
    ffmpeg_cmd += scale_gpu.format(height=360)
    if format == "m3u8":
        ffmpeg_cmd += SUBTIME + end_360_m3u8.format(output_dir=_VIDEOS_OUTPUT_DIR, output=filename)
    else:
        # Only encode MP4 for 360p if configured to do so
        if _RENDITION_CONFIG.get("360", {}).get("encode_mp4", True):
            ffmpeg_cmd += SUBTIME + end_360_mp4.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )

    # Add 720p rendition if source height supports it
    if height >= 720:
        ffmpeg_cmd += scale_gpu.format(height=720)
        if format == "m3u8":
            ffmpeg_cmd += SUBTIME + end_720_m3u8.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )
        else:
            # Only encode MP4 for 720p if configured to do so
            if _RENDITION_CONFIG.get("720", {}).get("encode_mp4", True):
                ffmpeg_cmd += SUBTIME + end_720_mp4.format(
                    output_dir=_VIDEOS_OUTPUT_DIR, output=filename
                )

    # Add 1080p rendition if source height supports it
    if height >= 1080:
        ffmpeg_cmd += scale_gpu.format(height=1080)
        if format == "m3u8":
            ffmpeg_cmd += SUBTIME + end_1080_m3u8.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )
        elif format == "mp4" and _RENDITION_CONFIG.get("1080", {}).get("encode_mp4", False):
            # Only encode MP4 for 1080p if configured to do so
            ffmpeg_cmd += SUBTIME + end_1080_mp4.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )

    return ffmpeg_cmd


def get_cmd_cpu(format: str, codec: str, height: int, file: str) -> str:
    """
    Generate CPU encoding command for FFmpeg.

    Args:
        format: Output format (m3u8 or mp4)
        codec: Input video codec
        height: Video height in pixels
        file: Input filename

    Returns:
        str: Complete FFmpeg command string
    """
    # Start with CPU base command
    encoder, _ = _choose_h264_encoder()
    ffmpeg_cmd = CPU.format(codec=codec, input=os.path.join(_VIDEOS_DIR, file))

    # Remove extension and sanitize
    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename(filename)

    # Add 360p rendition
    ffmpeg_cmd += scale_cpu.format(height=360, encoder=encoder)
    if format == "m3u8":
        ffmpeg_cmd += SUBTIME + end_360_m3u8.format(output_dir=_VIDEOS_OUTPUT_DIR, output=filename)
    else:
        # Only encode MP4 for 360p if configured to do so
        if _RENDITION_CONFIG.get("360", {}).get("encode_mp4", True):
            ffmpeg_cmd += SUBTIME + end_360_mp4.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )

    # Add 720p rendition if source height supports it
    if height >= 720:
        ffmpeg_cmd += scale_cpu.format(height=720, encoder=encoder)
        if format == "m3u8":
            ffmpeg_cmd += SUBTIME + end_720_m3u8.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )
        else:
            # Only encode MP4 for 720p if configured to do so
            if _RENDITION_CONFIG.get("720", {}).get("encode_mp4", True):
                ffmpeg_cmd += SUBTIME + end_720_mp4.format(
                    output_dir=_VIDEOS_OUTPUT_DIR, output=filename
                )

    # Add 1080p rendition if source height supports it
    if height >= 1080:
        ffmpeg_cmd += scale_cpu.format(height=1080, encoder=encoder)
        if format == "m3u8":
            ffmpeg_cmd += SUBTIME + end_1080_m3u8.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )
        elif format == "mp4" and _RENDITION_CONFIG.get("1080", {}).get("encode_mp4", False):
            # Only encode MP4 for 1080p if configured to do so
            ffmpeg_cmd += SUBTIME + end_1080_mp4.format(
                output_dir=_VIDEOS_OUTPUT_DIR, output=filename
            )

    return ffmpeg_cmd


def sanitize_filename(filename: str) -> str:
    """
    Remove accents and special characters from filename.

    Args:
        filename: Original filename

    Returns:
        str: Sanitized filename
    """
    filenamesan = "".join(
        (c for c in unicodedata.normalize("NFD", filename) if unicodedata.category(c) != "Mn")
    )
    filenamesan = filenamesan.replace(" ", "_")
    return filenamesan


@lru_cache(maxsize=4)
def _has_encoder(encoder: str) -> bool:
    """Return True if ffmpeg reports the encoder."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
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


def _choose_h264_encoder() -> tuple[str, str]:
    """Choose libx264 if available; otherwise force builtin h264."""
    if _has_encoder("libx264"):
        return "libx264", ""
    return "h264", "libx264 missing; forcing h264\n"


@lru_cache(maxsize=1)
def _nvenc_preflight() -> tuple[bool, str]:
    """Return (ok, details) for NVENC availability on this host.

    This catches common production failures like driver/API mismatches.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        # Use a reasonably sized frame to avoid NVENC minimum-dimension constraints.
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
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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


def _convert_file(src: str, dst: str) -> tuple[bool, str]:
    """Convert an image using ImageMagick convert if available."""
    try:
        output = subprocess.run(
            ["convert", src, dst], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        if output.returncode != 0:
            msg = f"convert failed ({output.returncode}) for {src}\n"
            if output.stdout:
                msg += output.stdout
            return False, msg
        return True, ""
    except FileNotFoundError:
        return False, "convert command not found; install ImageMagick to enable PNG fallback\n"
    except Exception as exc:
        return False, f"convert exception: {exc}\n"


def launch_cmd(ffmpeg_cmd: str, type: str, format: str) -> tuple[bool, str]:
    """
    Execute FFmpeg command and handle errors.

    Args:
        ffmpeg_cmd: Complete FFmpeg command to execute
        type: Encoding type (cpu, gpu, mp3, etc.)
        format: Output format

    Returns:
        tuple: (success: bool, log_message: str)
    """
    msg = ""
    encode_start = timer()
    return_value = False

    try:
        # Execute FFmpeg command
        output = subprocess.run(
            shlex.split(ffmpeg_cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False
        )

        encode_end = timer() - encode_start
        msg += ffmpeg_cmd + "\n"
        msg += "Encode file in {:.3}s.\n".format(encode_end)

        # Try to decode output, handle encoding errors
        try:
            msg += output.stdout.decode("utf-8")
        except UnicodeDecodeError:
            pass

        msg += "\n"

        if output.returncode != 0:
            msg += "ERROR RETURN CODE for type=%s and format=%s : %s" % (
                type,
                format,
                output.returncode,
            )
        else:
            return_value = True

    except subprocess.CalledProcessError as e:
        msg += 20 * "////" + "\n"
        msg += "Runtime Error: {0}\n".format(e)
    except OSError as err:
        msg += 20 * "////" + "\n"
        msg += "OS error: {0}\n".format(err)
    except Exception as exc:
        msg += 20 * "////" + "\n"
        msg += "Unexpected error: {0}\n".format(exc)

    return return_value, msg


def _safe_filename_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path) or "asset"
    name = sanitize_filename(name)
    if not name:
        name = "asset"
    return name


def _download_allowed_hosts_from_env() -> list[str]:
    allowed_hosts_raw = os.getenv("DOWNLOAD_ALLOWED_HOSTS", "")
    return [h.strip().lower().rstrip(".") for h in allowed_hosts_raw.split(",") if h.strip()]


def _download_allow_private_networks_from_env() -> bool:
    allow_private_raw = os.getenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    return allow_private_raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _validate_host_resolves_to_public_ip(host: str) -> None:
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos if info and info[4]})
    except Exception:
        ips = []
    if not ips:
        raise ValueError("Download URL host cannot be resolved")

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            raise ValueError("Download URL resolved to an invalid address")
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise ValueError("Download URL resolves to a private/loopback/link-local address")


def _download_url_to_dir(url: str, target_dir: str, prefix: str) -> str:
    """Download a remote asset to target_dir and return local absolute path.

    Keeps filenames stable by hashing the URL.
    """
    os.makedirs(target_dir, exist_ok=True)

    parsed_url = urllib.parse.urlparse(url)
    scheme = (parsed_url.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported for downloads")

    host = (parsed_url.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("Invalid download URL host")

    allowed_hosts = _download_allowed_hosts_from_env()
    if allowed_hosts and not _host_is_allowed(host, allowed_hosts):
        raise ValueError("Download URL host not allowed")

    allow_private = _download_allow_private_networks_from_env()
    if not allow_private and host in {"localhost"}:
        raise ValueError("Download URL host not allowed")

    if not allow_private:
        _validate_host_resolves_to_public_ip(host)

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    base = _safe_filename_from_url(url)
    local_name = f"{prefix}_{url_hash}_{base}"
    local_path = os.path.join(target_dir, local_name)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path

    req = urllib.request.Request(url, headers={"User-Agent": "esup-runner-ffmpeg/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    with open(local_path, "wb") as f:
        f.write(data)

    return local_path


def _probe_duration_seconds(path: str) -> float:
    """Return media duration in seconds (float)."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=nw=1:nk=1",
        path,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8").strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _probe_has_audio(path: str) -> bool:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode("utf-8").strip()
        return bool(out)
    except Exception:
        return False


def _watermark_overlay_xy(position_orig: str, margin: int = 54) -> tuple[str, str]:
    """Return overlay x,y expressions for FFmpeg overlay filter."""
    pos = (position_orig or "").lower().strip()
    if pos in ("top_left", "en haut à gauche", "en haut a gauche"):
        return f"{margin}", f"{margin}"
    if pos in ("top_right", "en haut à droite", "en haut a droite"):
        return f"main_w-overlay_w-{margin}", f"{margin}"
    if pos in ("bottom_left", "en bas à gauche", "en bas a gauche"):
        return f"{margin}", f"main_h-overlay_h-{margin}"
    if pos in ("bottom_right", "en bas à droite", "en bas a droite"):
        return f"main_w-overlay_w-{margin}", f"main_h-overlay_h-{margin}"
    # Default: top right
    return f"main_w-overlay_w-{margin}", f"{margin}"


def _build_normalize_1080p_filter(label_in: str, label_out: str) -> str:
    """Normalize to 16:9 padded 1080p like legacy Pod behavior."""
    # Keep the exact expression pattern used in the user's reference command.
    return (
        f"[{label_in}]"
        "scale=w='if(gt(a,16/9),16/9*1080,-2)':h='if(gt(a,16/9),-2,1080)',"
        "pad=ceil(16/9*1080):1080:(ow-iw)/2:(oh-ih)/2"
        f"[{label_out}]"
    )


def _run_ffmpeg_cmd(ffmpeg_cmd: str, log_type: str) -> bool:
    ok, out = launch_cmd(ffmpeg_cmd, log_type, "")
    encode_log(out)
    return ok


def _create_cut_intermediate(input_path: str, output_path: str, start: str, end: str) -> bool:
    encoder, _ = _choose_h264_encoder()
    ffmpeg_cmd = (
        "ffmpeg -hide_banner -threads 0 -y "
        f"-ss {shlex.quote(start)} -to {shlex.quote(end)} "
        f"-i {shlex.quote(input_path)} "
        "-map '0:v:0?' -map '0:a?' "
        f"-c:v {encoder} -c:a aac -ar 48000 -ac 2 -fps_mode passthrough "
        f"{shlex.quote(output_path)}"
    )
    return _run_ffmpeg_cmd(ffmpeg_cmd, "cut_intermediate")


def _create_watermarked_intermediate(
    input_path: str,
    watermark_path: str,
    output_path: str,
    position_orig: str,
    opacity_percent: str,
) -> bool:
    encoder, _ = _choose_h264_encoder()
    try:
        opacity = float(opacity_percent) / 100.0 if opacity_percent not in (None, "") else 1.0
    except Exception:
        opacity = 1.0
    opacity = max(0.0, min(1.0, opacity))

    x, y = _watermark_overlay_xy(position_orig)

    # Scale watermark relative to video height (10% height), keep aspect.
    # Normalize video to 1080p/16:9 to avoid odd overlays and keep behavior stable.
    filter_complex = (
        _build_normalize_1080p_filter("0:v", "vid")
        + ";"
        + f"[1:v]format=rgba,colorchannelmixer=aa={opacity:.3f}[logo];"
        + "[logo][vid]scale2ref=oh*mdar:ih*0.1[logo2][video2];"
        + f"[video2][logo2]overlay={x}:{y}[v]"
    )

    ffmpeg_cmd = (
        "ffmpeg -hide_banner -threads 0 -y "
        f"-i {shlex.quote(input_path)} "
        f"-i {shlex.quote(watermark_path)} "
        f"-filter_complex {shlex.quote(filter_complex)} "
        "-map '[v]' -map '0:a?' "
        f"-c:v {encoder} -c:a aac -ar 48000 -ac 2 -fps_mode passthrough "
        f"{shlex.quote(output_path)}"
    )

    return _run_ffmpeg_cmd(ffmpeg_cmd, "dressing_watermark")


def _parse_duration_seconds_fallback(value: Optional[str]) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return float(timestamp_to_seconds(s))


def _create_credits_concat_intermediate(
    main_path: str,
    opening_path: Optional[str],
    opening_duration_hint: Optional[str],
    ending_path: Optional[str],
    ending_duration_hint: Optional[str],
    output_path: str,
) -> bool:
    encoder, _ = _choose_h264_encoder()
    inputs: list[str] = []
    segments: list[dict] = []

    def add_segment(path: str, duration_hint: Optional[str]):
        duration = _probe_duration_seconds(path)
        if duration <= 0:
            duration = _parse_duration_seconds_fallback(duration_hint)
        segments.append(
            {
                "path": path,
                "duration": max(0.0, duration),
                "has_audio": _probe_has_audio(path),
            }
        )

    if opening_path:
        add_segment(opening_path, opening_duration_hint)
    add_segment(main_path, None)
    if ending_path:
        add_segment(ending_path, ending_duration_hint)

    # Build inputs list in same order
    for seg in segments:
        inputs.append(seg["path"])

    # Build filter_complex
    filter_parts: list[str] = []
    concat_inputs: list[str] = []

    for idx, seg in enumerate(segments):
        v_label = f"v{idx}"
        a_label = f"a{idx}"

        filter_parts.append(_build_normalize_1080p_filter(f"{idx}:v", v_label))

        if seg["has_audio"]:
            filter_parts.append(
                f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,aresample=async=1[{a_label}]"
            )
        else:
            # Generate silent audio of segment duration (best-effort)
            dur = seg["duration"]
            if dur <= 0:
                # Fallback to 0 => concat may still work for video-only flows
                dur = 0.0
            filter_parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.3f}[{a_label}]")

        concat_inputs.append(f"[{v_label}][{a_label}]")

    n = len(segments)
    filter_parts.append("".join(concat_inputs) + f"concat=n={n}:v=1:a=1:unsafe=1[v][a]")

    filter_complex = ";".join(filter_parts)

    ffmpeg_cmd = "ffmpeg -hide_banner -threads 0 -y "
    for p in inputs:
        ffmpeg_cmd += f"-i {shlex.quote(p)} "

    ffmpeg_cmd += (
        f"-filter_complex {shlex.quote(filter_complex)} "
        "-map '[v]' -map '[a]' "
        f"-c:v {encoder} -c:a aac -ar 48000 -ac 2 -fps_mode passthrough "
        f"{shlex.quote(output_path)}"
    )

    return _run_ffmpeg_cmd(ffmpeg_cmd, "dressing_credits")


def _apply_cut_for_dressing(
    current_main_path: str, base: str, has_opening: bool, has_ending: bool
) -> tuple[str, str]:
    msg = ""
    global SUBTIME, EFFECTIVE_DURATION
    cut_start = (_CUT_CONFIG or {}).get("start")
    cut_end = (_CUT_CONFIG or {}).get("end")
    if (has_opening or has_ending) and cut_start and cut_end:
        try:
            cut_output_name = f"{base}_dressing_cut.mp4"
            cut_output_path = os.path.join(_VIDEOS_DIR, cut_output_name)
            msg += f"Applying cut to main video only (before credits) -> {cut_output_name}\n"
            ok = _create_cut_intermediate(
                current_main_path, cut_output_path, str(cut_start), str(cut_end)
            )
            if ok:
                current_main_path = cut_output_path
                SUBTIME = " "
                EFFECTIVE_DURATION = 0
                msg += "Cut applied in dressing; disabling SUBTIME cut for final encode\n"
            else:
                msg += "Warning: cut intermediate failed, continuing without cut for dressing\n"
        except Exception as e:
            msg += f"Warning: cut intermediate failed ({e}), continuing without cut for dressing\n"
    return current_main_path, msg


def _apply_watermark_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict,
    assets_dir: str,
) -> tuple[str, str]:
    msg = ""
    watermark_url = dressing_config.get("watermark")
    if not watermark_url:
        return current_main_path, msg

    watermark_pos_orig = dressing_config.get("watermark_position_orig") or dressing_config.get(
        "watermark_position"
    )
    watermark_opacity = dressing_config.get("watermark_opacity")

    try:
        watermark_path = _download_url_to_dir(str(watermark_url), assets_dir, "watermark")
        wm_output_name = f"{base}_dressing_wm.mp4"
        wm_output_path = os.path.join(_VIDEOS_DIR, wm_output_name)

        msg += f"Applying watermark to main video -> {wm_output_name}\n"
        ok = _create_watermarked_intermediate(
            current_main_path,
            watermark_path,
            wm_output_path,
            str(watermark_pos_orig or "top_right"),
            str(watermark_opacity or "100"),
        )
        if ok:
            current_main_path = wm_output_path
        else:
            msg += "Warning: watermark dressing failed, continuing with original input\n"
    except Exception as e:
        msg += f"Warning: watermark dressing failed ({e}), continuing with original input\n"

    return current_main_path, msg


def _apply_credits_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict,
    assets_dir: str,
) -> tuple[str, str]:
    msg = ""
    opening_video_url = dressing_config.get("opening_credits_video")
    opening_duration_hint = dressing_config.get("opening_credits_video_duration")
    ending_video_url = dressing_config.get("ending_credits_video")
    ending_duration_hint = dressing_config.get("ending_credits_video_duration")

    has_opening = bool(opening_video_url)
    has_ending = bool(ending_video_url)
    if not (has_opening or has_ending):
        return current_main_path, msg

    try:
        opening_path = (
            _download_url_to_dir(str(opening_video_url), assets_dir, "opening")
            if has_opening
            else None
        )
        ending_path = (
            _download_url_to_dir(str(ending_video_url), assets_dir, "ending")
            if has_ending
            else None
        )

        credits_output_name = f"{base}_dressing.mp4"
        credits_output_path = os.path.join(_VIDEOS_DIR, credits_output_name)

        msg += "Applying credits concat "
        msg += (
            f"(opening={bool(opening_path)}, ending={bool(ending_path)}) -> {credits_output_name}\n"
        )

        ok = _create_credits_concat_intermediate(
            main_path=current_main_path,
            opening_path=opening_path,
            opening_duration_hint=str(opening_duration_hint) if opening_duration_hint else None,
            ending_path=ending_path,
            ending_duration_hint=str(ending_duration_hint) if ending_duration_hint else None,
            output_path=credits_output_path,
        )
        if ok:
            current_main_path = credits_output_path
        else:
            msg += "Warning: credits dressing failed, continuing with current main input\n"
    except Exception as e:
        msg += f"Warning: credits dressing failed ({e}), continuing with current main input\n"

    return current_main_path, msg


def apply_dressing_if_needed(input_filename: str, dressing_config: dict) -> tuple[str, str]:
    """Return (new_input_filename, log_message). May create intermediate media files."""
    msg = "--> apply_dressing_if_needed\n"

    if not dressing_config:
        return input_filename, msg

    watermark_url = dressing_config.get("watermark")
    opening_video_url = dressing_config.get("opening_credits_video")
    ending_video_url = dressing_config.get("ending_credits_video")

    has_watermark = bool(watermark_url)
    has_opening = bool(opening_video_url)
    has_ending = bool(ending_video_url)

    if not any([has_watermark, has_opening, has_ending]):
        msg += "No dressing operations detected\n"
        return input_filename, msg

    assets_dir = os.path.join(_VIDEOS_DIR, "dressing_assets")
    input_path = os.path.join(_VIDEOS_DIR, input_filename)

    base = sanitize_filename(os.path.splitext(os.path.basename(input_filename))[0])
    current_main_path = input_path

    current_main_path, cut_msg = _apply_cut_for_dressing(
        current_main_path, base, has_opening, has_ending
    )
    msg += cut_msg

    current_main_path, wm_msg = _apply_watermark_for_dressing(
        current_main_path, base, dressing_config, assets_dir
    )
    msg += wm_msg

    current_main_path, credits_msg = _apply_credits_for_dressing(
        current_main_path, base, dressing_config, assets_dir
    )
    msg += credits_msg

    new_filename = os.path.basename(current_main_path)
    if new_filename != input_filename:
        msg += f"Dressing input switched: {input_filename} -> {new_filename}\n"

    return new_filename, msg


def _build_encode_video_job(
    *,
    encoder_type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    filename: str,
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    if encoder_type == "gpu":
        ffmpeg_cmd = get_cmd_gpu(format, codec, height, file)
    else:
        ffmpeg_cmd = get_cmd_cpu(format, codec, height, file)

    add_info_video_title = "encode_video"
    add_info_video_content: Dict[str, object] = {
        "encoding_format": "video/mp2t" if format == "m3u8" else "video/mp4",
        "rendition": _RENDITION["360"],
        "filename": "360p_{output}.{ext}".format(output=filename, ext=format),
    }
    return ffmpeg_cmd, add_info_video_title, add_info_video_content, True, {}


def _build_encode_audio_job(
    *, kind: str, file: str, filename: str
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    if kind == "mp3":
        ffmpeg_cmd = MP3.format(
            input=os.path.join(_VIDEOS_DIR, file),
            output_dir=_VIDEOS_OUTPUT_DIR,
            output=filename,
        )
        add_info_video_content: Dict[str, object] = {
            "encoding_format": "audio/mp3",
            "filename": "audio_192k_{output}.mp3".format(output=filename),
        }
    else:
        ffmpeg_cmd = M4A.format(
            input=os.path.join(_VIDEOS_DIR, file),
            output_dir=_VIDEOS_OUTPUT_DIR,
            output=filename,
        )
        add_info_video_content = {
            "encoding_format": "video/mp4",
            "filename": "audio_192k_{output}.m4a".format(output=filename),
        }

    return ffmpeg_cmd, "encode_audio", add_info_video_content, True, {}


def _build_encode_thumbnail_job(
    *,
    file: str,
    filename: str,
    duration: int,
    thumbnail_index: int,
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    percentages = [0.25, 0.50, 0.75]
    timestamp = int(duration * percentages[thumbnail_index]) if duration > 0 else 0
    templates = [EXTRACT_THUMBNAIL_0, EXTRACT_THUMBNAIL_1, EXTRACT_THUMBNAIL_2]
    ffmpeg_cmd = templates[thumbnail_index].format(
        timestamp=timestamp,
        output_dir=_VIDEOS_OUTPUT_DIR,
        input=os.path.join(_VIDEOS_DIR, file),
        filename=filename,
        ext="png",
    )
    add_info_video_content: Dict[str, object] = {
        "filename": f"{filename}_{thumbnail_index}.png",
        "timestamp": timestamp,
        "percentage": int(percentages[thumbnail_index] * 100),
    }
    extra: Dict[str, Any] = {"templates": templates, "timestamp": timestamp}
    return ffmpeg_cmd, "encode_thumbnail", add_info_video_content, True, extra


def _run_and_collect_text(cmd: list[str]) -> tuple[int, str]:
    out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return int(out.returncode), out.stdout or ""


def _run_shell_bytes(cmd: str) -> tuple[int, bytes]:
    out = subprocess.run(
        shlex.split(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
    )
    return int(out.returncode), out.stdout or b""


def _try_sprite_imagemagick_append(
    *,
    temp_thumb_dir: str,
    num_thumbnails: int,
    sprite_path: str,
) -> tuple[bool, str]:
    local_msg = ""
    try:
        png_files = sorted(glob.glob(os.path.join(temp_thumb_dir, "thumb_*.png")))
        if len(png_files) != num_thumbnails or not png_files:
            return (
                False,
                "ImageMagick sprite fallback skipped (missing png thumbs or count mismatch)\n",
            )

        local_msg += "Fallback: ImageMagick +append (png thumbs -> overview.png)\n"
        im_cmd = ["convert", *png_files, "+append", sprite_path]
        im_out = subprocess.run(
            im_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if im_out.returncode == 0 and os.path.exists(sprite_path):
            local_msg += f"Sprite sheet created via ImageMagick: {sprite_path}\n"
            return True, local_msg

        local_msg += f"ImageMagick sprite fallback failed ({im_out.returncode})\n"
        if im_out.stdout:
            local_msg += im_out.stdout
        return False, local_msg
    except FileNotFoundError:
        return False, "convert command not found; cannot use ImageMagick sprite fallback\n"
    except Exception as e:
        return False, f"ImageMagick sprite fallback exception: {e}\n"


def _get_overview_max_single_row_thumbnails(thumb_width: int, thumb_height: int) -> int:
    """
    Return how many thumbnails can fit in a single-row sprite.

    Raises:
        ValueError: If configured dimensions are invalid or unsupported.
    """
    max_sprite_width = int(_OVERVIEW_CONFIG.get("max_sprite_width", 16384))
    max_sprite_height = int(_OVERVIEW_CONFIG.get("max_sprite_height", 16384))

    if thumb_width <= 0 or thumb_height <= 0:
        raise ValueError("Invalid overview thumbnail dimensions")

    if thumb_width > max_sprite_width or thumb_height > max_sprite_height:
        raise ValueError(
            f"Thumbnail size ({thumb_width}x{thumb_height}) exceeds max sprite "
            f"size ({max_sprite_width}x{max_sprite_height})"
        )

    max_columns = max_sprite_width // thumb_width
    if max_columns < 1:
        raise ValueError(
            f"Thumbnail width {thumb_width} is too large for max sprite width {max_sprite_width}"
        )

    return max_columns


def _compute_overview_single_row_plan(
    duration: int, requested_interval: int, thumb_width: int, thumb_height: int
) -> tuple[int, int, int, int]:
    """
    Compute sampling plan for a single-row sprite.

    Returns:
        tuple: (effective_interval, target_count, requested_count, max_single_row_count)
    """
    interval = max(1, int(requested_interval))
    requested_count = max(1, int(duration / interval))
    max_single_row_count = _get_overview_max_single_row_thumbnails(thumb_width, thumb_height)

    if requested_count <= max_single_row_count:
        return interval, requested_count, requested_count, max_single_row_count

    # Increase interval so count fits into one row.
    effective_interval = max(1, (duration + max_single_row_count - 1) // max_single_row_count)
    while (
        effective_interval > 1 and int(duration / (effective_interval - 1)) <= max_single_row_count
    ):
        effective_interval -= 1

    target_count = max(1, int(duration / effective_interval))
    target_count = min(target_count, max_single_row_count)
    return effective_interval, target_count, requested_count, max_single_row_count


def _format_overview_thumbnail_plan_msg(
    requested_count: int, num_thumbnails: int, max_single_row_count: int, interval: int
) -> str:
    if requested_count > num_thumbnails:
        return (
            f"Single-row overview requires fewer thumbnails: requested {requested_count}, "
            f"max {max_single_row_count}. Using interval={interval}s "
            f"({num_thumbnails} thumbnails).\n"
        )
    return f"Generating {num_thumbnails} overview thumbnails (1 per {interval}s)\n"


def _build_overview_generation_result_msg(
    temp_thumb_dir: str, expected_count: int
) -> tuple[bool, str, int]:
    generated_files = sorted(glob.glob(os.path.join(temp_thumb_dir, "thumb_*.png")))
    generated_count = len(generated_files)
    if generated_count == 0:
        return False, "Error: FFmpeg reported success but generated no thumbnails\n", 0

    if generated_count != expected_count:
        return (
            True,
            f"Generated {generated_count} thumbnails (requested {expected_count})\n",
            generated_count,
        )

    return True, f"Successfully generated {generated_count} thumbnails\n", generated_count


def encode(
    type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    duration: int = 0,
    thumbnail_index: int = 0,
) -> bool:
    """
    Main encoding function that routes to specific encoders.

    Args:
        type: Encoding type (cpu, gpu, mp3, m4a, thumbnail)
        format: Output format
        codec: Input video codec
        height: Video height in pixels
        file: Input filename
        duration: Video duration in seconds (for thumbnail generation)
        thumbnail_index: Index of thumbnail to generate (0, 1, or 2)

    Returns:
        bool: True if encoding succeeded, False otherwise
    """
    msg = "--> encode\n"
    # Remove extension and sanitize
    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename(filename)

    builders = {
        "gpu": lambda: _build_encode_video_job(
            encoder_type="gpu",
            format=format,
            codec=codec,
            height=height,
            file=file,
            filename=filename,
        ),
        "cpu": lambda: _build_encode_video_job(
            encoder_type="cpu",
            format=format,
            codec=codec,
            height=height,
            file=file,
            filename=filename,
        ),
        "mp3": lambda: _build_encode_audio_job(kind="mp3", file=file, filename=filename),
        "m4a": lambda: _build_encode_audio_job(kind="m4a", file=file, filename=filename),
        "thumbnail": lambda: _build_encode_thumbnail_job(
            file=file,
            filename=filename,
            duration=duration,
            thumbnail_index=thumbnail_index,
        ),
    }

    builder = builders.get(type)
    if builder is None:
        msg += "Unknown encoding type: %s\n" % type
        encode_log(msg)
        return False

    ffmpeg_cmd, add_info_video_title, add_info_video_content, add_info_video_append, extra = (
        builder()
    )

    # Execute the command (thumbnail uses PNG directly; no JPG fallback).
    if type == "thumbnail":
        return_value, return_msg = launch_cmd(ffmpeg_cmd, "thumbnail", format)
    else:
        return_value, return_msg = launch_cmd(ffmpeg_cmd, type, format)

    # Update metadata if encoding succeeded
    if return_value:
        add_info_video(add_info_video_title, add_info_video_content, add_info_video_append)
        add_more_info_video(add_info_video_title, height, filename, format)

    encode_log(msg + return_msg)
    return return_value


def add_more_info_video(add_info_video_title: str, height: int, filename: str, format: str):
    """
    Add additional rendition information to metadata.

    Args:
        add_info_video_title: Metadata key
        height: Video height in pixels
        filename: Sanitized filename
        format: Output format
    """
    if add_info_video_title == "encode_video" and height >= 720:
        # Add 720p info only if encoding MP4 is enabled for 720p or if format is m3u8
        if format == "m3u8" or _RENDITION_CONFIG.get("720", {}).get("encode_mp4", True):
            add_info_video_content = {
                "encoding_format": "video/mp2t" if format == "m3u8" else "video/mp4",
                "rendition": _RENDITION["720"],
                "filename": "720p_{output}.{ext}".format(output=filename, ext=format),
            }
            add_info_video(add_info_video_title, add_info_video_content, True)

        # Add 1080p info only if encoding MP4 is enabled for 1080p or if format is m3u8
        if height >= 1080:
            if format == "m3u8" or _RENDITION_CONFIG.get("1080", {}).get("encode_mp4", False):
                add_info_video_content = {
                    "encoding_format": "video/mp2t" if format == "m3u8" else "video/mp4",
                    "rendition": _RENDITION["1080"],
                    "filename": "1080p_{output}.{ext}".format(output=filename, ext=format),
                }
                add_info_video(add_info_video_title, add_info_video_content, True)


def generate_overview_thumbnails(
    file: str, duration: int, output_dir: str
) -> tuple[bool, str, int]:
    """
    Generate individual thumbnails for overview sprite sheet.

    Args:
        file: Input filename
        duration: Video duration in seconds
        output_dir: Output directory for thumbnails

    Returns:
        tuple: (success: bool, log_message: str, thumbnail_count: int)
    """
    msg = "--> generate_overview_thumbnails\n"

    if not _OVERVIEW_CONFIG.get("enabled", True):
        msg += "Overview generation disabled\n"
        return True, msg, 0

    requested_interval = int(_OVERVIEW_CONFIG.get("interval", 1))
    thumb_width = _OVERVIEW_CONFIG.get("thumbnail_width", 160)
    thumb_height = _OVERVIEW_CONFIG.get("thumbnail_height", 90)

    try:
        interval, num_thumbnails, requested_count, max_single_row_count = (
            _compute_overview_single_row_plan(
                duration=duration,
                requested_interval=requested_interval,
                thumb_width=thumb_width,
                thumb_height=thumb_height,
            )
        )
    except ValueError as e:
        msg += f"Error planning overview thumbnails: {e}\n"
        return False, msg, 0

    msg += _format_overview_thumbnail_plan_msg(
        requested_count, num_thumbnails, max_single_row_count, interval
    )

    # Create temporary directory for individual thumbnails
    temp_thumb_dir = os.path.join(output_dir, "overview_temp")
    os.makedirs(temp_thumb_dir, exist_ok=True)

    # Generate thumbnails using FFmpeg
    input_path = os.path.join(_VIDEOS_DIR, file)

    # Use FFmpeg to extract thumbnails at regular intervals (prefer PNG)
    vf = f"fps=1/{interval},scale={thumb_width}:{thumb_height}:flags=lanczos,setsar=1"

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        input_path,
        "-vf",
        vf,
        "-frames:v",
        str(num_thumbnails),
        os.path.join(temp_thumb_dir, "thumb_%04d.png"),
    ]

    msg += "cmd: " + " ".join(ffmpeg_cmd) + "\n"

    try:
        rc, out = _run_and_collect_text(ffmpeg_cmd)
        if rc == 0:
            ok_count, count_msg, generated_count = _build_overview_generation_result_msg(
                temp_thumb_dir, num_thumbnails
            )
            msg += count_msg
            return ok_count, msg, generated_count

        msg += f"Error generating overview thumbnails: {rc}\n"
        if out:
            msg += out

        return False, msg, 0

    except Exception as e:
        msg += f"Exception generating overview thumbnails: {e}\n"
        return False, msg, 0


def create_overview_sprite(output_dir: str, num_thumbnails: int) -> tuple[bool, str]:
    """
    Create sprite sheet from individual thumbnails using FFmpeg tile filter.

    Args:
        output_dir: Output directory
        num_thumbnails: Number of thumbnails to combine

    Returns:
        tuple: (success: bool, log_message: str)
    """
    msg = "--> create_overview_sprite\n"

    temp_thumb_dir = os.path.join(output_dir, "overview_temp")
    sprite_path = os.path.join(output_dir, "overview.png")

    thumb_width = int(_OVERVIEW_CONFIG.get("thumbnail_width", 160))
    thumb_height = int(_OVERVIEW_CONFIG.get("thumbnail_height", 90))
    try:
        max_single_row_count = _get_overview_max_single_row_thumbnails(thumb_width, thumb_height)
    except ValueError as e:
        msg += f"Error creating sprite sheet: {e}\n"
        return False, msg

    if num_thumbnails > max_single_row_count:
        msg += (
            f"Error creating sprite sheet: {num_thumbnails} thumbnails exceed single-row "
            f"capacity ({max_single_row_count})\n"
        )
        return False, msg

    msg += f"Creating sprite sheet: {num_thumbnails} thumbnails in 1 horizontal row\n"

    # Use FFmpeg with tile filter to create sprite sheet.
    # Force scale to match VTT xywh coordinates.
    ffmpeg_cmd = (
        f"ffmpeg -hide_banner -y "
        f"-pattern_type glob -framerate 1 -i '{temp_thumb_dir}/thumb_*.png' "
        f"-vf 'scale={thumb_width}:{thumb_height}:flags=lanczos,setsar=1,tile={num_thumbnails}x1:margin=0:padding=0' "
        f"-frames:v 1 "
        f"-c:v png "
        f"{sprite_path}"
    )

    try:
        rc0, out0 = _run_shell_bytes(ffmpeg_cmd)
        if rc0 != 0:
            msg += f"Error creating sprite sheet: {rc0}\n"
            try:
                msg += out0.decode("utf-8")
            except UnicodeDecodeError:
                pass

            ok_im, im_msg = _try_sprite_imagemagick_append(
                temp_thumb_dir=temp_thumb_dir,
                num_thumbnails=num_thumbnails,
                sprite_path=sprite_path,
            )
            msg += im_msg
            if ok_im:
                return True, msg

            return False, msg

        msg += f"Sprite sheet created: {sprite_path}\n"
        return True, msg

    except Exception as e:
        msg += f"Exception creating sprite sheet: {e}\n"
        return False, msg
    finally:
        # Clean up temporary thumbnails
        try:
            import shutil

            shutil.rmtree(temp_thumb_dir)
            msg += "Cleaned up temporary thumbnails\n"
        except Exception as e:
            msg += f"Warning: Could not clean up temp dir: {e}\n"


def generate_overview_vtt(output_dir: str, duration: int, num_thumbnails: int) -> tuple[bool, str]:
    """
    Generate WebVTT file for overview sprite sheet.

    Args:
        output_dir: Output directory
        duration: Video duration in seconds
        num_thumbnails: Number of thumbnails in sprite

    Returns:
        tuple: (success: bool, log_message: str)
    """
    msg = "--> generate_overview_vtt\n"

    if num_thumbnails <= 0:
        msg += "Error creating VTT file: no thumbnails available\n"
        return False, msg

    vtt_path = os.path.join(output_dir, "overview.vtt")
    thumb_width = _OVERVIEW_CONFIG.get("thumbnail_width", 160)
    thumb_height = _OVERVIEW_CONFIG.get("thumbnail_height", 90)

    try:
        with open(vtt_path, "w") as vtt_file:
            vtt_file.write("WEBVTT\n\n")

            for i in range(num_thumbnails):
                # Calculate time range
                start_time = int(i * duration / num_thumbnails)
                end_time = int(min(duration, (i + 1) * duration / num_thumbnails))
                if end_time <= start_time:
                    end_time = min(duration, start_time + 1)

                # Single-row sprite coordinates
                x = i * thumb_width
                y = 0

                # Format timestamps
                start_str = format_vtt_timestamp(start_time)
                end_str = format_vtt_timestamp(end_time)

                # Write VTT cue
                vtt_file.write(f"{start_str} --> {end_str}\n")
                vtt_file.write(f"overview.png#xywh={x},{y},{thumb_width},{thumb_height}\n\n")

        msg += f"VTT file created: {vtt_path}\n"
        return True, msg

    except Exception as e:
        msg += f"Error creating VTT file: {e}\n"
        return False, msg


def format_vtt_timestamp(seconds: int) -> str:
    """
    Format seconds as WebVTT timestamp (HH:MM:SS.mmm).

    Args:
        seconds: Time in seconds

    Returns:
        str: Formatted timestamp
    """
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.000"


def generate_overview(file: str, duration: int) -> tuple[bool, str]:
    """
    Generate complete overview (sprite sheet + VTT file) for video.

    Args:
        file: Input filename
        duration: Video duration in seconds

    Returns:
        tuple: (success: bool, log_message: str)
    """
    msg = "--> generate_overview\n"

    if not _OVERVIEW_CONFIG.get("enabled", True):
        msg += "Overview generation is disabled\n"
        return True, msg

    if duration < 1:
        msg += "Video too short for overview generation\n"
        return True, msg

    # Step 1: Generate individual thumbnails
    success, thumb_msg, num_thumbnails = generate_overview_thumbnails(
        file, duration, _VIDEOS_OUTPUT_DIR
    )
    msg += thumb_msg

    if not success or num_thumbnails == 0:
        msg += "Failed to generate overview thumbnails\n"
        return False, msg

    # Step 2: Create sprite sheet
    success, sprite_msg = create_overview_sprite(_VIDEOS_OUTPUT_DIR, num_thumbnails)
    msg += sprite_msg

    if not success:
        msg += "Failed to create sprite sheet\n"
        return False, msg

    # Step 3: Generate VTT file
    success, vtt_msg = generate_overview_vtt(_VIDEOS_OUTPUT_DIR, duration, num_thumbnails)
    msg += vtt_msg

    if not success:
        msg += "Failed to generate VTT file\n"
        return False, msg

    msg += "Overview generation complete\n"
    return True, msg


# =============================================================================
# VIDEO ANALYSIS FUNCTIONS
# =============================================================================


def get_info_from_video(probe_cmd: str) -> tuple[Optional[dict], str]:
    """
    Execute ffprobe command to get video metadata.

    Args:
        probe_cmd: ffprobe command string

    Returns:
        tuple: (video_info: dict or None, error_message: str)
    """
    info: Optional[dict] = None
    msg = ""

    try:
        output = subprocess.check_output(shlex.split(probe_cmd), stderr=subprocess.PIPE)
        info = json.loads(output)
    except subprocess.CalledProcessError as e:
        msg += 20 * "////" + "\n"
        msg += "Runtime Error: {0}\n".format(e)
    except OSError as err:
        msg += 20 * "////" + "\n"
        msg += "OS error: {0}\n".format(err)

    return info, msg


def get_info_video(file: str) -> dict:
    """
    Extract comprehensive information about video file.

    Args:
        file: Input filename

    Returns:
        dict: Video information including codec, dimensions, duration, etc.
    """
    if _DEBUG:
        print(os.environ["PATH"])

    msg = "--> get_info_video\n"
    probe_cmd = (
        "ffprobe -v quiet -show_format -show_streams " "-print_format json -i {}/{}"
    ).format(_VIDEOS_DIR, file)
    msg += probe_cmd + "\n"

    # Initialize default values
    has_stream_video = False
    has_stream_thumbnail = False
    has_stream_audio = False
    codec = ""
    height = 0
    duration = 0

    info, return_msg = get_info_from_video(probe_cmd)
    msg += json.dumps(info, indent=2) + "\n"
    msg += return_msg + "\n"

    if _DEBUG:
        print("Probe_cmd : " + probe_cmd)
        print("return_msg : " + return_msg)

    # Check if info is valid
    if info is None:
        msg += "\nError: Failed to get video information\n"
        return {}

    # Extract duration
    try:
        duration = int(float("%s" % info["format"]["duration"]))
    except (RuntimeError, KeyError, AttributeError, ValueError) as err:
        msg += "\nUnexpected error: {0}".format(err)

    # Analyze streams
    streams = info.get("streams", [])
    for stream in streams:
        stream_type = stream.get("codec_type", "unknown")
        codec_name = stream.get("codec_name", "unknown")

        msg += f"{stream_type}: {codec_name}\n"

        if stream_type == "video":
            codec = codec_name
            is_image_codec = any(ext in codec.lower() for ext in _IMAGE_CODEC)
            if is_image_codec:
                # It's already an image, no need to generate thumbnail
                has_stream_thumbnail = True
            else:
                # It's a video, we need to generate a thumbnail
                has_stream_video = True
                has_stream_thumbnail = True  # Will be generated from video
                height = stream.get("height", 0)

        elif stream_type == "audio":
            has_stream_audio = True

    encode_log(msg)

    return {
        "has_stream_video": has_stream_video,
        "has_stream_thumbnail": has_stream_thumbnail,
        "has_stream_audio": has_stream_audio,
        "codec": codec,
        "height": height,
        "duration": duration,
    }


# =============================================================================
# ENCODING ORCHESTRATION FUNCTIONS
# =============================================================================


def launch_encode_video(info_video: dict, file: str) -> tuple[bool, bool]:
    """
    Launch video encoding based on codec support.

    Args:
        info_video: Video information dictionary
        file: Input filename

    Returns:
        tuple: (m3u8_success: bool, mp4_success: bool)
    """
    codec = info_video.get("codec", "")
    height = info_video.get("height", 0)

    if _ENCODING_TYPE.upper() == "GPU" and codec in _LIST_CODEC:
        # Preflight NVENC to avoid producing broken/unreadable outputs when the driver is too old.
        nvenc_ok, nvenc_details = _nvenc_preflight()
        if not nvenc_ok:
            encode_log(
                "NVENC unavailable; falling back to CPU. "
                "(Typical fix: update NVIDIA driver to match the FFmpeg NVENC API requirement)\n"
                + nvenc_details
            )
            encode_m3u8 = encode_without_gpu("m3u8", codec, height, file)
            encode_mp4 = encode_without_gpu("mp4", codec, height, file)
        else:
            # Use GPU encoding for supported codecs, fallback to CPU per-format
            encode_m3u8 = encode_with_gpu("m3u8", codec, height, file)
            encode_mp4 = encode_with_gpu("mp4", codec, height, file)
    else:
        # Use CPU encoding for unsupported codecs
        encode_m3u8 = encode_without_gpu("m3u8", codec, height, file)
        encode_mp4 = encode_without_gpu("mp4", codec, height, file)

    return encode_m3u8, encode_mp4


def launch_encode_audio(info_video: dict, file: str) -> tuple[bool, str]:
    """
    Launch audio encoding.

    Args:
        info_video: Video information dictionary
        file: Input filename

    Returns:
        tuple: (success: bool, log_message: str)
    """
    encode_audio = True
    msg = ""

    # Only encode M4A if there's no video stream (pure audio file)
    if not info_video.get("has_stream_video", False):
        if encode("m4a", "", "", 0, file):
            msg += "encode m4a ok\n"
        else:
            encode_audio = False
            msg += 20 * "////" + "\n"
            msg += "error m4a"

    # Always try to encode MP3
    if encode("mp3", "", "", 0, file):
        msg += "encode mp3 ok\n"
    else:
        encode_audio = False
        msg += 20 * "////" + "\n"
        msg += "error mp3\n"

    return encode_audio, msg


def launch_encode(info_video: dict, file: str) -> bool:
    """
    Orchestrate complete encoding process.

    Args:
        info_video: Video information dictionary
        file: Input filename

    Returns:
        bool: True if all encodings succeeded, False otherwise
    """
    msg = "--> launch_encode\n"

    # Encode video if present
    encode_m3u8 = encode_mp4 = True
    if info_video.get("has_stream_video", False):
        encode_m3u8, encode_mp4 = launch_encode_video(info_video, file)

    # Extract thumbnails (3 thumbnails at 25%, 50%, 75%)
    encode_thumbnail = True
    if info_video.get("has_stream_thumbnail", False):
        duration = info_video.get("duration", 0)
        for i in range(3):
            if encode("thumbnail", "png", "", 0, file, duration, i):
                msg += f"thumbnail {i} ok\n"
            else:
                encode_thumbnail = False
                msg += 20 * "////" + "\n"
                msg += f"error thumbnail {i}\n"

    # Generate overview (sprite sheet + VTT) for video navigation
    encode_overview = True
    if info_video.get("has_stream_video", False):
        duration = info_video.get("duration", 0)
        overview_success, overview_msg = generate_overview(file, duration)
        msg += overview_msg
        if overview_success:
            msg += "overview generation ok\n"
            add_info_video(
                "encode_overview",
                {"sprite_filename": "overview.png", "vtt_filename": "overview.vtt"},
            )
        else:
            encode_overview = False
            msg += 20 * "////" + "\n"
            msg += "error generating overview\n"

    # Encode audio if present
    encode_audio = True
    if info_video.get("has_stream_audio", False):
        encode_audio, return_msg = launch_encode_audio(info_video, file)
        msg += return_msg

    encode_log(msg)

    # Return overall success
    return all([encode_audio, encode_thumbnail, encode_overview, encode_m3u8, encode_mp4])


# =============================================================================
# LOGGING AND METADATA FUNCTIONS
# =============================================================================


def encode_log(msg: str):
    """
    Write message to log file and optionally print to console.

    Args:
        msg: Message to log
    """
    if _DEBUG:
        print(msg)

    with open(_VIDEOS_OUTPUT_DIR + "/encoding.log", "a") as f:
        f.write("\n")
        f.write(msg)
        f.write("\n")


def add_info_video(key: str, value, append: bool = False):
    """
    Add encoding information to metadata JSON file.

    Args:
        key: Metadata key
        value: Metadata value (can be any JSON-serializable type)
        append: Whether to append to existing array or replace
    """
    data = {}

    # Load existing data
    try:
        with open(_VIDEOS_OUTPUT_DIR + "/info_video.json") as json_file:
            data = json.load(json_file)
    except (FileNotFoundError, JSONDecodeError):
        pass  # Start with empty data if file doesn't exist or is invalid

    # Update data structure
    if data.get(key) and append:
        existing_val = data[key]
        if isinstance(existing_val, list):
            existing_val.append(value)
        else:
            data[key] = [existing_val, value]
    else:
        data[key] = [value] if append else value

    # Write updated data
    with open(_VIDEOS_OUTPUT_DIR + "/info_video.json", "w") as outfile:
        json.dump(data, outfile, indent=2)


# =============================================================================
# MAIN FUNCTION
# =============================================================================


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Video encoding script")
    parser.add_argument("--encoding-type", required=True, help="CPU or GPU encoding type (Ex: CPU)")
    parser.add_argument("--base-dir", required=True, help="Base directory for input files")
    parser.add_argument("--input-file", required=True, help="Name of input file to encode")
    parser.add_argument("--work-dir", required=True, help="Work directory for output files")
    parser.add_argument("--debug", required=False, help="Run script in debug mode")
    parser.add_argument(
        "--hwaccel-device", required=False, help="HWACCEL_DEVICE parameter for GPU encoding (Ex: 0)"
    )
    parser.add_argument(
        "--cuda-visible-devices",
        required=False,
        help="CUDA_VISIBLE_DEVICES parameter for GPU encoding (Ex: 0,1)",
    )
    parser.add_argument(
        "--cuda-device-order",
        required=False,
        help="CUDA_DEVICE_ORDER parameter for GPU encoding (Ex: PCI_BUS_ID)",
    )
    parser.add_argument(
        "--cuda-path",
        required=False,
        help="CUDA_PATH parameter for GPU encoding (Ex: /usr/local/cuda-13.2)",
    )
    parser.add_argument(
        "--rendition",
        required=False,
        help='Rendition configuration JSON string (Ex: \'{"360": {"resolution": "640x360", "encode_mp4": true}}\')',
    )
    parser.add_argument(
        "--cut",
        required=False,
        help='Cut configuration JSON string (Ex: \'{"start": "00:00:05", "end": "00:00:17", "duration": "00:00:17"}\')',
    )
    parser.add_argument(
        "--dressing",
        required=False,
        help='Dressing configuration JSON string (Ex: \'{"watermark": "https://pod.univ.fr/media/files/xxx/watermark.png", "watermark_position": "En haut \\u00e0 droite", "watermark_position_orig": "top_right", "watermark_opacity": "100"}"\')',
    )
    return parser


def _parse_rendition_config(args, msg: str) -> str:
    if args.rendition:
        try:
            rendition_config = json.loads(args.rendition)
            _RENDITION_CONFIG.update(rendition_config)
            msg += f"Rendition configuration updated: {json.dumps(_RENDITION_CONFIG, indent=2)}\n"
        except (json.JSONDecodeError, ValueError) as e:
            msg += f"Warning: Failed to parse rendition parameter: {e}\n"
            msg += "Using default rendition configuration\n"
    return msg


def _parse_cut_config(args, msg: str) -> str:
    global _CUT_CONFIG, SUBTIME, EFFECTIVE_DURATION
    _CUT_CONFIG = {}
    EFFECTIVE_DURATION = 0
    SUBTIME = " "
    if args.cut:
        try:
            cut_config = json.loads(args.cut)
            _CUT_CONFIG = cut_config
            start = cut_config.get("start", "")
            end = cut_config.get("end", "")
            initial_duration = cut_config.get("initial_duration", "")

            if start and end:
                SUBTIME = f" -ss {start} -to {end} "
                msg += f"Cut configuration applied: start={start}, end={end}\n"
                msg += f"SUBTIME set to: {SUBTIME}\n"

                start_seconds = timestamp_to_seconds(start)
                end_seconds = timestamp_to_seconds(end)
                calculated_duration = end_seconds - start_seconds

                if initial_duration:
                    initial_duration_seconds = timestamp_to_seconds(initial_duration)
                    if end_seconds > initial_duration_seconds:
                        msg += f"Warning: end time ({end}) exceeds initial duration ({initial_duration}). Adjusting to initial duration.\n"
                        calculated_duration = initial_duration_seconds - start_seconds
                        SUBTIME = f" -ss {start} -to {initial_duration} "

                EFFECTIVE_DURATION = max(0, calculated_duration)
                msg += f"Effective duration after cut: {EFFECTIVE_DURATION} seconds\n"
            else:
                msg += "Warning: Cut configuration incomplete (missing start or end)\n"
        except (json.JSONDecodeError, ValueError) as e:
            msg += f"Warning: Failed to parse cut parameter: {e}\n"
            msg += "Cut configuration ignored\n"
    return msg


def _parse_dressing_config(args, msg: str) -> str:
    global _DRESSING_CONFIG
    _DRESSING_CONFIG = {}
    if args.dressing:
        try:
            _DRESSING_CONFIG = json.loads(args.dressing)
            msg += "Dressing configuration received\n"
        except (json.JSONDecodeError, ValueError) as e:
            msg += f"Warning: Failed to parse dressing parameter: {e}\n"
            msg += "Dressing configuration ignored\n"
    return msg


def _apply_cli_config(args) -> str:
    msg = ""
    global _DEBUG, _VIDEOS_DIR, _VIDEOS_OUTPUT_DIR, _ENCODING_TYPE, _HWACCEL_DEVICE
    _DEBUG = args.debug and args.debug.lower() == "true"
    _ENCODING_TYPE = args.encoding_type
    _VIDEOS_DIR = args.base_dir or "/tmp/esup-runner/task01"
    workdir = args.work_dir or "output"
    _VIDEOS_OUTPUT_DIR = os.path.join(_VIDEOS_DIR, workdir)

    msg = _parse_rendition_config(args, msg)
    msg = _parse_cut_config(args, msg)
    msg = _parse_dressing_config(args, msg)

    if _ENCODING_TYPE.upper() == "GPU":
        # If CUDA_VISIBLE_DEVICES isolates a single GPU, the in-process ordinal is 0.
        env_cuda = args.cuda_visible_devices or os.getenv("CUDA_VISIBLE_DEVICES") or "0"
        os.environ["CUDA_VISIBLE_DEVICES"] = env_cuda
        os.environ["CUDA_DEVICE_ORDER"] = args.cuda_device_order or "PCI_BUS_ID"
        os.environ["CUDA_PATH"] = args.cuda_path or "/usr/local/cuda-13.2"

        # Respect provided hwaccel-device only when multiple devices stay visible; otherwise default to 0.
        if "," in env_cuda:
            try:
                _HWACCEL_DEVICE = int(args.hwaccel_device or 0)
            except Exception:
                _HWACCEL_DEVICE = 0
        else:
            _HWACCEL_DEVICE = 0

    if not os.path.exists(_VIDEOS_OUTPUT_DIR):
        os.makedirs(_VIDEOS_OUTPUT_DIR)

    # Do not truncate an existing encoding.log: other handlers (e.g. studio)
    # may have already written generation diagnostics into it.
    encoding_log_path = _VIDEOS_OUTPUT_DIR + "/encoding.log"
    if not os.path.exists(encoding_log_path):
        open(encoding_log_path, "w").close()
    else:
        try:
            with open(encoding_log_path, "a") as f:
                f.write("\n\n===== ENCODING STAGE =====\n")
        except Exception:
            # Worst case: fall back to truncating if the file cannot be appended.
            open(encoding_log_path, "w").close()
    open(_VIDEOS_OUTPUT_DIR + "/info_video.json", "w").close()
    return msg


def _prepare_input_file(args) -> tuple[str, str]:
    msg = ""
    input_file = os.path.basename(args.input_file) if args.input_file else ""
    path_file = os.path.join(_VIDEOS_DIR, input_file)

    if not (os.path.isfile(path_file) and os.path.getsize(path_file) > 0):
        msg += "\nInvalid file or path: %s\n" % path_file
        return "", msg

    filename = sanitize_filename(input_file)
    original_path = os.path.join(_VIDEOS_DIR, input_file)
    new_path = os.path.join(_VIDEOS_DIR, filename)
    if original_path != new_path:
        os.rename(original_path, new_path)

    msg += "Encoding file: {}\n".format(filename)

    if _DRESSING_CONFIG:
        dressed_filename, dressing_msg = apply_dressing_if_needed(filename, _DRESSING_CONFIG)
        msg += dressing_msg
        if dressed_filename != filename:
            filename = dressed_filename
            add_info_video("dressing", _DRESSING_CONFIG)
            add_info_video("dressing_input", filename)

    return filename, msg


def _compute_working_duration(info_video: dict) -> tuple[int, str]:
    global SUBTIME
    msg = ""
    if EFFECTIVE_DURATION > 0:
        working_duration = EFFECTIVE_DURATION
        msg += f"Using effective duration from cut: {working_duration} seconds\n"
    else:
        working_duration = info_video["duration"]
        if not _DEBUG and working_duration > 0 and SUBTIME == " ":
            SUBTIME = " -ss 0 -to %s " % working_duration
    return working_duration, msg


def _process_encoding(args) -> str:
    msg = ""
    filename, prep_msg = _prepare_input_file(args)
    msg += prep_msg
    if not filename:
        return msg

    info_video = get_info_video(filename)
    working_duration, duration_msg = _compute_working_duration(info_video)
    msg += duration_msg

    info_video["duration"] = working_duration
    info_video["effective_duration"] = working_duration
    if EFFECTIVE_DURATION > 0:
        info_video["cut_applied"] = True

    msg += "\n" + json.dumps(info_video, indent=2) + "\n"

    for key, value in info_video.items():
        add_info_video(key, value)

    encode_result = launch_encode(info_video, filename)
    add_info_video("encode_result", encode_result)

    msg += "- End of encoding: %s\n" % time.ctime()
    return msg


def main():
    """Main encoding function."""
    msg = "--> Main\n"
    msg += "- Starting encoding cycle: %s\n" % time.ctime()

    parser = _build_arg_parser()
    args = parser.parse_args()

    msg += _apply_cli_config(args)
    msg += _process_encoding(args)

    encode_log(msg)


if __name__ == "__main__":
    main()
