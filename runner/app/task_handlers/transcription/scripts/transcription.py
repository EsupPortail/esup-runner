#!/usr/bin/env python3
"""
Standalone transcription script using openai-whisper CLI ("whisper").

This script receives a media or audio file, converts it to MP3 (mono, 16kHz)
with ffmpeg when needed, then runs the `whisper` command to produce subtitles
in VTT. If the input is already an MP3 file, the conversion step is skipped.

Usage example:
    python transcription.py \
        --base-dir /tmp/work --input-file input.mp4 --work-dir output \
        --language auto --model small \
        --format vtt --use-gpu false
"""

import argparse
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate subtitles using openai-whisper CLI")
    # Defaults from environment for standalone usage
    default_use_gpu = "true" if os.getenv("ENCODING_TYPE", "CPU").upper() == "GPU" else "false"
    default_gpu_device = os.getenv("GPU_HWACCEL_DEVICE", "0")
    parser.add_argument("--base-dir", required=True, help="Base directory containing input file")
    parser.add_argument(
        "--input-file", required=True, help="Input media filename relative to base-dir"
    )
    parser.add_argument(
        "--work-dir", required=True, help="Work/output subdirectory relative to base-dir"
    )
    parser.add_argument(
        "--language", default=os.getenv("WHISPER_LANGUAGE", "auto"), help="Language code or 'auto'"
    )
    parser.add_argument(
        "--format", default="vtt", choices=["vtt"], help="Output subtitle format (forced to vtt)"
    )
    parser.add_argument(
        "--model",
        default=os.getenv("WHISPER_MODEL", "small"),
        help="Whisper model name (tiny|base|small|medium|large[/-v3]|turbo)",
    )
    # legacy options removed: model-path, models-dir
    parser.add_argument(
        "--use-gpu",
        default=default_use_gpu,
        choices=["true", "false"],
        help="Use GPU acceleration for whisper (defaults from ENCODING_TYPE)",
    )
    parser.add_argument(
        "--gpu-device",
        default=default_gpu_device,
        help="GPU device index (defaults from GPU_HWACCEL_DEVICE)",
    )
    parser.add_argument("--debug", default="False", help="Debug mode")
    parser.add_argument(
        "--downmix-mono",
        default="true",
        choices=["true", "false"],
        help="Downmix audio to mono (faster)",
    )
    parser.add_argument("--sample-rate", default="16000", help="Resample audio to this Hz (faster)")
    parser.add_argument(
        "--vad-filter",
        default=os.getenv("WHISPER_VAD_FILTER", "true"),
        choices=["true", "false"],
        help="Enable VAD pre-filter in whisper",
    )
    # legacy option removed: queue
    parser.add_argument(
        "--audio-stream-index",
        default="0",
        help="Select audio stream index (0-based) for ffmpeg extraction",
    )
    # legacy option removed: vad-threshold
    parser.add_argument(
        "--timeout-factor", default="8", help="Max runtime = duration * factor (seconds)"
    )
    parser.add_argument("--min-timeout", default="60", help="Minimal timeout in seconds")
    # Normalization options
    parser.add_argument(
        "--normalize",
        default="false",
        choices=["true", "false"],
        help="Normalize MP3 before transcription using ffmpeg-normalize",
    )
    parser.add_argument(
        "--normalize-target-level",
        default=os.getenv("TRANSCRIPTION_NORMALIZE_TARGET_LEVEL", "-23"),
        help="Target level (LUFS) for ffmpeg-normalize (e.g., -23)",
    )
    # VTT writer options
    parser.add_argument(
        "--vtt-highlight-words",
        default="false",
        choices=["true", "false"],
        help="Highlight words in VTT output",
    )
    parser.add_argument(
        "--vtt-max-line-count", default="2", help="Max number of lines per subtitle"
    )
    parser.add_argument("--vtt-max-line-width", default="40", help="Max characters per line")
    return parser.parse_args()


# Cache for CLI help to feature-detect options
_WHISPER_HELP_CACHE: Optional[str] = None


def _get_whisper_help_text(debug: bool = False) -> str:
    global _WHISPER_HELP_CACHE
    if _WHISPER_HELP_CACHE is not None:
        return _WHISPER_HELP_CACHE
    try:
        p = subprocess.run(["whisper", "--help"], capture_output=True, text=True, timeout=10)
        _WHISPER_HELP_CACHE = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
        return _WHISPER_HELP_CACHE
    except Exception as e:
        if debug:
            print(f"Failed to get whisper --help: {e}")
        _WHISPER_HELP_CACHE = ""
        return _WHISPER_HELP_CACHE


def _cli_supports_option(possible_flags: list[str], debug: bool = False) -> Optional[str]:
    help_text = _get_whisper_help_text(debug=debug)
    for flag in possible_flags:
        if flag in help_text:
            return flag
    return None


def map_model_name(logical: str, context: str = "python") -> str:
    """Map generic model aliases to openai-whisper names.

    Examples:
    - "large" -> "large-v3"
    - "turbo" -> "large-v3-turbo" (if available in installed whisper)
    Otherwise returns the input as-is.
    """
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
    # Encode to MP3
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
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        print(f"ffmpeg audio extraction timed out after {timeout_sec}s")
        return 124
    if debug:
        print(proc.stdout)
        print(proc.stderr)
    return proc.returncode


def normalize_mp3_with_ffmpeg_normalize(
    mp3_path: Path, target_level: str, timeout_sec: int, debug: bool
) -> Path:
    """Normalize the MP3 loudness using ffmpeg-normalize. Returns output path if success, else original path.

    Creates a new file with suffix _norm before extension in the same directory.
    """
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
            print("Executing:", " ".join(shlex.quote(p) for p in cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if proc.returncode == 0 and out_path.exists():
            return out_path
        else:
            if debug:
                print(
                    "ffmpeg-normalize failed or did not produce output; stderr:\n"
                    + (proc.stderr or "")
                )
            return mp3_path
    except FileNotFoundError:
        # ffmpeg-normalize not installed
        if debug:
            print("ffmpeg-normalize not found; skipping normalization")
        return mp3_path
    except subprocess.TimeoutExpired:
        print("ffmpeg-normalize timed out; using original MP3")
        return mp3_path
    except Exception as e:
        if debug:
            print(f"Normalization error: {e}")
        return mp3_path


def _map_language_name_to_code(name: str) -> Optional[str]:
    """Best-effort mapping from language names printed by whisper to ISO-639-1 codes."""
    if not name:
        return None
    n = name.strip().lower()
    common = {
        "english": "en",
        "french": "fr",
        "spanish": "es",
        "german": "de",
        "italian": "it",
        "portuguese": "pt",
        "chinese": "zh",
        "cantonese": "yue",
        "japanese": "ja",
        "korean": "ko",
        "russian": "ru",
        "arabic": "ar",
        "hindi": "hi",
        "dutch": "nl",
        "polish": "pl",
        "turkish": "tr",
    }
    return common.get(n)


def _build_whisper_command(
    audio_path: Path,
    out_dir: Path,
    model_name: str,
    language: str,
    vad_filter: bool,
    debug: bool,
) -> list[str]:
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
    vad_flag = _cli_supports_option(["--vad_filter", "--vad-filter"], debug=debug)
    if vad_flag is not None:
        cmd += [vad_flag, "true" if vad_filter else "false"]
    elif debug and vad_filter:
        print("whisper CLI does not support a VAD option; ignoring --vad-filter request")
    return cmd


def _prepare_whisper_env(use_gpu: bool, gpu_device: int) -> tuple[list[str], Dict[str, str]]:
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


def _detect_language_from_stdout(stdout: str, language: str) -> Optional[str]:
    if language and language.lower() != "auto":
        return None
    for line in (stdout or "").splitlines():
        if "Detected language:" in line:
            try:
                detected_name = line.split(":", 1)[1].strip()
                return _map_language_name_to_code(detected_name)
            except Exception:
                return None
    return None


def run_whisper_cli(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    debug: bool,
) -> tuple[int, Optional[str]]:
    """Run the openai-whisper CLI to generate VTT subtitles from an audio file (MP3 or others)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    model_name = map_model_name(model, "cli")
    cmd = _build_whisper_command(
        audio_path=audio_path,
        out_dir=out_dir,
        model_name=model_name,
        language=language,
        vad_filter=vad_filter,
        debug=debug,
    )
    device_args, env = _prepare_whisper_env(use_gpu=use_gpu, gpu_device=gpu_device)
    cmd += device_args

    if debug:
        print("Executing:", " ".join(cmd), flush=True)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
    except subprocess.TimeoutExpired:
        print(f"whisper CLI timed out after {timeout_sec}s")
        return 124, None
    if debug:
        print(proc.stdout)
        print(proc.stderr)

    detected_code = _detect_language_from_stdout(proc.stdout or "", language)
    return proc.returncode, detected_code


def _import_whisper_modules() -> tuple[Optional[Any], Optional[Any], Optional[Callable[..., Any]]]:
    try:
        import torch  # type: ignore
        import whisper  # type: ignore
        from whisper.utils import get_writer  # type: ignore

        return torch, whisper, get_writer
    except Exception as e:
        print(f"Falling back to CLI: failed to import whisper API ({e})")
        return None, None, None


def _load_whisper_model(model_name: str, device: str) -> Optional[Any]:
    try:
        import whisper  # type: ignore

        return whisper.load_model(model_name, device=device)
    except Exception as e:
        print(f"Failed to load whisper model '{model_name}': {e}")
        return None


def _build_transcribe_kwargs(language: str, vad_filter: bool, device: str) -> Dict[str, object]:
    kwargs: Dict[str, object] = {"fp16": device == "cuda"}
    if language and language.lower() != "auto":
        kwargs["language"] = language
    kwargs["vad_filter"] = bool(vad_filter)
    return kwargs


def _transcribe_audio(
    wmodel: Any, audio_path: Path, transcribe_kwargs: Dict[str, object]
) -> Optional[Dict[str, object]]:
    try:
        return wmodel.transcribe(str(audio_path), **transcribe_kwargs)  # type: ignore
    except TypeError:
        kwargs = dict(transcribe_kwargs)
        kwargs.pop("vad_filter", None)
        return wmodel.transcribe(str(audio_path), **kwargs)  # type: ignore
    except Exception as e:
        print(f"Whisper transcription failed: {e}")
        return None


def _extract_detected_language(result: Dict[str, object]) -> Optional[str]:
    try:
        return str(result.get("language")) if isinstance(result, dict) else None
    except Exception:
        return None


def _write_vtt_result(
    result: Dict[str, object],
    audio_path: Path,
    out_dir: Path,
    get_writer: Callable[..., Any],
    word_options: Dict[str, object],
    debug: bool,
) -> bool:
    try:
        writer = get_writer("vtt", str(out_dir))
        filename_stem = Path(audio_path).stem
        if debug:
            print(f"Writing VTT to {out_dir}/{filename_stem}.vtt with options {word_options}")
        writer(result, filename_stem, word_options)  # type: ignore[arg-type]
        return True
    except Exception as e:
        print(f"Failed to write VTT: {e}")
        return False


def run_whisper_python(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    vtt_highlight_words: bool,
    vtt_max_line_count: int,
    vtt_max_line_width: int,
    debug: bool,
) -> tuple[int, Optional[str]]:
    """Run Whisper via Python API and write VTT with custom writer options.

    Note: This path does not enforce a hard timeout like subprocess; rely on caller-level time budget if needed.
    """
    torch, whisper, get_writer = _import_whisper_modules()
    if not torch or not whisper or not get_writer:
        return 255, None

    if debug:
        print(f"Using whisper Python API version: {whisper.__version__}")

    out_dir.mkdir(parents=True, exist_ok=True)

    model_name = map_model_name(model, "python")
    device = "cuda" if use_gpu else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    if debug:
        print(f"Loading whisper model '{model_name}' on {device} (dtype={dtype})")

    wmodel = _load_whisper_model(model_name, device)
    if wmodel is None:
        return 10, None

    transcribe_kwargs = _build_transcribe_kwargs(language, vad_filter, device)
    if debug:
        print(f"Transcribing: {audio_path} with args {transcribe_kwargs}")

    result = _transcribe_audio(wmodel, audio_path, transcribe_kwargs)
    if result is None:
        return 20, None

    detected_code = _extract_detected_language(result)
    word_options: Dict[str, object] = {
        "highlight_words": bool(vtt_highlight_words),
        "max_line_count": int(vtt_max_line_count),
        "max_line_width": int(vtt_max_line_width),
    }
    if not _write_vtt_result(result, audio_path, out_dir, get_writer, word_options, debug):
        return 21, detected_code

    return 0, detected_code


def _probe_duration_seconds(path: Path, debug: bool = False) -> float:
    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            str(path),
        ]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if p.returncode == 0:
            return float((p.stdout or "0").strip() or 0)
    except Exception as e:
        if debug:
            print(f"ffprobe failed to get duration: {e}")
    return 0.0


def _compute_timeout(args: argparse.Namespace, input_path: Path, debug: bool) -> int:
    dur = _probe_duration_seconds(input_path, debug=debug)
    try:
        timeout_factor = float(args.timeout_factor)
        min_timeout = int(args.min_timeout)
    except Exception:
        timeout_factor = 8.0
        min_timeout = 60
    timeout_sec = max(min_timeout, int((dur or 0) * timeout_factor))
    if debug:
        print(f"Probed duration: {dur:.3f}s, timeout: {timeout_sec}s")
    return timeout_sec


def _prepare_audio_source(
    args: argparse.Namespace, input_path: Path, work_dir: Path, timeout_sec: int, debug: bool
) -> tuple[int, Optional[Path]]:
    input_is_mp3 = input_path.suffix.lower() == ".mp3"
    mp3_path = work_dir / f"{Path(args.input_file).stem}.mp3"

    if input_is_mp3:
        audio_src = input_path
    else:
        rc = run_ffmpeg_to_mp3(
            input_path=input_path,
            mp3_path=mp3_path,
            sample_rate=int(args.sample_rate),
            downmix_mono=(args.downmix_mono == "true"),
            audio_index=int(args.audio_stream_index),
            timeout_sec=timeout_sec,
            debug=debug,
        )
        if rc != 0:
            print(f"ffmpeg extraction failed with return code {rc}")
            return rc, None
        audio_src = mp3_path

    if str(args.normalize).lower() == "true":
        audio_src = normalize_mp3_with_ffmpeg_normalize(
            mp3_path=audio_src,
            target_level=str(args.normalize_target_level),
            timeout_sec=timeout_sec,
            debug=debug,
        )
    return 0, audio_src


def _run_transcription(
    args: argparse.Namespace, audio_src: Path, work_dir: Path, timeout_sec: int, debug: bool
) -> int:
    rc, detected_lang = run_whisper_python(
        audio_path=audio_src,
        out_dir=work_dir,
        language=args.language,
        model=args.model,
        use_gpu=(args.use_gpu == "true"),
        gpu_device=int(args.gpu_device),
        vad_filter=(args.vad_filter == "true"),
        vtt_highlight_words=(str(args.vtt_highlight_words).lower() == "true"),
        vtt_max_line_count=int(args.vtt_max_line_count),
        vtt_max_line_width=int(args.vtt_max_line_width),
        debug=debug,
    )
    if rc == 255:
        rc, detected_lang = run_whisper_cli(
            audio_path=audio_src,
            out_dir=work_dir,
            language=args.language,
            model=args.model,
            use_gpu=(args.use_gpu == "true"),
            gpu_device=int(args.gpu_device),
            vad_filter=(args.vad_filter == "true"),
            timeout_sec=timeout_sec,
            debug=debug,
        )
    if rc != 0:
        print(f"whisper CLI failed with return code {rc}")
    return rc


def _build_vtt_stem_candidates(audio_src: Path) -> list[str]:
    """Build candidate stems for Whisper outputs when input filename contains dots."""
    stem = Path(audio_src).stem
    candidates = [stem]
    if "." in stem:
        parts = stem.split(".")
        # Some Whisper CLI variants effectively truncate dotted stems.
        for i in range(len(parts) - 1, 0, -1):
            candidate = ".".join(parts[:i])
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _find_generated_vtt(audio_src: Path, work_dir: Path) -> Optional[Path]:
    for stem in _build_vtt_stem_candidates(audio_src):
        candidate = work_dir / f"{stem}.vtt"
        if candidate.exists():
            return candidate
    return None


def _finalize_vtt(audio_src: Path, work_dir: Path) -> int:
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    generated_vtt = _find_generated_vtt(audio_src, work_dir)
    try:
        if generated_vtt is None:
            print("VTT output not found after whisper execution")
            return 5

        if generated_vtt != expected_vtt:
            generated_vtt.replace(expected_vtt)

        print(f"VTT written to: {expected_vtt}")
        return 0
    except Exception as e:
        print(f"Failed to finalize VTT: {e}")
        return 6


def main() -> int:
    args = parse_args()

    base_dir = Path(args.base_dir)
    input_path = base_dir / args.input_file
    work_dir = base_dir / args.work_dir
    debug = str(args.debug).lower() in ("true", "1", "yes")

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 2

    timeout_sec = _compute_timeout(args, input_path, debug)
    rc, audio_src = _prepare_audio_source(args, input_path, work_dir, timeout_sec, debug)
    if rc != 0 or audio_src is None:
        return rc

    rc = _run_transcription(args, audio_src, work_dir, timeout_sec, debug)
    if rc != 0:
        return rc

    return _finalize_vtt(audio_src, work_dir)


if __name__ == "__main__":
    raise SystemExit(main())
