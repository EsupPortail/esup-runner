"""FFmpeg command builders for encoding jobs.

Generates deterministic CPU/GPU command strings from runtime settings.
Keeps mapping, scaling, and output-segment composition outside orchestration.
"""

from __future__ import annotations

import os
from typing import Any, Dict


def build_audio_stream_map(
    audio_stream_indices: Any,
    *,
    fallback_map: str,
) -> str:
    """Build optional absolute audio mappings or return the primary-audio fallback."""
    if not isinstance(audio_stream_indices, list) or not audio_stream_indices:
        return fallback_map

    normalized_indices: list[int] = []
    for stream_index in audio_stream_indices:
        if type(stream_index) is not int or stream_index < 0:
            return fallback_map
        if stream_index not in normalized_indices:
            normalized_indices.append(stream_index)

    return " ".join(f"-map 0:{stream_index}?" for stream_index in normalized_indices)


def is_webm_source(
    *, file: str, codec: str, webm_extensions: set[str], webm_video_codecs: set[str]
) -> bool:
    """Return whether the input is a WebM source."""
    ext = os.path.splitext(str(file))[1].lower()
    if ext in webm_extensions:
        return True
    return (codec or "").strip().lower() in webm_video_codecs


def build_fps_mode_options(
    *,
    is_webm: bool,
    source_video_fps: float,
    webm_output_fps: int,
    webm_min_output_fps: int,
    webm_max_output_fps: int,
) -> str:
    """Return FPS mode options for the source type."""
    if is_webm:
        fps = webm_output_fps
        if source_video_fps > 0:
            fps = int(round(source_video_fps))
            fps = min(webm_max_output_fps, max(webm_min_output_fps, fps))
        return f"-fps_mode cfr -r {fps} "
    return "-fps_mode passthrough "


def build_nvenc_rate_control_options(*, is_webm: bool) -> str:
    """Return NVENC rate-control overrides for WebM sources."""
    if not is_webm:
        return ""
    return "-rc cbr -cbr 1 -spatial-aq 1 -aq-strength 8 -temporal-aq 1 -qmin 0 -qmax 35 "


def build_cpu_quality_options(*, is_webm: bool) -> str:
    """Return libx264 quality-bound overrides for WebM sources."""
    if not is_webm:
        return ""
    return "-qmin 0 -qmax 35 "


def get_cmd_gpu(
    format: str,
    codec: str,
    height: int,
    file: str,
    *,
    videos_dir: str,
    hwaccel_device: int,
    subtime: str,
    source_video_fps: float,
    audio_stream_map: str,
    gpu_template: str,
    scale_gpu_template: str,
    webm_extensions: set[str],
    webm_video_codecs: set[str],
    webm_output_fps: int,
    webm_min_output_fps: int,
    webm_max_output_fps: int,
    sanitize_filename_fn,
    select_renditions_for_encode_fn,
    build_video_output_segment_fn,
) -> str:
    """Generate GPU FFmpeg command for requested output format."""
    ffmpeg_cmd = gpu_template.format(
        hwaccel_device=hwaccel_device,
        codec=codec,
        input=os.path.join(videos_dir, file),
    )
    is_webm = is_webm_source(
        file=file,
        codec=codec,
        webm_extensions=webm_extensions,
        webm_video_codecs=webm_video_codecs,
    )
    fps_mode_options = build_fps_mode_options(
        is_webm=is_webm,
        source_video_fps=source_video_fps,
        webm_output_fps=webm_output_fps,
        webm_min_output_fps=webm_min_output_fps,
        webm_max_output_fps=webm_max_output_fps,
    )
    nvenc_rate_control_options = build_nvenc_rate_control_options(is_webm=is_webm)

    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename_fn(filename)
    selected = select_renditions_for_encode_fn(source_height=height, output_format=format)
    for rendition_key, rendition_cfg, rendition_height in selected:
        ffmpeg_cmd += scale_gpu_template.format(
            height=rendition_height,
            audio_stream_map=audio_stream_map,
            fps_mode_options=fps_mode_options,
            nvenc_rate_control_options=nvenc_rate_control_options,
        )
        ffmpeg_cmd += subtime + build_video_output_segment_fn(
            output_format=format,
            rendition_key=rendition_key,
            rendition_cfg=rendition_cfg,
            output_basename=filename,
        )
    return ffmpeg_cmd


def get_cmd_cpu(
    format: str,
    codec: str,
    height: int,
    file: str,
    *,
    videos_dir: str,
    subtime: str,
    source_video_fps: float,
    audio_stream_map: str,
    cpu_template: str,
    scale_cpu_template: str,
    webm_extensions: set[str],
    webm_video_codecs: set[str],
    webm_output_fps: int,
    webm_min_output_fps: int,
    webm_max_output_fps: int,
    choose_h264_encoder_fn,
    sanitize_filename_fn,
    select_renditions_for_encode_fn,
    build_video_output_segment_fn,
) -> str:
    """Generate CPU FFmpeg command for requested output format."""
    encoder, _ = choose_h264_encoder_fn()
    ffmpeg_cmd = cpu_template.format(codec=codec, input=os.path.join(videos_dir, file))
    is_webm = is_webm_source(
        file=file,
        codec=codec,
        webm_extensions=webm_extensions,
        webm_video_codecs=webm_video_codecs,
    )
    fps_mode_options = build_fps_mode_options(
        is_webm=is_webm,
        source_video_fps=source_video_fps,
        webm_output_fps=webm_output_fps,
        webm_min_output_fps=webm_min_output_fps,
        webm_max_output_fps=webm_max_output_fps,
    )
    cpu_quality_options = build_cpu_quality_options(is_webm=is_webm)

    filename = os.path.splitext(os.path.basename(file))[0]
    filename = sanitize_filename_fn(filename)
    selected = select_renditions_for_encode_fn(source_height=height, output_format=format)
    for rendition_key, rendition_cfg, rendition_height in selected:
        ffmpeg_cmd += scale_cpu_template.format(
            height=rendition_height,
            encoder=encoder,
            audio_stream_map=audio_stream_map,
            fps_mode_options=fps_mode_options,
            cpu_quality_options=cpu_quality_options,
        )
        ffmpeg_cmd += subtime + build_video_output_segment_fn(
            output_format=format,
            rendition_key=rendition_key,
            rendition_cfg=rendition_cfg,
            output_basename=filename,
        )
    return ffmpeg_cmd


def build_encode_video_job(
    *,
    encoder_type: str,
    format: str,
    codec: str,
    height: int,
    file: str,
    filename: str,
    get_cmd_gpu_fn,
    get_cmd_cpu_fn,
    build_video_metadata_entries_fn,
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build FFmpeg command and metadata for a video encode job."""
    if encoder_type == "gpu":
        ffmpeg_cmd = get_cmd_gpu_fn(format, codec, height, file)
    else:
        ffmpeg_cmd = get_cmd_cpu_fn(format, codec, height, file)

    metadata_entries = build_video_metadata_entries_fn(
        output_format=format,
        source_height=height,
        output_basename=filename,
    )
    if not metadata_entries:
        return (
            "",
            "encode_video",
            {},
            True,
            {
                "skip_execution": True,
                "skip_reason": f"No rendition to encode for format '{format}'",
            },
        )

    return (
        ffmpeg_cmd,
        "encode_video",
        metadata_entries[0],
        True,
        {"additional_renditions": metadata_entries[1:]},
    )


def build_encode_audio_job(
    *,
    kind: str,
    file: str,
    filename: str,
    videos_dir: str,
    videos_output_dir: str,
    mp3_template: str,
    m4a_template: str,
    subtime: str = " ",
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build FFmpeg command and metadata for an audio encode job."""
    audio_subtime = subtime or " "
    if kind == "mp3":
        ffmpeg_cmd = mp3_template.format(
            input=os.path.join(videos_dir, file),
            output_dir=videos_output_dir,
            output=filename,
            subtime=audio_subtime,
        )
        add_info_video_content: Dict[str, object] = {
            "encoding_format": "audio/mp3",
            "filename": "audio_192k_{output}.mp3".format(output=filename),
        }
    else:
        ffmpeg_cmd = m4a_template.format(
            input=os.path.join(videos_dir, file),
            output_dir=videos_output_dir,
            output=filename,
            subtime=audio_subtime,
        )
        add_info_video_content = {
            "encoding_format": "video/mp4",
            "filename": "audio_192k_{output}.m4a".format(output=filename),
        }
    return ffmpeg_cmd, "encode_audio", add_info_video_content, True, {}


def build_encode_thumbnail_job(
    *,
    file: str,
    filename: str,
    duration: float,
    thumbnail_index: int,
    videos_dir: str,
    videos_output_dir: str,
    thumbnail_templates: list[str],
) -> tuple[str, str, Dict[str, object], bool, Dict[str, Any]]:
    """Build FFmpeg command and metadata for thumbnail extraction."""
    percentages = [0.25, 0.50, 0.75]
    timestamp = int(duration * percentages[thumbnail_index]) if duration > 0 else 0
    input_path = os.path.join(videos_dir, file)
    output_path = os.path.join(videos_output_dir, f"{filename}_{thumbnail_index}.png")
    ffmpeg_cmd = thumbnail_templates[thumbnail_index].format(
        timestamp=timestamp,
        output_dir=videos_output_dir,
        input=input_path,
        filename=filename,
        ext="png",
    )
    fallback_cmd = thumbnail_templates[thumbnail_index].format(
        timestamp=0,
        output_dir=videos_output_dir,
        input=input_path,
        filename=filename,
        ext="png",
    )
    add_info_video_content: Dict[str, object] = {
        "filename": f"{filename}_{thumbnail_index}.png",
        "timestamp": timestamp,
        "percentage": int(percentages[thumbnail_index] * 100),
    }
    extra: Dict[str, Any] = {
        "templates": thumbnail_templates,
        "timestamp": timestamp,
        "output_path": output_path,
        "fallback_cmd": fallback_cmd if timestamp > 0 else "",
    }
    return ffmpeg_cmd, "encode_thumbnail", add_info_video_content, True, extra
