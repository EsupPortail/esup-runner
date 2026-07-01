"""Default runtime wiring for source transcription.

Assembles the concrete callbacks used by the transcription flow context.
Selects CPU/GPU chunking thresholds and language normalization helpers.
Exposes a stable runtime API consumed by top-level orchestration.
"""

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

import chunking_utils
import language_utils
import runtime_cli_utils
import transcription_flow_utils
import whisper_python_runtime_utils
from runtime_args_utils import _CPU_CHUNK_THRESHOLD_SECONDS, _GPU_CHUNK_THRESHOLD_SECONDS

build_transcribe_kwargs = chunking_utils.build_transcribe_kwargs
transcribe_audio = chunking_utils.transcribe_audio
normalize_chunk_overlap_seconds = chunking_utils.normalize_chunk_overlap_seconds
extract_audio_chunk = whisper_python_runtime_utils.extract_audio_chunk
write_vtt_result = whisper_python_runtime_utils.write_vtt_result


def resolve_transcription_language(requested_source_language: str) -> str:
    """Return the Whisper source language, defaulting to auto-detection."""
    normalized_source_language = cast(
        Optional[str],
        language_utils.normalize_language_code(requested_source_language),
    )
    if not normalized_source_language or normalized_source_language == "auto":
        return "auto"
    return normalized_source_language


def resolve_chunk_threshold_seconds(configured_value: object, use_gpu: bool) -> int:
    """Return the default chunk threshold for the current hardware profile."""
    return cast(
        int,
        chunking_utils.resolve_chunk_threshold_seconds(
            configured_value,
            use_gpu,
            cpu_threshold_seconds=_CPU_CHUNK_THRESHOLD_SECONDS,
            gpu_threshold_seconds=_GPU_CHUNK_THRESHOLD_SECONDS,
        ),
    )


def plan_audio_chunks(
    total_duration_sec: float,
    chunk_duration_sec: int,
    chunk_threshold_sec: int,
    chunk_overlap_sec: int,
) -> list[tuple[float, float]]:
    """Return chunk boundaries for long audio transcriptions."""
    return cast(
        list[tuple[float, float]],
        chunking_utils.plan_audio_chunks(
            total_duration_sec,
            chunk_duration_sec,
            chunk_threshold_sec,
            chunk_overlap_sec,
            normalize_chunk_overlap_seconds_fn=normalize_chunk_overlap_seconds,
        ),
    )


def build_transcription_flow_context() -> transcription_flow_utils.TranscriptionFlowContext:
    """Build the default dependency context for source transcription."""
    return transcription_flow_utils.TranscriptionFlowContext(
        resolve_transcription_language_fn=resolve_transcription_language,
        resolve_chunk_threshold_seconds_fn=resolve_chunk_threshold_seconds,
        run_whisper_python_fn=whisper_python_runtime_utils.run_whisper_python,
        run_whisper_cli_fn=runtime_cli_utils.run_whisper_cli_with_defaults,
        normalize_language_code_fn=language_utils.normalize_language_code,
    )


def run_transcription(
    args: Any,
    audio_src: Path,
    work_dir: Path,
    timeout_sec: int,
    effective_use_gpu: bool,
    debug: bool,
) -> tuple[int, Optional[str]]:
    """Run source-language transcription via the Python API first, then CLI."""
    return cast(
        tuple[int, Optional[str]],
        transcription_flow_utils.run_transcription(
            args,
            audio_src,
            work_dir,
            timeout_sec,
            effective_use_gpu,
            debug,
            context=build_transcription_flow_context(),
        ),
    )


def build_whisper_fallback_options(
    *,
    args: Any,
    effective_use_gpu: bool,
    timeout_sec: int,
    vtt_max_line_count: int,
    vtt_max_line_width: int,
) -> Dict[str, Any]:
    """Build options shared with Whisper fallback translation helpers."""
    return cast(
        Dict[str, Any],
        transcription_flow_utils.build_whisper_fallback_options(
            args=args,
            effective_use_gpu=effective_use_gpu,
            timeout_sec=timeout_sec,
            vtt_max_line_count=vtt_max_line_count,
            vtt_max_line_width=vtt_max_line_width,
            context=build_transcription_flow_context(),
        ),
    )
