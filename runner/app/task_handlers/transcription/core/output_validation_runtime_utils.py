"""Default runtime wiring for VTT finalization and validation.

Binds validation flow helpers to probing, parsing, and post-processing utilities.
Supplies default threshold constants used by final output quality checks.
Provides a thin runtime facade for callers that avoid manual dependency wiring.
"""

from pathlib import Path
from typing import Any, Dict, Optional, cast

from . import (
    gap_repair_runtime_utils,
    output_validation_flow_utils,
    runtime_media_utils,
    vtt_postprocess_utils,
    vtt_validation_utils,
)
from .runtime_args_utils import (
    _MAX_VTT_FINAL_GAP_SECONDS,
    _MAX_VTT_INTERNAL_GAP_COUNT,
    _MAX_VTT_INTERNAL_GAP_SECONDS,
    _MIN_VTT_COVERAGE_RATIO,
)

build_vtt_stem_candidates = output_validation_flow_utils.build_vtt_stem_candidates
parse_vtt_timestamp = vtt_validation_utils.parse_vtt_timestamp
format_vtt_timestamp = vtt_validation_utils.format_vtt_timestamp
postprocess_vtt_file = vtt_postprocess_utils.postprocess_vtt_file_with_defaults


def find_generated_vtt(audio_src: Path, work_dir: Path) -> Optional[Path]:
    """Locate the generated VTT file for a Whisper output stem."""
    return cast(
        Optional[Path],
        output_validation_flow_utils.find_generated_vtt(
            audio_src,
            work_dir,
            build_vtt_stem_candidates_fn=build_vtt_stem_candidates,
        ),
    )


def finalize_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    max_line_count: int = 2,
    max_line_width: int = 40,
    debug: bool = False,
) -> int:
    """Rename the generated VTT file to the expected final output name."""
    return cast(
        int,
        output_validation_flow_utils.finalize_vtt(
            audio_src,
            work_dir,
            max_line_count=max_line_count,
            max_line_width=max_line_width,
            debug=debug,
            find_generated_vtt_fn=find_generated_vtt,
            postprocess_vtt_file_fn=postprocess_vtt_file,
        ),
    )


def read_last_vtt_cue_end_seconds(vtt_path: Path) -> tuple[bool, bool, Optional[float]]:
    """Inspect a WebVTT file and return read status, cue presence, and last cue end."""
    return cast(
        tuple[bool, bool, Optional[float]],
        vtt_validation_utils.read_last_vtt_cue_end_seconds(
            vtt_path,
            parse_timestamp=parse_vtt_timestamp,
        ),
    )


def validate_vtt_coverage(
    vtt_path: Path,
    reference_duration_sec: float,
    min_coverage_ratio: float,
    max_final_gap_sec: float,
    debug: bool,
) -> int:
    """Fail when the generated VTT is clearly truncated versus the source duration."""
    return cast(
        int,
        vtt_validation_utils.validate_vtt_coverage(
            vtt_path=vtt_path,
            reference_duration_sec=reference_duration_sec,
            min_coverage_ratio=min_coverage_ratio,
            max_final_gap_sec=max_final_gap_sec,
            debug=debug,
            read_last_cue_end_seconds=read_last_vtt_cue_end_seconds,
        ),
    )


def validate_final_vtt_and_collect_gap_analysis(
    *,
    expected_vtt: Path,
    audio_src: Path,
    input_path: Path,
    debug: bool,
) -> tuple[int, Dict[str, Any]]:
    """Run final VTT validations and return non-blocking internal-gap analysis."""
    context = output_validation_flow_utils.FinalVttValidationContext(
        min_vtt_coverage_ratio=_MIN_VTT_COVERAGE_RATIO,
        max_vtt_final_gap_seconds=_MAX_VTT_FINAL_GAP_SECONDS,
        max_vtt_internal_gap_seconds=_MAX_VTT_INTERNAL_GAP_SECONDS,
        max_vtt_internal_gap_count=_MAX_VTT_INTERNAL_GAP_COUNT,
        probe_duration_seconds_fn=runtime_media_utils.probe_duration_seconds,
        validate_vtt_coverage_fn=validate_vtt_coverage,
        validate_vtt_internal_gaps_fn=gap_repair_runtime_utils.validate_vtt_internal_gaps,
        detect_vtt_internal_gaps_fn=gap_repair_runtime_utils.detect_vtt_internal_gaps,
    )
    return cast(
        tuple[int, Dict[str, Any]],
        output_validation_flow_utils.validate_final_vtt_and_collect_gap_analysis(
            expected_vtt=expected_vtt,
            audio_src=audio_src,
            input_path=input_path,
            debug=debug,
            context=context,
        ),
    )
