"""Top-level transcription script orchestration helpers.

Coordinates media prep, transcription, post-processing, translation, and checks.
Centralizes sequencing and return codes so the main flow stays deterministic.
Uses dependency-injected callbacks to keep orchestration testable in isolation.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class MainFlowContext:
    """Dependencies and thresholds required by `run_main_flow`."""

    extract_video_identification_fn: Callable[[Any], Dict[str, str]]
    compute_timeout_fn: Callable[[Any, Path, bool], int]
    prepare_audio_source_fn: Callable[[Any, Path, Path, int, bool], tuple[int, Optional[Path]]]
    resolve_effective_use_gpu_fn: Callable[[bool, int, bool], bool]
    run_transcription_fn: Callable[[Any, Path, Path, int, bool, bool], tuple[int, Optional[str]]]
    finalize_vtt_fn: Callable[..., int]
    run_non_blocking_internal_gap_repair_fn: Callable[..., Dict[str, Any]]
    build_whisper_fallback_options_fn: Callable[..., Dict[str, Any]]
    maybe_translate_final_vtt_fn: Callable[..., tuple[int, Dict[str, Any], Optional[str]]]
    validate_final_vtt_and_collect_gap_analysis_fn: Callable[..., tuple[int, Dict[str, Any]]]
    build_transcription_runtime_metadata_fn: Callable[..., Dict[str, Any]]
    write_info_video_metadata_fn: Callable[[Path, Dict[str, Any], bool], None]
    max_vtt_internal_gap_seconds: float
    max_vtt_internal_gap_count: int


def run_main_flow(
    args: Any,
    *,
    context: MainFlowContext,
) -> int:
    """Run the transcription workflow end-to-end and return an exit code."""
    resolved_context = context

    base_dir = Path(args.base_dir)
    input_path = base_dir / args.input_file
    work_dir = base_dir / args.work_dir
    debug = str(args.debug).lower() in ("true", "1", "yes")
    vtt_max_line_count = int(args.vtt_max_line_count)
    vtt_max_line_width = int(args.vtt_max_line_width)
    video_identification = resolved_context.extract_video_identification_fn(args)

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 2

    timeout_sec = resolved_context.compute_timeout_fn(args, input_path, debug)
    rc, audio_src = resolved_context.prepare_audio_source_fn(
        args, input_path, work_dir, timeout_sec, debug
    )
    if rc != 0 or audio_src is None:
        return rc

    effective_use_gpu = resolved_context.resolve_effective_use_gpu_fn(
        args.use_gpu == "true",
        int(args.gpu_device),
        debug,
    )
    rc, detected_language = resolved_context.run_transcription_fn(
        args,
        audio_src,
        work_dir,
        timeout_sec,
        effective_use_gpu,
        debug,
    )
    if rc != 0:
        return rc

    rc = resolved_context.finalize_vtt_fn(
        audio_src,
        work_dir,
        max_line_count=vtt_max_line_count,
        max_line_width=vtt_max_line_width,
        debug=debug,
    )
    if rc != 0:
        return rc

    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    vtt_gap_repair_metadata = resolved_context.run_non_blocking_internal_gap_repair_fn(
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
    )

    whisper_fallback_options = resolved_context.build_whisper_fallback_options_fn(
        args=args,
        effective_use_gpu=effective_use_gpu,
        timeout_sec=timeout_sec,
        vtt_max_line_count=vtt_max_line_count,
        vtt_max_line_width=vtt_max_line_width,
    )
    rc, translation_metadata, final_subtitle_language = (
        resolved_context.maybe_translate_final_vtt_fn(
            audio_src,
            work_dir,
            requested_language=args.language,
            detected_language=detected_language,
            whisper_fallback_options=whisper_fallback_options,
            use_gpu=effective_use_gpu,
            huggingface_models_dir=args.huggingface_models_dir,
            max_line_count=vtt_max_line_count,
            max_line_width=vtt_max_line_width,
            debug=debug,
        )
    )
    if rc != 0:
        return rc

    rc, final_gap_analysis = resolved_context.validate_final_vtt_and_collect_gap_analysis_fn(
        expected_vtt=expected_vtt,
        audio_src=audio_src,
        input_path=input_path,
        debug=debug,
    )
    if rc != 0:
        return rc

    task_metadata = dict(video_identification)
    vtt_internal_gaps_metadata: Dict[str, Any] = {
        "enabled": True,
        "blocking": False,
        "threshold_seconds": float(resolved_context.max_vtt_internal_gap_seconds),
        "allowed_gap_count": int(resolved_context.max_vtt_internal_gap_count),
        "repair": vtt_gap_repair_metadata,
        "final_output": {
            "read_ok": bool(final_gap_analysis.get("read_ok", False)),
            "gap_count": int(final_gap_analysis.get("gap_count", 0)),
            "largest_gap_seconds": float(final_gap_analysis.get("largest_gap_sec", 0.0)),
            "sample_gaps": list(final_gap_analysis.get("gaps", []))[:3],
        },
    }
    task_metadata.update(
        resolved_context.build_transcription_runtime_metadata_fn(
            requested_language=args.language,
            source_language=getattr(args, "source_language", "auto"),
            detected_language=detected_language,
            final_language=final_subtitle_language,
            whisper_model=args.model,
            use_gpu=effective_use_gpu,
            translation=translation_metadata,
            vtt_internal_gaps=vtt_internal_gaps_metadata,
        )
    )
    resolved_context.write_info_video_metadata_fn(work_dir, task_metadata, debug)
    return 0
