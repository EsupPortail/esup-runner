"""Output-finalization and validation flow helpers.

Orchestrates final VTT selection, normalization, and structural validations.
Applies coverage and gap guardrails before marking a run as successful.
Returns explicit diagnostics used by orchestration and runtime metadata.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class FinalVttValidationContext:
    """Thresholds and callbacks used by final VTT validation flow."""

    min_vtt_coverage_ratio: float
    max_vtt_final_gap_seconds: float
    max_vtt_internal_gap_seconds: float
    max_vtt_internal_gap_count: int
    probe_duration_seconds_fn: Callable[[Path, bool], float]
    validate_vtt_coverage_fn: Callable[..., int]
    validate_vtt_internal_gaps_fn: Callable[..., int]
    detect_vtt_internal_gaps_fn: Callable[[Path, float], Dict[str, Any]]


def build_vtt_stem_candidates(audio_src: Path) -> list[str]:
    """Build candidate stems for Whisper outputs when filename contains dots."""
    stem = Path(audio_src).stem
    candidates = [stem]
    if "." in stem:
        parts = stem.split(".")
        # Some Whisper CLI variants effectively truncate dotted stems.
        for index in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:index])
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def find_generated_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    build_vtt_stem_candidates_fn: Callable[[Path], list[str]],
) -> Optional[Path]:
    """Locate the generated VTT file for a Whisper output stem."""
    for stem in build_vtt_stem_candidates_fn(audio_src):
        candidate = work_dir / f"{stem}.vtt"
        if candidate.exists():
            return candidate
    return None


def finalize_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    max_line_count: int,
    max_line_width: int,
    debug: bool,
    find_generated_vtt_fn: Callable[[Path, Path], Optional[Path]],
    postprocess_vtt_file_fn: Callable[..., None],
) -> int:
    """Rename generated VTT to expected final output name and post-process it."""
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    generated_vtt = find_generated_vtt_fn(audio_src, work_dir)
    try:
        if generated_vtt is None:
            print("VTT output not found after whisper execution")
            return 5

        if generated_vtt != expected_vtt:
            generated_vtt.replace(expected_vtt)

        postprocess_vtt_file_fn(
            expected_vtt,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            debug=debug,
        )
        print(f"VTT written to: {expected_vtt}")
        return 0
    except Exception as exc:
        print(f"Failed to finalize VTT: {exc}")
        return 6


def validate_final_vtt_and_collect_gap_analysis(
    *,
    expected_vtt: Path,
    audio_src: Path,
    input_path: Path,
    debug: bool,
    context: FinalVttValidationContext,
) -> tuple[int, Dict[str, Any]]:
    """Run final VTT validations and return non-blocking internal-gap analysis."""
    resolved_context = context

    reference_duration_sec = resolved_context.probe_duration_seconds_fn(audio_src, debug)
    if reference_duration_sec <= 0:
        reference_duration_sec = resolved_context.probe_duration_seconds_fn(input_path, debug)

    rc = resolved_context.validate_vtt_coverage_fn(
        vtt_path=expected_vtt,
        reference_duration_sec=reference_duration_sec,
        min_coverage_ratio=resolved_context.min_vtt_coverage_ratio,
        max_final_gap_sec=resolved_context.max_vtt_final_gap_seconds,
        debug=debug,
    )
    if rc != 0:
        return rc, {}

    gap_validation_rc = resolved_context.validate_vtt_internal_gaps_fn(
        vtt_path=expected_vtt,
        max_internal_gap_sec=resolved_context.max_vtt_internal_gap_seconds,
        max_internal_gap_count=resolved_context.max_vtt_internal_gap_count,
        debug=debug,
    )
    final_gap_analysis = resolved_context.detect_vtt_internal_gaps_fn(
        expected_vtt,
        resolved_context.max_vtt_internal_gap_seconds,
    )
    if gap_validation_rc != 0:
        print(
            "VTT internal-gap warning (non-blocking): "
            f"count={int(final_gap_analysis.get('gap_count', 0))}, "
            f"threshold={float(resolved_context.max_vtt_internal_gap_seconds):.3f}s"
        )
    return 0, final_gap_analysis
