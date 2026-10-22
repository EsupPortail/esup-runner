"""Tests for Whisper Python execution."""

import types

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module


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


def test_whisper_python_runtime_utils_run_whisper_python_branches(monkeypatch, tmp_path):
    """Validate Whisper python runtime utils run whisper python branches."""
    whisper_runtime = _load_core_module("whisper_python_runtime_utils")

    base_kwargs = {
        "audio_path": tmp_path / "audio.mp3",
        "out_dir": tmp_path / "out",
        "language": "fr",
        "model": "small",
        "whisper_models_dir": str(tmp_path / "models"),
        "use_gpu": False,
        "gpu_device": 0,
        "vad_filter": False,
        "timeout_sec": 30,
        "chunk_duration_sec": 60,
        "chunk_overlap_sec": 2,
        "chunk_threshold_sec": 120,
        "vtt_highlight_words": True,
        "vtt_max_line_count": 2,
        "vtt_max_line_width": 40,
        "debug": True,
    }

    monkeypatch.setattr(
        whisper_runtime.runtime_cli_utils,
        "import_whisper_modules",
        lambda **_k: (None, None, None),
    )
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs)
    assert rc == 255
    assert detected is None

    torch_stub = types.SimpleNamespace(float16="f16", float32="f32")
    whisper_stub = types.SimpleNamespace(__version__="1.2.3")

    def get_writer_stub(*_a, **_k):
        return None

    monkeypatch.setattr(
        whisper_runtime.runtime_cli_utils,
        "import_whisper_modules",
        lambda **_k: (torch_stub, whisper_stub, get_writer_stub),
    )
    monkeypatch.setattr(
        whisper_runtime,
        "load_whisper_runtime_model",
        lambda **_k: (10, None, "cpu"),
    )
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs)
    assert rc == 10
    assert detected is None

    monkeypatch.setattr(
        whisper_runtime,
        "load_whisper_runtime_model",
        lambda **_k: (0, object(), "cpu"),
    )
    monkeypatch.setattr(
        whisper_runtime,
        "run_whisper_python_transcription",
        lambda **_k: (7, None, "en"),
    )
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs)
    assert rc == 7
    assert detected == "en"

    monkeypatch.setattr(
        whisper_runtime,
        "run_whisper_python_transcription",
        lambda **_k: (0, None, "en"),
    )
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs)
    assert rc == 20
    assert detected == "en"

    monkeypatch.setattr(
        whisper_runtime,
        "run_whisper_python_transcription",
        lambda **_k: (0, {"segments": [{"text": "bonjour"}]}, "fr"),
    )
    monkeypatch.setattr(
        whisper_runtime.language_utils,
        "normalize_language_code",
        lambda value: (value or "").lower() if value else None,
    )
    monkeypatch.setattr(
        whisper_runtime.segment_filter_utils,
        "filter_result_segments",
        lambda result, **_k: result,
    )
    monkeypatch.setattr(
        whisper_runtime.segment_filter_utils,
        "extract_detected_language",
        lambda _result: "fr",
    )
    monkeypatch.setattr(whisper_runtime, "write_vtt_result", lambda *_a, **_k: False)
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs)
    assert rc == 21
    assert detected == "fr"

    base_kwargs_auto = dict(base_kwargs)
    base_kwargs_auto["language"] = "auto"
    monkeypatch.setattr(whisper_runtime, "write_vtt_result", lambda *_a, **_k: True)
    rc, detected = whisper_runtime.run_whisper_python(**base_kwargs_auto)
    assert rc == 0
    assert detected == "fr"


@pytest.mark.parametrize(
    "language,retry_rc,retry_segments,expected_end,coverage_rc",
    [
        ("fr", 0, [{"start": 764.0, "end": 990.0, "text": "fin"}], 990.0, 0),
        ("auto", 0, [{"start": 764.0, "end": 990.0, "text": "fin"}], 990.0, 0),
        ("fr", 0, [{"start": 340.0, "end": 600.0, "text": "suite"}], 600.0, 7),
        ("fr", 0, [{"start": 1.0, "end": 100.0, "text": "début"}], 338.82, 7),
        ("fr", 0, [], 338.82, 7),
        ("fr", 0, [{"start": 764.0, "end": 990.0, "text": "Sous-titres"}], 338.82, 7),
        ("fr", 0, None, 338.82, 7),
        ("fr", 22, None, 338.82, 7),
    ],
)
def test_truncated_single_pass_retries_once_and_keeps_coverage_guard(
    monkeypatch, tmp_path, capsys, language, retry_rc, retry_segments, expected_end, coverage_rc
):
    """Recover late speech without accepting an empty or still truncated retry."""
    runtime = _load_core_module("whisper_python_runtime_utils")
    validation = _load_core_module("output_validation_runtime_utils")
    model = object()
    loads = []
    calls = []
    vtt_path = tmp_path / "audio.vtt"

    monkeypatch.setattr(
        runtime.runtime_cli_utils,
        "import_whisper_modules",
        lambda **_k: (object(), types.SimpleNamespace(__version__="test"), object()),
    )

    def load_model(**kwargs):
        loads.append(kwargs)
        return 0, model, "cuda"

    def transcribe(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                0,
                {
                    "language": "fr",
                    "segments": [
                        {"start": 1.84, "end": 338.82, "text": "début"},
                        {"start": 764.0, "end": 1000.0, "text": "Sous-titres"},
                    ],
                },
                "fr",
            )
        assert len(calls) == 2
        retry_result = (
            {"language": "fr", "segments": retry_segments} if retry_segments is not None else None
        )
        return retry_rc, retry_result, "fr"

    def write_result(result, *_args):
        cues = [
            f"{validation.format_vtt_timestamp(segment['start'])} --> "
            f"{validation.format_vtt_timestamp(segment['end'])}\n{segment['text']}"
            for segment in result["segments"]
        ]
        vtt_path.write_text("WEBVTT\n\n" + "\n\n".join(cues) + "\n", encoding="utf-8")
        return True

    monkeypatch.setattr(runtime, "load_whisper_runtime_model", load_model)
    monkeypatch.setattr(runtime, "run_whisper_python_transcription", transcribe)
    monkeypatch.setattr(runtime, "write_vtt_result", write_result)
    monkeypatch.setattr(runtime.runtime_media_utils, "probe_duration_seconds", lambda *_a: 1002.0)
    monkeypatch.setattr(
        runtime.runtime_media_utils, "probe_effective_duration_seconds", lambda *_a: 1002.0
    )

    rc, detected = runtime.run_whisper_python(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path,
        language=language,
        model="turbo",
        whisper_models_dir=None,
        use_gpu=True,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=8016,
        chunk_duration_sec=300,
        chunk_overlap_sec=3,
        chunk_threshold_sec=1800,
        vtt_highlight_words=False,
        vtt_max_line_count=2,
        vtt_max_line_width=40,
        debug=False,
    )

    assert (rc, detected) == (0, "fr")
    assert len(loads) == 1
    assert len(calls) == 2
    assert calls[0]["wmodel"] is model
    assert calls[1] == {**calls[0], "chunk_threshold_sec": 0}
    assert validation.read_last_vtt_cue_end_seconds(vtt_path) == (True, True, expected_end)
    assert validation.validate_vtt_coverage(vtt_path, 1002.0, 0.75, 300.0, False) == coverage_rc
    assert "retrying once with chunked transcription" in capsys.readouterr().out


@pytest.mark.parametrize(
    "result,duration,effective_duration,chunk_duration,chunk_threshold,expected",
    [
        ({"segments": [{"end": 338.82}]}, 1002.0, 1002.0, 300, 1800, True),
        ({"segments": [{"end": 338.82}]}, 1002.0, 0.0, 300, 1800, True),
        ({"segments": [{"end": 338.82}]}, 1002.0, 1200.0, 300, 1800, True),
        ({"segments": [{"end": 338.82}]}, 1002.0, 340.0, 300, 1800, False),
        ({"segments": [{"end": 338.82}]}, 1002.0, 1002.0, 0, 1800, False),
        ({"segments": [{"end": 338.82}]}, 1002.0, 1002.0, 300, 0, False),
        ({"segments": [{"end": 338.82}]}, 1002.0, 1002.0, 300, 800, False),
        ({"segments": [{"end": 100.0}]}, 200.0, 200.0, 300, 1800, False),
        ({"segments": [{"end": 338.82}]}, 0.0, 0.0, 300, 1800, False),
        ({"segments": [{"end": 752.0}]}, 1002.0, 1002.0, 300, 1800, False),
        ({"segments": [{"end": 310.0}]}, 600.0, 600.0, 300, 1800, False),
        ({"segments": []}, 1002.0, 1002.0, 300, 1800, False),
        ({"segments": "invalid"}, 1002.0, 1002.0, 300, 1800, False),
        ({"segments": [None, {}, {"end": "invalid"}]}, 1002.0, 1002.0, 300, 1800, False),
        ({"segments": [{"end": 1000.0}, {"end": 338.82}]}, 1002.0, 1002.0, 300, 1800, False),
    ],
)
def test_chunked_retry_respects_duration_silence_and_existing_chunking(
    monkeypatch,
    tmp_path,
    result,
    duration,
    effective_duration,
    chunk_duration,
    chunk_threshold,
    expected,
):
    """Limit retries to truncated single-pass results with a usable chunk plan."""
    runtime = _load_core_module("whisper_python_runtime_utils")
    monkeypatch.setattr(runtime.runtime_media_utils, "probe_duration_seconds", lambda *_a: duration)
    monkeypatch.setattr(
        runtime.runtime_media_utils,
        "probe_effective_duration_seconds",
        lambda *_a: effective_duration,
    )

    assert (
        runtime._should_retry_with_chunks(
            result, tmp_path / "audio.mp3", chunk_duration, chunk_threshold, False
        )
        is expected
    )
