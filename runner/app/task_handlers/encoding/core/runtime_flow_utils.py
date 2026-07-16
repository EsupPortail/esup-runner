#!/usr/bin/env python3
"""Core runtime implementation for the encoding workflow.

Contains encoding business logic, runtime globals, and helper wrappers.
Is consumed by orchestration wiring in `main_runtime_utils`.
"""

from __future__ import absolute_import, division, print_function

import copy
import json
import os
import re
import subprocess
import time
import unicodedata
from functools import lru_cache
from typing import Any, Dict, Optional, Union

from . import (
    dressing_runtime_utils,
    encoding_flow_utils,
    ffmpeg_command_utils,
    ffmpeg_runtime_utils,
    media_probe_utils,
    metadata_runtime_utils,
    overview_utils,
    rendition_utils,
)

# =============================================================================
# INITIAL CONFIGURATION
# =============================================================================
_DEBUG = True
_VIDEOS_DIR = "/tmp/esup-runner/task01"
_VIDEOS_OUTPUT_DIR = "/tmp/esup-runner/task01/output"
_ENCODING_TYPE = "CPU"
_HWACCEL_DEVICE = 0

# Video renditions encoding configuration (which formats to encode)
# 2160p is intentionally not part of the default ladder and is encoded only
# when explicitly provided in the rendition parameter.
_DEFAULT_RENDITION_CONFIG = {
    "360": {
        "resolution": "640x360",
        "video_bitrate": "750k",
        "audio_bitrate": "96k",
        "encode_mp4": True,
    },
    "720": {
        "resolution": "1280x720",
        "video_bitrate": "2000k",
        "audio_bitrate": "128k",
        "encode_mp4": True,
    },
    "1080": {
        "resolution": "1920x1080",
        "video_bitrate": "3000k",
        "audio_bitrate": "192k",
        "encode_mp4": False,
    },
}
_RENDITION_CONFIG = copy.deepcopy(_DEFAULT_RENDITION_CONFIG)

# Input validation patterns for rendition settings.
_RENDITION_KEY_RE = re.compile(r"^\d+$")
_RESOLUTION_RE = re.compile(r"^(?P<width>\d+)x(?P<height>\d+)$")
_BITRATE_RE = re.compile(r"^\d+(?:\.\d+)?[kKmMgG]$")

# Preserve historical rate-control ladder defaults for legacy renditions.
_LEGACY_RATE_LADDER = {
    "360": {"minrate": "500k", "maxrate": "1000k", "bufsize": "1500k"},
    "720": {"minrate": "1000k", "maxrate": "3000k", "bufsize": "4000k"},
    "1080": {"minrate": "2M", "maxrate": "4500k", "bufsize": "6M"},
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

# Probe more packets on malformed/fragmented MP4/MOV files where some stream
# parameters (notably pix_fmt) arrive late.
_INPUT_PROBE = "-probesize 100M -analyzeduration 100M"

# Keep the first audio stream as a safe fallback when probing cannot provide a
# usable list of recognized audio stream indices.
_FALLBACK_AUDIO_STREAM_MAP = "-map 0:a:0?"
_AUDIO_STREAM_MAP = _FALLBACK_AUDIO_STREAM_MAP

# Audio encoding templates
MP3 = (
    "time ffmpeg -i {input} -hide_banner -y {subtime}-c:a libmp3lame -q:a 2 "
    '-ar 44100 -vn -threads 0 "{output_dir}/audio_192k_{output}.mp3"'
)

M4A = (
    "time ffmpeg -i {input} -hide_banner -y {subtime}-c:a aac -ar 44100 "
    '-q:a 2 -vn -threads 0 "{output_dir}/audio_192k_{output}.m4a"'
)

# Thumbnail extraction templates (3 thumbnails at 25%, 50%, 75% of video duration)
_THUMBNAIL_MAX_SCALE_FILTER = (
    "-vf \"scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease\" "
)
EXTRACT_THUMBNAIL_0 = (
    "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y "
    + _THUMBNAIL_MAX_SCALE_FILTER
    + "-vframes 1 {output_dir}/{filename}_0.{ext}"
)
EXTRACT_THUMBNAIL_1 = (
    "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y "
    + _THUMBNAIL_MAX_SCALE_FILTER
    + "-vframes 1 {output_dir}/{filename}_1.{ext}"
)
EXTRACT_THUMBNAIL_2 = (
    "time ffmpeg -ss {timestamp} -i {input} -hide_banner -y "
    + _THUMBNAIL_MAX_SCALE_FILTER
    + "-vframes 1 {output_dir}/{filename}_2.{ext}"
)

# CPU encoding base command
CPU = f"time ffmpeg -hide_banner -y {_INPUT_PROBE} -i {{input}} "

# GPU encoding base command (using CUDA)
GPU = (
    "time ffmpeg -y -hwaccel_device {hwaccel_device} "
    "-hwaccel cuda -hwaccel_output_format cuda "
    f"{_INPUT_PROBE} -c:v:0 {{codec}}_cuvid -i {{input}} "
)

# Common encoding parameters
# NOTE: For GPU pipelines (CUDA frames + scale_cuda + NVENC), forcing a *software* pix_fmt
# (like yuv420p) can make FFmpeg try to insert swscale (auto_scale), which cannot consume
# CUDA hardware frames. Keep pix_fmt enforcement only for CPU pipelines.
COMMON_CPU = (
    " -map 0:v:0? {audio_stream_map} "
    "-c:a aac -ar 48000 -strict experimental -profile:v high "
    '-pix_fmt yuv420p -force_key_frames "expr:gte(t,n_forced*2)" '
    "-preset slow -qmin 20 -qmax 50 "
)

COMMON_GPU = (
    " -map 0:v:0? {audio_stream_map} "
    "-c:a aac -ar 48000 -strict experimental -profile:v high "
    '-force_key_frames "expr:gte(t,n_forced*2)" '
    "-preset p4 -qmin 20 -qmax 50 "
)

# GPU scaling filter
scale_gpu = (
    COMMON_GPU
    + "{fps_mode_options}"
    + '-vf "scale_cuda=-2:{height}" -c:v h264_nvenc -sc_threshold 0 -bf 0 -rc-lookahead 0 '
    + "{nvenc_rate_control_options}"
)

# CPU scaling filter (libx264 preferred; fallback decided at runtime)
scale_cpu = (
    COMMON_CPU
    + "{fps_mode_options}"
    + '-vf "scale=-2:{height}" -c:v {encoder} -sc_threshold 0 '
    + "{cpu_quality_options}"
)

# Output format options
HLS_OUTPUT_OPTIONS = (
    "-max_muxing_queue_size 9999 -hls_playlist_type vod -hls_list_size 0 "
    "-hls_time 2 -hls_flags single_file+independent_segments "
)

MP4_OUTPUT_OPTIONS = "-max_muxing_queue_size 9999 -movflags faststart -write_tmcd 0 "

# Global variable for subtime (seek position)
SUBTIME = " "

# Global variable for effective duration (after cut)
EFFECTIVE_DURATION = 0

# Global variable for dressing configuration
_DRESSING_CONFIG: dict = {}

# Global variable for cut configuration
_CUT_CONFIG: dict = {}

# Global variable for optional video identification metadata
_VIDEO_IDENTIFICATION: dict = {}

# Candidate values accepted for duration fields returned by ffprobe.
DurationValue = Union[str, int, float, None]

_WEBM_EXTENSIONS = {".webm"}
_WEBM_VIDEO_CODECS = {"vp8", "vp9", "av1"}
_WEBM_OUTPUT_FPS = 30
_WEBM_MIN_OUTPUT_FPS = 6
_WEBM_MAX_OUTPUT_FPS = 30

# Effective input video fps detected for the currently processed file.
_SOURCE_VIDEO_FPS = 0.0


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================


class EncodingValidationError(RuntimeError):
    """Raised when the input media should not be encoded."""


def _is_webm_source(*, file: str, codec: str) -> bool:
    """Return whether the current source should use WebM-specific handling."""
    return ffmpeg_command_utils.is_webm_source(
        file=file,
        codec=codec,
        webm_extensions=_WEBM_EXTENSIONS,
        webm_video_codecs=_WEBM_VIDEO_CODECS,
    )


def _build_fps_mode_options(*, is_webm_source: bool) -> str:
    """Build FPS options adapted to WebM or non-WebM sources."""
    return ffmpeg_command_utils.build_fps_mode_options(
        is_webm=is_webm_source,
        source_video_fps=_SOURCE_VIDEO_FPS,
        webm_output_fps=_WEBM_OUTPUT_FPS,
        webm_min_output_fps=_WEBM_MIN_OUTPUT_FPS,
        webm_max_output_fps=_WEBM_MAX_OUTPUT_FPS,
    )


def _build_nvenc_rate_control_options(*, is_webm_source: bool) -> str:
    """Build NVENC rate-control options for the current source profile."""
    return ffmpeg_command_utils.build_nvenc_rate_control_options(is_webm=is_webm_source)


def _build_cpu_quality_options(*, is_webm_source: bool) -> str:
    """Build CPU quality options for the current source profile."""
    return ffmpeg_command_utils.build_cpu_quality_options(is_webm=is_webm_source)


def _build_audio_stream_map(audio_stream_indices: Any) -> str:
    """Build audio mappings, falling back to the optional first audio stream."""
    return ffmpeg_command_utils.build_audio_stream_map(
        audio_stream_indices,
        fallback_map=_FALLBACK_AUDIO_STREAM_MAP,
    )


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


def _parse_bitrate_to_bps(bitrate: str) -> int:
    """Parse a human bitrate string into bits per second."""
    return rendition_utils.parse_bitrate_to_bps(bitrate)


def _format_bitrate_from_bps(bits_per_second: int) -> str:
    """Format a bitrate integer (bps) as FFmpeg-compatible text."""
    return rendition_utils.format_bitrate_from_bps(bits_per_second)


def _infer_video_bitrate(width: int, height: int) -> str:
    """Infer a video bitrate for a given target resolution."""
    return rendition_utils.infer_video_bitrate(
        width,
        height,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _infer_audio_bitrate(height: int) -> str:
    """Infer an audio bitrate for a given target height tier."""
    return rendition_utils.infer_audio_bitrate(
        height,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _build_rate_control(rendition_key: str, video_bitrate: str) -> Dict[str, str]:
    """Build minrate/maxrate/bufsize values for a rendition."""
    return rendition_utils.build_rate_control(
        rendition_key,
        video_bitrate,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
        legacy_rate_ladder=_LEGACY_RATE_LADDER,
    )


def _build_rendition_rate_options(rendition_key: str, rendition_cfg: Dict[str, Any]) -> str:
    """Build FFmpeg bitrate options for a rendition entry."""
    return rendition_utils.build_rendition_rate_options(
        rendition_key,
        rendition_cfg,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
        legacy_rate_ladder=_LEGACY_RATE_LADDER,
    )


def _merge_rendition_config(overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Merge CLI rendition overrides with current runtime config."""
    return rendition_utils.merge_rendition_config(overrides, rendition_config=_RENDITION_CONFIG)


def _validate_rendition_key_and_cfg(raw_key: Any, raw_cfg: Any) -> tuple[str, Dict[str, Any]]:
    """Validate rendition key/value shapes before normalization."""
    return rendition_utils.validate_rendition_key_and_cfg(raw_key, raw_cfg)


def _parse_rendition_resolution(key: str, raw_cfg: Dict[str, Any]) -> tuple[int, int]:
    """Parse and validate a rendition resolution field."""
    return rendition_utils.parse_rendition_resolution(key, raw_cfg)


def _normalize_video_bitrate(key: str, raw_cfg: Dict[str, Any], width: int, height: int) -> str:
    """Normalize or infer the `video_bitrate` for a rendition."""
    return rendition_utils.normalize_video_bitrate(
        key,
        raw_cfg,
        width,
        height,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _normalize_audio_bitrate(key: str, raw_cfg: Dict[str, Any], height: int) -> str:
    """Normalize or infer the `audio_bitrate` for a rendition."""
    return rendition_utils.normalize_audio_bitrate(
        key,
        raw_cfg,
        height,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _normalize_encode_mp4(key: str, raw_cfg: Dict[str, Any]) -> bool:
    """Normalize the `encode_mp4` flag for a rendition."""
    return rendition_utils.normalize_encode_mp4(key, raw_cfg)


def _normalize_rendition_entry(raw_key: Any, raw_cfg: Any) -> tuple[str, Dict[str, Any]]:
    """Normalize one rendition entry from user-provided configuration."""
    return rendition_utils.normalize_rendition_entry(
        raw_key,
        raw_cfg,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _validate_and_normalize_rendition_config(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Validate and normalize the full rendition configuration payload."""
    return rendition_utils.validate_and_normalize_rendition_config(
        config,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
    )


def _select_renditions_for_encode(
    *, source_height: int, output_format: str
) -> list[tuple[str, Dict[str, Any], int]]:
    """Select renditions eligible for the source and target format."""
    return rendition_utils.select_renditions_for_encode(
        rendition_config=_RENDITION_CONFIG,
        source_height=source_height,
        output_format=output_format,
    )


def _build_video_output_segment(
    *, output_format: str, rendition_key: str, rendition_cfg: Dict[str, Any], output_basename: str
) -> str:
    """Build the output segment used in the FFmpeg command for one rendition."""
    return rendition_utils.build_video_output_segment(
        output_format=output_format,
        rendition_key=rendition_key,
        rendition_cfg=rendition_cfg,
        output_basename=output_basename,
        videos_output_dir=_VIDEOS_OUTPUT_DIR,
        default_rendition_config=_DEFAULT_RENDITION_CONFIG,
        legacy_rate_ladder=_LEGACY_RATE_LADDER,
        hls_output_options=HLS_OUTPUT_OPTIONS,
        mp4_output_options=MP4_OUTPUT_OPTIONS,
    )


def _build_video_metadata_entries(
    *, output_format: str, source_height: int, output_basename: str
) -> list[Dict[str, object]]:
    """Build metadata entries for produced video renditions."""
    return rendition_utils.build_video_metadata_entries(
        rendition_config=_RENDITION_CONFIG,
        output_format=output_format,
        source_height=source_height,
        output_basename=output_basename,
    )


# =============================================================================
# CORE ENCODING FUNCTIONS
# =============================================================================


def encode_with_gpu(format: str, codec: str, height: int, file: str) -> bool:
    """Try GPU encoding first, then fall back to CPU."""
    return encoding_flow_utils.encode_with_gpu(
        format,
        codec,
        height,
        file,
        encode_fn=encode,
        encode_log_fn=encode_log,
    )


def encode_without_gpu(format: str, codec: str, height: int, file: str) -> bool:
    """Run an encode attempt on CPU only."""
    return encoding_flow_utils.encode_without_gpu(
        format,
        codec,
        height,
        file,
        encode_fn=encode,
        encode_log_fn=encode_log,
    )


def get_cmd_gpu(format: str, codec: str, height: int, file: str) -> str:
    """Build the FFmpeg command for GPU video encoding."""
    return ffmpeg_command_utils.get_cmd_gpu(
        format,
        codec,
        height,
        file,
        videos_dir=_VIDEOS_DIR,
        hwaccel_device=_HWACCEL_DEVICE,
        subtime=SUBTIME,
        source_video_fps=_SOURCE_VIDEO_FPS,
        audio_stream_map=_AUDIO_STREAM_MAP,
        gpu_template=GPU,
        scale_gpu_template=scale_gpu,
        webm_extensions=_WEBM_EXTENSIONS,
        webm_video_codecs=_WEBM_VIDEO_CODECS,
        webm_output_fps=_WEBM_OUTPUT_FPS,
        webm_min_output_fps=_WEBM_MIN_OUTPUT_FPS,
        webm_max_output_fps=_WEBM_MAX_OUTPUT_FPS,
        sanitize_filename_fn=sanitize_filename,
        select_renditions_for_encode_fn=_select_renditions_for_encode,
        build_video_output_segment_fn=_build_video_output_segment,
    )


def get_cmd_cpu(format: str, codec: str, height: int, file: str) -> str:
    """Build the FFmpeg command for CPU video encoding."""
    return ffmpeg_command_utils.get_cmd_cpu(
        format,
        codec,
        height,
        file,
        videos_dir=_VIDEOS_DIR,
        subtime=SUBTIME,
        source_video_fps=_SOURCE_VIDEO_FPS,
        audio_stream_map=_AUDIO_STREAM_MAP,
        cpu_template=CPU,
        scale_cpu_template=scale_cpu,
        webm_extensions=_WEBM_EXTENSIONS,
        webm_video_codecs=_WEBM_VIDEO_CODECS,
        webm_output_fps=_WEBM_OUTPUT_FPS,
        webm_min_output_fps=_WEBM_MIN_OUTPUT_FPS,
        webm_max_output_fps=_WEBM_MAX_OUTPUT_FPS,
        choose_h264_encoder_fn=_choose_h264_encoder,
        sanitize_filename_fn=sanitize_filename,
        select_renditions_for_encode_fn=_select_renditions_for_encode,
        build_video_output_segment_fn=_build_video_output_segment,
    )


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
    """Return whether FFmpeg exposes the requested encoder."""
    return ffmpeg_runtime_utils.has_encoder(encoder, subprocess_module=subprocess)


def _choose_h264_encoder() -> tuple[str, str]:
    """Pick the best available H.264 encoder and context note."""
    return ffmpeg_runtime_utils.choose_h264_encoder(has_encoder_fn=_has_encoder)


@lru_cache(maxsize=1)
def _nvenc_preflight() -> tuple[bool, str]:
    """Run NVENC preflight checks and return status/details."""
    return ffmpeg_runtime_utils.nvenc_preflight(subprocess_module=subprocess)


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
    """Execute an FFmpeg command and return status/log output."""
    global _AUDIO_STREAM_MAP
    success, msg = ffmpeg_runtime_utils.launch_cmd(
        ffmpeg_cmd,
        type,
        format,
        subprocess_module=subprocess,
    )
    selected_audio_count = _AUDIO_STREAM_MAP.count("-map ")
    should_retry_primary_audio = (
        not success
        and type in {"cpu", "gpu"}
        and selected_audio_count > 1
        and _AUDIO_STREAM_MAP in ffmpeg_cmd
    )
    if not should_retry_primary_audio:
        return success, msg

    fallback_cmd = ffmpeg_cmd.replace(_AUDIO_STREAM_MAP, _FALLBACK_AUDIO_STREAM_MAP)
    retry_notice = (
        "Recognized multi-track audio mapping failed; "
        "retrying with the primary audio stream only.\n"
    )
    retry_success, retry_msg = ffmpeg_runtime_utils.launch_cmd(
        fallback_cmd,
        type,
        format,
        subprocess_module=subprocess,
    )
    if retry_success:
        _AUDIO_STREAM_MAP = _FALLBACK_AUDIO_STREAM_MAP
    return retry_success, msg + retry_notice + retry_msg


def _safe_filename_from_url(url: str) -> str:
    """Return a stable sanitized filename derived from a URL."""
    return dressing_runtime_utils.safe_filename_from_url(
        url,
        sanitize_filename_fn=sanitize_filename,
    )


def _download_allowed_hosts_from_env() -> list[str]:
    """Load the optional download host allowlist from environment."""
    return dressing_runtime_utils.download_allowed_hosts_from_env()


def _download_allow_private_networks_from_env() -> bool:
    """Return whether private-network downloads are allowed."""
    return dressing_runtime_utils.download_allow_private_networks_from_env()


def _host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Return whether a host matches the configured allowlist."""
    return dressing_runtime_utils.host_is_allowed(host, allowed_hosts)


def _validate_host_resolves_to_public_ip(host: str) -> None:
    """Validate that a host resolves to a public IP address."""
    dressing_runtime_utils.validate_host_resolves_to_public_ip(host)


def _download_url_to_dir(url: str, target_dir: str, prefix: str) -> str:
    """Download a remote asset into `target_dir` and return local path."""
    return dressing_runtime_utils.download_url_to_dir(
        url,
        target_dir,
        prefix,
        sanitize_filename_fn=sanitize_filename,
    )


def _probe_duration_seconds(path: str) -> float:
    """Probe media duration in seconds."""
    return dressing_runtime_utils.probe_duration_seconds(path)


def _probe_has_audio(path: str) -> bool:
    """Probe whether a media file contains an audio stream."""
    return dressing_runtime_utils.probe_has_audio(path)


def _watermark_overlay_xy(position_orig: str, margin: int = 54) -> tuple[str, str]:
    """Build overlay coordinates for watermark positioning."""
    return dressing_runtime_utils.watermark_overlay_xy(position_orig, margin)


def _build_normalize_1080p_filter(label_in: str, label_out: str) -> str:
    """Build the normalization filter used for 1080p 16:9 dressing."""
    return dressing_runtime_utils.build_normalize_1080p_filter(label_in, label_out)


def _run_ffmpeg_cmd(ffmpeg_cmd: str, log_type: str) -> bool:
    """Run an FFmpeg command and forward logs to encoding log."""
    return dressing_runtime_utils.run_ffmpeg_cmd(
        ffmpeg_cmd,
        log_type,
        launch_cmd_fn=launch_cmd,
        encode_log_fn=encode_log,
    )


def _create_cut_intermediate(input_path: str, output_path: str, start: str, end: str) -> bool:
    """Create a temporary cut intermediate for dressing workflow."""
    return dressing_runtime_utils.create_cut_intermediate(
        input_path,
        output_path,
        start,
        end,
        choose_h264_encoder_fn=_choose_h264_encoder,
        run_ffmpeg_cmd_fn=_run_ffmpeg_cmd,
    )


def _create_watermarked_intermediate(
    input_path: str,
    watermark_path: str,
    output_path: str,
    position_orig: str,
    opacity_percent: str,
) -> bool:
    """Create a temporary watermarked intermediate."""
    return dressing_runtime_utils.create_watermarked_intermediate(
        input_path,
        watermark_path,
        output_path,
        position_orig,
        opacity_percent,
        choose_h264_encoder_fn=_choose_h264_encoder,
        watermark_overlay_xy_fn=_watermark_overlay_xy,
        build_normalize_1080p_filter_fn=_build_normalize_1080p_filter,
        run_ffmpeg_cmd_fn=_run_ffmpeg_cmd,
    )


def _parse_duration_seconds_fallback(value: Optional[str]) -> float:
    """Parse duration fallback values from float or timestamp-like text."""
    return dressing_runtime_utils.parse_duration_seconds_fallback(
        value,
        timestamp_to_seconds_fn=timestamp_to_seconds,
    )


def _create_credits_concat_intermediate(
    main_path: str,
    opening_path: Optional[str],
    opening_duration_hint: Optional[str],
    ending_path: Optional[str],
    ending_duration_hint: Optional[str],
    output_path: str,
) -> bool:
    """Create a concat intermediate with optional opening/ending credits."""
    return dressing_runtime_utils.create_credits_concat_intermediate(
        main_path,
        opening_path,
        opening_duration_hint,
        ending_path,
        ending_duration_hint,
        output_path,
        choose_h264_encoder_fn=_choose_h264_encoder,
        probe_duration_seconds_fn=_probe_duration_seconds,
        probe_has_audio_fn=_probe_has_audio,
        parse_duration_seconds_fallback_fn=_parse_duration_seconds_fallback,
        build_normalize_1080p_filter_fn=_build_normalize_1080p_filter,
        run_ffmpeg_cmd_fn=_run_ffmpeg_cmd,
    )


def _apply_cut_for_dressing(
    current_main_path: str, base: str, has_opening: bool, has_ending: bool
) -> tuple[str, str]:
    """Apply dressing-specific cut logic and sync shared globals."""
    global SUBTIME, EFFECTIVE_DURATION
    next_path, msg, next_subtime, next_effective_duration = (
        dressing_runtime_utils.apply_cut_for_dressing(
            current_main_path,
            base,
            has_opening,
            has_ending,
            cut_config=_CUT_CONFIG,
            subtime=SUBTIME,
            effective_duration=EFFECTIVE_DURATION,
            videos_dir=_VIDEOS_DIR,
            create_cut_intermediate_fn=_create_cut_intermediate,
        )
    )
    SUBTIME = next_subtime
    EFFECTIVE_DURATION = next_effective_duration
    return next_path, msg


def _apply_watermark_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict,
    assets_dir: str,
) -> tuple[str, str]:
    """Apply watermark dressing when configured."""
    return dressing_runtime_utils.apply_watermark_for_dressing(
        current_main_path,
        base,
        dressing_config,
        assets_dir,
        videos_dir=_VIDEOS_DIR,
        download_url_to_dir_fn=_download_url_to_dir,
        create_watermarked_intermediate_fn=_create_watermarked_intermediate,
    )


def _apply_credits_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict,
    assets_dir: str,
) -> tuple[str, str]:
    """Apply opening/ending credits dressing when configured."""
    return dressing_runtime_utils.apply_credits_for_dressing(
        current_main_path,
        base,
        dressing_config,
        assets_dir,
        videos_dir=_VIDEOS_DIR,
        download_url_to_dir_fn=_download_url_to_dir,
        create_credits_concat_intermediate_fn=_create_credits_concat_intermediate,
    )


def apply_dressing_if_needed(input_filename: str, dressing_config: dict) -> tuple[str, str]:
    """Apply dressing pipeline and return the effective input filename."""
    return dressing_runtime_utils.apply_dressing_if_needed(
        input_filename,
        dressing_config,
        videos_dir=_VIDEOS_DIR,
        sanitize_filename_fn=sanitize_filename,
        apply_cut_for_dressing_fn=_apply_cut_for_dressing,
        apply_watermark_for_dressing_fn=_apply_watermark_for_dressing,
        apply_credits_for_dressing_fn=_apply_credits_for_dressing,
    )


def _build_encode_video_job(
    *,
    encoder_type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    filename: str,
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build a video encode job payload for the flow orchestrator."""
    return ffmpeg_command_utils.build_encode_video_job(
        encoder_type=encoder_type,
        format=format,
        codec=codec,
        height=height,
        file=file,
        filename=filename,
        get_cmd_gpu_fn=get_cmd_gpu,
        get_cmd_cpu_fn=get_cmd_cpu,
        build_video_metadata_entries_fn=_build_video_metadata_entries,
    )


def _build_encode_audio_job(
    *, kind: str, file: str, filename: str
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build an audio encode job payload for the flow orchestrator."""
    return ffmpeg_command_utils.build_encode_audio_job(
        kind=kind,
        file=file,
        filename=filename,
        videos_dir=_VIDEOS_DIR,
        videos_output_dir=_VIDEOS_OUTPUT_DIR,
        mp3_template=MP3,
        m4a_template=M4A,
        subtime=SUBTIME,
    )


def _build_encode_thumbnail_job(
    *,
    file: str,
    filename: str,
    duration: float,
    thumbnail_index: int,
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build a thumbnail extraction job payload for the flow orchestrator."""
    return ffmpeg_command_utils.build_encode_thumbnail_job(
        file=file,
        filename=filename,
        duration=duration,
        thumbnail_index=thumbnail_index,
        videos_dir=_VIDEOS_DIR,
        videos_output_dir=_VIDEOS_OUTPUT_DIR,
        thumbnail_templates=[EXTRACT_THUMBNAIL_0, EXTRACT_THUMBNAIL_1, EXTRACT_THUMBNAIL_2],
    )


def _run_and_collect_text(cmd: list[str]) -> tuple[int, str]:
    """Run a command and collect merged text output."""
    return ffmpeg_runtime_utils.run_and_collect_text(cmd, subprocess_module=subprocess)


def _run_shell_bytes(cmd: str) -> tuple[int, bytes]:
    """Run a shell command and collect raw byte output."""
    return ffmpeg_runtime_utils.run_shell_bytes(cmd, subprocess_module=subprocess)


def _try_sprite_imagemagick_append(
    *,
    temp_thumb_dir: str,
    num_thumbnails: int,
    sprite_path: str,
) -> tuple[bool, str]:
    """Try ImageMagick fallback to build the overview sprite sheet."""
    return overview_utils.try_sprite_imagemagick_append(
        temp_thumb_dir=temp_thumb_dir,
        num_thumbnails=num_thumbnails,
        sprite_path=sprite_path,
    )


def _get_overview_max_single_row_thumbnails(
    thumb_width: int,
    thumb_height: int,
    *,
    max_sprite_width: Optional[int] = None,
    max_sprite_height: Optional[int] = None,
) -> int:
    """Return single-row overview capacity for thumbnail dimensions."""
    return overview_utils.get_overview_max_single_row_thumbnails(
        thumb_width,
        thumb_height,
        max_sprite_width=int(
            _OVERVIEW_CONFIG.get("max_sprite_width", 16384)
            if max_sprite_width is None
            else max_sprite_width
        ),
        max_sprite_height=int(
            _OVERVIEW_CONFIG.get("max_sprite_height", 16384)
            if max_sprite_height is None
            else max_sprite_height
        ),
    )


def _compute_overview_single_row_plan(
    duration: int,
    requested_interval: int,
    thumb_width: int,
    thumb_height: int,
    *,
    max_sprite_width: Optional[int] = None,
    max_sprite_height: Optional[int] = None,
) -> tuple[int, int, int, int]:
    """Compute overview sampling interval/count for single-row sprites."""
    return overview_utils.compute_overview_single_row_plan(
        duration,
        requested_interval,
        thumb_width,
        thumb_height,
        max_sprite_width=int(
            _OVERVIEW_CONFIG.get("max_sprite_width", 16384)
            if max_sprite_width is None
            else max_sprite_width
        ),
        max_sprite_height=int(
            _OVERVIEW_CONFIG.get("max_sprite_height", 16384)
            if max_sprite_height is None
            else max_sprite_height
        ),
    )


def _format_overview_thumbnail_plan_msg(
    requested_count: int, num_thumbnails: int, max_single_row_count: int, interval: int
) -> str:
    """Format a log message describing the chosen overview plan."""
    return overview_utils.format_overview_thumbnail_plan_msg(
        requested_count,
        num_thumbnails,
        max_single_row_count,
        interval,
    )


def _build_overview_generation_result_msg(
    temp_thumb_dir: str, expected_count: int
) -> tuple[bool, str, int]:
    """Build result summary from generated overview thumbnail files."""
    return overview_utils.build_overview_generation_result_msg(temp_thumb_dir, expected_count)


def encode(
    type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    duration: float = 0,
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
    return encoding_flow_utils.encode(
        type,
        format,
        codec,
        height,
        file,
        duration,
        thumbnail_index,
        sanitize_filename_fn=sanitize_filename,
        build_encode_video_job_fn=_build_encode_video_job,
        build_encode_audio_job_fn=_build_encode_audio_job,
        build_encode_thumbnail_job_fn=_build_encode_thumbnail_job,
        launch_cmd_fn=launch_cmd,
        add_info_video_fn=add_info_video,
        encode_log_fn=encode_log,
    )


def generate_overview_thumbnails(
    file: str, duration: int, output_dir: str
) -> tuple[bool, str, int]:
    """Generate overview thumbnails using configured runtime defaults."""
    return overview_utils.generate_overview_thumbnails(
        file,
        duration,
        output_dir,
        videos_dir=_VIDEOS_DIR,
        overview_config=_OVERVIEW_CONFIG,
        run_and_collect_text_fn=_run_and_collect_text,
        compute_overview_single_row_plan_fn=_compute_overview_single_row_plan,
        format_overview_thumbnail_plan_msg_fn=_format_overview_thumbnail_plan_msg,
        build_overview_generation_result_msg_fn=_build_overview_generation_result_msg,
    )


def create_overview_sprite(output_dir: str, num_thumbnails: int) -> tuple[bool, str]:
    """Create a single-row overview sprite from generated thumbnails."""
    return overview_utils.create_overview_sprite(
        output_dir,
        num_thumbnails,
        overview_config=_OVERVIEW_CONFIG,
        run_shell_bytes_fn=_run_shell_bytes,
        try_sprite_imagemagick_append_fn=_try_sprite_imagemagick_append,
        get_overview_max_single_row_thumbnails_fn=_get_overview_max_single_row_thumbnails,
    )


def generate_overview_vtt(output_dir: str, duration: int, num_thumbnails: int) -> tuple[bool, str]:
    """Generate overview VTT cues matching sprite coordinates."""
    return overview_utils.generate_overview_vtt(
        output_dir,
        duration,
        num_thumbnails,
        overview_config=_OVERVIEW_CONFIG,
        format_vtt_timestamp_fn=format_vtt_timestamp,
    )


def format_vtt_timestamp(seconds: int) -> str:
    """Format seconds as an `HH:MM:SS.mmm` WebVTT timestamp."""
    return overview_utils.format_vtt_timestamp(seconds)


def generate_overview(file: str, duration: int) -> tuple[bool, str]:
    """Run full overview generation (thumbnails + sprite + VTT)."""
    return overview_utils.generate_overview(
        file,
        duration,
        videos_output_dir=_VIDEOS_OUTPUT_DIR,
        overview_config=_OVERVIEW_CONFIG,
        generate_overview_thumbnails_fn=generate_overview_thumbnails,
        create_overview_sprite_fn=create_overview_sprite,
        generate_overview_vtt_fn=generate_overview_vtt,
    )


# =============================================================================
# VIDEO ANALYSIS FUNCTIONS
# =============================================================================


def get_info_from_video(probe_cmd: str) -> tuple[Optional[dict], str]:
    """Run ffprobe and parse source metadata payload."""
    return media_probe_utils.get_info_from_video(probe_cmd, subprocess_module=subprocess)


def _seconds_from_timestamp(value: str) -> float:
    """Convert timestamp text to seconds."""
    return media_probe_utils.seconds_from_timestamp(value)


def _duration_seconds_from_value(value: DurationValue) -> float:
    """Normalize duration values from ffprobe into seconds."""
    return media_probe_utils.duration_seconds_from_value(value)


def _parse_fps_value(raw_value: Any) -> float:
    """Parse FPS values from ffprobe ratios or numeric fields."""
    return media_probe_utils.parse_fps_value(raw_value)


def _probe_packet_based_fps(path: str, duration_seconds: int) -> float:
    """Estimate FPS from packet counts when stream FPS is missing."""
    return media_probe_utils.probe_packet_based_fps(
        path,
        duration_seconds,
        subprocess_module=subprocess,
    )


def _extract_duration_from_probe(info: Dict[str, Any]) -> int:
    """Extract rounded duration (seconds) from ffprobe metadata."""
    return media_probe_utils.extract_duration_from_probe(info)


def _is_image_codec_name(codec_name: str) -> bool:
    """Return whether a codec should be treated as image-like."""
    return media_probe_utils.is_image_codec_name(codec_name, image_codecs=_IMAGE_CODEC)


def _analyze_streams(streams: Any) -> tuple[bool, bool, bool, str, int, float, str]:
    """Analyze streams and return media capability flags/details."""
    return media_probe_utils.analyze_streams(
        streams,
        image_codecs=_IMAGE_CODEC,
    )


def _refine_source_fps(
    *, file: str, codec: str, duration: int, source_fps: float
) -> tuple[float, str]:
    """Refine source FPS estimation with codec-aware fallback logic."""
    return media_probe_utils.refine_source_fps(
        file=file,
        codec=codec,
        duration=duration,
        source_fps=source_fps,
        videos_dir=_VIDEOS_DIR,
        webm_video_codecs=_WEBM_VIDEO_CODECS,
        probe_packet_based_fps_fn=_probe_packet_based_fps,
    )


def get_info_video(file: str) -> dict:
    """Collect normalized source media information for encoding decisions."""
    return media_probe_utils.get_info_video(
        file,
        debug=_DEBUG,
        videos_dir=_VIDEOS_DIR,
        image_codecs=_IMAGE_CODEC,
        webm_video_codecs=_WEBM_VIDEO_CODECS,
        encode_log_fn=encode_log,
        get_info_from_video_fn=get_info_from_video,
        analyze_streams_fn=media_probe_utils.analyze_streams,
        extract_duration_from_probe_fn=_extract_duration_from_probe,
        refine_source_fps_fn=media_probe_utils.refine_source_fps,
        probe_packet_based_fps_fn=_probe_packet_based_fps,
    )


# =============================================================================
# ENCODING ORCHESTRATION FUNCTIONS
# =============================================================================


def launch_encode_video(info_video: dict, file: str) -> tuple[bool, bool]:
    """Launch video encodes (HLS/MP4) using runtime strategy."""
    return encoding_flow_utils.launch_encode_video(
        info_video,
        file,
        encoding_type=_ENCODING_TYPE,
        list_codec=_LIST_CODEC,
        select_renditions_for_encode_fn=_select_renditions_for_encode,
        nvenc_preflight_fn=_nvenc_preflight,
        encode_with_gpu_fn=encode_with_gpu,
        encode_without_gpu_fn=encode_without_gpu,
        encode_log_fn=encode_log,
    )


def launch_encode_audio(info_video: dict, file: str) -> tuple[bool, str]:
    """Launch audio derivatives encode workflow."""
    return encoding_flow_utils.launch_encode_audio(info_video, file, encode_fn=encode)


def launch_encode(info_video: dict, file: str) -> bool:
    """Launch the full encode workflow for the prepared input."""
    return encoding_flow_utils.launch_encode(
        info_video,
        file,
        encode_fn=encode,
        launch_encode_video_fn=launch_encode_video,
        launch_encode_audio_fn=launch_encode_audio,
        generate_overview_fn=generate_overview,
        add_info_video_fn=add_info_video,
        encode_log_fn=encode_log,
    )


# =============================================================================
# LOGGING AND METADATA FUNCTIONS
# =============================================================================


def encode_log(msg: str):
    """Append a message to `encoding.log` (and stdout in debug mode)."""
    metadata_runtime_utils.encode_log(msg, debug=_DEBUG, videos_output_dir=_VIDEOS_OUTPUT_DIR)


def add_info_video(key: str, value, append: bool = False):
    """Update `info_video.json` metadata for the current task."""
    metadata_runtime_utils.add_info_video(
        key,
        value,
        append=append,
        videos_output_dir=_VIDEOS_OUTPUT_DIR,
    )


# =============================================================================
# MAIN FUNCTION
# =============================================================================


def _parse_rendition_config(args, msg: str) -> str:
    """Parse and apply rendition configuration from CLI arguments."""
    global _RENDITION_CONFIG
    if args.rendition:
        try:
            rendition_override = json.loads(args.rendition)
            merged_rendition_config = _merge_rendition_config(rendition_override)
            _RENDITION_CONFIG = _validate_and_normalize_rendition_config(merged_rendition_config)
            msg += f"Rendition configuration updated: {json.dumps(_RENDITION_CONFIG, indent=2)}\n"
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            msg += f"Warning: Failed to parse rendition parameter: {e}\n"
            msg += "Using default rendition configuration\n"
            _RENDITION_CONFIG = copy.deepcopy(_DEFAULT_RENDITION_CONFIG)
    return msg


def _parse_cut_config(args, msg: str) -> str:
    """Parse and apply cut configuration from CLI arguments."""
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
    """Parse and apply dressing configuration from CLI arguments."""
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


def _parse_video_identification(args, msg: str) -> str:
    """Parse optional video identification metadata from CLI arguments."""
    global _VIDEO_IDENTIFICATION
    _VIDEO_IDENTIFICATION = {}

    optional_values = {
        "video_id": args.video_id,
        "video_slug": args.video_slug,
        "video_title": args.video_title,
    }
    for key, value in optional_values.items():
        if value is not None and str(value).strip():
            _VIDEO_IDENTIFICATION[key] = str(value)

    if _VIDEO_IDENTIFICATION:
        msg += (
            "Video identification metadata received: "
            f"{json.dumps(_VIDEO_IDENTIFICATION, ensure_ascii=False)}\n"
        )
    return msg


def _apply_cli_config(args) -> str:
    """Apply CLI options to the script's global runtime configuration."""
    msg = ""
    global _DEBUG, _VIDEOS_DIR, _VIDEOS_OUTPUT_DIR, _ENCODING_TYPE, _HWACCEL_DEVICE
    global _SOURCE_VIDEO_FPS, _AUDIO_STREAM_MAP
    _DEBUG = args.debug and args.debug.lower() == "true"
    _ENCODING_TYPE = args.encoding_type
    _SOURCE_VIDEO_FPS = 0.0
    _AUDIO_STREAM_MAP = _FALLBACK_AUDIO_STREAM_MAP
    _VIDEOS_DIR = args.base_dir or "/tmp/esup-runner/task01"
    workdir = args.work_dir or "output"
    _VIDEOS_OUTPUT_DIR = os.path.join(_VIDEOS_DIR, workdir)

    msg = _parse_rendition_config(args, msg)
    msg = _parse_cut_config(args, msg)
    msg = _parse_dressing_config(args, msg)
    msg = _parse_video_identification(args, msg)

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
    """Validate and normalize the input file before encoding."""
    msg = ""
    input_file = os.path.basename(args.input_file) if args.input_file else ""
    path_file = os.path.join(_VIDEOS_DIR, input_file)

    if not (os.path.isfile(path_file) and os.path.getsize(path_file) > 0):
        raise EncodingValidationError(f"Invalid file or path: {path_file}")

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
    """Compute the effective duration used for the encoding workflow."""
    global SUBTIME
    msg = ""
    if _CUT_CONFIG.get("start") and _CUT_CONFIG.get("end"):
        working_duration = EFFECTIVE_DURATION
        msg += f"Using effective duration from cut: {working_duration} seconds\n"
    else:
        working_duration = info_video["duration"]
        if not _DEBUG and working_duration > 0 and SUBTIME == " ":
            SUBTIME = " -ss 0 -to %s " % working_duration
    return working_duration, msg


def _validate_source_media_info(info_video: dict) -> None:
    """Validate that probed media metadata describes a readable source file."""
    if not isinstance(info_video, dict) or not info_video:
        raise EncodingValidationError(
            "Encoding aborted: source file does not appear to be a valid video file."
        )

    has_readable_media_stream = any(
        info_video.get(stream_flag, False)
        for stream_flag in ("has_stream_video", "has_stream_audio")
    )
    if not has_readable_media_stream:
        raise EncodingValidationError(
            "Encoding aborted: source file does not appear to be a valid video file "
            "(no readable audio/video streams found)."
        )


def _validate_working_duration(working_duration: int) -> None:
    """Reject zero-duration inputs before starting FFmpeg encoding jobs."""
    if working_duration > 0:
        return

    cut_start = _CUT_CONFIG.get("start")
    cut_end = _CUT_CONFIG.get("end")
    if cut_start and cut_end:
        raise EncodingValidationError(
            "Encoding aborted: effective video duration is 0 seconds after applying cut "
            f"(start={cut_start}, end={cut_end})."
        )

    raise EncodingValidationError("Encoding aborted: input video duration is 0 seconds.")


def _process_encoding(args) -> str:
    """Run the end-to-end encoding workflow for the provided CLI arguments."""
    global _SOURCE_VIDEO_FPS, _AUDIO_STREAM_MAP
    msg = ""
    filename, prep_msg = _prepare_input_file(args)
    msg += prep_msg
    if not filename:
        raise EncodingValidationError((prep_msg or "Invalid file or path").strip())

    info_video = get_info_video(filename)
    _SOURCE_VIDEO_FPS = float(info_video.get("source_fps") or 0.0)
    _AUDIO_STREAM_MAP = _build_audio_stream_map(info_video.get("audio_stream_indices"))
    if _SOURCE_VIDEO_FPS > 0:
        msg += f"Using source fps estimate for encode decisions: {_SOURCE_VIDEO_FPS:.3f}\n"
    if info_video.get("has_stream_audio", False):
        msg += f"Using audio stream mapping: {_AUDIO_STREAM_MAP}\n"
    _validate_source_media_info(info_video)
    working_duration, duration_msg = _compute_working_duration(info_video)
    msg += duration_msg
    _validate_working_duration(working_duration)

    info_video["duration"] = working_duration
    info_video["effective_duration"] = working_duration
    if EFFECTIVE_DURATION > 0:
        info_video["cut_applied"] = True
    if _VIDEO_IDENTIFICATION:
        info_video.update(_VIDEO_IDENTIFICATION)

    msg += "\n" + json.dumps(info_video, indent=2) + "\n"

    for key, value in info_video.items():
        add_info_video(key, value)

    encode_result = launch_encode(info_video, filename)
    add_info_video("encode_result", encode_result)
    if not encode_result:
        raise EncodingValidationError(
            "Encoding failed: one or more required outputs could not be generated. "
            "See encoding.log for details."
        )

    msg += "- End of encoding: %s\n" % time.ctime()
    return msg
