"""Validates Whisper model mapping and ffmpeg audio conversion with timeout handling."""

import importlib
import subprocess
import types
from pathlib import Path


def _load_transcription_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.transcription.core.{module_name}")
    return importlib.reload(module)


def test_runtime_cli_map_model_name_and_run_ffmpeg_to_mp3_branches(tmp_path):
    """Validate Runtime cli map model name and run ffmpeg to mp3 branches."""
    runtime_cli = _load_transcription_core_module("runtime_cli_utils")

    assert runtime_cli.map_model_name("large") == "large-v3"
    assert runtime_cli.map_model_name("turbo", context="cli") == "large-v3"
    assert runtime_cli.map_model_name("small", context="python") == "small"

    input_path = tmp_path / "input.mp4"
    input_path.write_bytes(b"video")
    mp3_path = tmp_path / "audio" / "out.mp3"

    recorded_cmds = []

    def _run_ok(cmd, **_kwargs):
        recorded_cmds.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    rc = runtime_cli.run_ffmpeg_to_mp3(
        input_path=input_path,
        mp3_path=mp3_path,
        sample_rate=16000,
        downmix_mono=True,
        audio_index=1,
        timeout_sec=30,
        debug=True,
        subprocess_run=_run_ok,
    )
    assert rc == 0
    assert mp3_path.parent.exists()
    assert "-ac" in recorded_cmds[-1]
    assert "-ar" in recorded_cmds[-1]
    assert "0:a:1" in recorded_cmds[-1]

    recorded_cmds.clear()
    rc = runtime_cli.run_ffmpeg_to_mp3(
        input_path=input_path,
        mp3_path=mp3_path,
        sample_rate=0,
        downmix_mono=False,
        audio_index=0,
        timeout_sec=30,
        debug=False,
        subprocess_run=_run_ok,
    )
    assert rc == 0
    assert "-ac" not in recorded_cmds[-1]
    assert "-ar" not in recorded_cmds[-1]

    rc = runtime_cli.run_ffmpeg_to_mp3(
        input_path=input_path,
        mp3_path=mp3_path,
        sample_rate=16000,
        downmix_mono=True,
        audio_index=0,
        timeout_sec=2,
        debug=False,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ffmpeg", timeout=2)
        ),
    )
    assert rc == 124


def test_runtime_cli_normalize_mp3_with_ffmpeg_normalize_branches(tmp_path):
    """Validate Runtime cli normalize mp3 with ffmpeg normalize branches."""
    runtime_cli = _load_transcription_core_module("runtime_cli_utils")

    mp3_path = tmp_path / "audio.mp3"
    mp3_path.write_bytes(b"audio")

    def _run_success(cmd, **_kwargs):
        out_path = Path(cmd[cmd.index("-o") + 1])
        out_path.write_bytes(b"normalized")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    normalized = runtime_cli.normalize_mp3_with_ffmpeg_normalize(
        mp3_path=mp3_path,
        target_level="-16",
        timeout_sec=30,
        debug=True,
        subprocess_run=_run_success,
    )
    assert normalized.name == "audio_norm.mp3"
    assert normalized.exists()

    unchanged = runtime_cli.normalize_mp3_with_ffmpeg_normalize(
        mp3_path=mp3_path,
        target_level="-16",
        timeout_sec=30,
        debug=True,
        subprocess_run=lambda *_a, **_k: types.SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="normalize failed",
        ),
    )
    assert unchanged == mp3_path.resolve()

    unchanged = runtime_cli.normalize_mp3_with_ffmpeg_normalize(
        mp3_path=mp3_path,
        target_level="-16",
        timeout_sec=30,
        debug=True,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert unchanged == mp3_path.resolve()

    unchanged = runtime_cli.normalize_mp3_with_ffmpeg_normalize(
        mp3_path=mp3_path,
        target_level="-16",
        timeout_sec=1,
        debug=False,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="ffmpeg-normalize", timeout=1)
        ),
    )
    assert unchanged == mp3_path.resolve()

    unchanged = runtime_cli.normalize_mp3_with_ffmpeg_normalize(
        mp3_path=mp3_path,
        target_level="-16",
        timeout_sec=30,
        debug=True,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert unchanged == mp3_path.resolve()


def test_runtime_cli_run_whisper_cli_debug_and_timeout_branches(tmp_path):
    """Validate Runtime cli run whisper cli debug and timeout branches."""
    runtime_cli = _load_transcription_core_module("runtime_cli_utils")

    recorded = {"cmd": None, "env": None, "detect_calls": 0}

    def _run_ok(cmd, **kwargs):
        recorded["cmd"] = cmd
        recorded["env"] = kwargs.get("env")
        return types.SimpleNamespace(returncode=0, stdout="Detected language: French", stderr="")

    rc, detected = runtime_cli.run_whisper_cli(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=30,
        debug=True,
        map_model_name_fn=runtime_cli.map_model_name,
        build_whisper_command_fn=lambda **_k: ["whisper", "audio.mp3"],
        prepare_whisper_env_fn=lambda *_a: (["--device", "cpu"], {"A": "B"}),
        detect_language_from_stdout_fn=lambda stdout, language: (
            recorded.__setitem__("detect_calls", recorded["detect_calls"] + 1) or "fr"
        ),
        print_transcription_dependency_resolution_hint_fn=lambda **_k: None,
        subprocess_run=_run_ok,
    )
    assert rc == 0
    assert detected == "fr"
    assert recorded["cmd"][-2:] == ["--device", "cpu"]
    assert recorded["env"] == {"A": "B"}
    assert recorded["detect_calls"] == 1

    rc, detected = runtime_cli.run_whisper_cli(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=2,
        debug=False,
        map_model_name_fn=runtime_cli.map_model_name,
        build_whisper_command_fn=lambda **_k: ["whisper", "audio.mp3"],
        prepare_whisper_env_fn=lambda *_a: ([], {}),
        detect_language_from_stdout_fn=lambda *_a: None,
        print_transcription_dependency_resolution_hint_fn=lambda **_k: None,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd="whisper", timeout=2)
        ),
    )
    assert rc == 124
    assert detected is None

    rc, detected = runtime_cli.run_whisper_cli(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=False,
        timeout_sec=2,
        debug=False,
        map_model_name_fn=runtime_cli.map_model_name,
        build_whisper_command_fn=lambda **_k: ["whisper", "audio.mp3"],
        prepare_whisper_env_fn=lambda *_a: ([], {}),
        detect_language_from_stdout_fn=lambda *_a: None,
        print_transcription_dependency_resolution_hint_fn=lambda **_k: None,
        subprocess_run=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("launch error")),
    )
    assert rc == 1
    assert detected is None


def test_whisper_python_runtime_utils_run_whisper_python_branches(monkeypatch, tmp_path):
    """Validate Whisper python runtime utils run whisper python branches."""
    whisper_runtime = _load_transcription_core_module("whisper_python_runtime_utils")

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
