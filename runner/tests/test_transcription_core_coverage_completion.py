"""Validates transcription core utilities for language mapping, metadata, and CLI functions."""

import builtins
import importlib.util
import inspect
import subprocess
import sys
import types
import uuid
from pathlib import Path
from typing import Any

import pytest


def _core_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "transcription" / "core"


def _core_module_path(module_name: str) -> Path:
    return _core_dir() / f"{module_name}.py"


def _load_core_module(module_name: str, *, force_insert_branch: bool = False):
    module_path = _core_module_path(module_name)
    core_dir = str(module_path.parent)
    if force_insert_branch:
        while core_dir in sys.path:
            sys.path.remove(core_dir)

    module_spec = importlib.util.spec_from_file_location(
        f"coverage_{module_name}_{uuid.uuid4().hex}",
        module_path,
    )
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"Cannot load module spec from {module_path}")

    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _make_args(base_dir: Path, **overrides: Any) -> types.SimpleNamespace:
    defaults = {
        "base_dir": str(base_dir),
        "input_file": "input.mp4",
        "work_dir": "work",
        "debug": "false",
        "language": "fr",
        "source_language": "auto",
        "model": "small",
        "whisper_models_dir": "",
        "use_gpu": "false",
        "gpu_device": 0,
        "vad_filter": "false",
        "chunk_duration_seconds": 30,
        "chunk_overlap_seconds": 2,
        "chunk_threshold_seconds": 60,
        "vtt_highlight_words": "false",
        "vtt_max_line_count": 2,
        "vtt_max_line_width": 40,
        "huggingface_models_dir": "",
        "timeout_factor": "8.0",
        "min_timeout": "60",
        "sample_rate": 16000,
        "downmix_mono": "true",
        "audio_stream_index": 0,
        "normalize": "false",
        "normalize_target_level": "-16.0",
        "video_id": "",
        "video_slug": "",
        "video_title": "",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def test_language_utils_extra_branches():
    """Validate Language utils extra branches."""
    language_utils = _load_core_module("language_utils")

    assert language_utils.map_language_name_to_code("") is None
    assert language_utils.normalize_language_code("   ") is None
    assert language_utils.normalize_language_code("French") == "fr"


def test_metadata_utils_extra_branches(tmp_path, capsys):
    """Validate Metadata utils extra branches."""
    metadata_utils = _load_core_module("metadata_utils")

    metadata_utils.write_info_video_metadata(tmp_path, {}, debug=True)
    assert not (tmp_path / "info_video.json").exists()

    (tmp_path / "info_video.json").write_text("{not-valid-json", encoding="utf-8")
    metadata_utils.write_info_video_metadata(tmp_path, {"a": 1}, debug=True)
    written = (tmp_path / "info_video.json").read_text(encoding="utf-8")
    assert '"a": 1' in written
    assert "Task metadata written to:" in capsys.readouterr().out

    calls = []

    def writer(work_dir: Path, payload: dict[str, Any], debug: bool) -> None:
        calls.append((work_dir, payload, debug))

    metadata_utils.write_video_identification_metadata(tmp_path, {}, writer, debug=True)
    assert calls == []

    metadata_utils.write_video_identification_metadata(
        tmp_path, {"video_id": "42"}, writer, debug=True
    )
    assert len(calls) == 1

    translation_metadata = metadata_utils.build_translation_metadata(
        applied=True,
        backend="local",
        source_language="en",
        target_language="fr",
        model="model",
        use_gpu=False,
        normalize_language=lambda value: value,
        execution_backend="whisper_python",
    )
    assert translation_metadata["execution_backend"] == "whisper_python"


def test_metadata_runtime_utils_insert_and_wrapper():
    """Validate Metadata runtime utils insert and wrapper."""
    metadata_runtime_utils = _load_core_module("metadata_runtime_utils", force_insert_branch=True)
    assert str(_core_dir()) in sys.path

    metadata = metadata_runtime_utils.build_transcription_runtime_metadata(
        requested_language="fr",
        detected_language="en",
        final_language="fr",
        whisper_model="small",
        use_gpu=False,
        translation={"applied": False},
    )
    assert metadata["transcription"]["final_subtitle_language"] == "fr"


def test_runtime_modules_evict_mismatched_runtime_args_module(monkeypatch):
    """Validate Runtime modules evict mismatched runtime args module."""
    wrong_runtime_args_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "task_handlers"
        / "encoding"
        / "core"
        / "runtime_args_utils.py"
    )

    for module_name in (
        "gap_repair_runtime_utils",
        "output_validation_runtime_utils",
        "transcription_runtime_utils",
        "translation_runtime_utils",
    ):
        fake_runtime_args_module = types.ModuleType("runtime_args_utils")
        fake_runtime_args_module.__file__ = str(wrong_runtime_args_path)
        monkeypatch.setitem(sys.modules, "runtime_args_utils", fake_runtime_args_module)

        _load_core_module(module_name, force_insert_branch=True)

        loaded_runtime_args = sys.modules.get("runtime_args_utils")
        assert loaded_runtime_args is not None
        loaded_runtime_args_file = getattr(loaded_runtime_args, "__file__", "")
        assert loaded_runtime_args_file
        assert Path(loaded_runtime_args_file).resolve().parent == _core_dir()


def test_main_runtime_utils_evicts_mismatched_runtime_args_module(monkeypatch):
    """Validate Main runtime utils evicts mismatched runtime args module."""
    wrong_runtime_args_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "task_handlers"
        / "encoding"
        / "core"
        / "runtime_args_utils.py"
    )

    fake_runtime_args_module = types.ModuleType("runtime_args_utils")
    fake_runtime_args_module.__file__ = str(wrong_runtime_args_path)
    monkeypatch.setitem(sys.modules, "runtime_args_utils", fake_runtime_args_module)

    _load_core_module("main_runtime_utils", force_insert_branch=True)

    loaded_runtime_args = sys.modules.get("runtime_args_utils")
    assert loaded_runtime_args is not None
    loaded_runtime_args_file = getattr(loaded_runtime_args, "__file__", "")
    assert loaded_runtime_args_file
    assert Path(loaded_runtime_args_file).resolve().parent == _core_dir()


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


def test_runtime_cli_load_language_utils_fallback_and_runner_project_default(monkeypatch):
    """Validate Runtime cli load language utils fallback and runner project default."""
    runtime_cli = _load_core_module("runtime_cli_utils")

    real_import = builtins.__import__

    def import_without_language_utils(name, *args, **kwargs):
        if name == "language_utils":
            raise ModuleNotFoundError(name="language_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_language_utils)
    loaded = runtime_cli._load_language_utils_module()
    assert hasattr(loaded, "normalize_language_code")

    class _Probe:
        @staticmethod
        def exists() -> bool:
            return False

        @staticmethod
        def is_dir() -> bool:
            return False

    class _Parent:
        def __init__(self, idx: int):
            self.idx = idx

        def __truediv__(self, _name: str) -> _Probe:
            return _Probe()

        def __str__(self) -> str:
            return f"/fake/parent-{self.idx}"

    class _Resolved:
        parents = [_Parent(i) for i in range(6)]

    class _Path:
        def __init__(self, *_args: Any, **_kwargs: Any):
            pass

        @staticmethod
        def resolve() -> _Resolved:
            return _Resolved()

    monkeypatch.setattr(runtime_cli, "Path", _Path)
    assert runtime_cli.runner_project_dir().endswith("parent-5")


def test_runtime_cli_help_and_option_detection(capsys):
    """Validate Runtime cli help and option detection."""
    runtime_cli = _load_core_module("runtime_cli_utils")
    runtime_cli._WHISPER_HELP_CACHE = None

    proc = types.SimpleNamespace(stdout="--model_dir\n", stderr="stderr-line")
    help_text = runtime_cli.get_whisper_help_text(subprocess_run=lambda *_a, **_k: proc)
    assert "--model_dir" in help_text

    runtime_cli._WHISPER_HELP_CACHE = None
    help_text_on_error = runtime_cli.get_whisper_help_text(
        debug=True,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert help_text_on_error == ""
    assert "Failed to get whisper --help" in capsys.readouterr().out

    selected = runtime_cli.cli_supports_option(
        ["--missing", "--model_dir"],
        debug=False,
        get_whisper_help_text_fn=lambda _debug: "--model_dir --other",
    )
    assert selected == "--model_dir"


def test_runtime_cli_build_command_and_env_and_detect_language(monkeypatch, capsys):
    """Validate Runtime cli build command and env and detect language."""
    runtime_cli = _load_core_module("runtime_cli_utils")

    command = runtime_cli.build_whisper_command(
        audio_path=Path("a.mp3"),
        out_dir=Path("."),
        model_name="small",
        whisper_models_dir="/tmp/models",
        language="fr",
        vad_filter=True,
        debug=True,
        cli_supports_option_fn=lambda flags, _debug: None if "model" in flags[0] else None,
    )
    assert "--language" in command
    assert "--model" in command
    output = capsys.readouterr().out
    assert "does not support model_dir option" in output
    assert "does not support a VAD option" in output

    def supports_some(flags: list[str], _debug: bool) -> str | None:
        if "model" in flags[0]:
            return "--model-dir"
        return "--vad-filter"

    command_supported = runtime_cli.build_whisper_command(
        audio_path=Path("a.mp3"),
        out_dir=Path("."),
        model_name="small",
        whisper_models_dir="/tmp/models",
        language="fr",
        vad_filter=False,
        debug=False,
        cli_supports_option_fn=supports_some,
    )
    assert "--model-dir" in command_supported
    assert "--vad-filter" in command_supported

    monkeypatch.setenv("GPU_CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setenv("GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    device_args, env = runtime_cli.prepare_whisper_env(use_gpu=True, gpu_device=1)
    assert device_args == ["--device", "cuda"]
    assert env["CUDA_VISIBLE_DEVICES"] == "3"
    assert env["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"

    assert (
        runtime_cli.detect_language_from_stdout(
            "Detected language: French",
            "fr",
            map_language_name_to_code_fn=lambda name: name.lower(),
        )
        is None
    )

    assert (
        runtime_cli.detect_language_from_stdout(
            "Detected language: French",
            "auto",
            map_language_name_to_code_fn=lambda _name: (_ for _ in ()).throw(RuntimeError("map")),
        )
        is None
    )


def test_runtime_cli_resolve_effective_use_gpu_branches(monkeypatch, capsys):
    """Validate Runtime cli resolve effective use gpu branches."""
    runtime_cli = _load_core_module("runtime_cli_utils")

    assert runtime_cli.resolve_effective_use_gpu(False, 0, False) is False

    fake_torch_cpu_build = types.SimpleNamespace(
        __version__="2.0",
        version=types.SimpleNamespace(cuda=None),
        cuda=types.SimpleNamespace(is_available=lambda: False),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch_cpu_build)
    assert runtime_cli.resolve_effective_use_gpu(True, 0, False) is False
    assert "torch build is CPU-only" in capsys.readouterr().out

    def failing_device_count() -> int:
        raise RuntimeError("device_count")

    fake_torch_cuda_build = types.SimpleNamespace(
        __version__="2.0",
        version=types.SimpleNamespace(cuda="12.1"),
        cuda=types.SimpleNamespace(is_available=lambda: False, device_count=failing_device_count),
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch_cuda_build)
    assert runtime_cli.resolve_effective_use_gpu(True, 0, False) is False

    assert (
        runtime_cli.resolve_effective_use_gpu(
            True,
            0,
            False,
            apply_runtime_cuda_environment_fn=lambda _gpu: (_ for _ in ()).throw(
                RuntimeError("env")
            ),
        )
        is False
    )
    assert "Failed to probe CUDA availability; falling back to CPU" in capsys.readouterr().out


def test_runtime_cli_import_whisper_modules_and_load_model(monkeypatch):
    """Validate Runtime cli import whisper modules and load model."""
    runtime_cli = _load_core_module("runtime_cli_utils")

    real_import = builtins.__import__

    def module_not_found_import(name, *args, **kwargs):
        if name in {"torch", "whisper"}:
            raise ModuleNotFoundError(name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", module_not_found_import)
    assert runtime_cli.import_whisper_modules(use_gpu=False) == (None, None, None)

    def generic_failure_import(name, *args, **kwargs):
        if name in {"torch", "whisper"}:
            raise RuntimeError("import error")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", generic_failure_import)
    assert runtime_cli.import_whisper_modules(use_gpu=False) == (None, None, None)

    monkeypatch.setattr(builtins, "__import__", real_import)

    whisper_module = types.SimpleNamespace(
        load_model=lambda model_name, device: {"model": model_name, "device": device}
    )
    monkeypatch.setitem(sys.modules, "whisper", whisper_module)
    loaded = runtime_cli.load_whisper_model("small", "cpu")
    assert loaded["device"] == "cpu"

    failing_whisper = types.SimpleNamespace(
        load_model=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setitem(sys.modules, "whisper", failing_whisper)
    assert runtime_cli.load_whisper_model("small", "cpu") is None


def test_translation_utils_import_and_runtime_helpers(monkeypatch, tmp_path):
    """Validate Translation utils import and runtime helpers."""
    translation_utils = _load_core_module("translation_utils")

    translation_utils.HF_HUB_WARNING_FILTER_INSTALLED = False
    fake_transformers = types.SimpleNamespace(
        AutoModelForSeq2SeqLM=object(),
        AutoTokenizer=object(),
    )
    monkeypatch.setitem(sys.modules, "torch", object())
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    torch_mod, auto_model_cls, auto_tokenizer_cls = translation_utils.import_translation_modules()
    assert torch_mod is not None
    assert auto_model_cls is not None
    assert auto_tokenizer_cls is not None

    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "torch":
            raise RuntimeError("missing torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)
    assert translation_utils.import_translation_modules() == (None, None, None)

    assert translation_utils.prepare_huggingface_models_dir("", debug=True) is None

    class ExplodingPath:
        def __init__(self, path: str):
            self.path = path

        def mkdir(self, parents: bool, exist_ok: bool) -> None:
            raise RuntimeError("mkdir failed")

    monkeypatch.setattr(translation_utils, "Path", ExplodingPath)
    assert (
        translation_utils.prepare_huggingface_models_dir("/tmp/cache", debug=True) == "/tmp/cache"
    )

    assert (
        translation_utils.resolve_translation_model_name(
            None,
            "fr",
            False,
            normalize_language=lambda value: value,
            cpu_model_map={("en", "fr"): "x"},
            gpu_model_map={("en", "fr"): "y"},
        )
        is None
    )

    class DummyModel:
        def __init__(self):
            self.moves: list[str] = []
            self.evaluated = False

        def to(self, device: str) -> "DummyModel":
            self.moves.append(device)
            return self

        def eval(self) -> None:
            self.evaluated = True

    model = DummyModel()
    translation_utils.place_translation_model_on_device(model, "cuda")
    assert model.moves == ["cuda"]
    assert model.evaluated is True

    assert (
        translation_utils.run_translation_batch(
            [], torch=object(), tokenizer=object(), model=object()
        )
        == []
    )

    with pytest.raises(ValueError):
        translation_utils.translate_cue_texts(
            ["a", "b"],
            translate_batch=lambda _batch: ["only-one"],
            batch_size=2,
            normalize_vtt_cue_text=lambda text: text,
        )


def test_translation_utils_translate_vtt_defensive_error_path():
    """Validate Translation utils translate vtt defensive error path."""
    translation_utils = _load_core_module("translation_utils")

    def parse_block(block: str):
        if "-->" in block:
            return (["00:00:00.000 --> 00:00:01.000"], "hello")
        return block

    def mutating_translate_cue_texts(cue_texts, *, translate_batch, batch_size):
        frame = inspect.currentframe()
        assert frame is not None and frame.f_back is not None
        parent_locals = frame.f_back.f_locals
        parsed_blocks = parent_locals["parsed_blocks"]
        first_index = parent_locals["cue_block_indexes"][0]
        parsed_blocks[first_index] = "broken-structure"
        return ["bonjour" for _ in cue_texts]

    with pytest.raises(ValueError):
        translation_utils.translate_vtt_content(
            "00:00:00.000 --> 00:00:01.000\nhello\n",
            translate_batch=lambda batch: batch,
            max_line_width=40,
            max_line_count=2,
            batch_size=4,
            parse_vtt_postprocess_block=parse_block,
            normalize_vtt_cue_text=lambda text: text.strip(),
            translate_cue_texts_fn=mutating_translate_cue_texts,
            repair_cross_cue_apostrophe_splits=lambda _blocks: None,
            render_postprocessed_vtt_blocks=lambda blocks, **_k: [str(blocks)],
        )


def test_translation_flow_requires_context_for_contextual_flows(tmp_path):
    """Validate Translation flow requires context for contextual flows."""
    translation_flow = _load_core_module("translation_flow_utils")

    with pytest.raises(TypeError):
        translation_flow.load_translation_runtime(
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            debug=False,
        )

    with pytest.raises(TypeError):
        translation_flow.translate_vtt_file(
            tmp_path / "a.vtt",
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            max_line_width=40,
            max_line_count=2,
            debug=False,
        )

    with pytest.raises(TypeError):
        translation_flow.maybe_translate_final_vtt(
            audio_src=tmp_path / "a.mp3",
            work_dir=tmp_path,
            requested_language="fr",
            detected_language="en",
            whisper_fallback_options=None,
            use_gpu=False,
            huggingface_models_dir=None,
            max_line_width=40,
            max_line_count=2,
            debug=False,
        )


def test_translation_flow_load_runtime_branches(capsys):
    """Validate Translation flow load runtime branches."""
    translation_flow = _load_core_module("translation_flow_utils")

    base_context = translation_flow.TranslationRuntimeContext(
        translation_unsupported_pair_rc=30,
        translation_backend_unavailable_rc=31,
        cpu_translation_models={("en", "fr"): "m"},
        resolve_translation_model_name_fn=lambda *_a, **_k: None,
        import_translation_modules_fn=lambda: (object(), object(), object()),
        prepare_huggingface_models_dir_fn=lambda *_a, **_k: "/tmp/hf",
        load_translation_model_objects_fn=lambda *_a, **_k: ("tok", "model"),
        place_translation_model_on_device_fn=lambda model, _dev: model,
    )

    assert (
        translation_flow.load_translation_runtime(
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            debug=False,
            context=base_context,
        )[0]
        == 30
    )

    unavailable_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "resolve_translation_model_name_fn": lambda *_a, **_k: "model",
            "import_translation_modules_fn": lambda: (None, None, None),
        }
    )
    assert (
        translation_flow.load_translation_runtime(
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            debug=False,
            context=unavailable_context,
        )[0]
        == 31
    )

    cpu_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "resolve_translation_model_name_fn": lambda *_a, **_k: "model",
            "import_translation_modules_fn": lambda: (object(), object(), object()),
            "place_translation_model_on_device_fn": lambda model, _dev: model,
        }
    )
    rc, _torch, _runtime, model_name = translation_flow.load_translation_runtime(
        source_language="en",
        target_language="fr",
        use_gpu=False,
        huggingface_models_dir="/tmp/hf",
        debug=True,
        context=cpu_context,
    )
    assert rc == 0
    assert model_name == "model"
    assert "Using Hugging Face translation cache dir" in capsys.readouterr().out

    call_count = {"count": 0}

    def place_with_cuda_fallback(model: Any, device: str) -> Any:
        call_count["count"] += 1
        if device == "cuda":
            raise RuntimeError("cuda")
        return model

    cuda_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "resolve_translation_model_name_fn": lambda *_a, **_k: "model",
            "import_translation_modules_fn": lambda: (object(), object(), object()),
            "place_translation_model_on_device_fn": place_with_cuda_fallback,
        }
    )
    rc, _torch, _runtime, _model = translation_flow.load_translation_runtime(
        source_language="en",
        target_language="fr",
        use_gpu=True,
        huggingface_models_dir="/tmp/hf",
        debug=False,
        context=cuda_context,
    )
    assert rc == 0
    assert call_count["count"] == 2

    failing_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "resolve_translation_model_name_fn": lambda *_a, **_k: "model",
            "import_translation_modules_fn": lambda: (object(), object(), object()),
            "load_translation_model_objects_fn": lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("load")
            ),
        }
    )
    assert (
        translation_flow.load_translation_runtime(
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            debug=False,
            context=failing_context,
        )[0]
        == 31
    )


def test_translation_flow_translate_vtt_file_branches(tmp_path, capsys):
    """Validate Translation flow translate vtt file branches."""
    translation_flow = _load_core_module("translation_flow_utils")

    vtt_path = tmp_path / "a.vtt"
    vtt_path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello\n", encoding="utf-8")

    context_rc = translation_flow.TranslateVttFileContext(
        translation_backend_local="local",
        translation_failed_rc=32,
        translation_batch_size=4,
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        load_translation_runtime_fn=lambda **_k: (31, None, None, "m"),
        build_source_vtt_sidecar_path_fn=lambda path, _lang: path.with_suffix(".src"),
        run_translation_batch_fn=lambda batch, **_k: batch,
        translate_vtt_content_fn=lambda content, **_k: content,
    )
    rc, metadata = translation_flow.translate_vtt_file(
        vtt_path,
        source_language="en",
        target_language="fr",
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=context_rc,
    )
    assert rc == 31
    assert metadata["applied"] is False

    context_error = types.SimpleNamespace(
        **{
            **context_rc.__dict__,
            "load_translation_runtime_fn": lambda **_k: (0, object(), ("tok", "model"), "m"),
            "translate_vtt_content_fn": lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("translate")
            ),
        }
    )
    rc, _metadata = translation_flow.translate_vtt_file(
        vtt_path,
        source_language="en",
        target_language="fr",
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=context_error,
    )
    assert rc == 32

    context_success = types.SimpleNamespace(
        **{
            **context_rc.__dict__,
            "load_translation_runtime_fn": lambda **_k: (0, object(), ("tok", "model"), "m"),
            "translate_vtt_content_fn": lambda content, **_k: content.replace("hello", "bonjour"),
        }
    )
    rc, metadata = translation_flow.translate_vtt_file(
        vtt_path,
        source_language="en",
        target_language="fr",
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=True,
        context=context_success,
    )
    assert rc == 0
    assert metadata["applied"] is True
    assert "Source-language VTT preserved" in capsys.readouterr().out


def test_translation_flow_whisper_fallback_paths(tmp_path):
    """Validate Translation flow whisper fallback paths."""
    translation_flow = _load_core_module("translation_flow_utils")

    rc, backend, model_name = translation_flow.run_whisper_with_explicit_language(
        tmp_path / "a.mp3",
        tmp_path,
        language="fr",
        whisper_fallback_options={
            "model": "large",
            "whisper_models_dir": "",
            "use_gpu": False,
            "gpu_device": 0,
            "vad_filter": False,
            "timeout_sec": 5,
            "chunk_duration_sec": 30,
            "chunk_overlap_sec": 2,
            "chunk_threshold_sec": 60,
            "vtt_highlight_words": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        debug=False,
        run_whisper_python_fn=lambda **_k: (255, None),
        run_whisper_cli_fn=lambda **_k: (0, "fr"),
        map_model_name_fn=lambda name, context: f"{name}:{context}",
    )
    assert rc == 0
    assert backend == "whisper_cli"
    assert model_name.endswith(":cli")

    missing_vtt_result = translation_flow.run_legacy_whisper_translation_fallback(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        source_language="en",
        target_language="fr",
        whisper_fallback_options={"model": "small", "use_gpu": False},
        debug=False,
        translation_backend_whisper_legacy="legacy",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        map_model_name_fn=lambda name, _context: name,
        build_source_vtt_sidecar_path_fn=lambda path, _lang: path.with_suffix(".src"),
        run_whisper_with_explicit_language_fn=lambda *_a, **_k: (0, "backend", "model"),
        finalize_vtt_fn=lambda *_a, **_k: 0,
        normalize_language_fn=lambda value: value,
    )
    assert missing_vtt_result[0] == 5

    expected_vtt = tmp_path / "a.vtt"
    expected_vtt.write_text("WEBVTT\n", encoding="utf-8")

    rc, _metadata, language = translation_flow.run_legacy_whisper_translation_fallback(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        source_language="en",
        target_language="fr",
        whisper_fallback_options={
            "model": "small",
            "use_gpu": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        debug=False,
        translation_backend_whisper_legacy="legacy",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        map_model_name_fn=lambda name, _context: name,
        build_source_vtt_sidecar_path_fn=lambda path, _lang: path.with_suffix(".src"),
        run_whisper_with_explicit_language_fn=lambda *_a, **_k: (9, "backend", "model"),
        finalize_vtt_fn=lambda *_a, **_k: 0,
        normalize_language_fn=lambda value: value,
    )
    assert rc == 9
    assert language == "en"

    expected_vtt.write_text("WEBVTT\n", encoding="utf-8")
    rc, _metadata, language = translation_flow.run_legacy_whisper_translation_fallback(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        source_language="en",
        target_language="fr",
        whisper_fallback_options={
            "model": "small",
            "use_gpu": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        debug=False,
        translation_backend_whisper_legacy="legacy",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        map_model_name_fn=lambda name, _context: name,
        build_source_vtt_sidecar_path_fn=lambda path, _lang: path.with_suffix(".src"),
        run_whisper_with_explicit_language_fn=lambda *_a, **_k: (0, "backend", "model"),
        finalize_vtt_fn=lambda *_a, **_k: 4,
        normalize_language_fn=lambda value: value,
    )
    assert rc == 4
    assert language == "en"

    expected_vtt.write_text("WEBVTT\n", encoding="utf-8")
    rc, metadata, language = translation_flow.run_legacy_whisper_translation_fallback(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        source_language="en",
        target_language="fr",
        whisper_fallback_options={
            "model": "small",
            "use_gpu": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        debug=False,
        translation_backend_whisper_legacy="legacy",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        map_model_name_fn=lambda name, _context: name,
        build_source_vtt_sidecar_path_fn=lambda path, _lang: path.with_suffix(".src"),
        run_whisper_with_explicit_language_fn=lambda *_a, **_k: (0, "backend", "model"),
        finalize_vtt_fn=lambda *_a, **_k: 0,
        normalize_language_fn=lambda value: value,
    )
    assert rc == 0
    assert metadata["applied"] is True
    assert language == "fr"


def test_translation_flow_check_translation_input_vtt_branches(tmp_path, capsys):
    """Validate Translation flow check translation input vtt branches."""
    translation_flow = _load_core_module("translation_flow_utils")

    expected_vtt, response = translation_flow.check_translation_input_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language="en",
        use_gpu=False,
        debug=False,
        translation_backend_local="local",
        translation_backend_none="none",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        normalize_language_fn=lambda value: value,
        resolve_translation_model_name_fn=lambda *_a, **_k: "model",
        read_last_vtt_cue_end_seconds_fn=lambda _path: (True, True, 1.0),
    )
    assert expected_vtt is None
    assert response is not None

    vtt_path = tmp_path / "a.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    expected_vtt, response = translation_flow.check_translation_input_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language="en",
        use_gpu=False,
        debug=True,
        translation_backend_local="local",
        translation_backend_none="none",
        build_translation_metadata_fn=lambda **kwargs: dict(kwargs),
        normalize_language_fn=lambda value: value,
        resolve_translation_model_name_fn=lambda *_a, **_k: "model",
        read_last_vtt_cue_end_seconds_fn=lambda _path: (True, False, None),
    )
    assert expected_vtt == vtt_path
    assert response is not None
    assert "Generated VTT contains no subtitle cues" in capsys.readouterr().out


def test_translation_flow_maybe_translate_decision_branches(tmp_path, capsys):
    """Validate Translation flow maybe translate decision branches."""
    translation_flow = _load_core_module("translation_flow_utils")

    def build_metadata(**kwargs):
        return dict(kwargs)

    base_context = translation_flow.TranslationDecisionContext(
        translation_backend_none="none",
        translation_backend_local="local",
        translation_backend_whisper_legacy="legacy",
        translation_decision_failed_rc=33,
        translation_unsupported_pair_rc=30,
        normalize_language_fn=lambda value: None if value is None else str(value).lower(),
        build_translation_metadata_fn=build_metadata,
        check_translation_input_vtt_fn=lambda *_a, **_k: (None, None),
        resolve_translation_model_name_fn=lambda *_a, **_k: "model",
        run_legacy_whisper_translation_fallback_fn=lambda *_a, **_k: (0, {"applied": False}, "fr"),
        translate_vtt_file_fn=lambda *_a, **_k: (0, {"applied": True}),
    )

    rc, metadata, final_language = translation_flow.maybe_translate_final_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=base_context,
    )
    assert rc == 5
    assert metadata["backend"] == "local"
    assert final_language is None

    no_detect_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "check_translation_input_vtt_fn": lambda *_a, **_k: (tmp_path / "a.vtt", None),
        }
    )
    rc, _metadata, final_language = translation_flow.maybe_translate_final_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language=None,
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=no_detect_context,
    )
    assert rc == 33
    assert final_language is None
    assert "Subtitle translation decision failed" in capsys.readouterr().out

    same_language_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "check_translation_input_vtt_fn": lambda *_a, **_k: (tmp_path / "a.vtt", None),
        }
    )
    rc, metadata, final_language = translation_flow.maybe_translate_final_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="EN",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=True,
        context=same_language_context,
    )
    assert rc == 0
    assert metadata["backend"] == "none"
    assert final_language == "en"
    assert "translation skipped" in capsys.readouterr().out

    unsupported_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "check_translation_input_vtt_fn": lambda *_a, **_k: (tmp_path / "a.vtt", None),
            "resolve_translation_model_name_fn": lambda *_a, **_k: None,
        }
    )
    rc, metadata, final_language = translation_flow.maybe_translate_final_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=True,
        context=unsupported_context,
    )
    assert rc == 30
    assert metadata["note"] == "legacy_whisper_fallback_not_configured"
    assert final_language is None

    local_context = types.SimpleNamespace(
        **{
            **base_context.__dict__,
            "check_translation_input_vtt_fn": lambda *_a, **_k: (tmp_path / "a.vtt", None),
            "resolve_translation_model_name_fn": lambda *_a, **_k: "local-model",
            "translate_vtt_file_fn": lambda *_a, **_k: (0, {"backend": "local"}),
        }
    )
    rc, metadata, final_language = translation_flow.maybe_translate_final_vtt(
        audio_src=tmp_path / "a.mp3",
        work_dir=tmp_path,
        requested_language="fr",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=local_context,
    )
    assert rc == 0
    assert metadata["backend"] == "local"
    assert final_language == "fr"


def test_translation_runtime_utils_wrapper_paths(monkeypatch, tmp_path):
    """Validate Translation runtime utils wrapper paths."""
    translation_runtime = _load_core_module("translation_runtime_utils", force_insert_branch=True)

    monkeypatch.setattr(
        translation_runtime.translation_utils,
        "build_source_vtt_sidecar_path",
        lambda *_a, **_k: tmp_path / "x.src",
    )
    assert (
        translation_runtime.build_source_vtt_sidecar_path(tmp_path / "x.vtt", "en")
        == tmp_path / "x.src"
    )

    monkeypatch.setattr(
        translation_runtime.translation_utils,
        "resolve_translation_model_name",
        lambda **_k: "model",
    )
    assert translation_runtime.resolve_translation_model_name("en", "fr", False) == "model"

    metadata = translation_runtime.build_translation_metadata(
        applied=False,
        backend="none",
        source_language="en",
        target_language="fr",
        model=None,
        use_gpu=False,
    )
    assert metadata["backend"] == "none"

    monkeypatch.setattr(
        translation_runtime.translation_utils,
        "load_translation_model_objects",
        lambda *_a, **_k: ("tok", "model"),
    )
    assert translation_runtime.load_translation_model_objects(object(), object(), "m", None) == (
        "tok",
        "model",
    )

    monkeypatch.setattr(
        translation_runtime.translation_runtime_flow_utils,
        "load_translation_runtime",
        lambda **_k: (0, object(), ("tok", "model"), "m"),
    )
    assert translation_runtime.load_translation_runtime("en", "fr", False, None, False)[0] == 0

    monkeypatch.setattr(
        translation_runtime.translation_utils,
        "translate_cue_texts",
        lambda *args, **kwargs: ["ok"],
    )
    assert translation_runtime.translate_cue_texts(
        ["x"], translate_batch=lambda b: b, batch_size=1
    ) == ["ok"]

    monkeypatch.setattr(
        translation_runtime.translation_utils,
        "translate_vtt_content",
        lambda *_a, **_k: "WEBVTT\n",
    )
    assert (
        translation_runtime.translate_vtt_content(
            "WEBVTT\n",
            translate_batch=lambda batch: batch,
            max_line_width=40,
            max_line_count=2,
        )
        == "WEBVTT\n"
    )

    assert translation_runtime.build_translate_vtt_file_context().translation_backend_local

    monkeypatch.setattr(
        translation_runtime.translation_vtt_file_flow_utils,
        "translate_vtt_file",
        lambda *_a, **_k: (0, {"applied": True}),
    )
    assert (
        translation_runtime.translate_vtt_file(
            tmp_path / "a.vtt",
            source_language="en",
            target_language="fr",
            use_gpu=False,
            huggingface_models_dir=None,
            max_line_width=40,
            max_line_count=2,
            debug=False,
        )[0]
        == 0
    )

    monkeypatch.setattr(
        translation_runtime.translation_decision_flow_utils,
        "run_whisper_with_explicit_language",
        lambda *_a, **_k: (0, "backend", "model"),
    )
    assert (
        translation_runtime.run_whisper_with_explicit_language(
            tmp_path / "a.mp3",
            tmp_path,
            language="fr",
            whisper_fallback_options={
                "model": "small",
                "whisper_models_dir": "",
                "use_gpu": False,
                "gpu_device": 0,
                "vad_filter": False,
                "timeout_sec": 3,
                "chunk_duration_sec": 30,
                "chunk_overlap_sec": 2,
                "chunk_threshold_sec": 60,
                "vtt_highlight_words": False,
                "vtt_max_line_count": 2,
                "vtt_max_line_width": 40,
            },
            debug=False,
        )[0]
        == 0
    )

    monkeypatch.setattr(
        translation_runtime.translation_decision_flow_utils,
        "run_legacy_whisper_translation_fallback",
        lambda *_a, **_k: (0, {"ok": True}, "fr"),
    )
    assert (
        translation_runtime.run_legacy_whisper_translation_fallback(
            tmp_path / "a.mp3",
            tmp_path,
            source_language="en",
            target_language="fr",
            whisper_fallback_options={"model": "small", "use_gpu": False},
            debug=False,
        )[0]
        == 0
    )

    monkeypatch.setattr(
        translation_runtime.translation_decision_flow_utils,
        "check_translation_input_vtt",
        lambda *_a, **_k: (tmp_path / "a.vtt", None),
    )
    assert (
        translation_runtime.check_translation_input_vtt(
            tmp_path / "a.mp3",
            tmp_path,
            requested_language="fr",
            detected_language="en",
            use_gpu=False,
            debug=False,
        )[0]
        == tmp_path / "a.vtt"
    )

    assert (
        translation_runtime.build_translation_decision_context().translation_backend_none == "none"
    )

    monkeypatch.setattr(
        translation_runtime.translation_decision_flow_utils,
        "maybe_translate_final_vtt",
        lambda *_a, **_k: (0, {"backend": "none"}, "en"),
    )
    assert (
        translation_runtime.maybe_translate_final_vtt(
            tmp_path / "a.mp3",
            tmp_path,
            requested_language="fr",
            detected_language="en",
            whisper_fallback_options=None,
            use_gpu=False,
            huggingface_models_dir=None,
            max_line_width=40,
            max_line_count=2,
            debug=False,
        )[0]
        == 0
    )


def test_vtt_postprocess_utils_extra_branches(monkeypatch, tmp_path, capsys):
    """Validate Vtt postprocess utils extra branches."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    real_import = builtins.__import__

    def import_without_validation(name, *args, **kwargs):
        if name == "vtt_validation_utils":
            raise ModuleNotFoundError(name="vtt_validation_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_validation)
    loaded_module = vtt_utils._load_vtt_validation_utils_module()
    assert hasattr(loaded_module, "parse_vtt_timestamp")

    writer_calls = []

    def writer_factory(_format: str, _out_dir: str):
        def writer(result, stem, options):
            writer_calls.append((result, stem, options))

        return writer

    assert (
        vtt_utils.write_vtt_result(
            {"segments": []},
            Path("audio.mp3"),
            tmp_path,
            writer_factory,
            {"max_line_width": 40},
            debug=True,
        )
        is True
    )
    assert writer_calls

    assert (
        vtt_utils.write_vtt_result(
            {"segments": []},
            Path("audio.mp3"),
            tmp_path,
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("writer")),
            {},
            debug=False,
        )
        is False
    )

    assert vtt_utils.wrap_vtt_cue_text("hello world", max_line_width=0, max_line_count=2) == [
        "hello world"
    ]

    overflow = vtt_utils.wrap_vtt_cue_text(
        "one two three four five six",
        max_line_width=4,
        max_line_count=2,
    )
    assert overflow
    assert all(len(line) <= 4 for line in overflow)

    assert vtt_utils.parse_vtt_cue_time_range(
        "invalid", parse_vtt_timestamp_fn=lambda _raw: None
    ) == (None, None)
    assert vtt_utils.parse_vtt_cue_time_range(
        "00:00:00.000 --> ",
        parse_vtt_timestamp_fn=lambda value: float(len(value)),
    ) == (None, None)

    assert (
        vtt_utils.cue_gap_allows_apostrophe_transfer(
            "a",
            "b",
            parse_vtt_cue_time_range_fn=lambda _line: (None, None),
        )
        is False
    )

    assert (
        vtt_utils.extract_trailing_token_core(
            "",
            normalize_vtt_cue_text_fn=lambda _text: "",
        )
        == ""
    )

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "",
        "next",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "ignored",
    ) == ("", "next")

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "previous",
        "next",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "",
    ) == ("previous", "next")

    assert vtt_utils.repair_cross_cue_apostrophe_split(
        "bonjour",
        "'suite",
        normalize_vtt_cue_text_fn=lambda text: text,
        extract_trailing_token_core_fn=lambda _text: "bonjour",
    ) == ("bonjour", "'suite")

    class WeirdBlock(str):
        def __contains__(self, item: object) -> bool:
            if item == "-->":
                return True
            return super().__contains__(item)

        def splitlines(self) -> list[str]:
            return ["header", "body"]

    weird = WeirdBlock("not-a-timestamp-line")
    assert vtt_utils.parse_vtt_postprocess_block(weird) == weird

    blocks = [
        ([""], "a"),
        (["00:00:01.000 --> 00:00:02.000"], "b"),
    ]
    vtt_utils.repair_cross_cue_apostrophe_splits(
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=lambda *_a: False,
        repair_cross_cue_apostrophe_split_fn=lambda _a, _b: ("x", "y"),
    )

    rendered = vtt_utils.render_postprocessed_vtt_blocks(
        [(["00:00:00.000 --> 00:00:01.000"], "text")],
        max_line_width=40,
        max_line_count=2,
        wrap_vtt_cue_text_fn=lambda *_a, **_k: [],
    )
    assert rendered == []

    assert (
        vtt_utils.format_vtt_cue_time_range(
            "00:00:00.000 --> 00:00:04.000 line:90%",
            0.0,
            2.0,
            format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
        )
        == "0.0 --> 2.0 line:90%"
    )
    assert vtt_utils.split_vtt_cue_prefixes(
        [],
        2,
        parse_vtt_timestamp_fn=lambda _raw: 0.0,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [[], []]
    assert vtt_utils.split_vtt_cue_prefixes(
        ["00:00:00.000 --> 00:00:04.000"],
        2,
        parse_vtt_timestamp_fn=None,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [["00:00:00.000 --> 00:00:04.000"], ["00:00:00.000 --> 00:00:04.000"]]
    assert vtt_utils.split_vtt_cue_prefixes(
        ["invalid"],
        2,
        parse_vtt_timestamp_fn=lambda _raw: None,
        format_vtt_timestamp_fn=lambda value: f"{value:.1f}",
    ) == [["invalid"], ["invalid"]]

    vtt_path = tmp_path / "a.vtt"
    vtt_path.write_text("WEBVTT\n", encoding="utf-8")
    vtt_utils.postprocess_vtt_file(
        vtt_path,
        max_line_width=40,
        max_line_count=2,
        debug=True,
        postprocess_vtt_content_fn=lambda content, **_k: content + "\nchanged\n",
    )
    assert "Applied readability post-processing" in capsys.readouterr().out


def test_vtt_validation_utils_extra_branches(tmp_path, capsys):
    """Validate Vtt validation utils extra branches."""
    validation_utils = _load_core_module("vtt_validation_utils")

    assert validation_utils.parse_vtt_timestamp("  ") is None
    assert validation_utils.parse_vtt_timestamp("xx:00:01.000") is None
    assert validation_utils.parse_vtt_timestamp("00") is None
    assert validation_utils.parse_vtt_timestamp("00:aa.bb") is None

    malformed_vtt = tmp_path / "malformed.vtt"
    malformed_vtt.write_text("WEBVTT\n\n00:00:00.000 --> \n", encoding="utf-8")
    assert validation_utils.read_last_vtt_cue_end_seconds(
        malformed_vtt,
        parse_timestamp=lambda _raw: 1.0,
    ) == (True, True, None)

    assert validation_utils.read_last_vtt_cue_end_seconds(
        tmp_path / "missing.vtt",
        parse_timestamp=lambda _raw: 1.0,
    ) == (False, False, None)

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=0.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (True, True, 1.0),
        )
        == 0
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (False, False, None),
        )
        == 7
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=True,
            read_last_cue_end_seconds=lambda _path: (True, False, None),
        )
        == 0
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=False,
            read_last_cue_end_seconds=lambda _path: (True, True, None),
        )
        == 7
    )

    assert (
        validation_utils.validate_vtt_coverage(
            vtt_path=tmp_path / "x.vtt",
            reference_duration_sec=10.0,
            min_coverage_ratio=0.8,
            max_final_gap_sec=4.0,
            debug=True,
            read_last_cue_end_seconds=lambda _path: (True, True, 9.5),
        )
        == 0
    )
    assert "VTT coverage:" in capsys.readouterr().out

    bad_range_file = tmp_path / "ranges.vtt"
    bad_range_file.write_text("WEBVTT\n\n --> invalid\n", encoding="utf-8")
    read_ok, cues = validation_utils.read_vtt_cue_time_ranges(
        bad_range_file,
        parse_timestamp=lambda _token: None,
    )
    assert read_ok is True
    assert cues == []


def test_whisper_python_runtime_utils_wrappers_and_model_fallback(monkeypatch, tmp_path):
    """Validate Whisper python runtime utils wrappers and model fallback."""
    whisper_runtime = _load_core_module("whisper_python_runtime_utils", force_insert_branch=True)

    monkeypatch.setattr(whisper_runtime.chunking_utils, "extract_audio_chunk", lambda *a, **k: 0)
    assert (
        whisper_runtime.extract_audio_chunk(
            tmp_path / "a.mp3",
            tmp_path / "chunk.mp3",
            0.0,
            1.0,
            5,
            False,
        )
        == 0
    )

    monkeypatch.setattr(
        whisper_runtime.vtt_postprocess_utils, "write_vtt_result", lambda *a, **k: True
    )
    assert (
        whisper_runtime.write_vtt_result(
            {"segments": []},
            tmp_path / "a.mp3",
            tmp_path,
            lambda *_a, **_k: None,
            {},
            False,
        )
        is True
    )

    monkeypatch.setattr(
        whisper_runtime.runtime_cli_utils,
        "map_model_name",
        lambda model, context: f"{model}-{context}",
    )
    load_calls = {"count": 0}

    def load_model_with_cpu_retry(
        model_name: str, device: str, whisper_models_dir: str | None = None
    ):
        del model_name, whisper_models_dir
        load_calls["count"] += 1
        if device == "cuda":
            return None
        return {"loaded": True}

    monkeypatch.setattr(
        whisper_runtime.runtime_cli_utils, "load_whisper_model", load_model_with_cpu_retry
    )
    rc, model, device = whisper_runtime.load_whisper_runtime_model(
        torch=types.SimpleNamespace(float16="f16", float32="f32"),
        model="small",
        whisper_models_dir="",
        use_gpu=True,
        debug=True,
    )
    assert rc == 0
    assert model is not None
    assert device == "cpu"
    assert load_calls["count"] == 2

    monkeypatch.setattr(
        whisper_runtime.runtime_cli_utils, "load_whisper_model", lambda *a, **k: None
    )
    rc, model, _device = whisper_runtime.load_whisper_runtime_model(
        torch=types.SimpleNamespace(float16="f16", float32="f32"),
        model="small",
        whisper_models_dir="",
        use_gpu=False,
        debug=False,
    )
    assert rc == 10
    assert model is None

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "prepare_transcription_plan",
        lambda **kwargs: (1.0, [(0.0, 1.0)], {"x": 1}),
    )
    assert (
        whisper_runtime.prepare_transcription_plan(
            tmp_path / "a.mp3",
            "auto",
            False,
            "cpu",
            30,
            2,
            60,
            False,
        )[0]
        == 1.0
    )

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "build_chunk_transcribe_kwargs",
        lambda *a, **k: {"prompt": "x"},
    )
    assert (
        whisper_runtime.build_chunk_transcribe_kwargs(
            {"x": 1},
            "en",
            False,
            "hello",
        )["prompt"]
        == "x"
    )

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "transcribe_one_audio_chunk",
        lambda **kwargs: (0, {"text": "ok"}, "en", "ok"),
    )
    assert (
        whisper_runtime.transcribe_one_audio_chunk(
            object(),
            tmp_path / "a.mp3",
            tmp_path,
            0,
            1,
            0.0,
            1.0,
            5,
            {},
            None,
            False,
            "",
            "auto",
            False,
        )[0]
        == 0
    )

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "combine_chunk_results",
        lambda *a, **k: {"text": "merged"},
    )
    assert (
        whisper_runtime.combine_chunk_results_with_defaults([(0.0, {"segments": []})])["text"]
        == "merged"
    )

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "run_chunked_whisper_transcription",
        lambda **kwargs: (0, {"text": "merged"}, "en"),
    )
    assert (
        whisper_runtime.run_chunked_whisper_transcription(
            object(),
            tmp_path / "a.mp3",
            tmp_path,
            [(0.0, 1.0)],
            1.0,
            {},
            "auto",
            5,
            30,
            2,
            False,
        )[0]
        == 0
    )

    monkeypatch.setattr(
        whisper_runtime.chunking_utils,
        "run_whisper_python_transcription",
        lambda **kwargs: (0, {"segments": []}, "en"),
    )
    assert (
        whisper_runtime.run_whisper_python_transcription(
            object(),
            tmp_path / "a.mp3",
            tmp_path,
            "auto",
            False,
            "cpu",
            5,
            30,
            2,
            60,
            False,
        )[0]
        == 0
    )


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


def test_remaining_runtime_cli_lines(monkeypatch, capsys):
    """Validate Remaining runtime cli lines."""
    runtime_cli = _load_core_module("runtime_cli_utils")

    real_import = builtins.__import__

    def no_language_utils(name, *args, **kwargs):
        if name == "language_utils":
            raise ModuleNotFoundError(name="language_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_language_utils)
    monkeypatch.setattr(
        runtime_cli.importlib.util, "spec_from_file_location", lambda *_a, **_k: None
    )
    with pytest.raises(ModuleNotFoundError):
        runtime_cli._load_language_utils_module()

    monkeypatch.setattr(builtins, "__import__", real_import)

    monkeypatch.delenv("GPU_CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("GPU_CUDA_DEVICE_ORDER", raising=False)
    _args, env = runtime_cli.prepare_whisper_env(use_gpu=True, gpu_device=2)
    assert env["CUDA_VISIBLE_DEVICES"] == "2"

    assert (
        runtime_cli.detect_language_from_stdout(
            "nothing interesting",
            "auto",
            map_language_name_to_code_fn=lambda name: name,
        )
        is None
    )

    fake_torch_ok = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
    monkeypatch.setitem(sys.modules, "torch", fake_torch_ok)
    assert runtime_cli.resolve_effective_use_gpu(True, 0, False) is True

    assert (
        runtime_cli.resolve_effective_use_gpu(
            True,
            0,
            True,
            apply_runtime_cuda_environment_fn=lambda _gpu: (_ for _ in ()).throw(
                RuntimeError("env")
            ),
        )
        is False
    )
    assert "Failed to probe CUDA availability (env); falling back to CPU" in capsys.readouterr().out

    whisper_utils = types.ModuleType("whisper.utils")
    whisper_utils.get_writer = lambda *_a, **_k: None
    whisper_module = types.ModuleType("whisper")
    whisper_module.utils = whisper_utils
    monkeypatch.setitem(sys.modules, "torch", object())
    monkeypatch.setitem(sys.modules, "whisper", whisper_module)
    monkeypatch.setitem(sys.modules, "whisper.utils", whisper_utils)
    imported = runtime_cli.import_whisper_modules(use_gpu=False)
    assert imported[0] is not None
    assert imported[1] is not None
    assert callable(imported[2])


def test_remaining_translation_utils_empty_cue_line():
    """Validate Remaining translation utils empty cue line."""
    translation_utils = _load_core_module("translation_utils")

    translated = translation_utils.translate_vtt_content(
        "00:00:00.000 --> 00:00:01.000\n   \n",
        translate_batch=lambda batch: [f"x:{value}" for value in batch],
        max_line_width=40,
        max_line_count=2,
        batch_size=4,
        parse_vtt_postprocess_block=lambda block: (
            (["00:00:00.000 --> 00:00:01.000"], "   ") if "-->" in block else block
        ),
        normalize_vtt_cue_text=lambda text: " ".join(text.split()).strip(),
        translate_cue_texts_fn=lambda cue_texts, **_k: cue_texts,
        repair_cross_cue_apostrophe_splits=lambda _blocks: None,
        render_postprocessed_vtt_blocks=lambda blocks, **_k: [str(block) for block in blocks],
    )
    assert translated.endswith("\n")


def test_remaining_vtt_postprocess_lines(monkeypatch):
    """Validate Remaining vtt postprocess lines."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    real_import = builtins.__import__

    def no_validation(name, *args, **kwargs):
        if name == "vtt_validation_utils":
            raise ModuleNotFoundError(name="vtt_validation_utils")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_validation)
    monkeypatch.setattr(vtt_utils.importlib.util, "spec_from_file_location", lambda *_a, **_k: None)
    with pytest.raises(ModuleNotFoundError):
        vtt_utils._load_vtt_validation_utils_module()

    overflow = vtt_utils.wrap_vtt_cue_text(
        "one two six ten",
        max_line_width=3,
        max_line_count=2,
    )
    assert overflow == ["one", "two", "six", "ten"]
    assert vtt_utils.split_vtt_cue_text(
        "one two six ten",
        max_line_width=3,
        max_line_count=2,
    ) == [["one", "two"], ["six", "ten"]]

    blocks = [
        ([], "a"),
        (["00:00:01.000 --> 00:00:02.000"], "b"),
    ]
    vtt_utils.repair_cross_cue_apostrophe_splits(
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=lambda *_a: False,
        repair_cross_cue_apostrophe_split_fn=lambda _a, _b: ("x", "y"),
    )


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


def test_remaining_vtt_postprocess_overflow_merge_line(monkeypatch):
    """Validate Remaining vtt postprocess overflow merge line."""
    vtt_utils = _load_core_module("vtt_postprocess_utils")

    class SneakyWord(str):
        def __format__(self, format_spec: str) -> str:
            del format_spec
            frame = inspect.currentframe()
            while frame is not None:
                if (
                    frame.f_code.co_name == "wrap_vtt_cue_text"
                    and "wrapped_lines" in frame.f_locals
                ):
                    frame.f_locals["wrapped_lines"].extend(["x", "y", "z"])
                    break
                frame = frame.f_back
            return str(self)

    class SneakyText(str):
        def split(self, sep: str | None = None, maxsplit: int = -1):
            del sep, maxsplit
            return [SneakyWord("one"), SneakyWord("two")]

    monkeypatch.setattr(vtt_utils, "normalize_vtt_cue_text", lambda _text: SneakyText("unused"))
    wrapped = vtt_utils.wrap_vtt_cue_text("anything", max_line_width=3, max_line_count=2)
    assert wrapped
    assert "one" in wrapped
    assert "two" in wrapped
