"""Tests for transcription audio chunking."""

import subprocess
import types
from typing import Any

from transcription_test_helpers import load_transcription_core_module as _load_core_module


def test_chunking_utils_missing_branches(monkeypatch, tmp_path, capsys):
    """Validate Chunking utils missing branches."""
    chunking_utils = _load_core_module("chunking_utils")

    class RetryModel:
        def transcribe(self, _audio: str, **kwargs: Any) -> dict[str, Any]:
            if "vad_filter" in kwargs:
                raise TypeError("vad unsupported")
            return {"segments": []}

    assert chunking_utils.transcribe_audio(
        RetryModel(), tmp_path / "a.mp3", {"vad_filter": True}
    ) == {"segments": []}

    class FailingModel:
        def transcribe(self, _audio: str, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("fail")

    assert chunking_utils.transcribe_audio(FailingModel(), tmp_path / "a.mp3", {}) is None

    assert chunking_utils.normalize_chunk_overlap_seconds(1, 5) == 0
    assert (
        chunking_utils.resolve_chunk_threshold_seconds(
            object(),
            True,
            cpu_threshold_seconds=10,
            gpu_threshold_seconds=20,
        )
        == 20
    )

    assert (
        chunking_utils.plan_audio_chunks(
            0,
            30,
            60,
            2,
            normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
        )
        == []
    )

    assert chunking_utils.plan_audio_chunks(
        5,
        0,
        60,
        2,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
    ) == [(0.0, 5.0)]

    assert chunking_utils.plan_audio_chunks(
        5,
        30,
        60,
        2,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
    ) == [(0.0, 5.0)]

    proc_ok = types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
    assert (
        chunking_utils.extract_audio_chunk(
            tmp_path / "a.mp3",
            tmp_path / "chunk.mp3",
            0.0,
            1.0,
            5,
            True,
            subprocess_run=lambda *a, **k: proc_ok,
        )
        == 0
    )

    assert (
        chunking_utils.extract_audio_chunk(
            tmp_path / "a.mp3",
            tmp_path / "chunk.mp3",
            0.0,
            1.0,
            5,
            False,
            subprocess_run=lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("ffmpeg", 5)
            ),
        )
        == 124
    )

    assert chunking_utils.offset_timestamp("x", 1.0) == "x"

    shifted = chunking_utils.offset_segment_timestamps(
        {
            "start": 0.0,
            "end": 1.0,
            "words": ["bad", {"start": 0.1, "end": 0.2}],
        },
        segment_id=1,
        offset_sec=1.0,
        offset_timestamp_fn=chunking_utils.offset_timestamp,
    )
    assert shifted["id"] == 1

    assert chunking_utils.trim_segment_to_time_window(
        {"text": "x"},
        0.0,
        1.0,
        safe_float_fn=lambda _value: None,
    ) == {"text": "x"}

    assert (
        chunking_utils.trim_segment_to_time_window(
            {"start": 2.0, "end": 3.0},
            0.0,
            1.0,
            safe_float_fn=lambda value: float(value),
        )
        is None
    )

    trimmed = chunking_utils.trim_segment_to_time_window(
        {
            "start": 0.0,
            "end": 10.0,
            "words": [
                {"x": 1},
                {"start": -1.0, "end": 0.0},
                {"start": 2.0, "end": 5.0},
            ],
        },
        1.0,
        4.0,
        safe_float_fn=lambda value: float(value) if isinstance(value, (int, float)) else None,
    )
    assert trimmed is not None
    assert len(trimmed["words"]) == 2

    assert (
        chunking_utils.merge_adjacent_identical_segment(
            [{"text": "same", "end": "bad"}],
            {"text": "same", "start": 0.0, "end": 1.0},
            safe_float_fn=lambda value: float(value) if isinstance(value, (int, float)) else None,
        )
        is False
    )

    assert (
        chunking_utils.merge_adjacent_identical_segment(
            [{"text": "same", "start": 0.0, "end": 1.0}],
            {"text": "same", "start": 2.0, "end": 3.0},
            safe_float_fn=lambda value: float(value),
        )
        is False
    )

    previous = [{"text": "same", "start": 0.0, "end": 1.0, "words": []}]
    merged_ok = chunking_utils.merge_adjacent_identical_segment(
        previous,
        {"text": "same", "start": 1.0, "end": 1.5, "words": [{"w": 1}]},
        safe_float_fn=lambda value: float(value),
    )
    assert merged_ok is True
    assert previous[0]["end"] == 1.5

    next_segment_id = chunking_utils.append_chunk_segments(
        merged_segments=[],
        chunk_segments=["bad", {"text": "drop"}, {"text": "keep"}],
        next_segment_id=0,
        offset_sec=0.0,
        keep_window=(0.0, 1.0),
        offset_segment_timestamps_fn=lambda segment, segment_id, _offset: {
            **segment,
            "id": segment_id,
        },
        trim_segment_to_time_window_fn=lambda segment, _start, _end: (
            None if segment.get("text") == "drop" else segment
        ),
        merge_adjacent_identical_segment_fn=lambda merged, next_segment: False,
    )
    assert next_segment_id == 1

    combined = chunking_utils.combine_chunk_results(
        [(0.0, {"segments": "invalid"}), (1.0, {"segments": [{"text": "ok"}], "language": "fr"})],
        keep_windows=None,
        extract_detected_language_fn=lambda result: result.get("language"),
        append_chunk_segments_fn=lambda merged, chunk_segments, next_id, _off, _window: (
            merged.extend(chunk_segments) or (next_id + len(chunk_segments))
        ),
        resolve_keep_window_fn=chunking_utils.resolve_keep_window,
        build_merged_result_text_fn=chunking_utils.build_merged_result_text,
    )
    assert combined["language"] == "fr"

    assert chunking_utils.build_initial_prompt_from_text("   ") is None
    assert chunking_utils.build_initial_prompt_from_text("short") == "short"

    assert (
        chunking_utils.prepare_transcription_plan(
            audio_path=tmp_path / "a.mp3",
            language="auto",
            vad_filter=False,
            device="cpu",
            chunk_duration_sec=30,
            chunk_overlap_sec=2,
            chunk_threshold_sec=60,
            debug=True,
            probe_duration_seconds_fn=lambda _path: 10.0,
            plan_audio_chunks_fn=lambda *_a: [(0.0, 10.0)],
            build_transcribe_kwargs_fn=lambda *_a: {"x": 1},
        )[0]
        == 10.0
    )

    kwargs = chunking_utils.build_chunk_transcribe_kwargs(
        {"x": 1},
        detected_language="en",
        explicit_language=False,
        previous_chunk_text="hello",
        build_initial_prompt_from_text_fn=lambda text: "prompt" if text else None,
    )
    assert kwargs["language"] == "en"
    assert kwargs["initial_prompt"] == "prompt"

    rc, result, _lang, _text = chunking_utils.transcribe_one_audio_chunk(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        chunk_dir=tmp_path,
        chunk_index=0,
        chunk_count=1,
        start_sec=0.0,
        duration_sec=1.0,
        timeout_sec=3,
        transcribe_kwargs={},
        detected_language=None,
        explicit_language=False,
        previous_chunk_text="",
        language="auto",
        debug=False,
        extract_audio_chunk_fn=lambda **_k: 9,
        build_chunk_transcribe_kwargs_fn=lambda *_a: {},
        transcribe_audio_fn=lambda *_a: {"text": "ok"},
        filter_result_segments_fn=lambda result, _expected, _debug: result,
        extract_detected_language_fn=lambda _result: "en",
    )
    assert rc == 22
    assert result is None

    rc, result, _lang, _text = chunking_utils.transcribe_one_audio_chunk(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        chunk_dir=tmp_path,
        chunk_index=0,
        chunk_count=1,
        start_sec=0.0,
        duration_sec=1.0,
        timeout_sec=3,
        transcribe_kwargs={},
        detected_language=None,
        explicit_language=False,
        previous_chunk_text="",
        language="auto",
        debug=True,
        extract_audio_chunk_fn=lambda **_k: 0,
        build_chunk_transcribe_kwargs_fn=lambda *_a: {},
        transcribe_audio_fn=lambda *_a: None,
        filter_result_segments_fn=lambda result, _expected, _debug: result,
        extract_detected_language_fn=lambda _result: "en",
    )
    assert rc == 20
    assert result is None

    rc, result, resolved_language, next_text = chunking_utils.transcribe_one_audio_chunk(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        chunk_dir=tmp_path,
        chunk_index=0,
        chunk_count=1,
        start_sec=0.0,
        duration_sec=1.0,
        timeout_sec=3,
        transcribe_kwargs={},
        detected_language=None,
        explicit_language=False,
        previous_chunk_text="",
        language="auto",
        debug=False,
        extract_audio_chunk_fn=lambda **_k: 0,
        build_chunk_transcribe_kwargs_fn=lambda *_a: {},
        transcribe_audio_fn=lambda *_a: {"text": "chunk"},
        filter_result_segments_fn=lambda result, _expected, _debug: result,
        extract_detected_language_fn=lambda _result: "en",
    )
    assert rc == 0
    assert result is not None
    assert resolved_language == "en"
    assert next_text == "chunk"

    rc, merged_result, _detected = chunking_utils.run_chunked_whisper_transcription(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        out_dir=tmp_path,
        chunk_plan=[(0.0, 1.0)],
        input_duration_sec=1.0,
        transcribe_kwargs={},
        language="auto",
        timeout_sec=3,
        chunk_duration_sec=30,
        chunk_overlap_sec=2,
        debug=True,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
        compute_chunk_keep_window_fn=lambda _plan, _index: (0.0, 1.0),
        transcribe_one_audio_chunk_fn=lambda **_k: (9, None, None, ""),
        combine_chunk_results_fn=lambda *_a, **_k: {"text": ""},
    )
    assert rc == 9
    assert merged_result is None

    rc, merged_result, _detected = chunking_utils.run_chunked_whisper_transcription(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        out_dir=tmp_path,
        chunk_plan=[(0.0, 1.0)],
        input_duration_sec=1.0,
        transcribe_kwargs={},
        language="auto",
        timeout_sec=3,
        chunk_duration_sec=30,
        chunk_overlap_sec=2,
        debug=False,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
        compute_chunk_keep_window_fn=lambda _plan, _index: (0.0, 1.0),
        transcribe_one_audio_chunk_fn=lambda **_k: (0, {"text": "ok"}, "en", "ok"),
        combine_chunk_results_fn=lambda *_a, **_k: {"text": "merged"},
    )
    assert rc == 0
    assert merged_result == {"text": "merged"}

    rc, result, detected = chunking_utils.run_whisper_python_transcription(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        out_dir=tmp_path,
        language="auto",
        vad_filter=False,
        device="cpu",
        timeout_sec=3,
        chunk_duration_sec=30,
        chunk_overlap_sec=2,
        chunk_threshold_sec=60,
        debug=False,
        prepare_transcription_plan_fn=lambda **_k: (1.0, [(0.0, 1.0)], {}),
        transcribe_audio_fn=lambda *_a: None,
        extract_detected_language_fn=lambda _result: "en",
        run_chunked_whisper_transcription_fn=lambda **_k: (0, {"text": "chunked"}, "en"),
    )
    assert rc == 20
    assert result is None
    assert detected is None

    rc, result, detected = chunking_utils.run_whisper_python_transcription(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        out_dir=tmp_path,
        language="auto",
        vad_filter=False,
        device="cpu",
        timeout_sec=3,
        chunk_duration_sec=30,
        chunk_overlap_sec=2,
        chunk_threshold_sec=60,
        debug=False,
        prepare_transcription_plan_fn=lambda **_k: (1.0, [(0.0, 1.0)], {}),
        transcribe_audio_fn=lambda *_a: {"text": "ok"},
        extract_detected_language_fn=lambda _result: "en",
        run_chunked_whisper_transcription_fn=lambda **_k: (0, {"text": "chunked"}, "en"),
    )
    assert rc == 0
    assert result == {"text": "ok"}
    assert detected == "en"

    rc, result, detected = chunking_utils.run_whisper_python_transcription(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        out_dir=tmp_path,
        language="auto",
        vad_filter=False,
        device="cpu",
        timeout_sec=3,
        chunk_duration_sec=30,
        chunk_overlap_sec=2,
        chunk_threshold_sec=60,
        debug=False,
        prepare_transcription_plan_fn=lambda **_k: (1.0, [(0.0, 1.0), (1.0, 1.0)], {}),
        transcribe_audio_fn=lambda *_a: {"text": "ok"},
        extract_detected_language_fn=lambda _result: "en",
        run_chunked_whisper_transcription_fn=lambda **_k: (0, {"text": "chunked"}, "en"),
    )
    assert rc == 0
    assert result == {"text": "chunked"}
    assert detected == "en"

    assert "Whisper chunk extraction failed" in capsys.readouterr().out


def test_remaining_chunking_lines(monkeypatch, tmp_path):
    """Validate Remaining chunking lines."""
    chunking_utils = _load_core_module("chunking_utils")

    trimmed = chunking_utils.trim_segment_to_time_window(
        {
            "start": 0.0,
            "end": 10.0,
            "words": [
                "not-a-dict",
                {"start": 2.0, "end": 5.0},
            ],
        },
        1.0,
        4.0,
        safe_float_fn=lambda value: float(value) if isinstance(value, (int, float)) else None,
    )
    assert trimmed is not None

    def exploding_unlink(self, missing_ok=True):
        del self, missing_ok
        raise RuntimeError("unlink")

    monkeypatch.setattr(chunking_utils.Path, "unlink", exploding_unlink)
    rc, result, _language, _text = chunking_utils.transcribe_one_audio_chunk(
        wmodel=object(),
        audio_path=tmp_path / "a.mp3",
        chunk_dir=tmp_path,
        chunk_index=0,
        chunk_count=1,
        start_sec=0.0,
        duration_sec=1.0,
        timeout_sec=3,
        transcribe_kwargs={},
        detected_language=None,
        explicit_language=False,
        previous_chunk_text="",
        language="auto",
        debug=False,
        extract_audio_chunk_fn=lambda **_k: 0,
        build_chunk_transcribe_kwargs_fn=lambda *_a: {},
        transcribe_audio_fn=lambda *_a: {"text": "ok"},
        filter_result_segments_fn=lambda payload, _expected, _debug: payload,
        extract_detected_language_fn=lambda _result: "en",
    )
    assert rc == 0
    assert result is not None
