"""Encoding business-flow orchestration helpers.

Coordinates video/audio/thumbnail/overview execution decisions.
Centralizes orchestration logic so command builders stay focused and testable.
"""

from __future__ import annotations

import os
from typing import Callable


def encode_with_gpu(
    format: str,
    codec: str,
    height: int,
    file: str,
    *,
    encode_fn,
    encode_log_fn,
) -> bool:
    """Try GPU encode first, then CPU fallback."""
    msg = "--> encode_with_gpu \n"
    return_value = False
    if encode_fn("gpu", format, codec, height, file):
        msg += "Encode GPU %s ok \n" % format
        return_value = True
    elif encode_fn("cpu", format, codec, height, file):
        msg += "Encode CPU %s ok \n" % format
        return_value = True

    if not return_value:
        msg += 20 * "////" + "\n"
        msg += "ERROR ENCODING %s FOR FILE %s \n" % (format, file)
    encode_log_fn(msg)
    return return_value


def encode_without_gpu(
    format: str,
    codec: str,
    height: int,
    file: str,
    *,
    encode_fn,
    encode_log_fn,
) -> bool:
    """Encode with CPU only."""
    msg = "--> encode_without_gpu \n"
    return_value = False
    if encode_fn("cpu", format, codec, height, file):
        msg += "Encode CPU %s ok \n" % format
        return_value = True
    else:
        msg += 20 * "////" + "\n"
        msg += "ERROR ENCODING %s FOR FILE %s \n" % (format, file)
    encode_log_fn(msg)
    return return_value


def encode(
    type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    duration: int = 0,
    thumbnail_index: int = 0,
    *,
    sanitize_filename_fn,
    build_encode_video_job_fn,
    build_encode_audio_job_fn,
    build_encode_thumbnail_job_fn,
    launch_cmd_fn: Callable[[str, str, str], tuple[bool, str]],
    add_info_video_fn,
    encode_log_fn,
) -> bool:
    """Route encoding job creation/execution by type."""
    msg = "--> encode\n"
    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename_fn(filename)

    builders = {
        "gpu": lambda: build_encode_video_job_fn(
            encoder_type="gpu",
            format=format,
            codec=codec,
            height=height,
            file=file,
            filename=filename,
        ),
        "cpu": lambda: build_encode_video_job_fn(
            encoder_type="cpu",
            format=format,
            codec=codec,
            height=height,
            file=file,
            filename=filename,
        ),
        "mp3": lambda: build_encode_audio_job_fn(kind="mp3", file=file, filename=filename),
        "m4a": lambda: build_encode_audio_job_fn(kind="m4a", file=file, filename=filename),
        "thumbnail": lambda: build_encode_thumbnail_job_fn(
            file=file,
            filename=filename,
            duration=duration,
            thumbnail_index=thumbnail_index,
        ),
    }

    builder = builders.get(type)
    if builder is None:
        msg += "Unknown encoding type: %s\n" % type
        encode_log_fn(msg)
        return False

    ffmpeg_cmd, add_title, add_content, add_append, extra = builder()
    if extra.get("skip_execution", False):
        reason = str(extra.get("skip_reason", "Skipping encode execution"))
        encode_log_fn(msg + reason + "\n")
        return True

    if type == "thumbnail":
        return_value, return_msg = launch_cmd_fn(ffmpeg_cmd, "thumbnail", format)
    else:
        return_value, return_msg = launch_cmd_fn(ffmpeg_cmd, type, format)

    if return_value and add_title and add_content:
        add_info_video_fn(add_title, add_content, add_append)
        if add_title == "encode_video":
            for rendition_entry in extra.get("additional_renditions", []):
                add_info_video_fn(add_title, rendition_entry, True)

    encode_log_fn(msg + return_msg)
    return return_value


def launch_encode_video(
    info_video: dict,
    file: str,
    *,
    encoding_type: str,
    list_codec: tuple[str, ...],
    select_renditions_for_encode_fn,
    nvenc_preflight_fn,
    encode_with_gpu_fn,
    encode_without_gpu_fn,
    encode_log_fn,
) -> tuple[bool, bool]:
    """Launch video encoding depending on codec + runtime capabilities."""
    codec = info_video.get("codec", "")
    height = info_video.get("height", 0)
    mp4_renditions = select_renditions_for_encode_fn(source_height=height, output_format="mp4")
    should_encode_mp4 = bool(mp4_renditions)

    if encoding_type.upper() == "GPU" and codec in list_codec:
        nvenc_ok, nvenc_details = nvenc_preflight_fn()
        if not nvenc_ok:
            encode_log_fn(
                "NVENC unavailable; falling back to CPU. "
                "(Typical fix: update NVIDIA driver to match the FFmpeg NVENC API requirement)\n"
                + nvenc_details
            )
            encode_m3u8 = encode_without_gpu_fn("m3u8", codec, height, file)
            encode_mp4 = (
                encode_without_gpu_fn("mp4", codec, height, file) if should_encode_mp4 else True
            )
        else:
            encode_m3u8 = encode_with_gpu_fn("m3u8", codec, height, file)
            encode_mp4 = (
                encode_with_gpu_fn("mp4", codec, height, file) if should_encode_mp4 else True
            )
    else:
        encode_m3u8 = encode_without_gpu_fn("m3u8", codec, height, file)
        encode_mp4 = (
            encode_without_gpu_fn("mp4", codec, height, file) if should_encode_mp4 else True
        )

    if not should_encode_mp4:
        encode_log_fn(
            f"Skipping mp4 encode: no enabled rendition for source height {height}. "
            "Set encode_mp4=true on at least one selected rendition to enable mp4 output.\n"
        )
    return encode_m3u8, encode_mp4


def launch_encode_audio(info_video: dict, file: str, *, encode_fn) -> tuple[bool, str]:
    """Launch audio derivative encoding."""
    encode_audio = True
    msg = ""
    if not info_video.get("has_stream_video", False):
        if encode_fn("m4a", "", "", 0, file):
            msg += "encode m4a ok\n"
        else:
            encode_audio = False
            msg += 20 * "////" + "\n"
            msg += "error m4a"
    if encode_fn("mp3", "", "", 0, file):
        msg += "encode mp3 ok\n"
    else:
        encode_audio = False
        msg += 20 * "////" + "\n"
        msg += "error mp3\n"
    return encode_audio, msg


def launch_encode(
    info_video: dict,
    file: str,
    *,
    encode_fn,
    launch_encode_video_fn,
    launch_encode_audio_fn,
    generate_overview_fn,
    add_info_video_fn,
    encode_log_fn,
) -> bool:
    """Orchestrate end-to-end encoding jobs."""
    msg = "--> launch_encode\n"

    encode_m3u8 = encode_mp4 = True
    if info_video.get("has_stream_video", False):
        encode_m3u8, encode_mp4 = launch_encode_video_fn(info_video, file)

    encode_thumbnail = True
    if info_video.get("has_stream_thumbnail", False):
        duration = info_video.get("duration", 0)
        for i in range(3):
            if encode_fn("thumbnail", "png", "", 0, file, duration, i):
                msg += f"thumbnail {i} ok\n"
            else:
                encode_thumbnail = False
                msg += 20 * "////" + "\n"
                msg += f"error thumbnail {i}\n"

    encode_overview = True
    if info_video.get("has_stream_video", False):
        duration = info_video.get("duration", 0)
        overview_success, overview_msg = generate_overview_fn(file, duration)
        msg += overview_msg
        if overview_success:
            msg += "overview generation ok\n"
            add_info_video_fn(
                "encode_overview",
                {"sprite_filename": "overview.png", "vtt_filename": "overview.vtt"},
            )
        else:
            encode_overview = False
            msg += 20 * "////" + "\n"
            msg += "error generating overview\n"

    encode_audio = True
    if info_video.get("has_stream_audio", False):
        encode_audio, return_msg = launch_encode_audio_fn(info_video, file)
        msg += return_msg

    encode_log_fn(msg)
    return all([encode_audio, encode_thumbnail, encode_overview, encode_m3u8, encode_mp4])
