"""Runtime helpers for whisper CLI/ffmpeg execution and model loading.

Wraps subprocess calls with consistent logging, timeout, and error handling.
Bridges CLI and Python execution modes so both share compatible behaviors.
Provides utility probes used to detect model and language capabilities.
"""

import importlib.util
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_WHISPER_HELP_CACHE: Optional[str] = None


def _load_language_utils_module():
    """Load `language_utils` in both package and file-spec execution modes."""
    try:
        import language_utils as module  # type: ignore

        return module
    except ModuleNotFoundError:
        module_path = Path(__file__).resolve().with_name("language_utils.py")
        spec = importlib.util.spec_from_file_location(
            "transcription_core_language_utils", module_path
        )
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def runner_project_dir() -> str:
    """Best-effort absolute path to the runner project root for operator hints."""
    try:
        current_file = Path(__file__).resolve()
        for parent in current_file.parents:
            if (parent / "pyproject.toml").exists() and (parent / "app").is_dir():
                return str(parent)
        return str(current_file.parents[5])
    except Exception:
        return "<runner-dir>"


def print_transcription_dependency_resolution_hint(
    *,
    use_gpu: bool,
    missing_python_module: Optional[str] = None,
    missing_cli_command: Optional[str] = None,
    runner_project_dir_fn: Callable[[], str] = runner_project_dir,
) -> None:
    """Print actionable remediation steps when Whisper runtime dependencies are missing."""
    mode = "GPU" if use_gpu else "CPU"
    sync_cmd = "make sync-transcription-gpu" if use_gpu else "make sync-transcription-cpu"
    runner_dir = runner_project_dir_fn()

    print("Transcription runtime dependencies are incomplete.")
    if missing_python_module:
        print(f"- Missing Python module: {missing_python_module}")
    if missing_cli_command:
        print(f"- Missing CLI command in PATH: {missing_cli_command}")

    print("Resolution:")
    print(f"  1) Install transcription dependencies ({mode} profile):")
    print(f"     cd {runner_dir} && {sync_cmd}")
    print("  2) Restart the runner service:")
    print("     systemctl --user restart esup-runner-runner")
    print("  3) Verify the runtime:")
    print(f'     cd {runner_dir} && uv run python -c "import torch, whisper"')
    print(f"     cd {runner_dir} && uv run which whisper")


def get_whisper_help_text(
    debug: bool = False,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> str:
    """Return cached `whisper --help` output for CLI feature detection."""
    global _WHISPER_HELP_CACHE
    if _WHISPER_HELP_CACHE is not None:
        return _WHISPER_HELP_CACHE
    try:
        proc = subprocess_run(["whisper", "--help"], capture_output=True, text=True, timeout=10)
        _WHISPER_HELP_CACHE = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        return _WHISPER_HELP_CACHE
    except Exception as exc:
        if debug:
            print(f"Failed to get whisper --help: {exc}")
        _WHISPER_HELP_CACHE = ""
        return _WHISPER_HELP_CACHE


def cli_supports_option(
    possible_flags: list[str],
    debug: bool = False,
    *,
    get_whisper_help_text_fn: Callable[[bool], str],
) -> Optional[str]:
    """Return the first supported CLI flag among the provided alternatives."""
    help_text = get_whisper_help_text_fn(debug)
    for flag in possible_flags:
        if flag in help_text:
            return flag
    return None


def map_model_name(logical: str, context: str = "python") -> str:
    """Map generic model aliases to openai-whisper names."""
    logical_lower = (logical or "").lower()
    if logical_lower == "large":
        return "large-v3"
    if logical_lower == "turbo" and context == "cli":
        # openai-whisper upstream does not provide a separate "turbo" checkpoint.
        # Fallback to a fast, accurate model available in the lib.
        return "large-v3"
    return logical_lower


def run_ffmpeg_to_mp3(
    input_path: Path,
    mp3_path: Path,
    sample_rate: int,
    downmix_mono: bool,
    audio_index: int,
    timeout_sec: int,
    debug: bool,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> int:
    """Extract audio to mono MP3 at desired sample rate using ffmpeg."""
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-map",
        f"0:a:{audio_index}",
    ]
    if downmix_mono:
        cmd += ["-ac", "1"]
    if sample_rate:
        cmd += ["-ar", str(int(sample_rate))]
    cmd += [
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(mp3_path),
    ]

    if debug:
        print("Executing:", " ".join(cmd), flush=True)

    try:
        proc = subprocess_run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        print(f"ffmpeg audio extraction timed out after {timeout_sec}s")
        return 124
    if debug:
        print(proc.stdout)
        print(proc.stderr)
    return int(proc.returncode)


def normalize_mp3_with_ffmpeg_normalize(
    mp3_path: Path,
    target_level: str,
    timeout_sec: int,
    debug: bool,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> Path:
    """Normalize the MP3 loudness with ffmpeg-normalize."""
    try:
        mp3_path = mp3_path.resolve()
        out_path = mp3_path.with_name(f"{mp3_path.stem}_norm{mp3_path.suffix}")
        cmd = [
            "ffmpeg-normalize",
            str(mp3_path),
            "--normalization-type",
            "ebu",
            "--target-level",
            str(target_level),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-f",
            "-o",
            str(out_path),
        ]
        if debug:
            print("Executing:", " ".join(shlex.quote(token) for token in cmd))
        proc = subprocess_run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if proc.returncode == 0 and out_path.exists():
            return out_path
        if debug:
            print(
                "ffmpeg-normalize failed or did not produce output; stderr:\n" + (proc.stderr or "")
            )
        return mp3_path
    except FileNotFoundError:
        if debug:
            print("ffmpeg-normalize not found; skipping normalization")
        return mp3_path
    except subprocess.TimeoutExpired:
        print("ffmpeg-normalize timed out; using original MP3")
        return mp3_path
    except Exception as exc:
        if debug:
            print(f"Normalization error: {exc}")
        return mp3_path


def build_whisper_command(
    audio_path: Path,
    out_dir: Path,
    model_name: str,
    whisper_models_dir: Optional[str],
    language: str,
    vad_filter: bool,
    debug: bool,
    *,
    cli_supports_option_fn: Callable[[list[str], bool], Optional[str]],
) -> list[str]:
    """Build the base Whisper CLI command for a transcription run."""
    cmd = [
        "whisper",
        str(audio_path),
        "--model",
        model_name,
        "--output_dir",
        str(out_dir),
        "--output_format",
        "vtt",
        "--verbose",
        "False",
    ]
    if language and language.lower() != "auto":
        cmd += ["--language", language]

    normalized_models_dir = str(whisper_models_dir or "").strip()
    if normalized_models_dir:
        model_dir_flag = cli_supports_option_fn(["--model_dir", "--model-dir"], debug)
        if model_dir_flag is not None:
            cmd += [model_dir_flag, normalized_models_dir]
        elif debug:
            print("whisper CLI does not support model_dir option; using default cache path")

    vad_flag = cli_supports_option_fn(["--vad_filter", "--vad-filter"], debug)
    if vad_flag is not None:
        cmd += [vad_flag, "true" if vad_filter else "false"]
    elif debug and vad_filter:
        print("whisper CLI does not support a VAD option; ignoring --vad-filter request")
    return cmd


def prepare_whisper_env(use_gpu: bool, gpu_device: int) -> tuple[list[str], Dict[str, str]]:
    """Prepare Whisper CLI device arguments and environment variables."""
    env = os.environ.copy()
    device_args: list[str] = []
    if use_gpu:
        device_args += ["--device", "cuda"]
        env_cuda = os.getenv("GPU_CUDA_VISIBLE_DEVICES", "").strip()
        if env_cuda:
            env["CUDA_VISIBLE_DEVICES"] = env_cuda
        elif gpu_device is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(int(gpu_device))
        cuda_order = os.getenv("GPU_CUDA_DEVICE_ORDER", "").strip()
        if cuda_order:
            env["CUDA_DEVICE_ORDER"] = cuda_order
    else:
        device_args += ["--device", "cpu", "--fp16", "False"]
    return device_args, env


def apply_runtime_cuda_environment(gpu_device: int) -> None:
    """Align in-process CUDA env with runner GPU settings before importing torch."""
    env_cuda = os.getenv("GPU_CUDA_VISIBLE_DEVICES", "").strip()
    if env_cuda:
        os.environ["CUDA_VISIBLE_DEVICES"] = env_cuda
    elif gpu_device is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(int(gpu_device))

    cuda_order = os.getenv("GPU_CUDA_DEVICE_ORDER", "").strip()
    if cuda_order:
        os.environ["CUDA_DEVICE_ORDER"] = cuda_order


def detect_language_from_stdout(
    stdout: str,
    language: str,
    *,
    map_language_name_to_code_fn: Callable[[str], Optional[str]],
) -> Optional[str]:
    """Extract detected language code from Whisper CLI stdout when auto mode is used."""
    if language and language.lower() != "auto":
        return None
    for line in (stdout or "").splitlines():
        if "Detected language:" in line:
            try:
                detected_name = line.split(":", 1)[1].strip()
                return map_language_name_to_code_fn(detected_name)
            except Exception:
                return None
    return None


def resolve_effective_use_gpu(
    requested_use_gpu: bool,
    gpu_device: int,
    debug: bool,
    *,
    apply_runtime_cuda_environment_fn: Callable[[int], None] = apply_runtime_cuda_environment,
) -> bool:
    """Return whether CUDA can actually be used for this run."""
    if not requested_use_gpu:
        return False

    try:
        apply_runtime_cuda_environment_fn(gpu_device)
        import torch  # type: ignore

        if torch.cuda.is_available():
            return True

        torch_version = str(getattr(torch, "__version__", "unknown"))
        torch_cuda_build = getattr(getattr(torch, "version", None), "cuda", None)
        cuda_visible = os.getenv("CUDA_VISIBLE_DEVICES", "").strip() or "<unset>"
        if torch_cuda_build is None:
            print(
                "CUDA requested but unavailable; falling back to CPU for transcription "
                f"(torch build is CPU-only: torch={torch_version}, "
                f"CUDA_VISIBLE_DEVICES={cuda_visible})"
            )
            return False

        device_count = "unknown"
        try:
            device_count = str(int(torch.cuda.device_count()))
        except Exception:
            pass
        print(
            "CUDA requested but unavailable; falling back to CPU for transcription "
            f"(torch={torch_version}, torch.version.cuda={torch_cuda_build}, "
            f"CUDA_VISIBLE_DEVICES={cuda_visible}, torch.cuda.device_count={device_count})"
        )
        return False
    except Exception as exc:
        if debug:
            print(f"Failed to probe CUDA availability ({exc}); falling back to CPU")
        else:
            print("Failed to probe CUDA availability; falling back to CPU")
        return False


def run_whisper_cli(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    debug: bool,
    *,
    map_model_name_fn: Callable[[str, str], str],
    build_whisper_command_fn: Callable[..., list[str]],
    prepare_whisper_env_fn: Callable[[bool, int], tuple[list[str], Dict[str, str]]],
    detect_language_from_stdout_fn: Callable[[str, str], Optional[str]],
    print_transcription_dependency_resolution_hint_fn: Callable[..., None],
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> tuple[int, Optional[str]]:
    """Run openai-whisper CLI to generate VTT subtitles from an audio file."""
    out_dir.mkdir(parents=True, exist_ok=True)

    model_name = map_model_name_fn(model, "cli")
    cmd = build_whisper_command_fn(
        audio_path=audio_path,
        out_dir=out_dir,
        model_name=model_name,
        whisper_models_dir=whisper_models_dir,
        language=language,
        vad_filter=vad_filter,
        debug=debug,
    )
    device_args, env = prepare_whisper_env_fn(use_gpu, gpu_device)
    cmd += device_args

    if debug:
        print("Executing:", " ".join(cmd), flush=True)

    try:
        proc = subprocess_run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
    except FileNotFoundError:
        print("Unable to run whisper CLI: command not found in PATH: whisper")
        print_transcription_dependency_resolution_hint_fn(
            use_gpu=use_gpu,
            missing_cli_command="whisper",
        )
        return 127, None
    except subprocess.TimeoutExpired:
        print(f"whisper CLI timed out after {timeout_sec}s")
        return 124, None
    except Exception as exc:
        print(f"whisper CLI execution failed before start: {exc}")
        return 1, None
    if debug:
        print(proc.stdout)
        print(proc.stderr)

    detected_code = detect_language_from_stdout_fn(proc.stdout or "", language)
    return proc.returncode, detected_code


def run_whisper_cli_with_defaults(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    debug: bool,
    *,
    subprocess_run: Callable[..., Any] = subprocess.run,
) -> tuple[int, Optional[str]]:
    """Run whisper CLI with the module's default dependency wiring."""
    _language_utils = _load_language_utils_module()

    return run_whisper_cli(
        audio_path=audio_path,
        out_dir=out_dir,
        language=language,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        gpu_device=gpu_device,
        vad_filter=vad_filter,
        timeout_sec=timeout_sec,
        debug=debug,
        map_model_name_fn=map_model_name,
        build_whisper_command_fn=lambda **kwargs: build_whisper_command(
            cli_supports_option_fn=lambda flags, debug_enabled: cli_supports_option(
                flags,
                debug=debug_enabled,
                get_whisper_help_text_fn=lambda help_debug: get_whisper_help_text(
                    help_debug,
                    subprocess_run=subprocess_run,
                ),
            ),
            **kwargs,
        ),
        prepare_whisper_env_fn=prepare_whisper_env,
        detect_language_from_stdout_fn=lambda stdout, language_code: detect_language_from_stdout(
            stdout,
            language_code,
            map_language_name_to_code_fn=_language_utils.map_language_name_to_code,
        ),
        print_transcription_dependency_resolution_hint_fn=print_transcription_dependency_resolution_hint,
        subprocess_run=subprocess_run,
    )


def import_whisper_modules(
    use_gpu: bool = False,
) -> tuple[Optional[Any], Optional[Any], Optional[Callable[..., Any]]]:
    """Import Whisper Python API modules and return them when available."""
    try:
        import torch  # type: ignore
        import whisper  # type: ignore
        from whisper.utils import get_writer  # type: ignore

        return torch, whisper, get_writer
    except ModuleNotFoundError as exc:
        missing_name = getattr(exc, "name", None) or str(exc)
        print("Falling back to CLI: whisper Python API dependencies are missing.")
        print(f"- Missing Python module: {missing_name}")
        print("Attempting whisper CLI fallback...")
        return None, None, None
    except Exception as exc:
        print(f"Falling back to CLI: failed to import whisper API ({exc})")
        return None, None, None


def load_whisper_model(
    model_name: str,
    device: str,
    whisper_models_dir: Optional[str] = None,
) -> Optional[Any]:
    """Load a Whisper model on the requested device."""
    try:
        import whisper  # type: ignore

        normalized_models_dir = str(whisper_models_dir or "").strip()
        if normalized_models_dir:
            Path(normalized_models_dir).mkdir(parents=True, exist_ok=True)
            return whisper.load_model(
                model_name,
                device=device,
                download_root=normalized_models_dir,
            )

        return whisper.load_model(model_name, device=device)
    except Exception as exc:
        print(f"Failed to load whisper model '{model_name}': {exc}")
        return None
