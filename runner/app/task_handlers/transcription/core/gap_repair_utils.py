"""Internal-gap repair helpers for transcription VTT workflows.

Analyzes suspicious internal gaps and selects focused audio windows to re-run.
Merges replacement cues with overlap-aware deduplication to avoid duplicates.
Stops after bounded attempts so repair stays best-effort and predictable.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class AttemptGapRepairContext:
    """Callbacks and tuning values for best-effort internal-gap repair."""

    context_padding_seconds: float
    min_window_seconds: float
    overlap_tolerance_seconds: float
    detect_vtt_internal_gaps_fn: Callable[[Path, float], Dict[str, Any]]
    read_vtt_cues_fn: Callable[[Path], tuple[bool, list[tuple[float, float, str]]]]
    probe_duration_seconds_fn: Callable[[Path], float]
    normalize_language_fn: Callable[[Optional[str]], Optional[str]]
    resolve_transcription_language_fn: Callable[[str], str]
    run_gap_window_rerun_fn: Callable[..., tuple[bool, list[tuple[float, float, str]]]]
    dedupe_sorted_vtt_cues_fn: Callable[
        [list[tuple[float, float, str]]], list[tuple[float, float, str]]
    ]
    render_vtt_from_cues_fn: Callable[..., str]
    postprocess_vtt_file_fn: Callable[..., None]


@dataclass(frozen=True)
class NonBlockingGapRepairContext:
    """Dependencies and thresholds for non-blocking gap-repair entrypoint."""

    max_vtt_internal_gap_seconds: float
    max_vtt_internal_gap_count: int
    max_internal_gap_repair_attempts: int
    attempt_best_effort_vtt_internal_gap_repair_fn: Callable[..., Dict[str, Any]]
    default_non_blocking_internal_gap_metadata_fn: Callable[..., Dict[str, Any]]


def read_vtt_cues(
    vtt_path: Path,
    *,
    parse_vtt_postprocess_block_fn: Callable[[str], Any],
    parse_vtt_cue_time_range_fn: Callable[[str], tuple[Optional[float], Optional[float]]],
    normalize_vtt_cue_text_fn: Callable[[str], str],
) -> tuple[bool, list[tuple[float, float, str]]]:
    """Read normalized subtitle cues as (start, end, text)."""
    cues: list[tuple[float, float, str]] = []
    try:
        content = vtt_path.read_text(encoding="utf-8")
    except Exception:
        return False, []

    for block in (content or "").split("\n\n"):
        parsed = parse_vtt_postprocess_block_fn(block)
        if isinstance(parsed, str):
            continue
        cue_prefix, cue_text = parsed
        if not cue_prefix:
            continue
        start_sec, end_sec = parse_vtt_cue_time_range_fn(cue_prefix[-1])
        if start_sec is None or end_sec is None:
            continue
        if float(end_sec) <= float(start_sec):
            continue
        normalized_text = normalize_vtt_cue_text_fn(cue_text)
        if not normalized_text:
            continue
        cues.append((float(start_sec), float(end_sec), normalized_text))
    return True, cues


def dedupe_sorted_vtt_cues(
    cues: list[tuple[float, float, str]],
    *,
    normalize_vtt_cue_text_fn: Callable[[str], str],
) -> list[tuple[float, float, str]]:
    """Dedupe obvious near-identical adjacent cues after merge."""
    merged: list[tuple[float, float, str]] = []
    for start_sec, end_sec, text in sorted(cues, key=lambda item: (item[0], item[1], item[2])):
        normalized_text = normalize_vtt_cue_text_fn(text)
        if not normalized_text:
            continue
        start_sec = round(float(start_sec), 3)
        end_sec = round(float(end_sec), 3)
        if end_sec <= start_sec:
            continue
        if not merged:
            merged.append((start_sec, end_sec, normalized_text))
            continue

        prev_start, prev_end, prev_text = merged[-1]
        same_text = normalized_text == prev_text
        almost_same_window = abs(start_sec - prev_start) <= 0.05 and abs(end_sec - prev_end) <= 0.08
        if same_text and almost_same_window:
            continue

        if same_text and start_sec <= prev_end + 0.05:
            merged[-1] = (prev_start, round(max(prev_end, end_sec), 3), prev_text)
            continue

        merged.append((start_sec, end_sec, normalized_text))
    return merged


def render_vtt_from_cues(
    cues: list[tuple[float, float, str]],
    *,
    max_line_width: int,
    max_line_count: int,
    format_vtt_timestamp_fn: Callable[[float], str],
    wrap_vtt_cue_text_fn: Callable[[str, int, int], list[str]],
) -> str:
    """Render a VTT document from normalized cues."""
    blocks: list[str] = ["WEBVTT"]
    for start_sec, end_sec, text in cues:
        wrapped_lines = wrap_vtt_cue_text_fn(text, max_line_width, max_line_count)
        if not wrapped_lines:
            continue
        timestamp_line = (
            f"{format_vtt_timestamp_fn(start_sec)} --> {format_vtt_timestamp_fn(end_sec)}"
        )
        blocks.append("\n".join([timestamp_line] + wrapped_lines))
    return "\n\n".join(blocks).rstrip() + "\n"


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
    extract_audio_chunk_fn: Callable[..., int],
    run_whisper_python_fn: Callable[..., tuple[int, Optional[str]]],
    run_whisper_cli_fn: Callable[..., tuple[int, Optional[str]]],
    find_generated_vtt_fn: Callable[[Path, Path], Optional[Path]],
    read_vtt_cues_fn: Callable[[Path], tuple[bool, list[tuple[float, float, str]]]],
) -> tuple[bool, list[tuple[float, float, str]]]:
    """Transcribe one short audio window and return cues overlapping the target gap."""
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_audio_stem = audio_src.stem.replace(".", "_")
    clip_stem = f"{safe_audio_stem}_gap_{int(start_sec * 1000):010d}_{int(duration_sec * 1000):07d}"
    clip_path = out_dir / f"{clip_stem}.mp3"

    rc = extract_audio_chunk_fn(
        audio_path=audio_src,
        chunk_path=clip_path,
        start_sec=float(start_sec),
        duration_sec=float(duration_sec),
        timeout_sec=timeout_sec,
        debug=debug,
    )
    if rc != 0:
        return False, []

    rc, _detected_lang = run_whisper_python_fn(
        audio_path=clip_path,
        out_dir=out_dir,
        language=transcription_language,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        gpu_device=gpu_device,
        vad_filter=vad_filter,
        timeout_sec=timeout_sec,
        chunk_duration_sec=0,
        chunk_overlap_sec=0,
        chunk_threshold_sec=0,
        vtt_highlight_words=False,
        vtt_max_line_count=2,
        vtt_max_line_width=40,
        debug=debug,
    )
    if rc == 255:
        rc, _detected_lang = run_whisper_cli_fn(
            audio_path=clip_path,
            out_dir=out_dir,
            language=transcription_language,
            model=model,
            whisper_models_dir=whisper_models_dir,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            vad_filter=vad_filter,
            timeout_sec=timeout_sec,
            debug=debug,
        )
    if rc != 0:
        return False, []

    generated_vtt = find_generated_vtt_fn(clip_path, out_dir)
    if generated_vtt is None or not generated_vtt.exists():
        return False, []

    read_ok, local_cues = read_vtt_cues_fn(generated_vtt)
    if not read_ok:
        return False, []

    adjusted_cues: list[tuple[float, float, str]] = []
    for local_start, local_end, local_text in local_cues:
        absolute_start = float(start_sec) + float(local_start)
        absolute_end = float(start_sec) + float(local_end)
        if absolute_end <= absolute_start:
            continue
        if absolute_end < gap_start_sec - float(overlap_tolerance_sec):
            continue
        if absolute_start > gap_end_sec + float(overlap_tolerance_sec):
            continue
        adjusted_cues.append((absolute_start, absolute_end, local_text))
    return True, adjusted_cues


def write_repaired_vtt_from_cues(
    *,
    vtt_path: Path,
    existing_cues: list[tuple[float, float, str]],
    inserted_cues: list[tuple[float, float, str]],
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    context: AttemptGapRepairContext,
) -> Optional[str]:
    """Write a candidate repaired VTT and return the previous content."""
    if not inserted_cues:
        return None

    try:
        original_vtt_content = vtt_path.read_text(encoding="utf-8")
    except Exception:
        original_vtt_content = None

    merged_cues = context.dedupe_sorted_vtt_cues_fn(existing_cues + inserted_cues)
    rendered_content = context.render_vtt_from_cues_fn(
        merged_cues,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )
    vtt_path.write_text(rendered_content, encoding="utf-8")
    context.postprocess_vtt_file_fn(
        vtt_path,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
    )
    return original_vtt_content


def final_gap_repair_analysis_metadata(
    *,
    vtt_path: Path,
    metadata: Dict[str, Any],
    initial_analysis: Dict[str, Any],
    final_analysis: Dict[str, Any],
    inserted_cue_count: int,
    original_vtt_content: Optional[str],
) -> None:
    """Finalize repair diagnostics, reverting candidate output when it got worse."""
    final_gap_count = int(final_analysis.get("gap_count", 0))
    final_largest_gap_sec = float(final_analysis.get("largest_gap_sec", 0.0))
    repair_improved_or_equal = (
        bool(final_analysis.get("read_ok", False))
        and final_gap_count <= metadata["detected_before_count"]
        and final_largest_gap_sec <= metadata["largest_gap_before_seconds"]
    )

    if inserted_cue_count > 0 and not repair_improved_or_equal and original_vtt_content is not None:
        vtt_path.write_text(original_vtt_content, encoding="utf-8")
        final_analysis = initial_analysis
        final_gap_count = int(final_analysis.get("gap_count", 0))
        final_largest_gap_sec = float(final_analysis.get("largest_gap_sec", 0.0))
        metadata["repair_reverted"] = True
        metadata["note"] = "repair_reverted_worse_gap_analysis"

    metadata["detected_after_count"] = final_gap_count
    metadata["largest_gap_after_seconds"] = final_largest_gap_sec
    metadata["read_ok"] = bool(final_analysis.get("read_ok", False))
    metadata["gaps_after"] = final_analysis.get("gaps", [])[:3]
    metadata["repair_improved_or_equal"] = repair_improved_or_equal


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
    context: AttemptGapRepairContext,
) -> Dict[str, Any]:
    """Best-effort repair pass for suspicious internal subtitle gaps."""
    resolved_context = context

    initial_analysis = resolved_context.detect_vtt_internal_gaps_fn(vtt_path, max_internal_gap_sec)
    metadata: Dict[str, Any] = {
        "enabled": True,
        "threshold_seconds": float(max_internal_gap_sec),
        "detected_before_count": int(initial_analysis.get("gap_count", 0)),
        "largest_gap_before_seconds": float(initial_analysis.get("largest_gap_sec", 0.0)),
        "rerun_attempted": False,
        "rerun_attempts": 0,
        "rerun_successes": 0,
        "inserted_cue_count": 0,
        "detected_after_count": int(initial_analysis.get("gap_count", 0)),
        "largest_gap_after_seconds": float(initial_analysis.get("largest_gap_sec", 0.0)),
        "read_ok": bool(initial_analysis.get("read_ok", False)),
    }
    if not initial_analysis.get("read_ok", False):
        metadata["note"] = "vtt_read_failed"
        return metadata

    suspicious_gaps = list(initial_analysis.get("gaps", []))
    if not suspicious_gaps:
        metadata["note"] = "no_suspicious_gap_detected"
        return metadata

    read_ok, existing_cues = resolved_context.read_vtt_cues_fn(vtt_path)
    if not read_ok:
        metadata["note"] = "vtt_cue_parse_failed"
        return metadata

    audio_duration_sec = resolved_context.probe_duration_seconds_fn(audio_src)
    transcription_language = resolved_context.normalize_language_fn(
        detected_language
    ) or resolved_context.resolve_transcription_language_fn("auto")
    repair_dir = work_dir / "_gap_repairs"
    max_attempts = max(0, int(max_repair_attempts))
    candidate_gaps = sorted(
        suspicious_gaps,
        key=lambda gap: float(gap.get("gap_sec", 0.0)),
        reverse=True,
    )[:max_attempts]

    inserted_cues: list[tuple[float, float, str]] = []
    rerun_successes = 0
    rerun_attempts = 0
    for gap in candidate_gaps:
        gap_start_sec = float(gap.get("previous_end_sec", 0.0))
        gap_end_sec = float(gap.get("next_start_sec", 0.0))
        padded_start_sec = max(0.0, gap_start_sec - float(resolved_context.context_padding_seconds))
        padded_end_sec = gap_end_sec + float(resolved_context.context_padding_seconds)
        if audio_duration_sec > 0:
            padded_end_sec = min(float(audio_duration_sec), padded_end_sec)
        window_duration_sec = padded_end_sec - padded_start_sec
        if window_duration_sec < float(resolved_context.min_window_seconds):
            continue

        rerun_attempts += 1
        ok, recovered_cues = resolved_context.run_gap_window_rerun_fn(
            audio_src=audio_src,
            out_dir=repair_dir,
            model=model,
            whisper_models_dir=whisper_models_dir,
            use_gpu=use_gpu,
            gpu_device=gpu_device,
            vad_filter=vad_filter,
            timeout_sec=timeout_sec,
            transcription_language=transcription_language,
            start_sec=padded_start_sec,
            duration_sec=window_duration_sec,
            gap_start_sec=gap_start_sec,
            gap_end_sec=gap_end_sec,
            overlap_tolerance_sec=resolved_context.overlap_tolerance_seconds,
            debug=debug,
        )
        if not ok or not recovered_cues:
            continue
        rerun_successes += 1
        inserted_cues.extend(recovered_cues)

    metadata["rerun_attempted"] = rerun_attempts > 0
    metadata["rerun_attempts"] = rerun_attempts
    metadata["rerun_successes"] = rerun_successes
    metadata["inserted_cue_count"] = len(inserted_cues)

    original_vtt_content = write_repaired_vtt_from_cues(
        vtt_path=vtt_path,
        existing_cues=existing_cues,
        inserted_cues=inserted_cues,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        context=resolved_context,
    )

    final_analysis = resolved_context.detect_vtt_internal_gaps_fn(vtt_path, max_internal_gap_sec)
    final_gap_repair_analysis_metadata(
        vtt_path=vtt_path,
        metadata=metadata,
        initial_analysis=initial_analysis,
        final_analysis=final_analysis,
        inserted_cue_count=len(inserted_cues),
        original_vtt_content=original_vtt_content,
    )
    return metadata


def default_non_blocking_internal_gap_metadata(
    *,
    note: str,
    threshold_seconds: float,
    allowed_gap_count: int,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the default non-blocking internal-gap metadata payload."""
    metadata: Dict[str, Any] = {
        "enabled": True,
        "blocking": False,
        "threshold_seconds": float(threshold_seconds),
        "allowed_gap_count": int(allowed_gap_count),
        "note": note,
    }
    if error:
        metadata["error"] = error
    return metadata


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
    context: NonBlockingGapRepairContext,
) -> Dict[str, Any]:
    """Run best-effort internal-gap repair without ever blocking the workflow."""
    resolved_context = context

    metadata = resolved_context.default_non_blocking_internal_gap_metadata_fn(
        note="pre_translation_repair_not_run",
        threshold_seconds=resolved_context.max_vtt_internal_gap_seconds,
        allowed_gap_count=resolved_context.max_vtt_internal_gap_count,
    )
    try:
        metadata = resolved_context.attempt_best_effort_vtt_internal_gap_repair_fn(
            vtt_path=expected_vtt,
            audio_src=audio_src,
            work_dir=work_dir,
            model=str(args.model),
            whisper_models_dir=str(args.whisper_models_dir),
            use_gpu=effective_use_gpu,
            gpu_device=int(args.gpu_device),
            vad_filter=(args.vad_filter == "true"),
            timeout_sec=timeout_sec,
            detected_language=detected_language,
            max_internal_gap_sec=resolved_context.max_vtt_internal_gap_seconds,
            max_repair_attempts=resolved_context.max_internal_gap_repair_attempts,
            max_line_width=vtt_max_line_width,
            max_line_count=vtt_max_line_count,
            debug=debug,
        )
    except Exception as exc:
        metadata = resolved_context.default_non_blocking_internal_gap_metadata_fn(
            note="pre_translation_repair_exception",
            threshold_seconds=resolved_context.max_vtt_internal_gap_seconds,
            allowed_gap_count=resolved_context.max_vtt_internal_gap_count,
            error=str(exc),
        )
        print(f"VTT internal-gap repair warning: {exc}")

    if int(metadata.get("detected_before_count", 0)) > 0:
        print(
            "VTT internal-gap check (non-blocking): "
            f"detected={int(metadata.get('detected_before_count', 0))}, "
            f"recovered_cues={int(metadata.get('inserted_cue_count', 0))}, "
            f"remaining={int(metadata.get('detected_after_count', 0))}"
        )
    return metadata
