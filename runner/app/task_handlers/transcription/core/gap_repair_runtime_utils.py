"""Default wiring for VTT internal-gap validation and repair.

Connects repair callbacks to ffmpeg, Whisper, VTT parsing, and post-processing.
Injects conservative thresholds from runtime args for non-blocking remediation.
Keeps repair behavior consistent between CLI runs and test doubles.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, cast

_CORE_DIR = Path(__file__).resolve().parent
_CORE_DIR_STR = str(_CORE_DIR)
if _CORE_DIR_STR in sys.path:
    sys.path.remove(_CORE_DIR_STR)
sys.path.insert(0, _CORE_DIR_STR)

_RUNTIME_ARGS_MODULE = sys.modules.get("runtime_args_utils")
if _RUNTIME_ARGS_MODULE is not None:
    _runtime_args_file = getattr(_RUNTIME_ARGS_MODULE, "__file__", "")
    if not _runtime_args_file or Path(_runtime_args_file).resolve().parent != _CORE_DIR:
        sys.modules.pop("runtime_args_utils", None)

import gap_repair_utils
import language_utils
import output_validation_flow_utils
import runtime_cli_utils
import runtime_media_utils
import vtt_postprocess_utils
import vtt_validation_utils
import whisper_python_runtime_utils
from runtime_args_utils import (
    _INTERNAL_GAP_REPAIR_CONTEXT_PADDING_SECONDS,
    _INTERNAL_GAP_REPAIR_CUE_OVERLAP_TOLERANCE_SECONDS,
    _INTERNAL_GAP_REPAIR_MIN_WINDOW_SECONDS,
    _MAX_INTERNAL_GAP_REPAIR_ATTEMPTS,
    _MAX_VTT_INTERNAL_GAP_COUNT,
    _MAX_VTT_INTERNAL_GAP_SECONDS,
)


def read_vtt_cue_time_ranges(vtt_path: Path) -> tuple[bool, list[tuple[float, float, int]]]:
    """Read cue time ranges from a WebVTT file."""
    return cast(
        tuple[bool, list[tuple[float, float, int]]],
        vtt_validation_utils.read_vtt_cue_time_ranges(
            vtt_path,
            parse_timestamp=vtt_validation_utils.parse_vtt_timestamp,
        ),
    )


def detect_vtt_internal_gaps(vtt_path: Path, max_internal_gap_sec: float) -> Dict[str, Any]:
    """Detect suspiciously long gaps between adjacent subtitle cues."""
    return cast(
        Dict[str, Any],
        vtt_validation_utils.detect_vtt_internal_gaps(
            vtt_path,
            max_internal_gap_sec,
            read_cue_time_ranges=read_vtt_cue_time_ranges,
        ),
    )


def validate_vtt_internal_gaps(
    vtt_path: Path,
    max_internal_gap_sec: float,
    max_internal_gap_count: int,
    debug: bool,
) -> int:
    """Fail when the generated VTT contains suspiciously long internal gaps."""
    return cast(
        int,
        vtt_validation_utils.validate_vtt_internal_gaps(
            vtt_path=vtt_path,
            max_internal_gap_sec=max_internal_gap_sec,
            max_internal_gap_count=max_internal_gap_count,
            debug=debug,
            detect_vtt_internal_gaps_fn=detect_vtt_internal_gaps,
        ),
    )


def read_vtt_cues(vtt_path: Path) -> tuple[bool, list[tuple[float, float, str]]]:
    """Read normalized subtitle cues as (start, end, text)."""
    return cast(
        tuple[bool, list[tuple[float, float, str]]],
        gap_repair_utils.read_vtt_cues(
            vtt_path,
            parse_vtt_postprocess_block_fn=vtt_postprocess_utils.parse_vtt_postprocess_block,
            parse_vtt_cue_time_range_fn=lambda timestamp_line: vtt_postprocess_utils.parse_vtt_cue_time_range(
                timestamp_line,
                parse_vtt_timestamp_fn=vtt_validation_utils.parse_vtt_timestamp,
            ),
            normalize_vtt_cue_text_fn=vtt_postprocess_utils.normalize_vtt_cue_text,
        ),
    )


def dedupe_sorted_vtt_cues(
    cues: list[tuple[float, float, str]],
) -> list[tuple[float, float, str]]:
    """Dedupe obvious near-identical adjacent cues after merge."""
    return cast(
        list[tuple[float, float, str]],
        gap_repair_utils.dedupe_sorted_vtt_cues(
            cues,
            normalize_vtt_cue_text_fn=vtt_postprocess_utils.normalize_vtt_cue_text,
        ),
    )


def render_vtt_from_cues(
    cues: list[tuple[float, float, str]],
    *,
    max_line_width: int,
    max_line_count: int,
) -> str:
    """Render a VTT document from normalized cues."""
    return cast(
        str,
        gap_repair_utils.render_vtt_from_cues(
            cues,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            format_vtt_timestamp_fn=vtt_validation_utils.format_vtt_timestamp,
            wrap_vtt_cue_text_fn=vtt_postprocess_utils.wrap_vtt_cue_text,
        ),
    )


def _find_generated_vtt(audio_src: Path, work_dir: Path) -> Optional[Path]:
    """Locate a VTT generated during a gap-window rerun."""
    return cast(
        Optional[Path],
        output_validation_flow_utils.find_generated_vtt(
            audio_src,
            work_dir,
            build_vtt_stem_candidates_fn=output_validation_flow_utils.build_vtt_stem_candidates,
        ),
    )


def run_gap_window_rerun(
    *,
    audio_src: Path,
    out_dir: Path,
    model: str,
    whisper_models_dir: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    transcription_language: str,
    start_sec: float,
    duration_sec: float,
    gap_start_sec: float,
    gap_end_sec: float,
    overlap_tolerance_sec: float,
    debug: bool,
) -> tuple[bool, list[tuple[float, float, str]]]:
    """Transcribe one short audio window and return cues overlapping the target gap."""
    return cast(
        tuple[bool, list[tuple[float, float, str]]],
        gap_repair_utils.run_gap_window_rerun(
            audio_src=audio_src,
            out_dir=out_dir,
            model=model,
            whisper_models_dir=whisper_models_dir,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            vad_filter=vad_filter,
            timeout_sec=timeout_sec,
            transcription_language=transcription_language,
            start_sec=start_sec,
            duration_sec=duration_sec,
            gap_start_sec=gap_start_sec,
            gap_end_sec=gap_end_sec,
            overlap_tolerance_sec=overlap_tolerance_sec,
            debug=debug,
            extract_audio_chunk_fn=whisper_python_runtime_utils.extract_audio_chunk,
            run_whisper_python_fn=whisper_python_runtime_utils.run_whisper_python,
            run_whisper_cli_fn=runtime_cli_utils.run_whisper_cli_with_defaults,
            find_generated_vtt_fn=_find_generated_vtt,
            read_vtt_cues_fn=read_vtt_cues,
        ),
    )


def attempt_best_effort_vtt_internal_gap_repair(
    *,
    vtt_path: Path,
    audio_src: Path,
    work_dir: Path,
    model: str,
    whisper_models_dir: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    detected_language: Optional[str],
    max_internal_gap_sec: float,
    max_repair_attempts: int,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> Dict[str, Any]:
    """Best-effort repair pass for suspicious internal subtitle gaps."""
    context = gap_repair_utils.AttemptGapRepairContext(
        context_padding_seconds=_INTERNAL_GAP_REPAIR_CONTEXT_PADDING_SECONDS,
        min_window_seconds=_INTERNAL_GAP_REPAIR_MIN_WINDOW_SECONDS,
        overlap_tolerance_seconds=_INTERNAL_GAP_REPAIR_CUE_OVERLAP_TOLERANCE_SECONDS,
        detect_vtt_internal_gaps_fn=detect_vtt_internal_gaps,
        read_vtt_cues_fn=read_vtt_cues,
        probe_duration_seconds_fn=lambda path: runtime_media_utils.probe_duration_seconds(
            path,
            debug=debug,
            subprocess_run=subprocess.run,
        ),
        normalize_language_fn=language_utils.normalize_language_code,
        resolve_transcription_language_fn=lambda _requested_language: "auto",
        run_gap_window_rerun_fn=run_gap_window_rerun,
        dedupe_sorted_vtt_cues_fn=dedupe_sorted_vtt_cues,
        render_vtt_from_cues_fn=render_vtt_from_cues,
        postprocess_vtt_file_fn=vtt_postprocess_utils.postprocess_vtt_file_with_defaults,
    )
    return cast(
        Dict[str, Any],
        gap_repair_utils.attempt_best_effort_vtt_internal_gap_repair(
            vtt_path=vtt_path,
            audio_src=audio_src,
            work_dir=work_dir,
            model=model,
            whisper_models_dir=whisper_models_dir,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            vad_filter=vad_filter,
            timeout_sec=timeout_sec,
            detected_language=detected_language,
            max_internal_gap_sec=max_internal_gap_sec,
            max_repair_attempts=max_repair_attempts,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            debug=debug,
            context=context,
        ),
    )


def default_non_blocking_internal_gap_metadata(
    *,
    note: str,
    error: Optional[str] = None,
    threshold_seconds: Optional[float] = None,
    allowed_gap_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the default non-blocking internal-gap metadata payload."""
    return cast(
        Dict[str, Any],
        gap_repair_utils.default_non_blocking_internal_gap_metadata(
            note=note,
            threshold_seconds=(
                float(threshold_seconds)
                if threshold_seconds is not None
                else _MAX_VTT_INTERNAL_GAP_SECONDS
            ),
            allowed_gap_count=(
                int(allowed_gap_count)
                if allowed_gap_count is not None
                else _MAX_VTT_INTERNAL_GAP_COUNT
            ),
            error=error,
        ),
    )


def run_non_blocking_internal_gap_repair(
    *,
    expected_vtt: Path,
    audio_src: Path,
    work_dir: Path,
    args: Any,
    timeout_sec: int,
    effective_use_gpu: bool,
    detected_language: Optional[str],
    vtt_max_line_width: int,
    vtt_max_line_count: int,
    debug: bool,
) -> Dict[str, Any]:
    """Run best-effort internal-gap repair without ever blocking the workflow."""
    context = gap_repair_utils.NonBlockingGapRepairContext(
        max_vtt_internal_gap_seconds=_MAX_VTT_INTERNAL_GAP_SECONDS,
        max_vtt_internal_gap_count=_MAX_VTT_INTERNAL_GAP_COUNT,
        max_internal_gap_repair_attempts=_MAX_INTERNAL_GAP_REPAIR_ATTEMPTS,
        attempt_best_effort_vtt_internal_gap_repair_fn=attempt_best_effort_vtt_internal_gap_repair,
        default_non_blocking_internal_gap_metadata_fn=default_non_blocking_internal_gap_metadata,
    )
    return cast(
        Dict[str, Any],
        gap_repair_utils.run_non_blocking_internal_gap_repair(
            expected_vtt=expected_vtt,
            audio_src=audio_src,
            work_dir=work_dir,
            args=args,
            timeout_sec=timeout_sec,
            effective_use_gpu=effective_use_gpu,
            detected_language=detected_language,
            vtt_max_line_width=vtt_max_line_width,
            vtt_max_line_count=vtt_max_line_count,
            debug=debug,
            context=context,
        ),
    )
