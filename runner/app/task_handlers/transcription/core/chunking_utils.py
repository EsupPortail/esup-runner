"""Chunking and long-audio transcription helpers.

Builds Whisper call options with chunk-aware defaults for long recordings.
Runs bounded audio-window transcription to reduce RAM and timeout pressure.
Merges chunk outputs back into a continuous subtitle timeline.
"""

import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def build_transcribe_kwargs(
    language: str,
    vad_filter: bool,
    device: str,
    *,
    chunked: bool,
) -> Dict[str, object]:
    """Build keyword arguments for a Whisper Python transcription call."""
    kwargs: Dict[str, object] = {
        "fp16": device == "cuda",
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    }
    if language and language.lower() != "auto":
        kwargs["language"] = language
    kwargs["vad_filter"] = bool(vad_filter)
    if chunked:
        kwargs["condition_on_previous_text"] = False
        kwargs["temperature"] = 0.0
        kwargs["word_timestamps"] = True
        kwargs["hallucination_silence_threshold"] = 2.0
    else:
        kwargs["condition_on_previous_text"] = True
        kwargs["word_timestamps"] = True
    return kwargs


def transcribe_audio(
    wmodel: Any,
    audio_path: Path,
    transcribe_kwargs: Dict[str, object],
) -> Optional[Dict[str, object]]:
    """Run Whisper transcription and gracefully retry without unsupported options."""
    try:
        return wmodel.transcribe(str(audio_path), **transcribe_kwargs)  # type: ignore
    except TypeError:
        kwargs = dict(transcribe_kwargs)
        kwargs.pop("vad_filter", None)
        return wmodel.transcribe(str(audio_path), **kwargs)  # type: ignore
    except Exception as exc:
        print(f"Whisper transcription failed: {exc}")
        return None


def normalize_chunk_overlap_seconds(chunk_duration_sec: int, chunk_overlap_sec: int) -> int:
    """Clamp chunk overlap to a sane range for the configured chunk duration."""
    if chunk_duration_sec <= 1:
        return 0
    return max(0, min(int(chunk_overlap_sec), int(chunk_duration_sec) - 1))


def resolve_chunk_threshold_seconds(
    configured_value: object,
    use_gpu: bool,
    *,
    cpu_threshold_seconds: int,
    gpu_threshold_seconds: int,
) -> int:
    """Return the chunk threshold to use for the current hardware profile."""
    try:
        if configured_value is not None and str(configured_value).strip() != "":
            return max(0, int(str(configured_value)))
    except (TypeError, ValueError):
        pass

    return gpu_threshold_seconds if use_gpu else cpu_threshold_seconds


def plan_audio_chunks(
    total_duration_sec: float,
    chunk_duration_sec: int,
    chunk_threshold_sec: int,
    chunk_overlap_sec: int,
    *,
    normalize_chunk_overlap_seconds_fn: Callable[[int, int], int],
) -> list[tuple[float, float]]:
    """Return chunk boundaries for long audio transcriptions."""
    if total_duration_sec <= 0:
        return []

    if chunk_duration_sec <= 0:
        return [(0.0, float(total_duration_sec))]

    if chunk_threshold_sec > 0 and total_duration_sec <= chunk_threshold_sec:
        return [(0.0, float(total_duration_sec))]

    normalized_overlap_sec = normalize_chunk_overlap_seconds_fn(
        int(chunk_duration_sec),
        int(chunk_overlap_sec),
    )
    chunks: list[tuple[float, float]] = []
    start_sec = 0.0
    while start_sec < total_duration_sec:
        duration_sec = min(float(chunk_duration_sec), float(total_duration_sec - start_sec))
        chunks.append((round(start_sec, 3), round(duration_sec, 3)))
        end_sec = start_sec + duration_sec
        if end_sec >= total_duration_sec:
            break
        start_sec = max(start_sec + 1.0, end_sec - float(normalized_overlap_sec))
    return chunks


def extract_audio_chunk(
    audio_path: Path,
    chunk_path: Path,
    start_sec: float,
    duration_sec: float,
    timeout_sec: int,
    debug: bool,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> int:
    """Extract a mono 16kHz MP3 chunk used for bounded-memory transcription."""
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(chunk_path),
    ]
    if debug:
        print("Executing:", " ".join(cmd), flush=True)
    try:
        proc = subprocess_run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        print(f"ffmpeg chunk extraction timed out after {timeout_sec}s")
        return 124
    if debug:
        print(proc.stdout)
        print(proc.stderr)
    return int(proc.returncode)


def offset_timestamp(value: object, offset_sec: float) -> object:
    """Offset a Whisper timestamp when it is numeric."""
    if isinstance(value, (int, float)):
        return round(float(value) + offset_sec, 3)
    return value


def offset_segment_timestamps(
    segment: Dict[str, object],
    segment_id: int,
    offset_sec: float,
    *,
    offset_timestamp_fn: Callable[[object, float], object],
) -> Dict[str, object]:
    """Offset segment and word timestamps for a chunked transcription merge."""
    shifted = dict(segment)
    shifted["id"] = segment_id
    shifted["start"] = offset_timestamp_fn(shifted.get("start"), offset_sec)
    shifted["end"] = offset_timestamp_fn(shifted.get("end"), offset_sec)

    words = shifted.get("words")
    if isinstance(words, list):
        shifted_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            shifted_word = dict(word)
            shifted_word["start"] = offset_timestamp_fn(shifted_word.get("start"), offset_sec)
            shifted_word["end"] = offset_timestamp_fn(shifted_word.get("end"), offset_sec)
            shifted_words.append(shifted_word)
        shifted["words"] = shifted_words

    return shifted


def compute_chunk_keep_window(
    chunk_plan: list[tuple[float, float]],
    chunk_index: int,
) -> tuple[float, float]:
    """Split overlap evenly so adjacent chunks do not duplicate subtitle cues."""
    chunk_start_sec, chunk_duration_sec = chunk_plan[chunk_index]
    chunk_end_sec = chunk_start_sec + chunk_duration_sec
    keep_start_sec = float(chunk_start_sec)
    keep_end_sec = float(chunk_end_sec)

    if chunk_index > 0:
        prev_start_sec, prev_duration_sec = chunk_plan[chunk_index - 1]
        prev_end_sec = prev_start_sec + prev_duration_sec
        if chunk_start_sec < prev_end_sec:
            keep_start_sec = chunk_start_sec + ((prev_end_sec - chunk_start_sec) / 2.0)

    if chunk_index + 1 < len(chunk_plan):
        next_start_sec, _ = chunk_plan[chunk_index + 1]
        if next_start_sec < chunk_end_sec:
            keep_end_sec = next_start_sec + ((chunk_end_sec - next_start_sec) / 2.0)

    return round(keep_start_sec, 3), round(keep_end_sec, 3)


def trim_segment_to_time_window(
    segment: Dict[str, object],
    keep_start_sec: float,
    keep_end_sec: float,
    *,
    safe_float_fn: Callable[[object], Optional[float]],
) -> Optional[Dict[str, object]]:
    """Trim a segment to the portion that should be kept after chunk overlap merging."""
    start_sec = safe_float_fn(segment.get("start"))
    end_sec = safe_float_fn(segment.get("end"))
    if start_sec is None or end_sec is None:
        return segment
    if end_sec <= keep_start_sec or start_sec >= keep_end_sec:
        return None

    trimmed = dict(segment)
    trimmed["start"] = round(max(start_sec, keep_start_sec), 3)
    trimmed["end"] = round(min(end_sec, keep_end_sec), 3)

    words = trimmed.get("words")
    if isinstance(words, list):
        trimmed_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            word_start_sec = safe_float_fn(word.get("start"))
            word_end_sec = safe_float_fn(word.get("end"))
            if word_start_sec is None or word_end_sec is None:
                trimmed_words.append(dict(word))
                continue
            if word_end_sec <= keep_start_sec or word_start_sec >= keep_end_sec:
                continue
            trimmed_word = dict(word)
            trimmed_word["start"] = round(max(word_start_sec, keep_start_sec), 3)
            trimmed_word["end"] = round(min(word_end_sec, keep_end_sec), 3)
            trimmed_words.append(trimmed_word)
        trimmed["words"] = trimmed_words

    return trimmed


def merge_adjacent_identical_segment(
    merged_segments: list[Dict[str, object]],
    next_segment: Dict[str, object],
    *,
    safe_float_fn: Callable[[object], Optional[float]],
) -> bool:
    """Merge adjacent identical cues that can appear on chunk overlap boundaries."""
    if not merged_segments:
        return False

    previous_segment = merged_segments[-1]
    previous_text = str(previous_segment.get("text", "")).strip()
    next_text = str(next_segment.get("text", "")).strip()
    if not previous_text or previous_text != next_text:
        return False

    previous_end_sec = safe_float_fn(previous_segment.get("end"))
    next_start_sec = safe_float_fn(next_segment.get("start"))
    next_end_sec = safe_float_fn(next_segment.get("end"))
    if previous_end_sec is None or next_start_sec is None or next_end_sec is None:
        return False
    if next_start_sec > previous_end_sec + 0.05:
        return False

    previous_segment["end"] = round(max(previous_end_sec, next_end_sec), 3)
    previous_words = previous_segment.get("words")
    next_words = next_segment.get("words")
    if isinstance(previous_words, list) and isinstance(next_words, list):
        previous_words.extend(next_words)
    return True


def resolve_keep_window(
    keep_windows: Optional[list[tuple[float, float]]],
    chunk_index: int,
) -> tuple[Optional[float], Optional[float]]:
    """Return the keep window that applies to one chunk result."""
    if keep_windows is None or chunk_index >= len(keep_windows):
        return None, None
    keep_start_sec, keep_end_sec = keep_windows[chunk_index]
    return keep_start_sec, keep_end_sec


def append_chunk_segments(
    merged_segments: list[Dict[str, object]],
    chunk_segments: list[object],
    next_segment_id: int,
    offset_sec: float,
    keep_window: tuple[Optional[float], Optional[float]],
    *,
    offset_segment_timestamps_fn: Callable[[Dict[str, object], int, float], Dict[str, object]],
    trim_segment_to_time_window_fn: Callable[
        [Dict[str, object], float, float], Optional[Dict[str, object]]
    ],
    merge_adjacent_identical_segment_fn: Callable[
        [list[Dict[str, object]], Dict[str, object]], bool
    ],
) -> int:
    """Offset, trim, and append one chunk's segments into the merged result."""
    keep_start_sec, keep_end_sec = keep_window

    for segment in chunk_segments:
        if not isinstance(segment, dict):
            continue

        shifted_segment = offset_segment_timestamps_fn(segment, next_segment_id, offset_sec)
        if keep_start_sec is not None and keep_end_sec is not None:
            trimmed_segment = trim_segment_to_time_window_fn(
                shifted_segment,
                keep_start_sec,
                keep_end_sec,
            )
            if trimmed_segment is None:
                continue
            shifted_segment = trimmed_segment

        if merge_adjacent_identical_segment_fn(merged_segments, shifted_segment):
            continue

        merged_segments.append(shifted_segment)
        next_segment_id += 1

    return next_segment_id


def build_merged_result_text(segments: list[Dict[str, object]]) -> str:
    """Join non-empty segment texts into Whisper's flattened `text` field."""
    return " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    )


def combine_chunk_results(
    chunk_results: list[tuple[float, Dict[str, object]]],
    keep_windows: Optional[list[tuple[float, float]]] = None,
    *,
    extract_detected_language_fn: Callable[[Dict[str, object]], Optional[str]],
    append_chunk_segments_fn: Callable[
        [
            list[Dict[str, object]],
            list[object],
            int,
            float,
            tuple[Optional[float], Optional[float]],
        ],
        int,
    ],
    resolve_keep_window_fn: Callable[
        [Optional[list[tuple[float, float]]], int], tuple[Optional[float], Optional[float]]
    ],
    build_merged_result_text_fn: Callable[[list[Dict[str, object]]], str],
) -> Dict[str, object]:
    """Merge multiple Whisper chunk results into a single writer-compatible result."""
    merged_segments: list[Dict[str, object]] = []
    detected_language: Optional[str] = None
    next_segment_id = 0

    for chunk_index, (offset_sec, chunk_result) in enumerate(chunk_results):
        if detected_language is None:
            detected_language = extract_detected_language_fn(chunk_result)

        chunk_segments = chunk_result.get("segments")
        if not isinstance(chunk_segments, list):
            continue
        next_segment_id = append_chunk_segments_fn(
            merged_segments,
            chunk_segments,
            next_segment_id,
            offset_sec,
            resolve_keep_window_fn(keep_windows, chunk_index),
        )

    merged: Dict[str, object] = {
        "text": build_merged_result_text_fn(merged_segments),
        "segments": merged_segments,
    }
    if detected_language:
        merged["language"] = detected_language
    return merged


def build_initial_prompt_from_text(text: str, max_chars: int = 224) -> Optional[str]:
    """Build a compact prompt tail reused as context for the next chunk."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized
    return normalized[-max_chars:]


def prepare_transcription_plan(
    audio_path: Path,
    language: str,
    vad_filter: bool,
    device: str,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    debug: bool,
    *,
    probe_duration_seconds_fn: Callable[[Path], float],
    plan_audio_chunks_fn: Callable[[float, int, int, int], list[tuple[float, float]]],
    build_transcribe_kwargs_fn: Callable[[str, bool, str, bool], Dict[str, object]],
) -> tuple[float, list[tuple[float, float]], Dict[str, object]]:
    """Build the chunk plan and matching Whisper kwargs for this audio file."""
    input_duration_sec = probe_duration_seconds_fn(audio_path)
    chunk_plan = plan_audio_chunks_fn(
        input_duration_sec,
        int(chunk_duration_sec),
        int(chunk_threshold_sec),
        int(chunk_overlap_sec),
    )
    transcribe_kwargs = build_transcribe_kwargs_fn(
        language,
        vad_filter,
        device,
        len(chunk_plan) > 1,
    )
    if debug:
        print(f"Transcribing: {audio_path} with args {transcribe_kwargs}")
    return input_duration_sec, chunk_plan, transcribe_kwargs


def build_chunk_transcribe_kwargs(
    transcribe_kwargs: Dict[str, object],
    detected_language: Optional[str],
    explicit_language: bool,
    previous_chunk_text: str,
    *,
    build_initial_prompt_from_text_fn: Callable[[str], Optional[str]],
) -> Dict[str, object]:
    """Apply chunk-to-chunk context before transcribing the next chunk."""
    chunk_kwargs = dict(transcribe_kwargs)
    if detected_language and not explicit_language:
        chunk_kwargs["language"] = detected_language

    initial_prompt = build_initial_prompt_from_text_fn(previous_chunk_text)
    if initial_prompt:
        chunk_kwargs["initial_prompt"] = initial_prompt
    return chunk_kwargs


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
    *,
    extract_audio_chunk_fn: Callable[..., int],
    build_chunk_transcribe_kwargs_fn: Callable[
        [Dict[str, object], Optional[str], bool, str], Dict[str, object]
    ],
    transcribe_audio_fn: Callable[[Any, Path, Dict[str, object]], Optional[Dict[str, object]]],
    filter_result_segments_fn: Callable[
        [Dict[str, object], Optional[str], bool], Dict[str, object]
    ],
    extract_detected_language_fn: Callable[[Dict[str, object]], Optional[str]],
) -> tuple[int, Optional[Dict[str, object]], Optional[str], str]:
    """Extract, transcribe, and post-process one chunk of the input audio."""
    chunk_path = chunk_dir / f"{audio_path.stem}.chunk_{chunk_index:04d}.mp3"
    rc = extract_audio_chunk_fn(
        audio_path=audio_path,
        chunk_path=chunk_path,
        start_sec=start_sec,
        duration_sec=duration_sec,
        timeout_sec=timeout_sec,
        debug=debug,
    )
    if rc != 0:
        print(f"Whisper chunk extraction failed (chunk {chunk_index + 1}/{chunk_count}, rc={rc})")
        return 22, None, detected_language, previous_chunk_text

    chunk_kwargs = build_chunk_transcribe_kwargs_fn(
        transcribe_kwargs,
        detected_language,
        explicit_language,
        previous_chunk_text,
    )
    if debug:
        print(
            "Transcribing chunk "
            f"{chunk_index + 1}/{chunk_count}: start={start_sec:.3f}s "
            f"duration={duration_sec:.3f}s"
        )

    chunk_result = transcribe_audio_fn(wmodel, chunk_path, chunk_kwargs)
    try:
        chunk_path.unlink(missing_ok=True)
    except Exception:
        pass

    if chunk_result is None:
        return 20, None, detected_language, previous_chunk_text

    chunk_expected_language = language if explicit_language else detected_language
    filtered_result = filter_result_segments_fn(
        chunk_result,
        chunk_expected_language,
        debug,
    )
    resolved_language = detected_language or extract_detected_language_fn(filtered_result)
    next_chunk_text = str(filtered_result.get("text", "")).strip()
    return 0, filtered_result, resolved_language, next_chunk_text


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
    *,
    normalize_chunk_overlap_seconds_fn: Callable[[int, int], int],
    compute_chunk_keep_window_fn: Callable[[list[tuple[float, float]], int], tuple[float, float]],
    transcribe_one_audio_chunk_fn: Callable[
        ..., tuple[int, Optional[Dict[str, object]], Optional[str], str]
    ],
    combine_chunk_results_fn: Callable[
        [list[tuple[float, Dict[str, object]]], Optional[list[tuple[float, float]]]],
        Dict[str, object],
    ],
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Run Whisper on overlapping chunks and merge the filtered results."""
    if debug:
        print(
            "Using chunked transcription: "
            f"{len(chunk_plan)} chunks, duration={input_duration_sec:.3f}s, "
            f"chunk_duration={int(chunk_duration_sec)}s, "
            f"chunk_overlap={normalize_chunk_overlap_seconds_fn(int(chunk_duration_sec), int(chunk_overlap_sec))}s"
        )

    merged_results: list[tuple[float, Dict[str, object]]] = []
    keep_windows = [compute_chunk_keep_window_fn(chunk_plan, i) for i in range(len(chunk_plan))]
    detected_language: Optional[str] = None
    explicit_language = bool(language and language.lower() != "auto")
    previous_chunk_text = ""
    chunk_dir = out_dir / "_chunks"

    for chunk_index, (start_sec, duration_sec) in enumerate(chunk_plan):
        rc, chunk_result, detected_language, previous_chunk_text = transcribe_one_audio_chunk_fn(
            wmodel=wmodel,
            audio_path=audio_path,
            chunk_dir=chunk_dir,
            chunk_index=chunk_index,
            chunk_count=len(chunk_plan),
            start_sec=start_sec,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            transcribe_kwargs=transcribe_kwargs,
            detected_language=detected_language,
            explicit_language=explicit_language,
            previous_chunk_text=previous_chunk_text,
            language=language,
            debug=debug,
        )
        if rc != 0:
            return rc, None, detected_language

        assert chunk_result is not None
        merged_results.append((start_sec, chunk_result))

    merged_result = combine_chunk_results_fn(merged_results, keep_windows)
    return 0, merged_result, detected_language


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
    *,
    prepare_transcription_plan_fn: Callable[
        ..., tuple[float, list[tuple[float, float]], Dict[str, object]]
    ],
    transcribe_audio_fn: Callable[[Any, Path, Dict[str, object]], Optional[Dict[str, object]]],
    extract_detected_language_fn: Callable[[Dict[str, object]], Optional[str]],
    run_chunked_whisper_transcription_fn: Callable[
        ..., tuple[int, Optional[Dict[str, object]], Optional[str]]
    ],
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Transcribe the file either in one pass or chunk-by-chunk."""
    input_duration_sec, chunk_plan, transcribe_kwargs = prepare_transcription_plan_fn(
        audio_path=audio_path,
        language=language,
        vad_filter=vad_filter,
        device=device,
        chunk_duration_sec=chunk_duration_sec,
        chunk_overlap_sec=chunk_overlap_sec,
        chunk_threshold_sec=chunk_threshold_sec,
        debug=debug,
    )

    if len(chunk_plan) <= 1:
        result = transcribe_audio_fn(wmodel, audio_path, transcribe_kwargs)
        if result is None:
            return 20, None, None
        return 0, result, extract_detected_language_fn(result)

    return run_chunked_whisper_transcription_fn(
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
    )
