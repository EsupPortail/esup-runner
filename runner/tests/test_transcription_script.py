import importlib.util
import os
import sys
import types
from pathlib import Path


def _load_transcription_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (
        repo_root / "app" / "task_handlers" / "transcription" / "scripts" / "transcription.py"
    )
    spec = importlib.util.spec_from_file_location("transcription_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finalize_vtt_accepts_truncated_stem_from_whisper_cli(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    whisper_cli_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.vtt"
    whisper_cli_vtt.write_text("WEBVTT\n\n")

    rc = tr._finalize_vtt(audio_src, work_dir)

    expected_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.189832.vtt"
    assert rc == 0
    assert expected_vtt.exists()
    assert not whisper_cli_vtt.exists()


def test_finalize_vtt_fails_when_no_vtt_found(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    rc = tr._finalize_vtt(audio_src, work_dir)

    assert rc == 5


def test_finalize_vtt_postprocesses_apostrophe_wrapping(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")

    generated_vtt = work_dir / "audio_192k_test.vtt"
    generated_vtt.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Je parle de l\n"
        "'usage responsable aujourd'hui.\n",
        encoding="utf-8",
    )

    rc = tr._finalize_vtt(audio_src, work_dir, max_line_count=2, max_line_width=24)

    assert rc == 0
    processed = generated_vtt.read_text(encoding="utf-8")
    assert "l'usage" in processed
    assert "l\n'usage" not in processed


def test_run_transcription_uses_auto_source_language_for_requested_target_language(
    monkeypatch, tmp_path
):
    tr = _load_transcription_script_module()

    captured = {}

    def fake_run_whisper_python(**kwargs):
        captured["language"] = kwargs["language"]
        return 0, "fr"

    monkeypatch.setattr(tr, "run_whisper_python", fake_run_whisper_python)
    monkeypatch.setattr(tr, "run_whisper_cli", lambda **kwargs: (255, None))

    args = tr.parse_args(
        [
            "--base-dir",
            str(tmp_path),
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
            "--language",
            "en",
        ]
    )

    rc, detected_language = tr._run_transcription(
        args,
        tmp_path / "audio.mp3",
        tmp_path / "output",
        60,
        False,
        False,
    )

    assert rc == 0
    assert detected_language == "fr"
    assert captured["language"] == "auto"


def test_run_whisper_cli_reports_resolution_hint_when_binary_is_missing(
    monkeypatch, tmp_path, capsys
):
    tr = _load_transcription_script_module()

    def fake_subprocess_run(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "whisper")

    monkeypatch.setattr(tr.subprocess, "run", fake_subprocess_run)

    rc, detected_language = tr.run_whisper_cli(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "output",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        debug=False,
    )

    captured = capsys.readouterr().out
    assert rc == 127
    assert detected_language is None
    assert "Unable to run whisper CLI: command not found in PATH: whisper" in captured
    assert "make sync-transcription-cpu" in captured


def test_runner_project_dir_returns_fallback_on_resolution_error(monkeypatch):
    tr = _load_transcription_script_module()

    class BrokenPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise RuntimeError("path resolution failed")

    monkeypatch.setattr(tr, "Path", BrokenPath)

    assert tr._runner_project_dir() == "<runner-dir>"


def test_dependency_resolution_hint_prints_missing_python_module(monkeypatch, capsys):
    tr = _load_transcription_script_module()
    monkeypatch.setattr(tr, "_runner_project_dir", lambda: "/opt/esup-runner/runner")

    tr._print_transcription_dependency_resolution_hint(
        use_gpu=True,
        missing_python_module="torch",
    )

    captured = capsys.readouterr().out
    assert "- Missing Python module: torch" in captured
    assert "make sync-transcription-gpu" in captured


def test_plan_audio_chunks_splits_long_audio():
    tr = _load_transcription_script_module()

    chunks = tr._plan_audio_chunks(
        total_duration_sec=2100.0,
        chunk_duration_sec=600,
        chunk_threshold_sec=1200,
        chunk_overlap_sec=0,
    )

    assert chunks == [
        (0.0, 600.0),
        (600.0, 600.0),
        (1200.0, 600.0),
        (1800.0, 300.0),
    ]


def test_plan_audio_chunks_uses_overlap_stride():
    tr = _load_transcription_script_module()

    chunks = tr._plan_audio_chunks(
        total_duration_sec=605.0,
        chunk_duration_sec=300,
        chunk_threshold_sec=120,
        chunk_overlap_sec=3,
    )

    assert chunks == [
        (0.0, 300.0),
        (297.0, 300.0),
        (594.0, 11.0),
    ]


def test_resolve_chunk_threshold_seconds_uses_cpu_default():
    tr = _load_transcription_script_module()

    threshold = tr._resolve_chunk_threshold_seconds(configured_value=None, use_gpu=False)

    assert threshold == 800


def test_resolve_chunk_threshold_seconds_uses_gpu_default():
    tr = _load_transcription_script_module()

    threshold = tr._resolve_chunk_threshold_seconds(configured_value=None, use_gpu=True)

    assert threshold == 1800


def test_resolve_chunk_threshold_seconds_keeps_explicit_override():
    tr = _load_transcription_script_module()

    threshold = tr._resolve_chunk_threshold_seconds(configured_value="1200", use_gpu=False)

    assert threshold == 1200


def test_parse_args_uses_internal_chunk_defaults(monkeypatch):
    tr = _load_transcription_script_module()

    monkeypatch.setenv("WHISPER_CHUNK_DURATION_SECONDS", "999")
    monkeypatch.setenv("WHISPER_CHUNK_OVERLAP_SECONDS", "9")

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.chunk_duration_seconds == str(tr._DEFAULT_CHUNK_DURATION_SECONDS)
    assert args.chunk_overlap_seconds == str(tr._DEFAULT_CHUNK_OVERLAP_SECONDS)


def test_parse_args_uses_default_huggingface_models_dir(monkeypatch):
    tr = _load_transcription_script_module()

    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.delenv("CACHE_DIR", raising=False)

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.huggingface_models_dir == tr._DEFAULT_HUGGINGFACE_MODELS_DIR


def test_parse_args_huggingface_default_follows_cache_dir(monkeypatch):
    tr = _load_transcription_script_module()

    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.setenv("CACHE_DIR", "/tmp/esup-cache")

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.huggingface_models_dir == "/tmp/esup-cache/huggingface"


def test_load_whisper_model_uses_configured_download_root(monkeypatch, tmp_path):
    tr = _load_transcription_script_module()

    captured = {}

    def fake_load_model(model_name, device=None, download_root=None):
        captured["model_name"] = model_name
        captured["device"] = device
        captured["download_root"] = download_root
        return object()

    fake_whisper = types.SimpleNamespace(load_model=fake_load_model)
    monkeypatch.setitem(sys.modules, "whisper", fake_whisper)

    target_dir = tmp_path / "whisper-cache"
    loaded = tr._load_whisper_model("small", "cpu", whisper_models_dir=str(target_dir))

    assert loaded is not None
    assert captured["model_name"] == "small"
    assert captured["device"] == "cpu"
    assert captured["download_root"] == str(target_dir)
    assert target_dir.is_dir()


def test_prepare_huggingface_models_dir_creates_directory(tmp_path):
    tr = _load_transcription_script_module()

    cache_dir = tmp_path / "hf-cache"

    resolved = tr._prepare_huggingface_models_dir(str(cache_dir), debug=False)

    assert resolved == str(cache_dir)
    assert cache_dir.is_dir()


def test_resolve_translation_model_name_uses_cpu_profile():
    tr = _load_transcription_script_module()

    model_name = tr._resolve_translation_model_name("fr", "en", use_gpu=False)

    assert model_name == "Helsinki-NLP/opus-mt-fr-en"


def test_resolve_translation_model_name_uses_gpu_profile():
    tr = _load_transcription_script_module()

    model_name = tr._resolve_translation_model_name("fr", "en", use_gpu=True)

    assert model_name == "Helsinki-NLP/opus-mt-tc-big-fr-en"


def test_load_translation_runtime_passes_cache_dir(monkeypatch, tmp_path):
    tr = _load_transcription_script_module()

    captured = {}

    class FakeTorch:
        @staticmethod
        def inference_mode():
            class _Ctx:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _Ctx()

    class FakeTokenizerCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["tokenizer_model_name"] = model_name
            captured["tokenizer_cache_dir"] = kwargs.get("cache_dir")
            return object()

    class FakeModel:
        device = "cpu"

        def to(self, _device):
            return self

        def eval(self):
            return None

    class FakeModelCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["model_model_name"] = model_name
            captured["model_cache_dir"] = kwargs.get("cache_dir")
            return FakeModel()

    monkeypatch.setattr(
        tr,
        "_import_translation_modules",
        lambda: (FakeTorch(), FakeModelCls, FakeTokenizerCls),
    )

    cache_dir = tmp_path / "hf-cache"
    rc, _torch, runtime, model_name = tr._load_translation_runtime(
        source_language="fr",
        target_language="en",
        use_gpu=False,
        huggingface_models_dir=str(cache_dir),
        debug=False,
    )

    assert rc == 0
    assert runtime is not None
    assert model_name == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["tokenizer_cache_dir"] == str(cache_dir)
    assert captured["model_cache_dir"] == str(cache_dir)


def test_load_translation_model_objects_passes_hf_token(monkeypatch):
    tr = _load_transcription_script_module()

    captured = {}

    class FakeTokenizerCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["tokenizer_model_name"] = model_name
            captured["tokenizer_kwargs"] = kwargs
            return object()

    class FakeModelCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["model_model_name"] = model_name
            captured["model_kwargs"] = kwargs
            return object()

    monkeypatch.setenv("HF_TOKEN", "hf_test_token")

    _tokenizer, _model = tr._load_translation_model_objects(
        FakeTokenizerCls,
        FakeModelCls,
        "Helsinki-NLP/opus-mt-fr-en",
        "/tmp/hf-cache",
    )

    assert captured["tokenizer_model_name"] == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["model_model_name"] == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["tokenizer_kwargs"]["cache_dir"] == "/tmp/hf-cache"
    assert captured["model_kwargs"]["cache_dir"] == "/tmp/hf-cache"
    assert captured["tokenizer_kwargs"]["token"] == "hf_test_token"
    assert captured["model_kwargs"]["token"] == "hf_test_token"


def test_run_translation_batch_sets_max_length_none_to_avoid_generation_warning():
    tr = _load_transcription_script_module()

    class FakeTensor:
        def to(self, _device):
            return self

    class FakeTokenizer:
        def __call__(self, texts, return_tensors, padding, truncation, max_length):
            assert texts == ["Bonjour."]
            assert return_tensors == "pt"
            assert padding is True
            assert truncation is True
            assert max_length == 512
            return {"input_ids": FakeTensor(), "attention_mask": FakeTensor()}

        def batch_decode(self, generated, skip_special_tokens):
            assert generated == ["GEN"]
            assert skip_special_tokens is True
            return ["Hello."]

    class FakeModel:
        device = "cpu"

        def __init__(self):
            self.kwargs = None

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return ["GEN"]

    class FakeTorch:
        @staticmethod
        def inference_mode():
            class _Ctx:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_model = FakeModel()
    translated = tr._run_translation_batch(
        ["Bonjour."],
        torch=FakeTorch(),
        tokenizer=FakeTokenizer(),
        model=fake_model,
    )

    assert translated == ["Hello."]
    assert fake_model.kwargs is not None
    assert fake_model.kwargs["max_length"] is None
    assert fake_model.kwargs["max_new_tokens"] == 256
    assert fake_model.kwargs["num_beams"] == 4


def test_combine_chunk_results_offsets_segments_and_words():
    tr = _load_transcription_script_module()

    merged = tr._combine_chunk_results(
        [
            (
                0.0,
                {
                    "text": "bonjour",
                    "language": "fr",
                    "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "bonjour"}],
                },
            ),
            (
                600.0,
                {
                    "text": "tout le monde",
                    "segments": [
                        {
                            "id": 0,
                            "start": 0.5,
                            "end": 2.0,
                            "text": "tout le monde",
                            "words": [{"word": "tout", "start": 0.5, "end": 0.8}],
                        }
                    ],
                },
            ),
        ]
    )

    assert merged["language"] == "fr"
    assert merged["text"] == "bonjour tout le monde"
    assert len(merged["segments"]) == 2
    assert merged["segments"][1]["id"] == 1
    assert merged["segments"][1]["start"] == 600.5
    assert merged["segments"][1]["end"] == 602.0
    assert merged["segments"][1]["words"][0]["start"] == 600.5


def test_combine_chunk_results_splits_overlap_without_duplicate_cue():
    tr = _load_transcription_script_module()

    chunk_plan = [(0.0, 300.0), (297.0, 300.0)]
    keep_windows = [
        tr._compute_chunk_keep_window(chunk_plan, 0),
        tr._compute_chunk_keep_window(chunk_plan, 1),
    ]
    merged = tr._combine_chunk_results(
        [
            (
                0.0,
                {
                    "text": "bonjour",
                    "segments": [{"id": 0, "start": 296.0, "end": 300.0, "text": "bonjour"}],
                },
            ),
            (
                297.0,
                {
                    "text": "bonjour",
                    "segments": [{"id": 0, "start": 0.0, "end": 3.0, "text": "bonjour"}],
                },
            ),
        ],
        keep_windows=keep_windows,
    )

    assert len(merged["segments"]) == 1
    assert merged["segments"][0]["text"] == "bonjour"
    assert merged["segments"][0]["start"] == 296.0
    assert merged["segments"][0]["end"] == 300.0


def test_validate_vtt_coverage_rejects_truncated_output(tmp_path):
    tr = _load_transcription_script_module()

    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:15:02.000\n" "Texte\n\n",
        encoding="utf-8",
    )

    rc = tr._validate_vtt_coverage(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 7


def test_validate_vtt_coverage_accepts_small_trailing_gap(tmp_path):
    tr = _load_transcription_script_module()

    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:32:30.000\n" "Texte\n\n",
        encoding="utf-8",
    )

    rc = tr._validate_vtt_coverage(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 0


def test_validate_vtt_coverage_accepts_empty_vtt_for_no_speech_audio(tmp_path):
    tr = _load_transcription_script_module()

    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text("WEBVTT\n\n", encoding="utf-8")

    rc = tr._validate_vtt_coverage(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 0


def test_parse_vtt_timestamp_accepts_minutes_seconds_format():
    tr = _load_transcription_script_module()

    parsed = tr._parse_vtt_timestamp("19:55.154")

    assert parsed == 1195.154


def test_parse_vtt_timestamp_accepts_comma_decimal_marker():
    tr = _load_transcription_script_module()

    parsed = tr._parse_vtt_timestamp("01:02:03,456")

    assert parsed == 3723.456


def test_hf_hub_warning_filter_drops_unauthenticated_hub_warning():
    tr = _load_transcription_script_module()

    matching = types.SimpleNamespace(
        getMessage=lambda: "You are sending unauthenticated requests to the HF Hub."
    )
    other = types.SimpleNamespace(getMessage=lambda: "another warning")

    assert tr._HF_HUB_WARNING_FILTER.filter(matching) is False
    assert tr._HF_HUB_WARNING_FILTER.filter(other) is True


def test_hf_hub_warning_filter_returns_true_when_record_message_fails():
    tr = _load_transcription_script_module()

    class _BrokenRecord:
        @staticmethod
        def getMessage():
            raise RuntimeError("boom")

    assert tr._HF_HUB_WARNING_FILTER.filter(_BrokenRecord()) is True


def test_apply_runtime_cuda_environment_prefers_explicit_cuda_visible_devices_env(monkeypatch):
    tr = _load_transcription_script_module()

    monkeypatch.setenv("GPU_CUDA_VISIBLE_DEVICES", "2,3")
    monkeypatch.setenv("GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "9")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)

    tr._apply_runtime_cuda_environment(gpu_device=0)

    assert os.getenv("CUDA_VISIBLE_DEVICES") == "2,3"
    assert os.getenv("CUDA_DEVICE_ORDER") == "PCI_BUS_ID"


def test_apply_runtime_cuda_environment_falls_back_to_gpu_device_when_env_missing(monkeypatch):
    tr = _load_transcription_script_module()

    monkeypatch.delenv("GPU_CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("GPU_CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)

    tr._apply_runtime_cuda_environment(gpu_device=7)

    assert os.getenv("CUDA_VISIBLE_DEVICES") == "7"
    assert os.getenv("CUDA_DEVICE_ORDER") is None


def test_build_transcribe_kwargs_disables_previous_text_conditioning_for_chunked_runs():
    tr = _load_transcription_script_module()

    kwargs = tr._build_transcribe_kwargs("fr", vad_filter=True, device="cpu", chunked=True)

    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["temperature"] == 0.0
    assert kwargs["word_timestamps"] is True
    assert kwargs["hallucination_silence_threshold"] == 2.0


def test_build_transcribe_kwargs_keeps_previous_text_conditioning_for_single_pass_runs():
    tr = _load_transcription_script_module()

    kwargs = tr._build_transcribe_kwargs("fr", vad_filter=True, device="cpu", chunked=False)

    assert kwargs["condition_on_previous_text"] is True
    assert kwargs["word_timestamps"] is True


def test_filter_result_segments_drops_punctuation_only_filler():
    tr = _load_transcription_script_module()

    result = {
        "text": "... Bonjour ...",
        "segments": [
            {"id": 0, "text": "...", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Bonjour", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = tr._filter_result_segments(result)

    assert filtered["text"] == "Bonjour"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Bonjour"


def test_filter_result_segments_drops_silence_hallucination_segment():
    tr = _load_transcription_script_module()

    result = {
        "text": "A Blood說, bon terrain sérieux. Salut",
        "segments": [
            {
                "id": 0,
                "text": "A Blood說, bon terrain sérieux.",
                "start": 0.0,
                "end": 2.0,
                "no_speech_prob": 0.91,
                "avg_logprob": -1.2,
                "compression_ratio": 3.5,
            },
            {
                "id": 1,
                "text": "Salut",
                "start": 2.0,
                "end": 3.0,
                "no_speech_prob": 0.05,
                "avg_logprob": -0.2,
                "compression_ratio": 1.4,
            },
        ],
    }

    filtered = tr._filter_result_segments(result)

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_unexpected_script_for_french():
    tr = _load_transcription_script_module()

    result = {
        "text": "Sneкая Bonjour",
        "segments": [
            {"id": 0, "text": "Sneкая", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Bonjour", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = tr._filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Bonjour"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Bonjour"


def test_filter_result_segments_drops_subtitle_credit_hallucination():
    tr = _load_transcription_script_module()

    result = {
        "text": "Sous-titrage ST' 501 Salut",
        "segments": [
            {"id": 0, "text": "Sous-titrage ST' 501", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Salut", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = tr._filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_subtitle_credit_without_digits():
    tr = _load_transcription_script_module()

    result = {
        "text": "Sous-titrage Société Radio-Canada Salut",
        "segments": [
            {"id": 0, "text": "Sous-titrage Société Radio-Canada", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Salut", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = tr._filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_numeric_repetition_loop():
    tr = _load_transcription_script_module()

    result = {
        "text": "On voit les yeux 1,2,3,1,2,3,1,2,3,1,2,3,1,2,3,1,2,3",
        "segments": [
            {
                "id": 0,
                "text": "On voit les yeux 1,2,3,1,2,3,1,2,3,1,2,3,1,2,3,1,2,3",
                "start": 0.0,
                "end": 5.0,
            },
            {"id": 1, "text": "Salut", "start": 5.0, "end": 6.0},
        ],
    }

    filtered = tr._filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_build_initial_prompt_from_text_keeps_tail():
    tr = _load_transcription_script_module()

    prompt = tr._build_initial_prompt_from_text("un deux trois quatre", max_chars=6)

    assert prompt == "quatre"


def test_postprocess_vtt_content_rewraps_cue_text_after_elision_fix():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "si on s\n"
        "'est déjà vus. Aujourd'hui on parle.\n"
    )

    processed = tr._postprocess_vtt_content(
        content,
        max_line_width=22,
        max_line_count=2,
    )

    assert "s'est" in processed
    assert "s\n'" not in processed
    cue_lines = processed.strip().split("\n\n")[1].splitlines()[1:]
    assert len(cue_lines) <= 2


def test_postprocess_vtt_content_repairs_french_apostrophe_split_across_cues():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "00:10.420 --> 00:14.480\n"
        "je suis ravie de vous retrouver si on s\n\n"
        "00:14.480 --> 00:19.580\n"
        "'est déjà vus. Enchantée pour les autres.\n"
    )

    processed = tr._postprocess_vtt_content(
        content,
        max_line_width=40,
        max_line_count=2,
    )

    assert "s'est" in processed
    assert "\n'est déjà" not in processed


def test_postprocess_vtt_content_repairs_english_contraction_split_across_cues():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "we\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "'re ready to go.\n"
    )

    processed = tr._postprocess_vtt_content(
        content,
        max_line_width=40,
        max_line_count=2,
    )

    assert "we're" in processed
    assert "\n're ready" not in processed


def test_postprocess_vtt_content_drops_duplicate_french_apostrophe_overlap_across_cues():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "01:04:19.900 --> 01:04:22.500\n"
        "veut dire qu'au sein de l'institution,\n\n"
        "01:04:22.500 --> 01:04:26.060\n"
        "'institution, on propose de ne pas réinventer.\n"
    )

    processed = tr._postprocess_vtt_content(
        content,
        max_line_width=48,
        max_line_count=2,
    )

    assert "l'institution" in processed
    assert "'institution, on propose" not in processed


def test_postprocess_vtt_content_drops_duplicate_english_contraction_overlap_across_cues():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "and we're\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "'re still checking the last details.\n"
    )

    processed = tr._postprocess_vtt_content(
        content,
        max_line_width=48,
        max_line_count=2,
    )

    assert "and we're" in processed
    assert "'re still checking" not in processed


def test_translate_vtt_content_preserves_timestamps_and_translates_cues():
    tr = _load_transcription_script_module()

    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Bonjour tout le monde.\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "N'hésitez pas.\n"
    )

    translated = tr._translate_vtt_content(
        content,
        translate_batch=lambda batch: [
            "Hello everyone." if "Bonjour" in text else "Don't hesitate." for text in batch
        ],
        max_line_width=32,
        max_line_count=2,
        batch_size=1,
    )

    assert "00:00:00.000 --> 00:00:02.000" in translated
    assert "00:00:02.000 --> 00:00:04.000" in translated
    assert "Hello everyone." in translated
    assert "Don't hesitate." in translated


def test_translate_vtt_file_rewrites_final_vtt_and_keeps_source_sidecar(monkeypatch, tmp_path):
    tr = _load_transcription_script_module()

    vtt_path = tmp_path / "audio_192k_test.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Bonjour.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        tr,
        "_load_translation_runtime",
        lambda **kwargs: (
            0,
            object(),
            ("fake-tokenizer", "fake-model"),
            "Helsinki-NLP/opus-mt-fr-en",
        ),
    )

    def fake_run_translation_batch(texts, *, torch, tokenizer, model):
        assert tokenizer == "fake-tokenizer"
        assert model == "fake-model"
        return ["Hello." for _text in texts]

    monkeypatch.setattr(tr, "_run_translation_batch", fake_run_translation_batch)

    rc, translation_metadata = tr._translate_vtt_file(
        vtt_path,
        source_language="fr",
        target_language="en",
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=32,
        max_line_count=2,
        debug=False,
    )

    source_sidecar = tmp_path / "audio_192k_test.source-fr.webvtt.txt"
    assert rc == 0
    assert translation_metadata["applied"] is True
    assert translation_metadata["model"] == "Helsinki-NLP/opus-mt-fr-en"
    assert translation_metadata["hardware_profile"] == "cpu"
    assert source_sidecar.exists()
    assert "Bonjour." in source_sidecar.read_text(encoding="utf-8")
    assert "Hello." in vtt_path.read_text(encoding="utf-8")


def test_maybe_translate_final_vtt_skips_when_requested_language_matches_detected(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Hello.\n",
        encoding="utf-8",
    )

    rc, translation_metadata, final_language = tr._maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language="en",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    assert rc == 0
    assert final_language == "en"
    assert translation_metadata["applied"] is False
    assert not (work_dir / "audio_192k_test.source-en.webvtt.txt").exists()


def test_maybe_translate_final_vtt_accepts_empty_vtt_for_non_verbal_audio(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    rc, translation_metadata, final_language = tr._maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language="en",
        detected_language=None,
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    assert rc == 0
    assert final_language == "en"
    assert translation_metadata["applied"] is False
    assert translation_metadata["backend"] == "none"
    assert translation_metadata["note"] == "no_speech_or_non_verbal_audio"
    assert not (work_dir / "audio_192k_test.source-en.webvtt.txt").exists()


def test_maybe_translate_final_vtt_uses_whisper_legacy_fallback_for_unsupported_pair(
    monkeypatch, tmp_path
):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Bonjour.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        tr,
        "_run_legacy_whisper_translation_fallback",
        lambda *args, **kwargs: (
            0,
            {
                "applied": True,
                "backend": "whisper_legacy_fallback",
                "source_language": "fr",
                "target_language": "de",
                "model": "large-v3",
                "hardware_profile": "gpu",
            },
            "de",
        ),
    )

    rc, translation_metadata, final_language = tr._maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language="de",
        detected_language="fr",
        whisper_fallback_options={
            "model": "turbo",
            "use_gpu": True,
            "gpu_device": 0,
            "vad_filter": True,
            "timeout_sec": 60,
            "chunk_duration_sec": 300,
            "chunk_overlap_sec": 3,
            "chunk_threshold_sec": 1800,
            "vtt_highlight_words": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        use_gpu=True,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    assert rc == 0
    assert final_language == "de"
    assert translation_metadata["applied"] is True
    assert translation_metadata["backend"] == "whisper_legacy_fallback"
    assert translation_metadata["model"] == "large-v3"


def test_build_transcription_runtime_metadata_includes_translation_model():
    tr = _load_transcription_script_module()

    metadata = tr._build_transcription_runtime_metadata(
        requested_language="en",
        detected_language="fr",
        final_language="en",
        whisper_model="turbo",
        use_gpu=True,
        translation={
            "applied": True,
            "backend": "local_translation",
            "source_language": "fr",
            "target_language": "en",
            "model": "Helsinki-NLP/opus-mt-tc-big-fr-en",
            "hardware_profile": "gpu",
        },
    )

    assert metadata["transcription"]["whisper_model"] == "turbo"
    assert metadata["transcription"]["detected_source_language"] == "fr"
    assert metadata["transcription"]["final_subtitle_language"] == "en"
    assert metadata["transcription"]["translation"]["backend"] == "local_translation"
    assert metadata["transcription"]["translation"]["model"] == "Helsinki-NLP/opus-mt-tc-big-fr-en"


def test_write_info_video_metadata_merges_runtime_details(tmp_path):
    tr = _load_transcription_script_module()

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)

    tr._write_info_video_metadata(work_dir, {"video_id": "abc123"}, debug=False)
    tr._write_info_video_metadata(
        work_dir,
        {
            "transcription": {
                "translation": {
                    "applied": True,
                    "backend": "local_translation",
                    "model": "Helsinki-NLP/opus-mt-fr-en",
                }
            }
        },
        debug=False,
    )

    info_content = (work_dir / "info_video.json").read_text(encoding="utf-8")
    assert '"video_id": "abc123"' in info_content
    assert '"model": "Helsinki-NLP/opus-mt-fr-en"' in info_content
