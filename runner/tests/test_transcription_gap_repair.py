"""Tests for transcription gap detection and repair."""

from pathlib import Path

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module
from transcription_test_helpers import make_transcription_args as _make_args


def test_gap_repair_utils_requires_context_for_contextual_flows(tmp_path):
    """Validate Gap repair utils requires context for contextual flows."""
    gap_repair_utils = _load_core_module("gap_repair_utils")

    with pytest.raises(TypeError):
        gap_repair_utils.attempt_best_effort_vtt_internal_gap_repair(
            vtt_path=tmp_path / "x.vtt",
            audio_src=tmp_path / "a.mp3",
            work_dir=tmp_path,
            model="small",
            whisper_models_dir="",
            use_gpu=False,
            gpu_device=0,
            vad_filter=False,
            timeout_sec=5,
            detected_language="en",
            max_internal_gap_sec=1.0,
            max_repair_attempts=1,
            max_line_width=40,
            max_line_count=2,
            debug=False,
        )

    with pytest.raises(TypeError):
        gap_repair_utils.run_non_blocking_internal_gap_repair(
            expected_vtt=tmp_path / "x.vtt",
            audio_src=tmp_path / "a.mp3",
            work_dir=tmp_path,
            args=_make_args(tmp_path),
            timeout_sec=10,
            effective_use_gpu=False,
            detected_language="en",
            vtt_max_line_width=40,
            vtt_max_line_count=2,
            debug=False,
        )


def test_gap_repair_runtime_utils_wrappers_and_context(monkeypatch, tmp_path):
    """Validate Gap repair runtime utils wrappers and context."""
    gap_runtime = _load_core_module("gap_repair_runtime_utils", force_insert_branch=True)

    monkeypatch.setattr(
        gap_runtime.vtt_validation_utils,
        "read_vtt_cue_time_ranges",
        lambda *args, **kwargs: (True, [(0.0, 1.0, 1)]),
    )
    assert gap_runtime.read_vtt_cue_time_ranges(tmp_path / "a.vtt")[0] is True

    monkeypatch.setattr(
        gap_runtime.vtt_validation_utils,
        "detect_vtt_internal_gaps",
        lambda *args, **kwargs: {"gap_count": 0},
    )
    assert gap_runtime.detect_vtt_internal_gaps(tmp_path / "a.vtt", 1.0)["gap_count"] == 0

    monkeypatch.setattr(
        gap_runtime.vtt_validation_utils,
        "validate_vtt_internal_gaps",
        lambda **kwargs: 0,
    )
    assert gap_runtime.validate_vtt_internal_gaps(tmp_path / "a.vtt", 1.0, 0, debug=False) == 0

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils, "read_vtt_cues", lambda *args, **kwargs: (True, [])
    )
    assert gap_runtime.read_vtt_cues(tmp_path / "a.vtt") == (True, [])

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils, "dedupe_sorted_vtt_cues", lambda *args, **kwargs: []
    )
    assert gap_runtime.dedupe_sorted_vtt_cues([]) == []

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils, "render_vtt_from_cues", lambda *args, **kwargs: "WEBVTT\n"
    )
    assert gap_runtime.render_vtt_from_cues([], max_line_width=40, max_line_count=2).startswith(
        "WEBVTT"
    )

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils,
        "run_gap_window_rerun",
        lambda **kwargs: (True, [(0.0, 0.5, "ok")]),
    )
    ok, cues = gap_runtime.run_gap_window_rerun(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path,
        model="small",
        whisper_models_dir="",
        use_gpu=False,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=5,
        transcription_language="auto",
        start_sec=0.0,
        duration_sec=1.0,
        gap_start_sec=0.0,
        gap_end_sec=1.0,
        overlap_tolerance_sec=0.1,
        debug=False,
    )
    assert ok and cues

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils,
        "attempt_best_effort_vtt_internal_gap_repair",
        lambda **kwargs: {"attempted": True},
    )
    attempt = gap_runtime.attempt_best_effort_vtt_internal_gap_repair(
        vtt_path=tmp_path / "x.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path,
        model="small",
        whisper_models_dir="",
        use_gpu=False,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=5,
        detected_language="en",
        max_internal_gap_sec=1.0,
        max_repair_attempts=1,
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )
    assert attempt["attempted"] is True

    default_meta = gap_runtime.default_non_blocking_internal_gap_metadata(note="note")
    assert default_meta["note"] == "note"

    monkeypatch.setattr(
        gap_runtime.gap_repair_utils,
        "run_non_blocking_internal_gap_repair",
        lambda **kwargs: {"non_blocking": True},
    )
    result = gap_runtime.run_non_blocking_internal_gap_repair(
        expected_vtt=tmp_path / "x.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path,
        args=_make_args(tmp_path),
        timeout_sec=10,
        effective_use_gpu=False,
        detected_language="en",
        vtt_max_line_width=40,
        vtt_max_line_count=2,
        debug=False,
    )
    assert result["non_blocking"] is True


def test_gap_repair_render_vtt_from_cues_uses_legacy_wrap_fallback():
    """Validate Gap repair render vtt from cues uses legacy wrap fallback."""
    gap_utils = _load_core_module("gap_repair_utils")

    rendered = gap_utils.render_vtt_from_cues(
        [(0.0, 6.0, "ignored")],
        max_line_width=40,
        max_line_count=2,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
        wrap_vtt_cue_text_fn=lambda *_args: ["one", "two", "three"],
    )

    assert "0.0 --> 3.0\none\ntwo" in rendered
    assert "3.0 --> 6.0\nthree" in rendered


def test_gap_repair_runtime_finds_generated_vtt(monkeypatch):
    """Validate generated VTT lookup delegation."""
    gap_runtime = _load_core_module("gap_repair_runtime_utils")
    expected = Path("/tmp/generated.vtt")
    monkeypatch.setattr(
        gap_runtime.output_validation_flow_utils,
        "find_generated_vtt",
        lambda *_a, **_k: expected,
    )

    found = gap_runtime._find_generated_vtt(Path("/tmp/audio.mp3"), Path("/tmp"))

    assert found == expected
