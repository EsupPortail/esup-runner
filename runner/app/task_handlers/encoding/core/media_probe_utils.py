"""Media probing helpers for the encoding runtime pipeline.

Collects and normalizes ffprobe metadata for source media analysis.
Provides stream/fps/duration utilities reused by orchestration wrappers.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from typing import Any, Dict, Optional, Union

DurationValue = Union[str, int, float, None]


def get_info_from_video(
    probe_cmd: str,
    *,
    subprocess_module=subprocess,
) -> tuple[Optional[dict], str]:
    """Execute ffprobe command to get video metadata."""
    info: Optional[dict] = None
    msg = ""
    try:
        output = subprocess_module.check_output(
            shlex.split(probe_cmd), stderr=subprocess_module.PIPE
        )
        info = json.loads(output)
    except subprocess_module.CalledProcessError as e:
        msg += 20 * "////" + "\n"
        msg += "Runtime Error: {0}\n".format(e)
    except OSError as err:
        msg += 20 * "////" + "\n"
        msg += "OS error: {0}\n".format(err)
    return info, msg


def seconds_from_timestamp(value: str) -> float:
    """Parse ``MM:SS`` or ``HH:MM:SS(.ms)`` into seconds."""
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return 0.0
    try:
        nums = [float(part) for part in parts]
    except (TypeError, ValueError):
        return 0.0
    if len(nums) == 2:
        minutes, seconds = nums
        return minutes * 60 + seconds
    hours, minutes, seconds = nums
    return hours * 3600 + minutes * 60 + seconds


def duration_seconds_from_value(value: DurationValue) -> float:
    """Convert a ffprobe-like duration value into seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except (TypeError, ValueError):
        return seconds_from_timestamp(s)


def parse_fps_value(raw_value: Any) -> float:
    """Parse an ffprobe frame-rate value like ``30000/1001`` into a float."""
    if raw_value is None:
        return 0.0
    text = str(raw_value).strip()
    if not text or text == "0/0":
        return 0.0
    if "/" in text:
        parts = text.split("/", 1)
        try:
            num = float(parts[0])
            den = float(parts[1])
            if den <= 0:
                return 0.0
            return num / den
        except (TypeError, ValueError):
            return 0.0
    try:
        value = float(text)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def probe_packet_based_fps(
    path: str,
    duration_seconds: int,
    *,
    subprocess_module=subprocess,
) -> float:
    """Estimate fps from packet count when stream metadata is unreliable."""
    if duration_seconds <= 0:
        return 0.0
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_packets",
        "-show_entries",
        "stream=nb_read_packets",
        "-of",
        "default=nw=1:nk=1",
        path,
    ]
    try:
        out = (
            subprocess_module.check_output(cmd, stderr=subprocess_module.STDOUT)
            .decode("utf-8")
            .strip()
        )
        packet_count = int(out) if out else 0
    except Exception:
        packet_count = 0
    if packet_count <= 0:
        return 0.0
    return float(packet_count) / float(duration_seconds)


def extract_duration_from_probe(info: Dict[str, Any]) -> int:
    """Extract best available duration (in whole seconds) from ffprobe JSON."""
    if not isinstance(info, dict):
        return 0

    candidates: list[DurationValue] = []
    format_info = info.get("format")
    if isinstance(format_info, dict):
        candidates.append(format_info.get("duration"))
        tags = format_info.get("tags")
        if isinstance(tags, dict):
            candidates.append(tags.get("DURATION"))
            candidates.append(tags.get("duration"))

    streams = info.get("streams")
    if isinstance(streams, list):
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            candidates.append(stream.get("duration"))
            tags = stream.get("tags")
            if isinstance(tags, dict):
                candidates.append(tags.get("DURATION"))
                candidates.append(tags.get("duration"))

    max_duration = max((duration_seconds_from_value(v) for v in candidates), default=0.0)
    return int(max_duration) if max_duration > 0 else 0


def extract_primary_video_duration_from_probe(
    info: Dict[str, Any],
    *,
    image_codecs: list[str],
) -> float:
    """Extract the first non-image video stream duration from ffprobe JSON."""
    if not isinstance(info, dict):
        return 0.0

    streams = info.get("streams")
    if not isinstance(streams, list):
        return 0.0

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") != "video":
            continue
        if is_image_codec_name(str(stream.get("codec_name", "")), image_codecs=image_codecs):
            continue

        candidates: list[DurationValue] = [stream.get("duration")]
        tags = stream.get("tags")
        if isinstance(tags, dict):
            candidates.append(tags.get("DURATION"))
            candidates.append(tags.get("duration"))

        duration = max((duration_seconds_from_value(v) for v in candidates), default=0.0)
        return duration if duration > 0 else 0.0

    return 0.0


def is_image_codec_name(codec_name: str, *, image_codecs: list[str]) -> bool:
    """Return whether a codec name should be treated as image-only."""
    codec_text = str(codec_name or "").lower()
    return any(ext in codec_text for ext in image_codecs)


def analyze_streams(
    streams: Any,
    *,
    image_codecs: list[str],
) -> tuple[bool, bool, bool, str, int, float, str]:
    """Analyze ffprobe streams and return media flags + primary video metadata."""
    has_stream_video = False
    has_stream_thumbnail = False
    has_stream_audio = False
    codec = ""
    height = 0
    source_fps = 0.0
    stream_log = ""
    selected_primary_video = False

    if not isinstance(streams, list):
        return (
            has_stream_video,
            has_stream_thumbnail,
            has_stream_audio,
            codec,
            height,
            source_fps,
            stream_log,
        )

    for stream in streams:
        if not isinstance(stream, dict):
            continue

        stream_type = stream.get("codec_type", "unknown")
        codec_name = stream.get("codec_name", "unknown")
        stream_log += f"{stream_type}: {codec_name}\n"

        if stream_type == "audio":
            has_stream_audio = True
            continue
        if stream_type != "video":
            continue
        if is_image_codec_name(codec_name, image_codecs=image_codecs):
            has_stream_thumbnail = True
            continue

        has_stream_video = True
        has_stream_thumbnail = True
        if selected_primary_video:
            continue

        codec = codec_name
        height = stream.get("height", 0)
        avg_fps = parse_fps_value(stream.get("avg_frame_rate"))
        real_fps = parse_fps_value(stream.get("r_frame_rate"))
        source_fps = avg_fps if avg_fps > 0 else real_fps
        selected_primary_video = True

    return (
        has_stream_video,
        has_stream_thumbnail,
        has_stream_audio,
        codec,
        height,
        source_fps,
        stream_log,
    )


def extract_primary_video_encoding_metadata(
    streams: Any,
    *,
    image_codecs: list[str],
) -> tuple[str, str]:
    """Return ``(profile, pix_fmt)`` for the first non-image video stream."""
    if not isinstance(streams, list):
        return "", ""

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        if stream.get("codec_type") != "video":
            continue
        codec_name = str(stream.get("codec_name", ""))
        if is_image_codec_name(codec_name, image_codecs=image_codecs):
            continue
        return str(stream.get("profile") or ""), str(stream.get("pix_fmt") or "")

    return "", ""


def refine_source_fps(
    *,
    file: str,
    codec: str,
    duration: int,
    source_fps: float,
    videos_dir: str,
    webm_video_codecs: set[str],
    probe_packet_based_fps_fn,
) -> tuple[float, str]:
    """Refine source fps and return ``(fps, log_line)``."""
    if not codec:
        return source_fps, ""

    if codec.lower() in webm_video_codecs:
        source_path = os.path.join(videos_dir, file)
        packet_fps = probe_packet_based_fps_fn(source_path, duration)
        if packet_fps > 0:
            return packet_fps, f"webm packet-based fps estimate: {packet_fps:.3f}\n"
        if source_fps > 0:
            return source_fps, f"webm stream fps estimate: {source_fps:.3f}\n"
        return source_fps, ""

    if source_fps > 0:
        return source_fps, f"stream fps estimate: {source_fps:.3f}\n"
    return source_fps, ""


def get_info_video(
    file: str,
    *,
    debug: bool,
    videos_dir: str,
    image_codecs: list[str],
    webm_video_codecs: set[str],
    encode_log_fn,
    get_info_from_video_fn,
    analyze_streams_fn,
    extract_duration_from_probe_fn,
    refine_source_fps_fn,
    probe_packet_based_fps_fn,
    extract_primary_video_duration_from_probe_fn=extract_primary_video_duration_from_probe,
) -> dict:
    """Extract comprehensive stream/duration information for an input file."""
    if debug:
        print(os.environ["PATH"])

    msg = "--> get_info_video\n"
    probe_cmd = (
        "ffprobe -v quiet -show_format -show_streams " "-print_format json -i {}/{}"
    ).format(videos_dir, file)
    msg += probe_cmd + "\n"

    info, return_msg = get_info_from_video_fn(probe_cmd)
    msg += json.dumps(info, indent=2) + "\n"
    msg += return_msg + "\n"

    if debug:
        print("Probe_cmd : " + probe_cmd)
        print("return_msg : " + return_msg)

    if info is None:
        msg += "\nError: Failed to get video information\n"
        return {}

    duration = extract_duration_from_probe_fn(info)
    video_duration = extract_primary_video_duration_from_probe_fn(
        info,
        image_codecs=image_codecs,
    )
    if duration <= 0:
        msg += "Warning: duration unavailable in ffprobe metadata; defaulting to 0\n"

    (
        has_stream_video,
        has_stream_thumbnail,
        has_stream_audio,
        codec,
        height,
        source_fps,
        stream_log,
    ) = analyze_streams_fn(info.get("streams", []), image_codecs=image_codecs)
    msg += stream_log
    video_profile, pix_fmt = extract_primary_video_encoding_metadata(
        info.get("streams", []),
        image_codecs=image_codecs,
    )

    if has_stream_video:
        source_fps, fps_log = refine_source_fps_fn(
            file=file,
            codec=codec,
            duration=duration,
            source_fps=source_fps,
            videos_dir=videos_dir,
            webm_video_codecs=webm_video_codecs,
            probe_packet_based_fps_fn=probe_packet_based_fps_fn,
        )
        msg += fps_log

    encode_log_fn(msg)

    return {
        "has_stream_video": has_stream_video,
        "has_stream_thumbnail": has_stream_thumbnail,
        "has_stream_audio": has_stream_audio,
        "codec": codec,
        "height": height,
        "duration": duration,
        "video_duration": video_duration,
        "source_fps": source_fps,
        "profile": video_profile,
        "pix_fmt": pix_fmt,
    }
