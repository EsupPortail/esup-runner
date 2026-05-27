"""Transcription execution flow helpers.

Runs source-language transcription through Python API or CLI fallbacks.
Decides language and chunking strategy via injected runtime dependencies.
Normalizes outputs so downstream validation sees a consistent VTT artifact.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class TranscriptionFlowContext:
    """Dependencies used to execute source-language transcription."""

    resolve_transcription_language_fn: Callable[[str], str]
    resolve_chunk_threshold_seconds_fn: Callable[..., int]
    run_whisper_python_fn: Callable[..., tuple[int, Optional[str]]]
    run_whisper_cli_fn: Callable[..., tuple[int, Optional[str]]]
    normalize_language_code_fn: Callable[[Optional[str]], Optional[str]]


def run_transcription(
    args: Any,
    audio_src: Path,
    work_dir: Path,
    timeout_sec: int,
    effective_use_gpu: bool,
    debug: bool,
    *,
    context: TranscriptionFlowContext,
) -> tuple[int, Optional[str]]:
    """Run source-language transcription via Python API first, then CLI."""
    resolved_context = context

    transcription_language = resolved_context.resolve_transcription_language_fn(args.language)
    chunk_threshold_sec = resolved_context.resolve_chunk_threshold_seconds_fn(
        configured_value=args.chunk_threshold_seconds,
        use_gpu=effective_use_gpu,
    )

    backend_name = "whisper Python API"
    rc, detected_lang = resolved_context.run_whisper_python_fn(
        audio_path=audio_src,
        out_dir=work_dir,
        language=transcription_language,
        model=args.model,
        whisper_models_dir=str(args.whisper_models_dir),
        use_gpu=effective_use_gpu,
        gpu_device=int(args.gpu_device),
        vad_filter=(args.vad_filter == "true"),
        timeout_sec=timeout_sec,
        chunk_duration_sec=int(args.chunk_duration_seconds),
        chunk_overlap_sec=int(args.chunk_overlap_seconds),
        chunk_threshold_sec=chunk_threshold_sec,
        vtt_highlight_words=(str(args.vtt_highlight_words).lower() == "true"),
        vtt_max_line_count=int(args.vtt_max_line_count),
        vtt_max_line_width=int(args.vtt_max_line_width),
        debug=debug,
    )
    if rc == 255:
        print("Whisper Python API unavailable; attempting whisper CLI fallback.")
        rc, detected_lang = resolved_context.run_whisper_cli_fn(
            audio_path=audio_src,
            out_dir=work_dir,
            language=transcription_language,
            model=args.model,
            whisper_models_dir=str(args.whisper_models_dir),
            use_gpu=effective_use_gpu,
            gpu_device=int(args.gpu_device),
            vad_filter=(args.vad_filter == "true"),
            timeout_sec=timeout_sec,
            debug=debug,
        )
        backend_name = "whisper CLI"

    if rc != 0:
        print(f"{backend_name} failed with return code {rc}")

    return rc, resolved_context.normalize_language_code_fn(detected_lang)


def build_whisper_fallback_options(
    *,
    args: Any,
    effective_use_gpu: bool,
    timeout_sec: int,
    vtt_max_line_count: int,
    vtt_max_line_width: int,
    context: TranscriptionFlowContext,
) -> Dict[str, Any]:
    """Build options shared with Whisper fallback translation helpers."""
    return {
        "model": args.model,
        "whisper_models_dir": str(args.whisper_models_dir),
        "use_gpu": effective_use_gpu,
        "gpu_device": int(args.gpu_device),
        "vad_filter": args.vad_filter == "true",
        "timeout_sec": timeout_sec,
        "chunk_duration_sec": int(args.chunk_duration_seconds),
        "chunk_overlap_sec": int(args.chunk_overlap_seconds),
        "chunk_threshold_sec": context.resolve_chunk_threshold_seconds_fn(
            configured_value=args.chunk_threshold_seconds,
            use_gpu=effective_use_gpu,
        ),
        "vtt_highlight_words": str(args.vtt_highlight_words).lower() == "true",
        "vtt_max_line_count": int(vtt_max_line_count),
        "vtt_max_line_width": int(vtt_max_line_width),
    }
