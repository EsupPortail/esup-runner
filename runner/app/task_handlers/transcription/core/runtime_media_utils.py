"""Media probing and source-audio preparation helpers.

Uses ffprobe and ffmpeg to measure duration and normalize input audio sources.
Builds source-audio preparation steps with predictable timeout behavior.
Keeps media I/O details isolated from orchestration and business flow code.
"""

import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Optional

from . import runtime_cli_utils

_SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")


def probe_duration_seconds(
    path: Path,
    debug: bool = False,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> float:
    """Probe the media duration in seconds with ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
        proc = subprocess_run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0:
            return float((proc.stdout or "0").strip() or 0)
    except Exception as exc:
        if debug:
            print(f"ffprobe failed to get duration: {exc}")
    return 0.0


def _run_silence_detection(
    path: Path,
    duration_seconds: float,
    debug: bool,
    subprocess_run: Callable[..., Any],
) -> Optional[str]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-vn",
        "-af",
        "silencedetect=noise=-35dB:d=2",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess_run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30, min(600, int(duration_seconds * 2) + 30)),
        )
    except Exception as exc:
        if debug:
            print(f"ffmpeg failed to detect trailing silence: {exc}")
        return None

    if proc.returncode != 0:
        if debug:
            print("ffmpeg failed to detect trailing silence")
        return None

    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def _parse_trailing_silence_start(output: str, duration_seconds: float) -> Optional[float]:
    silence_starts = [float(match.group(1)) for match in _SILENCE_START_RE.finditer(output)]
    silence_ends = [float(match.group(1)) for match in _SILENCE_END_RE.finditer(output)]
    if not silence_starts or not silence_ends:
        return None

    trailing_silence_start = silence_starts[-1]
    trailing_silence_end = silence_ends[-1]
    if trailing_silence_end < duration_seconds - 1.0:
        return None
    if trailing_silence_start <= 0 or trailing_silence_start >= trailing_silence_end:
        return None

    return trailing_silence_start


def probe_effective_duration_seconds(
    path: Path,
    debug: bool = False,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> float:
    """Return media duration with confirmed trailing silence excluded."""
    duration_seconds = probe_duration_seconds(
        path,
        debug=debug,
        subprocess_run=subprocess_run,
    )
    if duration_seconds <= 0:
        return 0.0

    output = _run_silence_detection(path, duration_seconds, debug, subprocess_run)
    if output is None:
        return duration_seconds

    trailing_silence_start = _parse_trailing_silence_start(output, duration_seconds)
    if trailing_silence_start is None:
        return duration_seconds

    if debug:
        print(
            "Detected trailing silence: "
            f"start={trailing_silence_start:.3f}s, "
            f"duration={duration_seconds:.3f}s"
        )
    return trailing_silence_start


def compute_timeout(
    args: Any,
    input_path: Path,
    debug: bool,
    *,
    probe_duration_seconds_fn: Callable[[Path, bool], float],
) -> int:
    """Compute a runtime timeout from media duration and CLI settings."""
    duration_seconds = probe_duration_seconds_fn(input_path, debug)
    try:
        timeout_factor = float(args.timeout_factor)
        min_timeout = int(args.min_timeout)
    except Exception:
        timeout_factor = 8.0
        min_timeout = 60
    timeout_sec = max(min_timeout, int((duration_seconds or 0) * timeout_factor))
    if debug:
        print(f"Probed duration: {duration_seconds:.3f}s, timeout: {timeout_sec}s")
    return timeout_sec


def compute_timeout_with_defaults(args: Any, input_path: Path, debug: bool) -> int:
    """Compute a runtime timeout using the default ffprobe implementation."""
    return compute_timeout(
        args,
        input_path,
        debug,
        probe_duration_seconds_fn=lambda path, debug_enabled: probe_duration_seconds(
            path,
            debug=debug_enabled,
            subprocess_run=subprocess.run,
        ),
    )


def prepare_audio_source(
    args: Any,
    input_path: Path,
    work_dir: Path,
    timeout_sec: int,
    debug: bool,
    *,
    run_ffmpeg_to_mp3_fn: Callable[..., int],
    normalize_mp3_with_ffmpeg_normalize_fn: Callable[..., Path],
) -> tuple[int, Optional[Path]]:
    """Prepare the audio source file used for transcription."""
    input_is_mp3 = input_path.suffix.lower() == ".mp3"
    mp3_path = work_dir / f"{Path(args.input_file).stem}.mp3"

    if input_is_mp3:
        audio_src = input_path
    else:
        rc = run_ffmpeg_to_mp3_fn(
            input_path=input_path,
            mp3_path=mp3_path,
            sample_rate=int(args.sample_rate),
            downmix_mono=(args.downmix_mono == "true"),
            audio_index=int(args.audio_stream_index),
            timeout_sec=timeout_sec,
            debug=debug,
        )
        if rc != 0:
            print(f"ffmpeg extraction failed with return code {rc}")
            return rc, None
        audio_src = mp3_path

    if str(args.normalize).lower() == "true":
        audio_src = normalize_mp3_with_ffmpeg_normalize_fn(
            mp3_path=audio_src,
            target_level=str(args.normalize_target_level),
            timeout_sec=timeout_sec,
            debug=debug,
        )

    return 0, audio_src


def prepare_audio_source_with_defaults(
    args: Any,
    input_path: Path,
    work_dir: Path,
    timeout_sec: int,
    debug: bool,
) -> tuple[int, Optional[Path]]:
    """Prepare the source audio using the default ffmpeg helpers."""
    return prepare_audio_source(
        args,
        input_path,
        work_dir,
        timeout_sec,
        debug,
        run_ffmpeg_to_mp3_fn=runtime_cli_utils.run_ffmpeg_to_mp3,
        normalize_mp3_with_ffmpeg_normalize_fn=runtime_cli_utils.normalize_mp3_with_ffmpeg_normalize,
    )
