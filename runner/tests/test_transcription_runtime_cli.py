"""Tests for the transcription command line and runtime."""

import builtins
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from transcription_test_helpers import load_transcription_core_module as _load_core_module


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


def test_runtime_cli_loads_top_level_language_utils(monkeypatch):
    """Validate the direct-import branch used by script execution."""
    runtime_cli = _load_core_module("runtime_cli_utils")
    language_utils = types.ModuleType("language_utils")
    monkeypatch.setitem(sys.modules, "language_utils", language_utils)

    assert runtime_cli._load_language_utils_module() is language_utils


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


def test_runtime_cli_map_model_name_and_run_ffmpeg_to_mp3_branches(tmp_path):
    """Validate Runtime cli map model name and run ffmpeg to mp3 branches."""
    runtime_cli = _load_core_module("runtime_cli_utils")

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
    runtime_cli = _load_core_module("runtime_cli_utils")

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
    runtime_cli = _load_core_module("runtime_cli_utils")

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
