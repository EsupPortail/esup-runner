"""WebVTT timestamp parsing and validation helpers.

Parses cue timestamps into numeric values used by validation routines.
Computes coverage and gap diagnostics that act as subtitle quality guardrails.
Returns structured validation details for logging, gating, and repair flows.
"""

from pathlib import Path
from typing import Any, Callable, Dict, Optional


def parse_vtt_timestamp(raw: str) -> Optional[float]:
    """Parse a WebVTT timestamp into seconds."""
    value = raw.strip()
    if not value:
        return None

    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_part, seconds_part = parts
    elif len(parts) == 3:
        hours_part, minutes_part, seconds_part = parts
        try:
            hours = int(hours_part)
        except ValueError:
            return None
    else:
        return None

    if "." not in seconds_part:
        return None

    seconds_whole, millis_part = seconds_part.split(".", 1)
    try:
        minutes = int(minutes_part)
        seconds = int(seconds_whole)
        millis = int(millis_part[:3].ljust(3, "0"))
    except ValueError:
        return None

    return float(hours * 3600 + minutes * 60 + seconds) + (millis / 1000.0)


def read_last_vtt_cue_end_seconds(
    vtt_path: Path,
    *,
    parse_timestamp: Callable[[str], Optional[float]],
) -> tuple[bool, bool, Optional[float]]:
    """Inspect a WebVTT file and return read status, cue presence, and last cue end."""
    saw_cue = False
    last_end_sec: Optional[float] = None
    try:
        for line in vtt_path.read_text(encoding="utf-8").splitlines():
            if "-->" not in line:
                continue
            saw_cue = True
            try:
                raw_end = line.split("-->", 1)[1].strip().split()[0]
            except Exception:
                continue
            parsed = parse_timestamp(raw_end)
            if parsed is not None:
                last_end_sec = parsed
    except Exception:
        return False, False, None
    return True, saw_cue, last_end_sec


def validate_vtt_coverage(
    *,
    vtt_path: Path,
    reference_duration_sec: float,
    min_coverage_ratio: float,
    max_final_gap_sec: float,
    debug: bool,
    read_last_cue_end_seconds: Callable[[Path], tuple[bool, bool, Optional[float]]],
) -> int:
    """Fail when the generated VTT is clearly truncated versus the source duration."""
    if reference_duration_sec <= 0:
        return 0

    read_ok, has_cues, last_end_sec = read_last_cue_end_seconds(vtt_path)
    if not read_ok:
        print("VTT coverage validation failed: unable to read the generated VTT")
        return 7
    if not has_cues:
        if debug:
            print(
                "VTT coverage: generated VTT contains no subtitle cues; "
                "treating it as a valid no-speech result"
            )
        return 0
    if last_end_sec is None or last_end_sec <= 0:
        print("VTT coverage validation failed: unable to read the last subtitle cue")
        return 7

    coverage_ratio = last_end_sec / float(reference_duration_sec)
    final_gap_sec = max(0.0, float(reference_duration_sec) - last_end_sec)
    if debug:
        print(
            "VTT coverage: "
            f"last_end={last_end_sec:.3f}s, duration={reference_duration_sec:.3f}s, "
            f"coverage_ratio={coverage_ratio:.3f}, final_gap={final_gap_sec:.3f}s"
        )

    if coverage_ratio < min_coverage_ratio and final_gap_sec > max_final_gap_sec:
        print(
            "VTT coverage validation failed: output appears truncated "
            f"(last cue at {last_end_sec:.3f}s for duration {reference_duration_sec:.3f}s, "
            f"coverage={coverage_ratio:.3f}, gap={final_gap_sec:.3f}s)"
        )
        return 7

    return 0


def read_vtt_cue_time_ranges(
    vtt_path: Path,
    *,
    parse_timestamp: Callable[[str], Optional[float]],
) -> tuple[bool, list[tuple[float, float, int]]]:
    """Read cue time ranges from a WebVTT file."""
    cues: list[tuple[float, float, int]] = []
    try:
        for line_number, line in enumerate(
            vtt_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "-->" not in line:
                continue
            try:
                raw_start, raw_end = line.split("-->", 1)
                start_token = raw_start.strip().split()[0]
                end_token = raw_end.strip().split()[0]
            except Exception:
                continue
            start_sec = parse_timestamp(start_token)
            end_sec = parse_timestamp(end_token)
            if start_sec is None or end_sec is None:
                continue
            if end_sec <= start_sec:
                continue
            cues.append((start_sec, end_sec, line_number))
    except Exception:
        return False, []

    return True, cues


def detect_vtt_internal_gaps(
    vtt_path: Path,
    max_internal_gap_sec: float,
    *,
    read_cue_time_ranges: Callable[[Path], tuple[bool, list[tuple[float, float, int]]]],
) -> Dict[str, Any]:
    """Detect suspiciously long gaps between adjacent subtitle cues."""
    read_ok, cues = read_cue_time_ranges(vtt_path)
    if not read_ok:
        return {
            "read_ok": False,
            "cue_count": 0,
            "gap_threshold_sec": float(max_internal_gap_sec),
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        }

    gaps: list[Dict[str, Any]] = []
    if max_internal_gap_sec > 0:
        for i in range(1, len(cues)):
            previous_end = float(cues[i - 1][1])
            next_start = float(cues[i][0])
            next_line = int(cues[i][2])
            gap_sec = next_start - previous_end
            if gap_sec >= float(max_internal_gap_sec):
                gaps.append(
                    {
                        "gap_sec": gap_sec,
                        "previous_end_sec": previous_end,
                        "next_start_sec": next_start,
                        "line_number": next_line,
                    }
                )

    largest_gap_sec = max((float(gap["gap_sec"]) for gap in gaps), default=0.0)
    return {
        "read_ok": True,
        "cue_count": len(cues),
        "gap_threshold_sec": float(max_internal_gap_sec),
        "gap_count": len(gaps),
        "largest_gap_sec": largest_gap_sec,
        "gaps": gaps,
    }


def validate_vtt_internal_gaps(
    *,
    vtt_path: Path,
    max_internal_gap_sec: float,
    max_internal_gap_count: int,
    debug: bool,
    detect_vtt_internal_gaps_fn: Callable[[Path, float], Dict[str, Any]],
) -> int:
    """Fail when the generated VTT contains suspiciously long internal gaps."""
    if max_internal_gap_sec <= 0:
        return 0

    analysis = detect_vtt_internal_gaps_fn(vtt_path, max_internal_gap_sec)
    if not analysis.get("read_ok", False):
        print("VTT internal-gap validation failed: unable to read the generated VTT")
        return 8

    gap_count = int(analysis.get("gap_count", 0))
    suspicious_gaps = list(analysis.get("gaps", []))
    if int(analysis.get("cue_count", 0)) < 2:
        return 0

    if debug:
        print(
            "VTT internal-gap validation: "
            f"suspicious_gaps={gap_count}, "
            f"threshold={max_internal_gap_sec:.3f}s, "
            f"largest_gap={float(analysis.get('largest_gap_sec', 0.0)):.3f}s"
        )

    if gap_count > int(max_internal_gap_count):
        sample = ", ".join(
            (
                f"{float(gap.get('previous_end_sec', 0.0)):.3f}"
                f"->{float(gap.get('next_start_sec', 0.0)):.3f} "
                f"({float(gap.get('gap_sec', 0.0)):.3f}s, line {int(gap.get('line_number', 0))})"
            )
            for gap in suspicious_gaps[:3]
        )
        print(
            "VTT internal-gap validation failed: output contains suspiciously long internal gaps "
            f"(count={gap_count}, allowed={max_internal_gap_count}, "
            f"threshold={max_internal_gap_sec:.3f}s, samples=[{sample}])"
        )
        return 8

    return 0


def format_vtt_timestamp(value_sec: float) -> str:
    """Format seconds to WebVTT HH:MM:SS.mmm."""
    safe_value = max(0.0, float(value_sec))
    hours = int(safe_value // 3600)
    minutes = int((safe_value % 3600) // 60)
    seconds = safe_value - (hours * 3600) - (minutes * 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
