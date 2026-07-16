"""Tests for Whisper Python execution."""

import types

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
