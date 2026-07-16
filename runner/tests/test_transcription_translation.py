"""Tests for transcribed subtitle translation flows."""

import builtins
import inspect
import sys
import types
from typing import Any

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module


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
