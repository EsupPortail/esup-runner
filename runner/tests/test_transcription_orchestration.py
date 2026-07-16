"""Tests for transcription orchestration and runtime flows."""

import sys
import types
from pathlib import Path

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module
from transcription_test_helpers import make_transcription_args as _make_args


def test_runtime_modules_leave_unrelated_runtime_args_module_untouched(monkeypatch):
    """Qualified imports do not evict an unrelated top-level module."""
    for module_name in (
        "gap_repair_runtime_utils",
        "output_validation_runtime_utils",
        "transcription_runtime_utils",
        "translation_runtime_utils",
    ):
        fake_runtime_args_module = types.ModuleType("runtime_args_utils")
        monkeypatch.setitem(sys.modules, "runtime_args_utils", fake_runtime_args_module)

        _load_core_module(module_name, force_insert_branch=True)

        assert sys.modules["runtime_args_utils"] is fake_runtime_args_module


def test_main_runtime_utils_leaves_unrelated_runtime_args_module_untouched(monkeypatch):
    """Main runtime uses the qualified args module without top-level eviction."""
    fake_runtime_args_module = types.ModuleType("runtime_args_utils")
    monkeypatch.setitem(sys.modules, "runtime_args_utils", fake_runtime_args_module)

    _load_core_module("main_runtime_utils", force_insert_branch=True)

    assert sys.modules["runtime_args_utils"] is fake_runtime_args_module


def test_main_runtime_utils_context_and_main(monkeypatch):
    """Validate Main runtime utils context and main."""
    main_runtime_utils = _load_core_module("main_runtime_utils", force_insert_branch=True)

    context = main_runtime_utils.build_main_flow_context()
    assert context.max_vtt_internal_gap_seconds > 0

    monkeypatch.setattr(main_runtime_utils, "parse_args", lambda: "args")
    monkeypatch.setattr(
        main_runtime_utils.main_orchestration_utils, "run_main_flow", lambda *_a, **_k: 7
    )
    assert main_runtime_utils.main() == 7


def test_output_validation_flow_utils_missing_fields_and_finalize_failure(tmp_path):
    """Validate Output validation flow utils missing fields and finalize failure."""
    output_flow = _load_core_module("output_validation_flow_utils")

    with pytest.raises(TypeError):
        output_flow.validate_final_vtt_and_collect_gap_analysis(
            expected_vtt=tmp_path / "x.vtt",
            audio_src=tmp_path / "a.mp3",
            input_path=tmp_path / "a.mp3",
            debug=False,
        )

    generated = tmp_path / "a.vtt"
    generated.write_text("WEBVTT\n", encoding="utf-8")

    rc = output_flow.finalize_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        max_line_count=2,
        max_line_width=40,
        debug=False,
        find_generated_vtt_fn=lambda *_a: generated,
        postprocess_vtt_file_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert rc == 6


def test_output_validation_runtime_utils_wrappers(monkeypatch, tmp_path):
    """Validate Output validation runtime utils wrappers."""
    output_runtime = _load_core_module("output_validation_runtime_utils", force_insert_branch=True)

    monkeypatch.setattr(
        output_runtime.output_validation_flow_utils,
        "find_generated_vtt",
        lambda *_a, **_k: tmp_path / "x.vtt",
    )
    assert output_runtime.find_generated_vtt(tmp_path / "a.mp3", tmp_path) == tmp_path / "x.vtt"

    monkeypatch.setattr(
        output_runtime.output_validation_flow_utils, "finalize_vtt", lambda *_a, **_k: 0
    )
    assert output_runtime.finalize_vtt(tmp_path / "a.mp3", tmp_path, debug=False) == 0

    monkeypatch.setattr(
        output_runtime.vtt_validation_utils,
        "read_last_vtt_cue_end_seconds",
        lambda *args, **kwargs: (True, True, 2.0),
    )
    assert output_runtime.read_last_vtt_cue_end_seconds(tmp_path / "x.vtt")[2] == 2.0

    monkeypatch.setattr(
        output_runtime.vtt_validation_utils, "validate_vtt_coverage", lambda **kwargs: 0
    )
    assert output_runtime.validate_vtt_coverage(tmp_path / "x.vtt", 10.0, 0.8, 4.0, False) == 0

    monkeypatch.setattr(
        output_runtime.output_validation_flow_utils,
        "validate_final_vtt_and_collect_gap_analysis",
        lambda **kwargs: (0, {"gap_count": 0}),
    )
    rc, analysis = output_runtime.validate_final_vtt_and_collect_gap_analysis(
        expected_vtt=tmp_path / "x.vtt",
        audio_src=tmp_path / "a.mp3",
        input_path=tmp_path / "a.mp3",
        debug=False,
    )
    assert rc == 0
    assert analysis["gap_count"] == 0


def test_main_orchestration_utils_legacy_and_early_returns(tmp_path):
    """Validate Main orchestration utils legacy and early returns."""
    main_orch = _load_core_module("main_orchestration_utils")

    with pytest.raises(TypeError):
        main_orch.run_main_flow(_make_args(tmp_path))

    args_missing = _make_args(tmp_path, input_file="missing.mp4")
    context = main_orch.MainFlowContext(
        extract_video_identification_fn=lambda _a: {},
        compute_timeout_fn=lambda *_a: 1,
        prepare_audio_source_fn=lambda *_a: (0, tmp_path / "audio.mp3"),
        resolve_effective_use_gpu_fn=lambda *_a: False,
        run_transcription_fn=lambda *_a: (0, "en"),
        finalize_vtt_fn=lambda *_a, **_k: 0,
        run_non_blocking_internal_gap_repair_fn=lambda **_k: {},
        build_whisper_fallback_options_fn=lambda **_k: {},
        maybe_translate_final_vtt_fn=lambda *_a, **_k: (0, {}, "fr"),
        validate_final_vtt_and_collect_gap_analysis_fn=lambda **_k: (0, {}),
        build_transcription_runtime_metadata_fn=lambda **_k: {},
        write_info_video_metadata_fn=lambda *_a, **_k: None,
        max_vtt_internal_gap_seconds=1.0,
        max_vtt_internal_gap_count=0,
    )
    assert main_orch.run_main_flow(args_missing, context=context) == 2

    input_path = tmp_path / "input.mp4"
    input_path.write_text("x", encoding="utf-8")
    args = _make_args(tmp_path)

    context_prepare_fail = types.SimpleNamespace(
        **{**context.__dict__, "prepare_audio_source_fn": lambda *_a: (9, None)}
    )
    assert main_orch.run_main_flow(args, context=context_prepare_fail) == 9

    context_transcription_fail = types.SimpleNamespace(
        **{**context.__dict__, "run_transcription_fn": lambda *_a: (8, None)}
    )
    assert main_orch.run_main_flow(args, context=context_transcription_fail) == 8

    context_finalize_fail = types.SimpleNamespace(
        **{**context.__dict__, "finalize_vtt_fn": lambda *_a, **_k: 7}
    )
    assert main_orch.run_main_flow(args, context=context_finalize_fail) == 7

    context_translate_fail = types.SimpleNamespace(
        **{**context.__dict__, "maybe_translate_final_vtt_fn": lambda *_a, **_k: (6, {}, None)}
    )
    assert main_orch.run_main_flow(args, context=context_translate_fail) == 6

    context_validate_fail = types.SimpleNamespace(
        **{
            **context.__dict__,
            "validate_final_vtt_and_collect_gap_analysis_fn": lambda **_k: (5, {}),
        }
    )
    assert main_orch.run_main_flow(args, context=context_validate_fail) == 5


def test_runtime_media_utils_extra_branches(monkeypatch, tmp_path, capsys):
    """Validate Runtime media utils extra branches."""
    runtime_media = _load_core_module("runtime_media_utils", force_insert_branch=True)

    proc_ok = types.SimpleNamespace(returncode=0, stdout="12.5", stderr="")
    assert (
        runtime_media.probe_duration_seconds(
            tmp_path / "a.mp3", subprocess_run=lambda *a, **k: proc_ok
        )
        == 12.5
    )

    assert (
        runtime_media.probe_duration_seconds(
            tmp_path / "a.mp3",
            debug=True,
            subprocess_run=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ffprobe")),
        )
        == 0.0
    )
    assert "ffprobe failed to get duration" in capsys.readouterr().out

    bad_args = types.SimpleNamespace(timeout_factor="oops", min_timeout="oops")
    timeout_value = runtime_media.compute_timeout(
        bad_args,
        tmp_path / "a.mp4",
        True,
        probe_duration_seconds_fn=lambda *_a: 3.0,
    )
    assert timeout_value == 60

    monkeypatch.setattr(runtime_media, "probe_duration_seconds", lambda *a, **k: 4.0)
    wrapped_timeout = runtime_media.compute_timeout_with_defaults(
        _make_args(tmp_path, timeout_factor="2.0", min_timeout="1"),
        tmp_path / "a.mp4",
        False,
    )
    assert wrapped_timeout == 8

    mp3_input = tmp_path / "input.mp3"
    mp3_input.write_text("x", encoding="utf-8")
    args_mp3 = _make_args(tmp_path, input_file="input.mp3")
    rc, audio_src = runtime_media.prepare_audio_source(
        args_mp3,
        mp3_input,
        tmp_path,
        timeout_sec=3,
        debug=False,
        run_ffmpeg_to_mp3_fn=lambda **_k: 0,
        normalize_mp3_with_ffmpeg_normalize_fn=lambda **_k: mp3_input,
    )
    assert rc == 0
    assert audio_src == mp3_input

    non_mp3 = tmp_path / "input.mp4"
    non_mp3.write_text("x", encoding="utf-8")
    rc, audio_src = runtime_media.prepare_audio_source(
        _make_args(tmp_path),
        non_mp3,
        tmp_path,
        timeout_sec=3,
        debug=False,
        run_ffmpeg_to_mp3_fn=lambda **_k: 9,
        normalize_mp3_with_ffmpeg_normalize_fn=lambda **_k: non_mp3,
    )
    assert rc == 9
    assert audio_src is None

    monkeypatch.setattr(runtime_media.runtime_cli_utils, "run_ffmpeg_to_mp3", lambda **_k: 0)
    monkeypatch.setattr(
        runtime_media.runtime_cli_utils,
        "normalize_mp3_with_ffmpeg_normalize",
        lambda **_k: tmp_path / "norm.mp3",
    )
    rc, wrapped_audio_src = runtime_media.prepare_audio_source_with_defaults(
        _make_args(tmp_path, normalize="true"),
        non_mp3,
        tmp_path,
        timeout_sec=3,
        debug=False,
    )
    assert rc == 0
    assert wrapped_audio_src == tmp_path / "norm.mp3"


def test_segment_filter_utils_extra_branches(capsys):
    """Validate Segment filter utils extra branches."""
    segment_filter = _load_core_module("segment_filter_utils")

    assert segment_filter.is_punctuation_only_text("") is True
    assert segment_filter.safe_float("not-a-number") is None
    assert segment_filter.language_uses_latin_script("") is False
    assert segment_filter.looks_like_subtitle_credit("") is False
    assert segment_filter.looks_like_repetition_loop("") is False

    suspicious = {
        "text": "hello world",
        "compression_ratio": 4.0,
        "avg_logprob": -1.2,
        "no_speech_prob": 0.1,
    }
    assert segment_filter.should_drop_segment(suspicious, "en") is True

    class ExplodingDict(dict):
        def get(self, key: object, default: object = None) -> object:
            raise RuntimeError("explode")

    assert segment_filter.extract_detected_language(ExplodingDict()) is None

    unchanged = segment_filter.filter_result_segments(
        {"segments": "invalid"}, expected_language="en"
    )
    assert unchanged["segments"] == "invalid"

    filtered = segment_filter.filter_result_segments(
        {
            "segments": [
                "invalid-entry",
                {"text": "..."},
                {"text": "valid text", "avg_logprob": -0.1, "compression_ratio": 1.0},
            ]
        },
        expected_language="en",
        debug=True,
    )
    assert filtered["text"] == "valid text"
    assert "Dropping suspicious segment" in capsys.readouterr().out


def test_transcription_flow_utils_missing_fields_and_fallback_paths(capsys):
    """Validate Transcription flow utils missing fields and fallback paths."""
    transcription_flow = _load_core_module("transcription_flow_utils")

    with pytest.raises(TypeError):
        transcription_flow.run_transcription(
            _make_args(Path("."), language="fr"),
            audio_src=Path("audio.mp3"),
            work_dir=Path("."),
            timeout_sec=3,
            effective_use_gpu=False,
            debug=False,
        )

    with pytest.raises(TypeError):
        transcription_flow.build_whisper_fallback_options(
            args=_make_args(Path("."), language="fr"),
            effective_use_gpu=False,
            timeout_sec=5,
            vtt_max_line_count=2,
            vtt_max_line_width=40,
        )

    args = _make_args(Path("."), language="fr")
    context = transcription_flow.TranscriptionFlowContext(
        resolve_transcription_language_fn=lambda _l: "auto",
        resolve_chunk_threshold_seconds_fn=lambda **_k: 10,
        run_whisper_python_fn=lambda **_k: (255, None),
        run_whisper_cli_fn=lambda **_k: (1, "english"),
        normalize_language_code_fn=lambda value: "en" if value else None,
    )

    rc, detected = transcription_flow.run_transcription(
        args,
        audio_src=Path("audio.mp3"),
        work_dir=Path("."),
        timeout_sec=3,
        effective_use_gpu=False,
        debug=False,
        context=context,
    )
    assert rc == 1
    assert detected == "en"
    assert "Whisper Python API unavailable" in capsys.readouterr().out

    fallback_options = transcription_flow.build_whisper_fallback_options(
        args=args,
        effective_use_gpu=False,
        timeout_sec=5,
        vtt_max_line_count=2,
        vtt_max_line_width=40,
        context=transcription_flow.TranscriptionFlowContext(
            resolve_transcription_language_fn=lambda _l: "auto",
            resolve_chunk_threshold_seconds_fn=lambda **_k: 42,
            run_whisper_python_fn=lambda **_k: (0, None),
            run_whisper_cli_fn=lambda **_k: (0, None),
            normalize_language_code_fn=lambda value: value,
        ),
    )
    assert fallback_options["chunk_threshold_sec"] == 42


def test_transcription_runtime_utils_wrappers(monkeypatch, tmp_path):
    """Validate Transcription runtime utils wrappers."""
    transcription_runtime = _load_core_module(
        "transcription_runtime_utils", force_insert_branch=True
    )

    monkeypatch.setattr(
        transcription_runtime.chunking_utils,
        "resolve_chunk_threshold_seconds",
        lambda *_a, **_k: 17,
    )
    assert transcription_runtime.resolve_chunk_threshold_seconds("", False) == 17

    monkeypatch.setattr(
        transcription_runtime.chunking_utils,
        "plan_audio_chunks",
        lambda *_a, **_k: [(0.0, 1.0)],
    )
    assert transcription_runtime.plan_audio_chunks(1.0, 10, 5, 2) == [(0.0, 1.0)]

    context = transcription_runtime.build_transcription_flow_context()
    assert callable(context.run_whisper_python_fn)

    monkeypatch.setattr(
        transcription_runtime.transcription_flow_utils,
        "run_transcription",
        lambda *_a, **_k: (0, "en"),
    )
    assert transcription_runtime.run_transcription(
        _make_args(tmp_path),
        tmp_path / "audio.mp3",
        tmp_path,
        5,
        False,
        False,
    ) == (0, "en")

    monkeypatch.setattr(
        transcription_runtime.transcription_flow_utils,
        "build_whisper_fallback_options",
        lambda **_k: {"ok": True},
    )
    opts = transcription_runtime.build_whisper_fallback_options(
        args=_make_args(tmp_path),
        effective_use_gpu=False,
        timeout_sec=3,
        vtt_max_line_count=2,
        vtt_max_line_width=40,
    )
    assert opts["ok"] is True


def test_remaining_main_orchestration_and_transcription_runtime_lines():
    """Validate Remaining main orchestration and transcription runtime lines."""
    main_orch = _load_core_module("main_orchestration_utils")
    built_context = main_orch.MainFlowContext(
        extract_video_identification_fn=lambda _a: {},
        compute_timeout_fn=lambda *_a: 1,
        prepare_audio_source_fn=lambda *_a: (0, Path("audio.mp3")),
        resolve_effective_use_gpu_fn=lambda *_a: False,
        run_transcription_fn=lambda *_a: (0, "en"),
        finalize_vtt_fn=lambda *_a, **_k: 0,
        run_non_blocking_internal_gap_repair_fn=lambda **_k: {},
        build_whisper_fallback_options_fn=lambda **_k: {},
        maybe_translate_final_vtt_fn=lambda *_a, **_k: (0, {}, "fr"),
        validate_final_vtt_and_collect_gap_analysis_fn=lambda **_k: (0, {}),
        build_transcription_runtime_metadata_fn=lambda **_k: {},
        write_info_video_metadata_fn=lambda *_a, **_k: None,
        max_vtt_internal_gap_seconds=1.0,
        max_vtt_internal_gap_count=0,
    )
    assert built_context.max_vtt_internal_gap_seconds == 1.0

    transcription_runtime = _load_core_module("transcription_runtime_utils")
    assert transcription_runtime.resolve_transcription_language("auto") == "auto"
    assert transcription_runtime.resolve_transcription_language("fr") == "fr"
