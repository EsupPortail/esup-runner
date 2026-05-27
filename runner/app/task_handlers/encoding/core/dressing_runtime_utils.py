"""Dressing-stage helpers for the encoding runtime pipeline.

Builds reusable operations for cut/watermark/credits transformations.
Keeps FFmpeg dressing steps isolated from the top-level orchestration flow.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Optional


def safe_filename_from_url(url: str, *, sanitize_filename_fn) -> str:
    """Return a sanitized filename derived from a URL."""
    parsed = urllib.parse.urlparse(url)
    name = os.path.basename(parsed.path) or "asset"
    name = sanitize_filename_fn(name)
    if not name:
        name = "asset"
    return name


def download_allowed_hosts_from_env() -> list[str]:
    """Return the configured allowlist of download hosts."""
    allowed_hosts_raw = os.getenv("DOWNLOAD_ALLOWED_HOSTS", "")
    return [h.strip().lower().rstrip(".") for h in allowed_hosts_raw.split(",") if h.strip()]


def download_allow_private_networks_from_env() -> bool:
    """Return whether private-network downloads are allowed."""
    allow_private_raw = os.getenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    return allow_private_raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Return whether a host matches the configured allowlist."""
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def validate_host_resolves_to_public_ip(host: str) -> None:
    """Reject hosts that do not resolve to public IP addresses."""
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


def download_url_to_dir(
    url: str,
    target_dir: str,
    prefix: str,
    *,
    sanitize_filename_fn,
) -> str:
    """Download a remote asset to target_dir and return local absolute path."""
    os.makedirs(target_dir, exist_ok=True)

    parsed_url = urllib.parse.urlparse(url)
    scheme = (parsed_url.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are supported for downloads")

    host = (parsed_url.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("Invalid download URL host")

    allowed_hosts = download_allowed_hosts_from_env()
    if allowed_hosts and not host_is_allowed(host, allowed_hosts):
        raise ValueError("Download URL host not allowed")

    allow_private = download_allow_private_networks_from_env()
    if not allow_private and host in {"localhost"}:
        raise ValueError("Download URL host not allowed")
    if not allow_private:
        validate_host_resolves_to_public_ip(host)

    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    base = safe_filename_from_url(url, sanitize_filename_fn=sanitize_filename_fn)
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


def probe_duration_seconds(path: str) -> float:
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


def probe_has_audio(path: str) -> bool:
    """Return whether media contains a primary audio stream."""
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


def watermark_overlay_xy(position_orig: str, margin: int = 54) -> tuple[str, str]:
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
    return f"main_w-overlay_w-{margin}", f"{margin}"


def build_normalize_1080p_filter(label_in: str, label_out: str) -> str:
    """Normalize to a 16:9 padded 1080p frame."""
    return (
        f"[{label_in}]"
        "scale=w='if(gt(a,16/9),16/9*1080,-2)':h='if(gt(a,16/9),-2,1080)',"
        "pad=ceil(16/9*1080):1080:(ow-iw)/2:(oh-ih)/2"
        f"[{label_out}]"
    )


def run_ffmpeg_cmd(ffmpeg_cmd: str, log_type: str, *, launch_cmd_fn, encode_log_fn) -> bool:
    """Run an FFmpeg command and append command output to encoding log."""
    ok, out = launch_cmd_fn(ffmpeg_cmd, log_type, "")
    encode_log_fn(out)
    return bool(ok)


def create_cut_intermediate(
    input_path: str,
    output_path: str,
    start: str,
    end: str,
    *,
    choose_h264_encoder_fn,
    run_ffmpeg_cmd_fn,
) -> bool:
    """Create an intermediate file with the requested cut."""
    encoder, _ = choose_h264_encoder_fn()
    ffmpeg_cmd = (
        "ffmpeg -hide_banner -threads 0 -y "
        f"-ss {shlex.quote(start)} -to {shlex.quote(end)} "
        f"-i {shlex.quote(input_path)} "
        "-map '0:v:0?' -map '0:a?' "
        f"-c:v {encoder} -c:a aac -ar 48000 -ac 2 -fps_mode passthrough "
        f"{shlex.quote(output_path)}"
    )
    return bool(run_ffmpeg_cmd_fn(ffmpeg_cmd, "cut_intermediate"))


def create_watermarked_intermediate(
    input_path: str,
    watermark_path: str,
    output_path: str,
    position_orig: str,
    opacity_percent: str,
    *,
    choose_h264_encoder_fn,
    watermark_overlay_xy_fn,
    build_normalize_1080p_filter_fn,
    run_ffmpeg_cmd_fn,
) -> bool:
    """Create an intermediate file with a watermark applied."""
    encoder, _ = choose_h264_encoder_fn()
    try:
        opacity = float(opacity_percent) / 100.0 if opacity_percent not in (None, "") else 1.0
    except Exception:
        opacity = 1.0
    opacity = max(0.0, min(1.0, opacity))

    x, y = watermark_overlay_xy_fn(position_orig)
    filter_complex = (
        build_normalize_1080p_filter_fn("0:v", "vid")
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
    return bool(run_ffmpeg_cmd_fn(ffmpeg_cmd, "dressing_watermark"))


def parse_duration_seconds_fallback(value: Optional[str], *, timestamp_to_seconds_fn) -> float:
    """Parse a fallback duration string into seconds."""
    if value is None:
        return 0.0
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return float(timestamp_to_seconds_fn(s))


def create_credits_concat_intermediate(
    main_path: str,
    opening_path: Optional[str],
    opening_duration_hint: Optional[str],
    ending_path: Optional[str],
    ending_duration_hint: Optional[str],
    output_path: str,
    *,
    choose_h264_encoder_fn,
    probe_duration_seconds_fn,
    probe_has_audio_fn,
    parse_duration_seconds_fallback_fn,
    build_normalize_1080p_filter_fn,
    run_ffmpeg_cmd_fn,
) -> bool:
    """Concatenate optional opening/ending credits around the main video."""
    encoder, _ = choose_h264_encoder_fn()
    inputs: list[str] = []
    segments: list[dict[str, Any]] = []

    def add_segment(path: str, duration_hint: Optional[str]) -> None:
        duration = probe_duration_seconds_fn(path)
        if duration <= 0:
            duration = parse_duration_seconds_fallback_fn(duration_hint)
        segments.append(
            {
                "path": path,
                "duration": max(0.0, duration),
                "has_audio": probe_has_audio_fn(path),
            }
        )

    if opening_path:
        add_segment(opening_path, opening_duration_hint)
    add_segment(main_path, None)
    if ending_path:
        add_segment(ending_path, ending_duration_hint)

    for seg in segments:
        inputs.append(seg["path"])

    filter_parts: list[str] = []
    concat_inputs: list[str] = []
    for idx, seg in enumerate(segments):
        v_label = f"v{idx}"
        a_label = f"a{idx}"
        filter_parts.append(build_normalize_1080p_filter_fn(f"{idx}:v", v_label))
        if seg["has_audio"]:
            filter_parts.append(
                f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,aresample=async=1[{a_label}]"
            )
        else:
            dur = seg["duration"] if seg["duration"] > 0 else 0.0
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
    return bool(run_ffmpeg_cmd_fn(ffmpeg_cmd, "dressing_credits"))


def apply_cut_for_dressing(
    current_main_path: str,
    base: str,
    has_opening: bool,
    has_ending: bool,
    *,
    cut_config: dict[str, Any],
    subtime: str,
    effective_duration: int,
    videos_dir: str,
    create_cut_intermediate_fn,
) -> tuple[str, str, str, int]:
    """Apply configured cut before credits in dressing mode."""
    msg = ""
    next_subtime = subtime
    next_effective_duration = effective_duration
    cut_start = (cut_config or {}).get("start")
    cut_end = (cut_config or {}).get("end")
    if (has_opening or has_ending) and cut_start and cut_end:
        try:
            cut_output_name = f"{base}_dressing_cut.mp4"
            cut_output_path = os.path.join(videos_dir, cut_output_name)
            msg += f"Applying cut to main video only (before credits) -> {cut_output_name}\n"
            ok = create_cut_intermediate_fn(
                current_main_path, cut_output_path, str(cut_start), str(cut_end)
            )
            if ok:
                current_main_path = cut_output_path
                next_subtime = " "
                next_effective_duration = 0
                msg += "Cut applied in dressing; disabling SUBTIME cut for final encode\n"
            else:
                msg += "Warning: cut intermediate failed, continuing without cut for dressing\n"
        except Exception as e:
            msg += f"Warning: cut intermediate failed ({e}), continuing without cut for dressing\n"
    return current_main_path, msg, next_subtime, next_effective_duration


def apply_watermark_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict[str, Any],
    assets_dir: str,
    *,
    videos_dir: str,
    download_url_to_dir_fn,
    create_watermarked_intermediate_fn,
) -> tuple[str, str]:
    """Apply watermark dressing on the current main media."""
    msg = ""
    watermark_url = dressing_config.get("watermark")
    if not watermark_url:
        return current_main_path, msg

    watermark_pos_orig = dressing_config.get("watermark_position_orig") or dressing_config.get(
        "watermark_position"
    )
    watermark_opacity = dressing_config.get("watermark_opacity")
    try:
        watermark_path = download_url_to_dir_fn(str(watermark_url), assets_dir, "watermark")
        wm_output_name = f"{base}_dressing_wm.mp4"
        wm_output_path = os.path.join(videos_dir, wm_output_name)
        msg += f"Applying watermark to main video -> {wm_output_name}\n"
        ok = create_watermarked_intermediate_fn(
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


def apply_credits_for_dressing(
    current_main_path: str,
    base: str,
    dressing_config: dict[str, Any],
    assets_dir: str,
    *,
    videos_dir: str,
    download_url_to_dir_fn,
    create_credits_concat_intermediate_fn,
) -> tuple[str, str]:
    """Apply opening/ending credits around current main media."""
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
            download_url_to_dir_fn(str(opening_video_url), assets_dir, "opening")
            if has_opening
            else None
        )
        ending_path = (
            download_url_to_dir_fn(str(ending_video_url), assets_dir, "ending")
            if has_ending
            else None
        )
        credits_output_name = f"{base}_dressing.mp4"
        credits_output_path = os.path.join(videos_dir, credits_output_name)
        msg += "Applying credits concat "
        msg += (
            f"(opening={bool(opening_path)}, ending={bool(ending_path)}) -> {credits_output_name}\n"
        )
        ok = create_credits_concat_intermediate_fn(
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


def apply_dressing_if_needed(
    input_filename: str,
    dressing_config: dict[str, Any],
    *,
    videos_dir: str,
    sanitize_filename_fn,
    apply_cut_for_dressing_fn,
    apply_watermark_for_dressing_fn,
    apply_credits_for_dressing_fn,
) -> tuple[str, str]:
    """Return ``(new_input_filename, log_message)`` after dressing stage."""
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

    assets_dir = os.path.join(videos_dir, "dressing_assets")
    input_path = os.path.join(videos_dir, input_filename)
    base = sanitize_filename_fn(os.path.splitext(os.path.basename(input_filename))[0])
    current_main_path = input_path

    current_main_path, cut_msg = apply_cut_for_dressing_fn(
        current_main_path,
        base,
        has_opening,
        has_ending,
    )
    msg += cut_msg

    current_main_path, wm_msg = apply_watermark_for_dressing_fn(
        current_main_path,
        base,
        dressing_config,
        assets_dir,
    )
    msg += wm_msg

    current_main_path, credits_msg = apply_credits_for_dressing_fn(
        current_main_path,
        base,
        dressing_config,
        assets_dir,
    )
    msg += credits_msg

    new_filename = os.path.basename(current_main_path)
    if new_filename != input_filename:
        msg += f"Dressing input switched: {input_filename} -> {new_filename}\n"
    return new_filename, msg
