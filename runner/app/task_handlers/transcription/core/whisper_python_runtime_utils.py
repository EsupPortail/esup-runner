"""Whisper Python API runtime wiring for transcription.

Runs Python-side transcription with chunk extraction and segment filtering hooks.
Adapts Whisper outputs into cleaned cues and final VTT serialization.
Provides runtime-safe wrappers so failures map to explicit return codes.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, cast

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import chunking_utils
import language_utils
import runtime_cli_utils
import runtime_media_utils
import segment_filter_utils
import vtt_postprocess_utils


def extract_audio_chunk(
    audio_path: Path,
    chunk_path: Path,
    start_sec: float,
    duration_sec: float,
    timeout_sec: int,
    debug: bool,
) -> int:
    """Extract a mono 16kHz MP3 chunk used for bounded-memory transcription."""
    return cast(
        int,
        chunking_utils.extract_audio_chunk(
            audio_path,
            chunk_path,
            start_sec,
            duration_sec,
            timeout_sec,
            debug,
            subprocess_run=subprocess.run,
        ),
    )


def write_vtt_result(
    result: Dict[str, object],
    audio_path: Path,
    out_dir: Path,
    get_writer: Any,
    word_options: Dict[str, object],
    debug: bool,
) -> bool:
    """Write a Whisper transcription result to a VTT file."""
    return cast(
        bool,
        vtt_postprocess_utils.write_vtt_result(
            result,
            audio_path,
            out_dir,
            get_writer,
            word_options,
            debug,
        ),
    )


def load_whisper_runtime_model(
    torch: Any,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    debug: bool,
) -> tuple[int, Optional[Any], str]:
    """Load the Whisper model and transparently fall back to CPU if needed."""
    model_name = runtime_cli_utils.map_model_name(model, "python")
    device = "cuda" if use_gpu else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    if debug:
        print(f"Loading whisper model '{model_name}' on {device} (dtype={dtype})")

    wmodel = runtime_cli_utils.load_whisper_model(
        model_name,
        device,
        whisper_models_dir=whisper_models_dir,
    )
    if wmodel is None and device == "cuda":
        print("Retrying whisper model load on CPU")
        device = "cpu"
        wmodel = runtime_cli_utils.load_whisper_model(
            model_name,
            device,
            whisper_models_dir=whisper_models_dir,
        )

    if wmodel is None:
        return 10, None, device
    return 0, wmodel, device


def prepare_transcription_plan(
    audio_path: Path,
    language: str,
    vad_filter: bool,
    device: str,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    debug: bool,
) -> tuple[float, list[tuple[float, float]], Dict[str, object]]:
    """Build the chunk plan and matching Whisper kwargs for this audio file."""
    return cast(
        tuple[float, list[tuple[float, float]], Dict[str, object]],
        chunking_utils.prepare_transcription_plan(
            audio_path=audio_path,
            language=language,
            vad_filter=vad_filter,
            device=device,
            chunk_duration_sec=chunk_duration_sec,
            chunk_overlap_sec=chunk_overlap_sec,
            chunk_threshold_sec=chunk_threshold_sec,
            debug=debug,
            probe_duration_seconds_fn=lambda path: runtime_media_utils.probe_duration_seconds(
                path,
                debug=debug,
                subprocess_run=subprocess.run,
            ),
            plan_audio_chunks_fn=lambda total, duration, threshold, overlap: chunking_utils.plan_audio_chunks(
                total,
                duration,
                threshold,
                overlap,
                normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
            ),
            build_transcribe_kwargs_fn=lambda lang, vad, dev, chunked: chunking_utils.build_transcribe_kwargs(
                lang,
                vad,
                dev,
                chunked=chunked,
            ),
        ),
    )


def build_chunk_transcribe_kwargs(
    transcribe_kwargs: Dict[str, object],
    detected_language: Optional[str],
    explicit_language: bool,
    previous_chunk_text: str,
) -> Dict[str, object]:
    """Apply chunk-to-chunk context before transcribing the next chunk."""
    return cast(
        Dict[str, object],
        chunking_utils.build_chunk_transcribe_kwargs(
            transcribe_kwargs,
            detected_language,
            explicit_language,
            previous_chunk_text,
            build_initial_prompt_from_text_fn=chunking_utils.build_initial_prompt_from_text,
        ),
    )


def transcribe_one_audio_chunk(
    wmodel: Any,
    audio_path: Path,
    chunk_dir: Path,
    chunk_index: int,
    chunk_count: int,
    start_sec: float,
    duration_sec: float,
    timeout_sec: int,
    transcribe_kwargs: Dict[str, object],
    detected_language: Optional[str],
    explicit_language: bool,
    previous_chunk_text: str,
    language: str,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str], str]:
    """Extract, transcribe, and post-process one chunk of the input audio."""
    return cast(
        tuple[int, Optional[Dict[str, object]], Optional[str], str],
        chunking_utils.transcribe_one_audio_chunk(
            wmodel=wmodel,
            audio_path=audio_path,
            chunk_dir=chunk_dir,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            start_sec=start_sec,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            transcribe_kwargs=transcribe_kwargs,
            detected_language=detected_language,
            explicit_language=explicit_language,
            previous_chunk_text=previous_chunk_text,
            language=language,
            debug=debug,
            extract_audio_chunk_fn=extract_audio_chunk,
            build_chunk_transcribe_kwargs_fn=build_chunk_transcribe_kwargs,
            transcribe_audio_fn=chunking_utils.transcribe_audio,
            filter_result_segments_fn=lambda result, expected_language, debug_enabled: segment_filter_utils.filter_result_segments(
                result,
                expected_language=expected_language,
                debug=debug_enabled,
            ),
            extract_detected_language_fn=segment_filter_utils.extract_detected_language,
        ),
    )


def combine_chunk_results_with_defaults(
    chunk_results: list[tuple[float, Dict[str, object]]],
    keep_windows: Optional[list[tuple[float, float]]] = None,
) -> Dict[str, object]:
    """Merge chunk results using the default segment-filtering callbacks."""
    return cast(
        Dict[str, object],
        chunking_utils.combine_chunk_results(
            chunk_results,
            keep_windows=keep_windows,
            extract_detected_language_fn=segment_filter_utils.extract_detected_language,
            append_chunk_segments_fn=lambda merged_segments, chunk_segments, next_segment_id, offset_sec, keep_window: chunking_utils.append_chunk_segments(
                merged_segments,
                chunk_segments,
                next_segment_id,
                offset_sec,
                keep_window,
                offset_segment_timestamps_fn=lambda segment, segment_id, segment_offset_sec: chunking_utils.offset_segment_timestamps(
                    segment,
                    segment_id,
                    segment_offset_sec,
                    offset_timestamp_fn=chunking_utils.offset_timestamp,
                ),
                trim_segment_to_time_window_fn=lambda segment, keep_start_sec, keep_end_sec: chunking_utils.trim_segment_to_time_window(
                    segment,
                    keep_start_sec,
                    keep_end_sec,
                    safe_float_fn=segment_filter_utils.safe_float,
                ),
                merge_adjacent_identical_segment_fn=lambda merged, next_segment: chunking_utils.merge_adjacent_identical_segment(
                    merged,
                    next_segment,
                    safe_float_fn=segment_filter_utils.safe_float,
                ),
            ),
            resolve_keep_window_fn=chunking_utils.resolve_keep_window,
            build_merged_result_text_fn=chunking_utils.build_merged_result_text,
        ),
    )


def run_chunked_whisper_transcription(
    wmodel: Any,
    audio_path: Path,
    out_dir: Path,
    chunk_plan: list[tuple[float, float]],
    input_duration_sec: float,
    transcribe_kwargs: Dict[str, object],
    language: str,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Run Whisper on overlapping chunks and merge the filtered results."""
    return cast(
        tuple[int, Optional[Dict[str, object]], Optional[str]],
        chunking_utils.run_chunked_whisper_transcription(
            wmodel=wmodel,
            audio_path=audio_path,
            out_dir=out_dir,
            chunk_plan=chunk_plan,
            input_duration_sec=input_duration_sec,
            transcribe_kwargs=transcribe_kwargs,
            language=language,
            timeout_sec=timeout_sec,
            chunk_duration_sec=chunk_duration_sec,
            chunk_overlap_sec=chunk_overlap_sec,
            debug=debug,
            normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
            compute_chunk_keep_window_fn=chunking_utils.compute_chunk_keep_window,
            transcribe_one_audio_chunk_fn=transcribe_one_audio_chunk,
            combine_chunk_results_fn=combine_chunk_results_with_defaults,
        ),
    )


def run_whisper_python_transcription(
    wmodel: Any,
    audio_path: Path,
    out_dir: Path,
    language: str,
    vad_filter: bool,
    device: str,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Transcribe the file either in one pass or chunk-by-chunk."""
    return cast(
        tuple[int, Optional[Dict[str, object]], Optional[str]],
        chunking_utils.run_whisper_python_transcription(
            wmodel=wmodel,
            audio_path=audio_path,
            out_dir=out_dir,
            language=language,
            vad_filter=vad_filter,
            device=device,
            timeout_sec=timeout_sec,
            chunk_duration_sec=chunk_duration_sec,
            chunk_overlap_sec=chunk_overlap_sec,
            chunk_threshold_sec=chunk_threshold_sec,
            debug=debug,
            prepare_transcription_plan_fn=prepare_transcription_plan,
            transcribe_audio_fn=chunking_utils.transcribe_audio,
            extract_detected_language_fn=segment_filter_utils.extract_detected_language,
            run_chunked_whisper_transcription_fn=run_chunked_whisper_transcription,
        ),
    )


def run_whisper_python(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    vtt_highlight_words: bool,
    vtt_max_line_count: int,
    vtt_max_line_width: int,
    debug: bool,
) -> tuple[int, Optional[str]]:
    """Run Whisper via Python API and write VTT with custom writer options."""
    torch, whisper, get_writer = runtime_cli_utils.import_whisper_modules(use_gpu=use_gpu)
    if not torch or not whisper or not get_writer:
        return 255, None

    if debug:
        print(f"Using whisper Python API version: {whisper.__version__}")

    out_dir.mkdir(parents=True, exist_ok=True)
    rc, wmodel, device = load_whisper_runtime_model(
        torch=torch,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        debug=debug,
    )
    if rc != 0 or wmodel is None:
        return 10, None

    rc, result, detected_code = run_whisper_python_transcription(
        wmodel=wmodel,
        audio_path=audio_path,
        out_dir=out_dir,
        language=language,
        vad_filter=vad_filter,
        device=device,
        timeout_sec=timeout_sec,
        chunk_duration_sec=chunk_duration_sec,
        chunk_overlap_sec=chunk_overlap_sec,
        chunk_threshold_sec=chunk_threshold_sec,
        debug=debug,
    )
    if rc != 0:
        return rc, detected_code
    if result is None:
        return 20, detected_code

    expected_language = (
        language_utils.normalize_language_code(language)
        if language and language.lower() != "auto"
        else language_utils.normalize_language_code(detected_code)
    )
    result = segment_filter_utils.filter_result_segments(
        result,
        expected_language=expected_language,
        debug=debug,
    )
    detected_code = language_utils.normalize_language_code(
        segment_filter_utils.extract_detected_language(result) or detected_code
    )
    word_options: Dict[str, object] = {
        "highlight_words": bool(vtt_highlight_words),
        "max_line_count": int(vtt_max_line_count),
        "max_line_width": int(vtt_max_line_width),
    }
    if not write_vtt_result(result, audio_path, out_dir, get_writer, word_options, debug):
        return 21, detected_code

    return 0, detected_code
