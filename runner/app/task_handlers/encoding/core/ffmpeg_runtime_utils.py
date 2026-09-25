"""Runtime helpers for FFmpeg command execution and capability checks.

Wraps subprocess interactions for encoder discovery and preflight diagnostics.
Keeps command launch/reporting behavior consistent across CPU and GPU flows.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from functools import lru_cache
from timeit import default_timer as timer
from typing import Optional

from app.core.encoding_diagnostics import INCOMPLETE_OUTPUT_PREFIX

OUTPUT_DURATION_TOLERANCE_SECONDS = 5.0
_PROBE_DIAGNOSTIC_MAX_CHARS = 2000


@lru_cache(maxsize=4)
def has_encoder(encoder: str, *, subprocess_module=subprocess) -> bool:
    """Return whether ffmpeg reports a specific encoder."""
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


def choose_h264_encoder(*, has_encoder_fn=has_encoder) -> tuple[str, str]:
    """Choose libx264 when available; otherwise fallback to builtin h264."""
    if has_encoder_fn("libx264"):
        return "libx264", ""
    return "h264", "libx264 missing; forcing h264\n"


@lru_cache(maxsize=1)
def nvenc_preflight(*, subprocess_module=subprocess) -> tuple[bool, str]:
    """Return ``(ok, details)`` for NVENC availability."""
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
            cmd,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.STDOUT,
            text=True,
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


def launch_cmd(
    ffmpeg_cmd: str,
    type: str,
    format: str,
    *,
    subprocess_module=subprocess,
) -> tuple[bool, str]:
    """Execute FFmpeg command and collect a readable log message."""
    msg = ""
    encode_start = timer()
    return_value = False

    try:
        output = subprocess_module.run(
            shlex.split(ffmpeg_cmd),
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.STDOUT,
            text=False,
        )

        encode_end = timer() - encode_start
        msg += ffmpeg_cmd + "\n"
        msg += "Encode file in {:.3}s.\n".format(encode_end)
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
    except subprocess_module.CalledProcessError as e:
        msg += 20 * "////" + "\n"
        msg += "Runtime Error: {0}\n".format(e)
    except OSError as err:
        msg += 20 * "////" + "\n"
        msg += "OS error: {0}\n".format(err)
    except Exception as exc:
        msg += 20 * "////" + "\n"
        msg += "Unexpected error: {0}\n".format(exc)

    return return_value, msg


def _probe_diagnostic(value: object) -> str:
    """Return a compact ffprobe diagnostic suitable for ``encoding.log``."""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value or "")
    normalized = " ".join(text.split())
    return normalized[-_PROBE_DIAGNOSTIC_MAX_CHARS:]


def _duration_seconds(value: object) -> float:
    """Convert a numeric ffprobe duration to seconds."""
    try:
        duration = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return duration if duration > 0 else 0.0


def _run_ffprobe_duration_probe(
    path: str,
    *,
    subprocess_module=subprocess,
) -> tuple[object, str]:
    """Run ffprobe and return its decoded JSON payload."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,duration",
        "-of",
        "json",
        path,
    ]
    try:
        output = subprocess_module.run(
            cmd,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.PIPE,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "ffprobe timed out after 120 seconds"
    except FileNotFoundError as exc:
        return None, f"ffprobe executable not found: {exc}"
    except OSError as exc:
        return None, f"ffprobe execution failed: {exc}"
    except Exception as exc:
        return None, f"unexpected ffprobe failure: {exc}"

    if output.returncode != 0:
        diagnostic = _probe_diagnostic(output.stderr or output.stdout)
        detail = f": {diagnostic}" if diagnostic else ""
        return None, f"ffprobe exited with code {output.returncode}{detail}"

    try:
        payload = json.loads(output.stdout or "")
    except (TypeError, json.JSONDecodeError) as exc:
        diagnostic = _probe_diagnostic(output.stdout)
        detail = f"; output: {diagnostic}" if diagnostic else ""
        return None, f"ffprobe returned invalid JSON: {exc}{detail}"

    return payload, ""


def probe_stream_durations(
    path: str,
    *,
    subprocess_module=subprocess,
) -> tuple[Optional[dict[str, list[float]]], str]:
    """Probe individual audio/video stream durations in one encoded output."""
    payload, probe_error = _run_ffprobe_duration_probe(
        path,
        subprocess_module=subprocess_module,
    )
    if probe_error:
        return None, probe_error

    durations: dict[str, list[float]] = {"video": [], "audio": []}
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return durations, ""

    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_type = stream.get("codec_type")
        if stream_type not in durations:
            continue
        duration = _duration_seconds(stream.get("duration"))
        if duration > 0:
            durations[stream_type].append(duration)

    return durations, ""


def _is_nonempty_file(path: str) -> bool:
    """Return whether an expected encoded output exists and is non-empty."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _validate_stream_durations(
    output_path: str,
    durations: dict[str, list[float]],
    *,
    expected_stream_durations: dict[str, list[float]],
    duration_tolerance: float,
) -> tuple[bool, str, list[str]]:
    """Validate expected stream counts and durations for a probed output."""
    is_valid = True
    msg = ""
    duration_summary: list[str] = []
    for stream_type, expected_values in expected_stream_durations.items():
        if not expected_values:
            continue
        values = durations[stream_type]
        duration_summary.append(f"{stream_type}=" + ",".join(f"{value:.3f}s" for value in values))
        if len(values) < len(expected_values):
            is_valid = False
            msg += (
                f"Output validation failed for {output_path}: expected "
                f"{len(expected_values)} {stream_type} stream(s) with a duration, "
                f"found {len(values)}\n"
            )
            continue

        for stream_index, expected_duration in enumerate(expected_values):
            duration = values[stream_index]
            minimum_duration = max(
                0.0,
                expected_duration - max(0.0, duration_tolerance),
            )
            if duration < minimum_duration:
                is_valid = False
                msg += (
                    f"Output validation failed for {output_path}: {stream_type} "
                    f"stream {stream_index} stops at {duration:.3f}s; expected at least "
                    f"{minimum_duration:.3f}s (target {expected_duration:.3f}s, "
                    f"tolerance {duration_tolerance:.3f}s)\n"
                )
                msg += (
                    f"WARNING: {INCOMPLETE_OUTPUT_PREFIX} {os.path.basename(output_path)!r}, "
                    f"{stream_type} stream {stream_index}: {duration:.3f}s produced, "
                    f"{expected_duration:.3f}s expected, approximately "
                    f"{expected_duration - duration:.3f}s missing. "
                    "Check source integrity and FFmpeg errors; "
                    "see encoding.log for all output diagnostics.\n"
                )

    return is_valid, msg, duration_summary


def _validate_video_output(
    output_path: str,
    probe_path: str,
    *,
    expected_stream_durations: dict[str, list[float]],
    duration_tolerance: float,
    subprocess_module=subprocess,
) -> tuple[bool, str]:
    """Validate one published output and the media file backing it."""
    missing_paths = [
        path for path in dict.fromkeys((output_path, probe_path)) if not _is_nonempty_file(path)
    ]
    if missing_paths:
        msg = "".join(
            f"Output validation failed: missing or empty file: {path}\n" for path in missing_paths
        )
        return False, msg

    durations, probe_error = probe_stream_durations(
        probe_path,
        subprocess_module=subprocess_module,
    )
    if durations is None:
        return False, f"Output validation failed for {output_path}: {probe_error}\n"

    is_valid, msg, duration_summary = _validate_stream_durations(
        output_path,
        durations,
        expected_stream_durations=expected_stream_durations,
        duration_tolerance=duration_tolerance,
    )
    if is_valid:
        msg += f"ffprobe output validation ok: {output_path} ({'; '.join(duration_summary)})\n"
    return is_valid, msg


def validate_video_outputs(
    outputs: list[tuple[str, str]],
    *,
    expected_stream_durations: dict[str, list[float]],
    duration_tolerance: float = OUTPUT_DURATION_TOLERANCE_SECONDS,
    subprocess_module=subprocess,
) -> tuple[bool, str]:
    """Validate all HLS/MP4 outputs and their per-stream durations with ffprobe.

    Each tuple contains the published output path and the path to probe. For HLS
    single-file outputs, these are respectively the ``.m3u8`` playlist and its
    ``.ts`` media file.
    """
    msg = "--> validate_video_outputs\n"

    expected_video_durations = expected_stream_durations.get("video", [])
    if not expected_video_durations or expected_video_durations[0] <= 0:
        return False, msg + "Output validation failed: expected video duration is not positive\n"
    if not outputs:
        return False, msg + "Output validation failed: no video output was selected\n"

    is_valid = True
    for output_path, probe_path in outputs:
        output_is_valid, output_msg = _validate_video_output(
            output_path,
            probe_path,
            expected_stream_durations=expected_stream_durations,
            duration_tolerance=duration_tolerance,
            subprocess_module=subprocess_module,
        )
        msg += output_msg
        if not output_is_valid:
            is_valid = False

    return is_valid, msg


def run_and_collect_text(cmd: list[str], *, subprocess_module=subprocess) -> tuple[int, str]:
    """Run a command and return its exit code and merged text output."""
    out = subprocess_module.run(
        cmd,
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.STDOUT,
        text=True,
    )
    return int(out.returncode), out.stdout or ""


def run_shell_bytes(cmd: str, *, subprocess_module=subprocess) -> tuple[int, bytes]:
    """Run a shell-like command string and return bytes output."""
    out = subprocess_module.run(
        shlex.split(cmd),
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.STDOUT,
        text=False,
    )
    return int(out.returncode), out.stdout or b""
