"""Rendition config parsing and selection helpers.

Validates rendition payloads and normalizes bitrate/resolution fields.
Builds deterministic rendition selections and metadata output structures.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict

RENDITION_KEY_RE = re.compile(r"^\d+$")
RESOLUTION_RE = re.compile(r"^(?P<width>\d+)x(?P<height>\d+)$")
BITRATE_RE = re.compile(r"^\d+(?:\.\d+)?[kKmMgG]$")


def parse_bitrate_to_bps(bitrate: str) -> int:
    """Parse a bitrate like 750k/3M/1.5M into bits per second."""
    if not isinstance(bitrate, str):
        raise ValueError("Bitrate must be a string")

    text = bitrate.strip()
    if not BITRATE_RE.fullmatch(text):
        raise ValueError(
            f"Invalid bitrate '{bitrate}'. Expected format like '750k', '3M' or '1.5M'."
        )

    unit = text[-1].lower()
    value = float(text[:-1])
    multiplier = {"k": 1000, "m": 1_000_000, "g": 1_000_000_000}[unit]
    return max(1, int(value * multiplier))


def format_bitrate_from_bps(bits_per_second: int) -> str:
    """Format a bitrate in bps into a compact FFmpeg-compatible string."""
    if bits_per_second >= 1_000_000 and bits_per_second % 1_000_000 == 0:
        return f"{bits_per_second // 1_000_000}M"
    return f"{max(1, int(round(bits_per_second / 1000)))}k"


def infer_video_bitrate(
    width: int,
    height: int,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> str:
    """Infer a video bitrate for renditions that omit video_bitrate."""
    default_1080 = default_rendition_config.get("1080", {})
    ref_resolution = str(default_1080.get("resolution", "1920x1080"))
    ref_match = RESOLUTION_RE.fullmatch(ref_resolution)
    if ref_match is None:
        ref_width = 1920
        ref_height = 1080
    else:
        ref_width = int(ref_match.group("width"))
        ref_height = int(ref_match.group("height"))

    ref_video_bitrate = str(default_1080.get("video_bitrate", "3000k"))
    ref_bps = parse_bitrate_to_bps(ref_video_bitrate)

    ref_pixels = max(1, ref_width * ref_height)
    target_pixels = max(1, width * height)
    inferred_bps = max(150_000, int(ref_bps * target_pixels / ref_pixels))
    return format_bitrate_from_bps(inferred_bps)


def infer_audio_bitrate(
    height: int,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> str:
    """Infer an audio bitrate for renditions that omit audio_bitrate."""
    ladders: list[tuple[int, str]] = []
    for rendition_key, rendition_cfg in default_rendition_config.items():
        try:
            key_height = int(rendition_key)
        except ValueError:
            continue
        audio_bitrate = str(rendition_cfg.get("audio_bitrate", "128k"))
        ladders.append((key_height, audio_bitrate))

    if not ladders:
        return "128k"

    ladders.sort(key=lambda item: item[0])
    for key_height, audio_bitrate in ladders:
        if height <= key_height:
            return audio_bitrate
    return ladders[-1][1]


def build_rate_control(
    rendition_key: str,
    video_bitrate: str,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
    legacy_rate_ladder: Dict[str, Dict[str, str]],
) -> Dict[str, str]:
    """Build FFmpeg rate-control settings for one rendition."""
    legacy = legacy_rate_ladder.get(rendition_key)
    default_video_bitrate = default_rendition_config.get(rendition_key, {}).get("video_bitrate")
    if legacy and video_bitrate == default_video_bitrate:
        return legacy

    video_bps = parse_bitrate_to_bps(video_bitrate)
    minrate = format_bitrate_from_bps(int(video_bps * 2 / 3))
    maxrate = format_bitrate_from_bps(int(video_bps * 3 / 2))
    bufsize = format_bitrate_from_bps(int(video_bps * 2))
    return {"minrate": minrate, "maxrate": maxrate, "bufsize": bufsize}


def build_rendition_rate_options(
    rendition_key: str,
    rendition_cfg: Dict[str, Any],
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
    legacy_rate_ladder: Dict[str, Dict[str, str]],
) -> str:
    """Build bitrate-related FFmpeg options for one rendition."""
    video_bitrate = rendition_cfg["video_bitrate"]
    audio_bitrate = rendition_cfg["audio_bitrate"]
    rate_control = build_rate_control(
        rendition_key,
        video_bitrate,
        default_rendition_config=default_rendition_config,
        legacy_rate_ladder=legacy_rate_ladder,
    )
    return (
        f"-b:a {audio_bitrate} -minrate {rate_control['minrate']} "
        f"-b:v {video_bitrate} -maxrate {rate_control['maxrate']} "
        f"-bufsize {rate_control['bufsize']} "
    )


def merge_rendition_config(
    overrides: Dict[str, Any],
    *,
    rendition_config: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Merge CLI rendition overrides into the current rendition configuration."""
    if not isinstance(overrides, dict):
        raise ValueError("Rendition configuration must be a JSON object")

    merged = copy.deepcopy(rendition_config)
    for raw_key, raw_value in overrides.items():
        key = str(raw_key).strip()
        if not key:
            raise ValueError("Rendition key cannot be empty")

        if raw_value is None:
            merged.pop(key, None)
            continue

        if not isinstance(raw_value, dict):
            raise ValueError(f"Rendition '{key}' must be an object")

        current = merged.get(key, {})
        if not isinstance(current, dict):
            current = {}
        merged[key] = {**current, **raw_value}

    return merged


def validate_rendition_key_and_cfg(raw_key: Any, raw_cfg: Any) -> tuple[str, Dict[str, Any]]:
    """Validate a rendition key and its payload shape."""
    key = str(raw_key).strip()
    if not RENDITION_KEY_RE.fullmatch(key):
        raise ValueError(
            f"Invalid rendition key '{raw_key}'. Use a numeric height key (e.g. '360', '720', '2160')."
        )
    if not isinstance(raw_cfg, dict):
        raise ValueError(f"Rendition '{key}' must be an object")
    return key, raw_cfg


def parse_rendition_resolution(key: str, raw_cfg: Dict[str, Any]) -> tuple[int, int]:
    """Validate and parse the rendition resolution field."""
    resolution = raw_cfg.get("resolution")
    if not isinstance(resolution, str):
        raise ValueError(f"Rendition '{key}' missing required string field 'resolution'")

    resolution_text = resolution.strip()
    match = RESOLUTION_RE.fullmatch(resolution_text)
    if match is None:
        raise ValueError(
            f"Rendition '{key}' has invalid resolution '{resolution}'. Expected 'WIDTHxHEIGHT'."
        )

    width = int(match.group("width"))
    height = int(match.group("height"))
    if width <= 0 or height <= 0:
        raise ValueError(f"Rendition '{key}' resolution must contain positive integers")

    key_height = int(key)
    if key_height != height:
        raise ValueError(f"Rendition '{key}' must match resolution height ({height}).")

    return width, height


def normalize_video_bitrate(
    key: str,
    raw_cfg: Dict[str, Any],
    width: int,
    height: int,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> str:
    """Return a validated (or inferred) video bitrate for one rendition."""
    video_bitrate = raw_cfg.get("video_bitrate")
    if video_bitrate is None:
        video_bitrate = infer_video_bitrate(
            width,
            height,
            default_rendition_config=default_rendition_config,
        )
    elif not isinstance(video_bitrate, str):
        raise ValueError(f"Rendition '{key}' field 'video_bitrate' must be a string")

    parse_bitrate_to_bps(video_bitrate)
    return video_bitrate.strip()


def normalize_audio_bitrate(
    key: str,
    raw_cfg: Dict[str, Any],
    height: int,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> str:
    """Return a validated (or inferred) audio bitrate for one rendition."""
    audio_bitrate = raw_cfg.get("audio_bitrate")
    if audio_bitrate is None:
        audio_bitrate = infer_audio_bitrate(
            height,
            default_rendition_config=default_rendition_config,
        )
    elif not isinstance(audio_bitrate, str):
        raise ValueError(f"Rendition '{key}' field 'audio_bitrate' must be a string")

    parse_bitrate_to_bps(audio_bitrate)
    return audio_bitrate.strip()


def normalize_encode_mp4(key: str, raw_cfg: Dict[str, Any]) -> bool:
    """Validate and return the encode_mp4 flag for one rendition."""
    encode_mp4 = raw_cfg.get("encode_mp4")
    if not isinstance(encode_mp4, bool):
        raise ValueError(f"Rendition '{key}' field 'encode_mp4' must be a boolean")
    return encode_mp4


def normalize_rendition_entry(
    raw_key: Any,
    raw_cfg: Any,
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> tuple[str, Dict[str, Any]]:
    """Validate and normalize a single rendition entry."""
    key, cfg = validate_rendition_key_and_cfg(raw_key, raw_cfg)
    width, height = parse_rendition_resolution(key, cfg)
    video_bitrate = normalize_video_bitrate(
        key,
        cfg,
        width,
        height,
        default_rendition_config=default_rendition_config,
    )
    audio_bitrate = normalize_audio_bitrate(
        key,
        cfg,
        height,
        default_rendition_config=default_rendition_config,
    )
    encode_mp4 = normalize_encode_mp4(key, cfg)
    return (
        key,
        {
            "resolution": f"{width}x{height}",
            "video_bitrate": video_bitrate,
            "audio_bitrate": audio_bitrate,
            "encode_mp4": encode_mp4,
        },
    )


def validate_and_normalize_rendition_config(
    config: Dict[str, Any],
    *,
    default_rendition_config: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """Validate rendition config and return a normalized numeric-order mapping."""
    if not isinstance(config, dict):
        raise ValueError("Rendition configuration must be an object")
    if not config:
        raise ValueError("Rendition configuration cannot be empty")

    normalized: Dict[str, Dict[str, Any]] = {}
    for raw_key, raw_cfg in config.items():
        key, entry = normalize_rendition_entry(
            raw_key,
            raw_cfg,
            default_rendition_config=default_rendition_config,
        )
        normalized[key] = entry

    return {k: normalized[k] for k in sorted(normalized, key=lambda key: int(key))}


def select_renditions_for_encode(
    *,
    rendition_config: Dict[str, Dict[str, Any]],
    source_height: int,
    output_format: str,
) -> list[tuple[str, Dict[str, Any], int]]:
    """Select renditions to encode for the source height and format."""
    renditions = sorted(rendition_config.items(), key=lambda item: int(item[0]))
    selected: list[tuple[str, Dict[str, Any], int]] = []

    for idx, (rendition_key, rendition_cfg) in enumerate(renditions):
        rendition_height = int(rendition_key)
        if idx > 0 and source_height < rendition_height:
            continue
        if output_format == "mp4" and not rendition_cfg.get("encode_mp4", True):
            continue
        selected.append((rendition_key, rendition_cfg, rendition_height))

    return selected


def build_video_output_segment(
    *,
    output_format: str,
    rendition_key: str,
    rendition_cfg: Dict[str, Any],
    output_basename: str,
    videos_output_dir: str,
    default_rendition_config: Dict[str, Dict[str, Any]],
    legacy_rate_ladder: Dict[str, Dict[str, str]],
    hls_output_options: str,
    mp4_output_options: str,
) -> str:
    """Build one FFmpeg output segment for a rendition."""
    rate_options = build_rendition_rate_options(
        rendition_key,
        rendition_cfg,
        default_rendition_config=default_rendition_config,
        legacy_rate_ladder=legacy_rate_ladder,
    )
    output_path = f"{videos_output_dir}/{rendition_key}p_{output_basename}.{output_format}"
    if output_format == "m3u8":
        return f'{rate_options}{hls_output_options}"{output_path}" '
    return f'{rate_options}{mp4_output_options}"{output_path}" '


def build_video_metadata_entries(
    *,
    rendition_config: Dict[str, Dict[str, Any]],
    output_format: str,
    source_height: int,
    output_basename: str,
) -> list[Dict[str, object]]:
    """Return metadata entries for all renditions encoded in a video job."""
    entries: list[Dict[str, object]] = []
    selected = select_renditions_for_encode(
        rendition_config=rendition_config,
        source_height=source_height,
        output_format=output_format,
    )
    for rendition_key, rendition_cfg, _ in selected:
        entries.append(
            {
                "encoding_format": "video/mp2t" if output_format == "m3u8" else "video/mp4",
                "rendition": rendition_cfg["resolution"],
                "filename": f"{rendition_key}p_{output_basename}.{output_format}",
            }
        )
    return entries
