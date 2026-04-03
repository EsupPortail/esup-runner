#!/usr/bin/env python3
"""
Standalone transcription script using Whisper for subtitle generation.

This script receives a media or audio file, converts it to MP3 (mono, 16kHz)
with ffmpeg when needed, then runs Whisper to produce subtitles in VTT. If the
requested subtitle language differs from the detected spoken language, the
script translates the finalized VTT while preserving timestamps. If the input
is already an MP3 file, the conversion step is skipped.

High-level workflow:
1. Extract or normalize the audio when needed.
2. Transcribe in source-language auto-detection mode so Whisper first tries to
   understand what is actually spoken.
3. Finalize and clean the source VTT.
4. If the requested subtitle language differs from the detected spoken
   language:
   - use the dedicated local FR<->EN translation pipeline when available;
   - otherwise fall back to Whisper's older best-effort multilingual behavior
     for broader language coverage.
5. Keep the final subtitles in `<stem>.vtt` and record runtime metadata in
   `info_video.json`.

When a translation step rewrites the main `<stem>.vtt` output, the original
source-language subtitles are preserved next to it as a sidecar file named
`<stem>.source-<lang>.webvtt.txt`. We intentionally avoid a `.vtt` extension
for that sidecar so downstream clients that pick the first VTT file only see
the final deliverable subtitles. In `info_video.json`, the `source_sidecar`
field simply stores that sidecar filename so operators can recover or inspect
the pre-translation VTT.

Usage example:
    python transcription.py \
        --base-dir /tmp/work --input-file input.mp4 --work-dir output \
        --language auto --model small \
        --format vtt --use-gpu false
"""

import argparse
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

# Internal safety thresholds for the final VTT validation.
# These are intentionally kept in code instead of the public runner configuration:
# they protect against silently truncated subtitles, and loosening them is rarely
# something an operator should tune per deployment.
_MIN_VTT_COVERAGE_RATIO = 0.75
_MAX_VTT_FINAL_GAP_SECONDS = 300.0

# Chunking defaults are hardware-sensitive. On CPU we prefer chunking sooner to
# cap runtime/memory spikes for long files, while on GPU we keep larger single-pass
# windows because a Tesla T4-class card handles Whisper Turbo much more comfortably.
_CPU_CHUNK_THRESHOLD_SECONDS = 800
_GPU_CHUNK_THRESHOLD_SECONDS = 1800

# Chunk duration and overlap are also intentionally kept internal. These values
# were tuned for long-form media and usually do not deserve a public `.env`
# knob, even though the CLI still exposes them for ad hoc maintenance runs.
_DEFAULT_CHUNK_DURATION_SECONDS = 300
_DEFAULT_CHUNK_OVERLAP_SECONDS = 3
_DEFAULT_WHISPER_MODELS_DIR = "/home/esup-runner/.cache/esup-runner/whisper-models"
_DEFAULT_HUGGINGFACE_MODELS_DIR = "/home/esup-runner/.cache/esup-runner/huggingface"

# Translation remains intentionally internal for now: the public API keeps a
# single `language` parameter, which expresses the final subtitle language. When
# it differs from the detected spoken language, the runner transcribes first and
# then translates the generated VTT while preserving timestamps.
#
# Design note:
# - FR<->EN uses a dedicated local text-translation step because it is more
#   controllable and generally better than asking Whisper to "translate" the
#   audio directly.
# - Other target languages keep a backward-compatible Whisper fallback so the
#   runner still provides broader multilingual coverage, even if quality is less
#   predictable than the dedicated FR/EN pipeline.
_CPU_TRANSLATION_MODELS = {
    ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
}
_GPU_TRANSLATION_MODELS = {
    ("en", "fr"): "Helsinki-NLP/opus-mt-tc-big-en-fr",
    ("fr", "en"): "Helsinki-NLP/opus-mt-tc-big-fr-en",
}
# Translate cue texts in reasonably small batches. This keeps CPU-only runs
# responsive, avoids oversized generation requests on GPU, and still amortizes
# tokenizer/model overhead better than a cue-by-cue loop.
_TRANSLATION_BATCH_SIZE = 24
# Translation-stage return codes live in a dedicated range so operators can
# distinguish "source transcription failed" from "the transcription worked but
# the optional subtitle translation step failed or could not be decided".
_TRANSLATION_UNSUPPORTED_PAIR_RC = 30
_TRANSLATION_BACKEND_UNAVAILABLE_RC = 31
_TRANSLATION_FAILED_RC = 32
_TRANSLATION_DECISION_FAILED_RC = 33
# Metadata labels written to `info_video.json` so callers can tell whether the
# task kept the source subtitles, used local FR<->EN translation, or fell back
# to Whisper's broader but lower-confidence multilingual path.
_TRANSLATION_BACKEND_NONE = "none"
_TRANSLATION_BACKEND_LOCAL = "local_translation"
_TRANSLATION_BACKEND_WHISPER_LEGACY = "whisper_legacy_fallback"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the transcription script."""
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
        "--language",
        default=os.getenv("WHISPER_LANGUAGE", "auto"),
        help="Final subtitle language code or 'auto' to keep the detected spoken language",
    )
    parser.add_argument(
        "--format", default="vtt", choices=["vtt"], help="Output subtitle format (forced to vtt)"
    )
    parser.add_argument(
        "--model",
        default=os.getenv("WHISPER_MODEL", "small"),
        help="Whisper model name (tiny|base|small|medium|large[/-v3]|turbo)",
    )
    parser.add_argument(
        "--whisper-models-dir",
        default=os.getenv("WHISPER_MODELS_DIR", _DEFAULT_WHISPER_MODELS_DIR),
        help="Directory used to cache Whisper models",
    )
    parser.add_argument(
        "--video-id",
        default="",
        help="Optional external video identifier used for tracing (Ex: 12345).",
    )
    parser.add_argument(
        "--video-slug",
        default="",
        help="Optional external video slug used for tracing (Ex: introduction-python).",
    )
    parser.add_argument(
        "--video-title",
        default="",
        help="Optional external video title used for tracing.",
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
    # These remain available on the script CLI for troubleshooting, but they are
    # not meant to be standard runner-level configuration variables.
    parser.add_argument(
        "--chunk-duration-seconds",
        default=str(_DEFAULT_CHUNK_DURATION_SECONDS),
        help="Chunk duration for long audio transcriptions in seconds (0 disables chunking)",
    )
    parser.add_argument(
        "--chunk-overlap-seconds",
        default=str(_DEFAULT_CHUNK_OVERLAP_SECONDS),
        help="Overlap between consecutive chunks in seconds",
    )
    parser.add_argument(
        "--chunk-threshold-seconds",
        default=os.getenv("WHISPER_CHUNK_THRESHOLD_SECONDS"),
        help="Optional override for the automatic chunking threshold (defaults: 800 on CPU, 1800 on GPU)",
    )
    parser.add_argument(
        "--huggingface-models-dir",
        default=os.getenv("HUGGINGFACE_MODELS_DIR", _DEFAULT_HUGGINGFACE_MODELS_DIR),
        help="Directory used to cache Hugging Face translation models",
    )
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
    return parser.parse_args(argv)


def _extract_video_identification(args: argparse.Namespace) -> Dict[str, str]:
    """Extract optional video identification metadata from CLI args."""
    values = {
        "video_id": args.video_id,
        "video_slug": args.video_slug,
        "video_title": args.video_title,
    }
    return {
        key: str(value) for key, value in values.items() if value is not None and str(value).strip()
    }


def _write_video_identification_metadata(
    work_dir: Path, metadata: Dict[str, str], debug: bool = False
) -> None:
    """Persist optional video identification metadata into info_video.json."""
    if metadata:
        _write_info_video_metadata(work_dir, metadata, debug=debug)


def _write_info_video_metadata(
    work_dir: Path, metadata: Dict[str, Any], debug: bool = False
) -> None:
    """Merge task metadata into info_video.json.

    The same file is used for both external video identifiers and runtime
    details such as detected languages or the translation model that was
    actually selected for this run.
    """
    if not metadata:
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    info_path = work_dir / "info_video.json"
    data: Dict[str, Any] = {}
    try:
        if info_path.exists():
            loaded = json.loads(info_path.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}

    data.update(metadata)
    info_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if debug:
        print(f"Task metadata written to: {info_path}")


# Cache for CLI help to feature-detect options
_WHISPER_HELP_CACHE: Optional[str] = None


def _get_whisper_help_text(debug: bool = False) -> str:
    """Return cached `whisper --help` output for CLI feature detection."""
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
    """Return the first supported CLI flag among the provided alternatives."""
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


def _normalize_language_code(language: Optional[str]) -> Optional[str]:
    """Normalize a runner/Whisper language value to a stable short code."""
    if language is None:
        return None

    normalized = str(language).strip().lower()
    if not normalized:
        return None
    if normalized == "auto":
        return "auto"

    mapped_code = _map_language_name_to_code(normalized)
    if mapped_code:
        return mapped_code

    return normalized.replace("_", "-").split("-", 1)[0]


def _build_whisper_command(
    audio_path: Path,
    out_dir: Path,
    model_name: str,
    whisper_models_dir: Optional[str],
    language: str,
    vad_filter: bool,
    debug: bool,
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
        model_dir_flag = _cli_supports_option(["--model_dir", "--model-dir"], debug=debug)
        if model_dir_flag is not None:
            cmd += [model_dir_flag, normalized_models_dir]
        elif debug:
            print("whisper CLI does not support model_dir option; using default cache path")

    vad_flag = _cli_supports_option(["--vad_filter", "--vad-filter"], debug=debug)
    if vad_flag is not None:
        cmd += [vad_flag, "true" if vad_filter else "false"]
    elif debug and vad_filter:
        print("whisper CLI does not support a VAD option; ignoring --vad-filter request")
    return cmd


def _prepare_whisper_env(use_gpu: bool, gpu_device: int) -> tuple[list[str], Dict[str, str]]:
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


def _detect_language_from_stdout(stdout: str, language: str) -> Optional[str]:
    """Extract the detected language code from Whisper CLI stdout when auto mode is used."""
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


def _resolve_effective_use_gpu(requested_use_gpu: bool, debug: bool) -> bool:
    """Return whether CUDA can actually be used for this run.

    When GPU is requested but CUDA is unavailable (driver/runtime mismatch,
    CPU-only environment, etc.), transcription transparently falls back to CPU.
    """
    if not requested_use_gpu:
        return False

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return True

        print("CUDA requested but unavailable; falling back to CPU for transcription")
        return False
    except Exception as e:
        if debug:
            print(f"Failed to probe CUDA availability ({e}); falling back to CPU")
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
) -> tuple[int, Optional[str]]:
    """Run the openai-whisper CLI to generate VTT subtitles from an audio file (MP3 or others)."""
    out_dir.mkdir(parents=True, exist_ok=True)

    model_name = map_model_name(model, "cli")
    cmd = _build_whisper_command(
        audio_path=audio_path,
        out_dir=out_dir,
        model_name=model_name,
        whisper_models_dir=whisper_models_dir,
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
    """Import Whisper Python API modules and return them when available."""
    try:
        import torch  # type: ignore
        import whisper  # type: ignore
        from whisper.utils import get_writer  # type: ignore

        return torch, whisper, get_writer
    except Exception as e:
        print(f"Falling back to CLI: failed to import whisper API ({e})")
        return None, None, None


def _load_whisper_model(
    model_name: str, device: str, whisper_models_dir: Optional[str] = None
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
    except Exception as e:
        print(f"Failed to load whisper model '{model_name}': {e}")
        return None


# Matches segments that contain only punctuation-like filler. Those cues are
# usually produced when Whisper tries to decode silence or very weak speech.
_PUNCT_ONLY_TEXT_RE = re.compile(r"^[\s\.\,\!\?\:\;…'\"`\-\(\)\[\]\{\}/\\|_]+$")

# Whisper's VTT wrapping can split just before an apostrophe, which creates
# artifacts such as `l` on one line and `'usage` on the next one. We only join
# well-known French/English elision or contraction stems so that we do not
# rewrite arbitrary quoted text.
_APOSTROPHE_JOIN_STEMS = frozenset(
    {
        "aren",
        "aujourd",
        "c",
        "couldn",
        "d",
        "didn",
        "doesn",
        "don",
        "hadn",
        "hasn",
        "haven",
        "he",
        "how",
        "i",
        "isn",
        "it",
        "j",
        "jusqu",
        "l",
        "let",
        "lorsqu",
        "m",
        "mustn",
        "n",
        "needn",
        "presqu",
        "puisqu",
        "qu",
        "quelqu",
        "quoiqu",
        "s",
        "she",
        "shouldn",
        "t",
        "that",
        "there",
        "they",
        "wasn",
        "we",
        "weren",
        "what",
        "where",
        "who",
        "won",
        "wouldn",
        "you",
    }
)
_APOSTROPHE_JOIN_RE = re.compile(
    r"\b("
    + "|".join(re.escape(stem) for stem in sorted(_APOSTROPHE_JOIN_STEMS, key=len, reverse=True))
    + r")\s+'(?=[A-Za-zÀ-ÖØ-öø-ÿ])",
    re.IGNORECASE,
)
_TOKEN_EDGE_PUNCT_RE = re.compile(r"^[^A-Za-zÀ-ÖØ-öø-ÿ']+|[^A-Za-zÀ-ÖØ-öø-ÿ'-]+$")
_TRAILING_WORD_RE = re.compile(r"([A-Za-zÀ-ÖØ-öø-ÿ]+)$")
_LEADING_APOSTROPHE_TOKEN_RE = re.compile(r"^'[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ-]*")
_MAX_APOSTROPHE_CUE_JOIN_GAP_SECONDS = 0.25
_VTT_POSTPROCESS_CUE_BLOCK = tuple[list[str], str]

# Catches the most common subtitle-credit hallucinations that models sometimes
# emit on silent tails or noisy stretches.
_SUBTITLE_CREDIT_TEXT_RE = re.compile(
    r"^(?:sous[- ]?titrage|sous[- ]?titres?|subtitles?|captions?)\b",
    re.IGNORECASE,
)

# We use lightweight script detection to reject obviously out-of-place text for
# Latin-script languages such as French or English. This is intentionally narrow:
# it targets strong hallucination signals without penalizing normal punctuation.
_CYRILLIC_TEXT_RE = re.compile(r"[\u0400-\u04FF]")
_CJK_TEXT_RE = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]")

# Languages where a sudden switch to Cyrillic or CJK output is a reliable sign
# of transcription drift rather than valid content.
_LATIN_SCRIPT_LANGUAGES = {
    "ca",
    "cs",
    "da",
    "de",
    "en",
    "es",
    "eu",
    "fi",
    "fr",
    "gl",
    "hr",
    "hu",
    "is",
    "it",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "sv",
    "tr",
}


def _build_transcribe_kwargs(
    language: str, vad_filter: bool, device: str, *, chunked: bool
) -> Dict[str, object]:
    """Build keyword arguments for a Whisper Python transcription call."""
    kwargs: Dict[str, object] = {
        "fp16": device == "cuda",
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
    }
    if language and language.lower() != "auto":
        kwargs["language"] = language
    kwargs["vad_filter"] = bool(vad_filter)
    if chunked:
        # Chunked runs already reuse a short prompt tail between chunks.
        # Disabling previous-text conditioning reduces the risk of repetition loops
        # and cross-chunk hallucinations on long, resource-constrained transcriptions.
        kwargs["condition_on_previous_text"] = False
        # Keep chunked decoding deterministic. High-temperature fallback is more likely
        # to invent filler, repeated text, or multilingual artifacts on weak audio spans.
        kwargs["temperature"] = 0.0
        # Word timestamps unlock Whisper's built-in hallucination skipping on silence.
        kwargs["word_timestamps"] = True
        kwargs["hallucination_silence_threshold"] = 2.0
    else:
        # For single-pass transcriptions, keeping previous-text conditioning helps
        # preserve local continuity across Whisper's internal decoding windows.
        # We still request word timestamps because Whisper's VTT writer only
        # enforces max_line_width / max_line_count when per-word timings exist.
        kwargs["condition_on_previous_text"] = True
        kwargs["word_timestamps"] = True
    return kwargs


def _transcribe_audio(
    wmodel: Any, audio_path: Path, transcribe_kwargs: Dict[str, object]
) -> Optional[Dict[str, object]]:
    """Run Whisper transcription and gracefully retry without unsupported options."""
    try:
        return wmodel.transcribe(str(audio_path), **transcribe_kwargs)  # type: ignore
    except TypeError:
        kwargs = dict(transcribe_kwargs)
        kwargs.pop("vad_filter", None)
        return wmodel.transcribe(str(audio_path), **kwargs)  # type: ignore
    except Exception as e:
        print(f"Whisper transcription failed: {e}")
        return None


def _normalize_chunk_overlap_seconds(chunk_duration_sec: int, chunk_overlap_sec: int) -> int:
    """Clamp chunk overlap to a sane range for the configured chunk duration."""
    if chunk_duration_sec <= 1:
        return 0
    return max(0, min(int(chunk_overlap_sec), int(chunk_duration_sec) - 1))


def _resolve_chunk_threshold_seconds(configured_value: object, use_gpu: bool) -> int:
    """Return the chunk threshold to use for the current hardware profile.

    The runner exposes `WHISPER_CHUNK_THRESHOLD_SECONDS` as an override, but the
    default should follow the actual execution mode:
    - CPU: chunk earlier to avoid very long, memory-heavy transcriptions
    - GPU: allow longer single-pass spans to preserve continuity
    """
    try:
        if configured_value is not None and str(configured_value).strip() != "":
            return max(0, int(str(configured_value)))
    except (TypeError, ValueError):
        pass

    return _GPU_CHUNK_THRESHOLD_SECONDS if use_gpu else _CPU_CHUNK_THRESHOLD_SECONDS


def _resolve_transcription_language(requested_language: str) -> str:
    """Return the language Whisper should use for the source transcription pass.

    `language` now represents the final subtitle language exposed to callers.
    The audio may be spoken in a different language, so the first pass must stay
    in auto-detection mode to avoid forcing Whisper to decode French speech as
    English (or the opposite) before the translation step decides what to do.
    """
    return "auto"


def _plan_audio_chunks(
    total_duration_sec: float,
    chunk_duration_sec: int,
    chunk_threshold_sec: int,
    chunk_overlap_sec: int,
) -> list[tuple[float, float]]:
    """Return chunk boundaries for long audio transcriptions."""
    if total_duration_sec <= 0:
        return []

    if chunk_duration_sec <= 0:
        return [(0.0, float(total_duration_sec))]

    if chunk_threshold_sec > 0 and total_duration_sec <= chunk_threshold_sec:
        return [(0.0, float(total_duration_sec))]

    normalized_overlap_sec = _normalize_chunk_overlap_seconds(
        chunk_duration_sec=int(chunk_duration_sec),
        chunk_overlap_sec=int(chunk_overlap_sec),
    )
    chunks: list[tuple[float, float]] = []
    start_sec = 0.0
    while start_sec < total_duration_sec:
        duration_sec = min(float(chunk_duration_sec), float(total_duration_sec - start_sec))
        chunks.append((round(start_sec, 3), round(duration_sec, 3)))
        end_sec = start_sec + duration_sec
        if end_sec >= total_duration_sec:
            break
        start_sec = max(start_sec + 1.0, end_sec - float(normalized_overlap_sec))
    return chunks


def _extract_audio_chunk(
    audio_path: Path,
    chunk_path: Path,
    start_sec: float,
    duration_sec: float,
    timeout_sec: int,
    debug: bool,
) -> int:
    """Extract a mono 16kHz MP3 chunk used for bounded-memory transcription."""
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_sec:.3f}",
        "-t",
        f"{duration_sec:.3f}",
        "-i",
        str(audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(chunk_path),
    ]
    if debug:
        print("Executing:", " ".join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        print(f"ffmpeg chunk extraction timed out after {timeout_sec}s")
        return 124
    if debug:
        print(proc.stdout)
        print(proc.stderr)
    return proc.returncode


def _offset_timestamp(value: object, offset_sec: float) -> object:
    """Offset a Whisper timestamp when it is numeric."""
    if isinstance(value, (int, float)):
        return round(float(value) + offset_sec, 3)
    return value


def _offset_segment_timestamps(
    segment: Dict[str, object], segment_id: int, offset_sec: float
) -> Dict[str, object]:
    """Offset segment and word timestamps for a chunked transcription merge."""
    shifted = dict(segment)
    shifted["id"] = segment_id
    shifted["start"] = _offset_timestamp(shifted.get("start"), offset_sec)
    shifted["end"] = _offset_timestamp(shifted.get("end"), offset_sec)

    words = shifted.get("words")
    if isinstance(words, list):
        shifted_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            shifted_word = dict(word)
            shifted_word["start"] = _offset_timestamp(shifted_word.get("start"), offset_sec)
            shifted_word["end"] = _offset_timestamp(shifted_word.get("end"), offset_sec)
            shifted_words.append(shifted_word)
        shifted["words"] = shifted_words

    return shifted


def _compute_chunk_keep_window(
    chunk_plan: list[tuple[float, float]], chunk_index: int
) -> tuple[float, float]:
    """Split overlap evenly so adjacent chunks do not duplicate subtitle cues."""
    chunk_start_sec, chunk_duration_sec = chunk_plan[chunk_index]
    chunk_end_sec = chunk_start_sec + chunk_duration_sec
    keep_start_sec = float(chunk_start_sec)
    keep_end_sec = float(chunk_end_sec)

    if chunk_index > 0:
        prev_start_sec, prev_duration_sec = chunk_plan[chunk_index - 1]
        prev_end_sec = prev_start_sec + prev_duration_sec
        if chunk_start_sec < prev_end_sec:
            keep_start_sec = chunk_start_sec + ((prev_end_sec - chunk_start_sec) / 2.0)

    if chunk_index + 1 < len(chunk_plan):
        next_start_sec, _ = chunk_plan[chunk_index + 1]
        if next_start_sec < chunk_end_sec:
            keep_end_sec = next_start_sec + ((chunk_end_sec - next_start_sec) / 2.0)

    return round(keep_start_sec, 3), round(keep_end_sec, 3)


def _trim_segment_to_time_window(
    segment: Dict[str, object], keep_start_sec: float, keep_end_sec: float
) -> Optional[Dict[str, object]]:
    """Trim a segment to the portion that should be kept after chunk overlap merging."""
    start_sec = _safe_float(segment.get("start"))
    end_sec = _safe_float(segment.get("end"))
    if start_sec is None or end_sec is None:
        return segment
    if end_sec <= keep_start_sec or start_sec >= keep_end_sec:
        return None

    trimmed = dict(segment)
    trimmed["start"] = round(max(start_sec, keep_start_sec), 3)
    trimmed["end"] = round(min(end_sec, keep_end_sec), 3)

    words = trimmed.get("words")
    if isinstance(words, list):
        trimmed_words = []
        for word in words:
            if not isinstance(word, dict):
                continue
            word_start_sec = _safe_float(word.get("start"))
            word_end_sec = _safe_float(word.get("end"))
            if word_start_sec is None or word_end_sec is None:
                trimmed_words.append(dict(word))
                continue
            if word_end_sec <= keep_start_sec or word_start_sec >= keep_end_sec:
                continue
            trimmed_word = dict(word)
            trimmed_word["start"] = round(max(word_start_sec, keep_start_sec), 3)
            trimmed_word["end"] = round(min(word_end_sec, keep_end_sec), 3)
            trimmed_words.append(trimmed_word)
        trimmed["words"] = trimmed_words

    return trimmed


def _merge_adjacent_identical_segment(
    merged_segments: list[Dict[str, object]], next_segment: Dict[str, object]
) -> bool:
    """Merge adjacent identical cues that can appear on chunk overlap boundaries."""
    if not merged_segments:
        return False

    previous_segment = merged_segments[-1]
    previous_text = str(previous_segment.get("text", "")).strip()
    next_text = str(next_segment.get("text", "")).strip()
    if not previous_text or previous_text != next_text:
        return False

    previous_end_sec = _safe_float(previous_segment.get("end"))
    next_start_sec = _safe_float(next_segment.get("start"))
    next_end_sec = _safe_float(next_segment.get("end"))
    if previous_end_sec is None or next_start_sec is None or next_end_sec is None:
        return False
    if next_start_sec > previous_end_sec + 0.05:
        return False

    previous_segment["end"] = round(max(previous_end_sec, next_end_sec), 3)
    previous_words = previous_segment.get("words")
    next_words = next_segment.get("words")
    if isinstance(previous_words, list) and isinstance(next_words, list):
        previous_words.extend(next_words)
    return True


def _resolve_keep_window(
    keep_windows: Optional[list[tuple[float, float]]], chunk_index: int
) -> tuple[Optional[float], Optional[float]]:
    """Return the keep window that applies to one chunk result."""
    if keep_windows is None or chunk_index >= len(keep_windows):
        return None, None
    keep_start_sec, keep_end_sec = keep_windows[chunk_index]
    return keep_start_sec, keep_end_sec


def _append_chunk_segments(
    merged_segments: list[Dict[str, object]],
    chunk_segments: list[object],
    next_segment_id: int,
    offset_sec: float,
    keep_window: tuple[Optional[float], Optional[float]],
) -> int:
    """Offset, trim, and append one chunk's segments into the merged result."""
    keep_start_sec, keep_end_sec = keep_window

    for segment in chunk_segments:
        if not isinstance(segment, dict):
            continue

        shifted_segment = _offset_segment_timestamps(segment, next_segment_id, offset_sec)
        if keep_start_sec is not None and keep_end_sec is not None:
            trimmed_segment = _trim_segment_to_time_window(
                shifted_segment,
                keep_start_sec=keep_start_sec,
                keep_end_sec=keep_end_sec,
            )
            if trimmed_segment is None:
                continue
            shifted_segment = trimmed_segment

        if _merge_adjacent_identical_segment(merged_segments, shifted_segment):
            continue

        merged_segments.append(shifted_segment)
        next_segment_id += 1

    return next_segment_id


def _build_merged_result_text(segments: list[Dict[str, object]]) -> str:
    """Join non-empty segment texts into Whisper's flattened `text` field."""
    return " ".join(
        str(segment.get("text", "")).strip()
        for segment in segments
        if str(segment.get("text", "")).strip()
    )


def _combine_chunk_results(
    chunk_results: list[tuple[float, Dict[str, object]]],
    keep_windows: Optional[list[tuple[float, float]]] = None,
) -> Dict[str, object]:
    """Merge multiple Whisper chunk results into a single writer-compatible result."""
    merged_segments: list[Dict[str, object]] = []
    detected_language: Optional[str] = None
    next_segment_id = 0

    for chunk_index, (offset_sec, chunk_result) in enumerate(chunk_results):
        if detected_language is None:
            detected_language = _extract_detected_language(chunk_result)

        chunk_segments = chunk_result.get("segments")
        if not isinstance(chunk_segments, list):
            continue
        next_segment_id = _append_chunk_segments(
            merged_segments=merged_segments,
            chunk_segments=chunk_segments,
            next_segment_id=next_segment_id,
            offset_sec=offset_sec,
            keep_window=_resolve_keep_window(keep_windows, chunk_index),
        )

    merged: Dict[str, object] = {
        "text": _build_merged_result_text(merged_segments),
        "segments": merged_segments,
    }
    if detected_language:
        merged["language"] = detected_language
    return merged


def _is_punctuation_only_text(text: str) -> bool:
    """Return whether text only contains punctuation-like filler."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    return _PUNCT_ONLY_TEXT_RE.fullmatch(normalized) is not None


def _safe_float(value: object) -> Optional[float]:
    """Return a float when conversion is possible."""
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _language_uses_latin_script(language: Optional[str]) -> bool:
    """Return whether the expected transcription language normally uses Latin script."""
    if not language:
        return False
    return language.strip().lower() in _LATIN_SCRIPT_LANGUAGES


def _contains_unexpected_script(text: str, expected_language: Optional[str]) -> bool:
    """Return whether text contains scripts that are unexpected for the target language."""
    if not _language_uses_latin_script(expected_language):
        return False
    return _CYRILLIC_TEXT_RE.search(text) is not None or _CJK_TEXT_RE.search(text) is not None


def _looks_like_subtitle_credit(text: str) -> bool:
    """Return whether text looks like a burned-in subtitle credit hallucination."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return False
    return _SUBTITLE_CREDIT_TEXT_RE.search(normalized) is not None


def _looks_like_repetition_loop(text: str) -> bool:
    """Return whether text contains a long low-diversity repetition loop."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return False

    numeric_tokens = re.findall(r"\d+", normalized)
    if len(numeric_tokens) >= 12 and len(set(numeric_tokens)) <= 4:
        compact = re.sub(r"\s+", "", normalized)
        numeric_chars = "".join(ch for ch in compact if ch.isdigit())
        if len(numeric_chars) >= max(12, int(len(compact) * 0.3)):
            return True

    return False


def _should_drop_segment(
    segment: Dict[str, object], expected_language: Optional[str] = None
) -> bool:
    """Drop silence or obviously hallucinatory segments from chunked results."""
    text = str(segment.get("text", "")).strip()
    if _is_punctuation_only_text(text):
        return True
    if _looks_like_subtitle_credit(text):
        return True
    if _looks_like_repetition_loop(text):
        return True
    if _contains_unexpected_script(text, expected_language):
        return True

    no_speech_prob = _safe_float(segment.get("no_speech_prob"))
    avg_logprob = _safe_float(segment.get("avg_logprob"))
    compression_ratio = _safe_float(segment.get("compression_ratio"))

    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob >= 0.6
        and avg_logprob < -0.8
    ):
        return True

    if (
        compression_ratio is not None
        and avg_logprob is not None
        and compression_ratio > 3.0
        and avg_logprob < -0.8
    ):
        return True

    return False


def _filter_result_segments(
    result: Dict[str, object],
    expected_language: Optional[str] = None,
    debug: bool = False,
) -> Dict[str, object]:
    """Filter obviously bad segments and rebuild the result text."""
    segments = result.get("segments")
    if not isinstance(segments, list):
        return result
    effective_language = expected_language or _extract_detected_language(result)

    filtered_segments: list[Dict[str, object]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if _should_drop_segment(segment, effective_language):
            if debug:
                print(
                    "Dropping suspicious segment: "
                    f"text={segment.get('text', '')!r}, "
                    f"expected_language={effective_language!r}, "
                    f"no_speech_prob={segment.get('no_speech_prob')}, "
                    f"avg_logprob={segment.get('avg_logprob')}, "
                    f"compression_ratio={segment.get('compression_ratio')}"
                )
            continue
        filtered_segments.append(segment)

    filtered_result = dict(result)
    filtered_result["segments"] = filtered_segments
    filtered_result["text"] = " ".join(
        str(segment.get("text", "")).strip()
        for segment in filtered_segments
        if str(segment.get("text", "")).strip()
    )
    return filtered_result


def _build_initial_prompt_from_text(text: str, max_chars: int = 224) -> Optional[str]:
    """Build a compact prompt tail reused as context for the next chunk."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized
    return normalized[-max_chars:]


def _extract_detected_language(result: Dict[str, object]) -> Optional[str]:
    """Extract the detected language code from a Whisper Python result."""
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
    """Write a Whisper transcription result to a VTT file."""
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


def _normalize_vtt_cue_text(text: str) -> str:
    """Normalize cue text spacing before wrapping it again."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return ""

    # Repair the common `l 'usage` / `s 'est` artifacts introduced by hard wraps.
    return _APOSTROPHE_JOIN_RE.sub(r"\1'", normalized)


def _wrap_vtt_cue_text(text: str, max_line_width: int, max_line_count: int) -> list[str]:
    """Wrap cue text while keeping the configured number of lines under control."""
    normalized = _normalize_vtt_cue_text(text)
    if not normalized:
        return []

    if max_line_width <= 0 or max_line_count <= 1:
        return [normalized]

    words = normalized.split()
    wrapped_lines: list[str] = []
    current_line = ""

    for word in words:
        candidate = word if not current_line else f"{current_line} {word}"
        if (
            current_line
            and len(candidate) > max_line_width
            and len(wrapped_lines) < max_line_count - 1
        ):
            wrapped_lines.append(current_line)
            current_line = word
        else:
            current_line = candidate

    if current_line:
        wrapped_lines.append(current_line)

    if len(wrapped_lines) <= max_line_count:
        return wrapped_lines

    return wrapped_lines[: max_line_count - 1] + [" ".join(wrapped_lines[max_line_count - 1 :])]


def _parse_vtt_cue_time_range(timestamp_line: str) -> tuple[Optional[float], Optional[float]]:
    """Parse the start/end timestamps from one WebVTT cue header line."""
    if "-->" not in timestamp_line:
        return None, None

    try:
        raw_start, raw_end = timestamp_line.split("-->", 1)
        start_token = raw_start.strip().split()[0]
        end_token = raw_end.strip().split()[0]
    except Exception:
        return None, None

    return _parse_vtt_timestamp(start_token), _parse_vtt_timestamp(end_token)


def _cue_gap_allows_apostrophe_transfer(
    previous_timestamp_line: str, next_timestamp_line: str
) -> bool:
    """Return whether two adjacent cues are close enough to repair a split word."""
    previous_start_sec, previous_end_sec = _parse_vtt_cue_time_range(previous_timestamp_line)
    next_start_sec, _next_end_sec = _parse_vtt_cue_time_range(next_timestamp_line)
    if previous_start_sec is None or previous_end_sec is None or next_start_sec is None:
        return False

    gap_sec = next_start_sec - previous_end_sec
    return 0.0 <= gap_sec <= _MAX_APOSTROPHE_CUE_JOIN_GAP_SECONDS


def _extract_token_core(token: str) -> str:
    """Return a token stripped from edge punctuation but keeping apostrophes."""
    return _TOKEN_EDGE_PUNCT_RE.sub("", (token or "").strip())


def _split_leading_token(text: str) -> tuple[str, str]:
    """Split the first token from the remaining normalized text."""
    token, _separator, remainder = (text or "").partition(" ")
    return token, remainder.strip()


def _extract_trailing_token_core(text: str) -> str:
    """Return the normalized last token core from a cue text."""
    normalized = _normalize_vtt_cue_text(text)
    if not normalized:
        return ""
    return _extract_token_core(normalized.rsplit(" ", 1)[-1])


def _repair_cross_cue_apostrophe_split(previous_text: str, next_text: str) -> tuple[str, str]:
    """Move a leading apostrophe token back to the previous cue when safe."""
    normalized_previous = _normalize_vtt_cue_text(previous_text)
    normalized_next = _normalize_vtt_cue_text(next_text)
    if not normalized_previous or not normalized_next:
        return normalized_previous, normalized_next

    previous_last_core = _extract_trailing_token_core(normalized_previous)
    if not previous_last_core:
        return normalized_previous, normalized_next

    next_first_token, next_remainder = _split_leading_token(normalized_next)
    next_first_core = _extract_token_core(next_first_token)
    if _LEADING_APOSTROPHE_TOKEN_RE.match(next_first_core) is None:
        return normalized_previous, normalized_next

    # Case 1: the previous cue ends with a bare elision stem (`s`, `l`, `we`, `don`).
    # We can safely move the apostrophe token into that cue.
    if previous_last_core.lower() in _APOSTROPHE_JOIN_STEMS:
        merged_previous = _normalize_vtt_cue_text(f"{normalized_previous}{next_first_token}")
        return merged_previous, next_remainder

    # Case 2: the previous cue already contains the full apostrophe token
    # (`l'institution`, `we're`) and the next cue repeats only the apostrophe
    # suffix because of a boundary overlap. In that case we only drop the
    # duplicated leading token from the next cue.
    if previous_last_core.lower().endswith(next_first_core.lower()):
        return normalized_previous, next_remainder

    return normalized_previous, normalized_next


def _parse_vtt_postprocess_block(block: str) -> str | _VTT_POSTPROCESS_CUE_BLOCK:
    """Convert a raw VTT block into either a cue tuple or a passthrough string."""
    if "-->" not in block:
        return block

    block_lines = block.splitlines()
    timestamp_index = next((index for index, line in enumerate(block_lines) if "-->" in line), -1)
    if timestamp_index < 0:
        return block

    cue_prefix = block_lines[: timestamp_index + 1]
    cue_text_lines = [line.strip() for line in block_lines[timestamp_index + 1 :] if line.strip()]
    return (cue_prefix, " ".join(cue_text_lines))


def _repair_cross_cue_apostrophe_splits(blocks: list[str | _VTT_POSTPROCESS_CUE_BLOCK]) -> None:
    """Repair safe apostrophe splits that landed on two adjacent cue blocks."""
    for index in range(len(blocks) - 1):
        previous_block = blocks[index]
        next_block = blocks[index + 1]
        if isinstance(previous_block, str) or isinstance(next_block, str):
            continue

        previous_prefix, previous_text = previous_block
        next_prefix, next_text = next_block
        if not previous_prefix or not next_prefix:
            continue
        if not _cue_gap_allows_apostrophe_transfer(previous_prefix[-1], next_prefix[-1]):
            continue

        repaired_previous_text, repaired_next_text = _repair_cross_cue_apostrophe_split(
            previous_text,
            next_text,
        )
        blocks[index] = (previous_prefix, repaired_previous_text)
        blocks[index + 1] = (next_prefix, repaired_next_text)


def _render_postprocessed_vtt_blocks(
    blocks: list[str | _VTT_POSTPROCESS_CUE_BLOCK],
    *,
    max_line_width: int,
    max_line_count: int,
) -> list[str]:
    """Render parsed VTT blocks back to strings after readability cleanup."""
    rendered_blocks: list[str] = []
    for parsed_block in blocks:
        if isinstance(parsed_block, str):
            rendered_blocks.append(parsed_block)
            continue

        cue_prefix, cue_text = parsed_block
        wrapped_text_lines = _wrap_vtt_cue_text(
            cue_text,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
        )
        if not wrapped_text_lines:
            continue
        rendered_blocks.append("\n".join(cue_prefix + wrapped_text_lines))
    return rendered_blocks


def _postprocess_vtt_content(
    content: str,
    *,
    max_line_width: int,
    max_line_count: int,
) -> str:
    """Apply safe readability cleanup to a VTT document."""
    parsed_blocks = [_parse_vtt_postprocess_block(block) for block in (content or "").split("\n\n")]
    _repair_cross_cue_apostrophe_splits(parsed_blocks)
    processed_blocks = _render_postprocessed_vtt_blocks(
        parsed_blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )

    return "\n\n".join(processed_blocks).rstrip() + "\n"


def _postprocess_vtt_file(
    vtt_path: Path,
    *,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> None:
    """Rewrite the generated VTT with small readability-focused fixes."""
    original_content = vtt_path.read_text(encoding="utf-8")
    processed_content = _postprocess_vtt_content(
        original_content,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )
    if processed_content != original_content:
        vtt_path.write_text(processed_content, encoding="utf-8")
        if debug:
            print(f"Applied readability post-processing to: {vtt_path}")


def _build_source_vtt_sidecar_path(vtt_path: Path, source_language: str) -> Path:
    """Return the sidecar path used to preserve the pre-translation source VTT.

    Example:
    - main translated file: `audio_192k_test.vtt`
    - preserved source VTT: `audio_192k_test.source-fr.webvtt.txt`

    The sidecar intentionally does not use the `.vtt` extension because some
    client applications consume the first VTT they find in the task output.
    Only the translated deliverable should remain discoverable as a VTT file.
    """
    normalized_source_language = _normalize_language_code(source_language) or "source"
    return vtt_path.with_name(f"{vtt_path.stem}.source-{normalized_source_language}.webvtt.txt")


def _prepare_huggingface_models_dir(
    models_dir: Optional[str], debug: bool = False
) -> Optional[str]:
    """Create and return the configured Hugging Face cache directory.

    Translation models are loaded through `transformers`, so we keep their cache
    separate from Whisper's cache directory to make storage ownership and
    cleanup easier to understand operationally.
    """
    normalized_models_dir = str(models_dir or "").strip()
    if not normalized_models_dir:
        return None

    try:
        cache_path = Path(normalized_models_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        return str(cache_path)
    except Exception as e:
        if debug:
            print(f"Failed to prepare Hugging Face cache directory '{normalized_models_dir}': {e}")
        return normalized_models_dir


def _resolve_translation_model_name(
    source_language: Optional[str], target_language: Optional[str], use_gpu: bool
) -> Optional[str]:
    """Return the internal translation model name for the requested language pair.

    We intentionally use lighter Marian models on CPU-only runners and bigger
    `tc-big` variants on GPU runners. That keeps CPU deployments usable while
    letting GPU deployments trade extra resources for better translation quality.
    """
    normalized_source_language = _normalize_language_code(source_language)
    normalized_target_language = _normalize_language_code(target_language)
    if not normalized_source_language or not normalized_target_language:
        return None
    model_map = _GPU_TRANSLATION_MODELS if use_gpu else _CPU_TRANSLATION_MODELS
    return model_map.get((normalized_source_language, normalized_target_language))


def _translation_hardware_profile(use_gpu: bool) -> str:
    """Return a stable label for metadata/debug logs."""
    return "gpu" if use_gpu else "cpu"


def _build_translation_metadata(
    *,
    applied: bool,
    backend: str,
    source_language: Optional[str],
    target_language: Optional[str],
    model: Optional[str],
    use_gpu: bool,
    source_sidecar: Optional[str] = None,
    execution_backend: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Build normalized translation metadata for info_video.json.

    `source_sidecar` is only a filename. It points to the preserved source
    subtitle file written next to the final VTT when a translation step replaces
    the main output. The sidecar keeps WebVTT content but uses a non-`.vtt`
    filename on purpose so client-facing consumers keep selecting the final
    translated VTT.
    """
    metadata: Dict[str, Any] = {
        "applied": bool(applied),
        "backend": backend,
        "source_language": _normalize_language_code(source_language),
        "target_language": _normalize_language_code(target_language),
        "model": model,
        "hardware_profile": _translation_hardware_profile(use_gpu),
    }
    if source_sidecar:
        metadata["source_sidecar"] = source_sidecar
    if execution_backend:
        metadata["execution_backend"] = execution_backend
    if note:
        metadata["note"] = note
    return metadata


def _import_translation_modules() -> tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Import the local subtitle translation backend on demand."""
    try:
        import torch  # type: ignore
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # type: ignore

        return torch, AutoModelForSeq2SeqLM, AutoTokenizer
    except Exception as e:
        print(
            "Subtitle translation backend unavailable: "
            f"{e}. Install the transcription extras with translation support."
        )
        return None, None, None


def _load_translation_model_objects(
    auto_tokenizer_cls: Any,
    auto_model_cls: Any,
    model_name: str,
    cache_dir: Optional[str],
) -> tuple[Any, Any]:
    """Load the tokenizer and seq2seq model from Hugging Face."""
    from_pretrained_kwargs: Dict[str, object] = {}
    if cache_dir:
        from_pretrained_kwargs["cache_dir"] = cache_dir
    tokenizer = auto_tokenizer_cls.from_pretrained(model_name, **from_pretrained_kwargs)
    model = auto_model_cls.from_pretrained(model_name, **from_pretrained_kwargs)
    return tokenizer, model


def _place_translation_model_on_device(model: Any, device: str) -> Any:
    """Move the translation model to the target device and switch to eval mode."""
    if device == "cuda":
        model = model.to("cuda")
    else:
        model = model.to("cpu")
    model.eval()
    return model


def _load_translation_runtime(
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    debug: bool,
) -> tuple[int, Optional[Any], Optional[Any], Optional[str]]:
    """Load the internal FR<->EN subtitle translation runtime."""
    model_name = _resolve_translation_model_name(
        source_language,
        target_language,
        use_gpu=use_gpu,
    )
    if not model_name:
        print(
            "Subtitle translation is only supported for the following language pairs: "
            + ", ".join(f"{src}->{dst}" for src, dst in sorted(_CPU_TRANSLATION_MODELS.keys()))
        )
        return _TRANSLATION_UNSUPPORTED_PAIR_RC, None, None, None

    torch, auto_model_cls, auto_tokenizer_cls = _import_translation_modules()
    if not torch or not auto_model_cls or not auto_tokenizer_cls:
        return _TRANSLATION_BACKEND_UNAVAILABLE_RC, None, None, model_name

    device = "cuda" if use_gpu else "cpu"
    cache_dir = _prepare_huggingface_models_dir(huggingface_models_dir, debug=debug)
    if debug:
        print(f"Loading subtitle translation model '{model_name}' on {device}")
        if cache_dir:
            print(f"Using Hugging Face translation cache dir: {cache_dir}")

    try:
        tokenizer, model = _load_translation_model_objects(
            auto_tokenizer_cls,
            auto_model_cls,
            model_name,
            cache_dir,
        )
        if device == "cuda":
            try:
                model = _place_translation_model_on_device(model, "cuda")
            except Exception as cuda_error:
                print(
                    "Subtitle translation model failed to start on CUDA; "
                    f"retrying on CPU ({cuda_error})"
                )
                model = _place_translation_model_on_device(model, "cpu")
        else:
            model = _place_translation_model_on_device(model, "cpu")
        return 0, torch, (tokenizer, model), model_name
    except Exception as e:
        print(f"Failed to load subtitle translation model '{model_name}': {e}")
        return _TRANSLATION_BACKEND_UNAVAILABLE_RC, None, None, model_name


def _run_translation_batch(
    texts: list[str],
    *,
    torch: Any,
    tokenizer: Any,
    model: Any,
) -> list[str]:
    """Translate a batch of cue texts while preserving one output per input cue."""
    if not texts:
        return []

    tokenized = tokenizer(
        texts,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    )
    model_device = getattr(model, "device", None)
    if model_device is not None:
        tokenized = {key: value.to(model_device) for key, value in tokenized.items()}

    with torch.inference_mode():
        generated = model.generate(
            **tokenized,
            max_new_tokens=256,
            num_beams=4,
        )

    return [
        " ".join(str(text).split()).strip()
        for text in tokenizer.batch_decode(generated, skip_special_tokens=True)
    ]


def _translate_cue_texts(
    cue_texts: list[str],
    *,
    translate_batch: Callable[[list[str]], list[str]],
    batch_size: int,
) -> list[str]:
    """Translate cue texts in small batches and keep empty outputs safe."""
    translated_texts: list[str] = []
    normalized_batch_size = max(1, int(batch_size))

    for index in range(0, len(cue_texts), normalized_batch_size):
        source_batch = cue_texts[index : index + normalized_batch_size]
        translated_batch = translate_batch(source_batch)
        if len(translated_batch) != len(source_batch):
            raise ValueError("subtitle translation returned a different cue count than requested")

        for source_text, translated_text in zip(source_batch, translated_batch):
            normalized_source_text = _normalize_vtt_cue_text(source_text)
            normalized_translated_text = _normalize_vtt_cue_text(str(translated_text))
            translated_texts.append(normalized_translated_text or normalized_source_text)

    return translated_texts


def _translate_vtt_content(
    content: str,
    *,
    translate_batch: Callable[[list[str]], list[str]],
    max_line_width: int,
    max_line_count: int,
    batch_size: int = _TRANSLATION_BATCH_SIZE,
) -> str:
    """Translate VTT cue texts while preserving timestamps and block structure."""
    parsed_blocks: list[str | _VTT_POSTPROCESS_CUE_BLOCK] = [
        _parse_vtt_postprocess_block(block) for block in (content or "").split("\n\n")
    ]

    cue_block_indexes: list[int] = []
    cue_texts: list[str] = []
    for index, parsed_block in enumerate(parsed_blocks):
        if isinstance(parsed_block, str):
            continue
        _cue_prefix, cue_text = parsed_block
        normalized_cue_text = _normalize_vtt_cue_text(cue_text)
        if not normalized_cue_text:
            continue
        cue_block_indexes.append(index)
        cue_texts.append(normalized_cue_text)

    if cue_texts:
        translated_texts = _translate_cue_texts(
            cue_texts,
            translate_batch=translate_batch,
            batch_size=batch_size,
        )
        for cue_block_index, translated_text in zip(cue_block_indexes, translated_texts):
            parsed_block = parsed_blocks[cue_block_index]
            if isinstance(parsed_block, str):
                raise ValueError("subtitle translation lost cue structure while applying results")
            cue_prefix, _original_text = parsed_block
            parsed_blocks[cue_block_index] = (cue_prefix, translated_text)

    _repair_cross_cue_apostrophe_splits(parsed_blocks)
    processed_blocks = _render_postprocessed_vtt_blocks(
        parsed_blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )
    return "\n\n".join(processed_blocks).rstrip() + "\n"


def _translate_vtt_file(
    vtt_path: Path,
    *,
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> tuple[int, Dict[str, Any]]:
    """Translate a finalized VTT file and keep the original source subtitles as a sidecar.

    This is the preferred path for the dedicated local FR<->EN translation
    pipeline: we start from the already-generated source VTT, translate cue
    texts only, and keep the original timestamps unchanged.
    """
    rc, torch, runtime, model_name = _load_translation_runtime(
        source_language=source_language,
        target_language=target_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        debug=debug,
    )
    translation_metadata = _build_translation_metadata(
        applied=False,
        backend=_TRANSLATION_BACKEND_LOCAL,
        source_language=source_language,
        target_language=target_language,
        model=model_name,
        use_gpu=use_gpu,
    )
    if rc != 0 or torch is None or runtime is None:
        return rc, translation_metadata

    tokenizer, model = runtime
    original_content = vtt_path.read_text(encoding="utf-8")

    try:
        translated_content = _translate_vtt_content(
            original_content,
            translate_batch=lambda batch: _run_translation_batch(
                batch,
                torch=torch,
                tokenizer=tokenizer,
                model=model,
            ),
            max_line_width=max_line_width,
            max_line_count=max_line_count,
        )
    except Exception as e:
        print(f"Subtitle translation failed: {e}")
        return _TRANSLATION_FAILED_RC, translation_metadata

    # Preserve the original source-language subtitles before replacing the main
    # `<stem>.vtt` output with the translated version.
    source_sidecar_path = _build_source_vtt_sidecar_path(vtt_path, source_language)
    source_sidecar_path.write_text(original_content, encoding="utf-8")
    vtt_path.write_text(translated_content, encoding="utf-8")
    translation_metadata = _build_translation_metadata(
        applied=True,
        backend=_TRANSLATION_BACKEND_LOCAL,
        source_language=source_language,
        target_language=target_language,
        model=model_name,
        use_gpu=use_gpu,
        source_sidecar=str(source_sidecar_path.name),
    )

    if debug:
        print(f"Source-language VTT preserved at: {source_sidecar_path}")
        print(
            "Translated VTT written to: "
            f"{vtt_path} (from {source_language} to {target_language})"
        )

    return 0, translation_metadata


def _build_transcription_runtime_metadata(
    *,
    requested_language: str,
    detected_language: Optional[str],
    final_language: Optional[str],
    whisper_model: str,
    use_gpu: bool,
    translation: Dict[str, Any],
) -> Dict[str, Any]:
    """Build stable runtime metadata written to info_video.json."""
    normalized_detected_language = _normalize_language_code(detected_language)
    normalized_requested_language = _normalize_language_code(requested_language)
    normalized_final_language = _normalize_language_code(final_language)
    return {
        "transcription": {
            "whisper_model": map_model_name(whisper_model, "python"),
            "hardware_profile": _translation_hardware_profile(use_gpu),
            "requested_subtitle_language": normalized_requested_language,
            "detected_source_language": normalized_detected_language,
            "final_subtitle_language": normalized_final_language,
            "translation": translation,
        }
    }


def _run_whisper_with_explicit_language(
    audio_src: Path,
    work_dir: Path,
    *,
    language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
) -> tuple[int, str, str]:
    """Run Whisper with an explicit target language for legacy best-effort fallback.

    This preserves the historical runner behavior for non-FR/EN target languages:
    Whisper is asked directly for the requested subtitle language, even though it
    is not a dedicated local text-translation pipeline. The output remains
    best-effort and is intentionally documented as lower-confidence behavior.
    """
    logical_model = str(whisper_fallback_options["model"])
    use_gpu = bool(whisper_fallback_options["use_gpu"])
    rc, _detected_lang = run_whisper_python(
        audio_path=audio_src,
        out_dir=work_dir,
        language=language,
        model=logical_model,
        whisper_models_dir=str(whisper_fallback_options.get("whisper_models_dir", "")),
        use_gpu=use_gpu,
        gpu_device=int(whisper_fallback_options["gpu_device"]),
        vad_filter=bool(whisper_fallback_options["vad_filter"]),
        timeout_sec=int(whisper_fallback_options["timeout_sec"]),
        chunk_duration_sec=int(whisper_fallback_options["chunk_duration_sec"]),
        chunk_overlap_sec=int(whisper_fallback_options["chunk_overlap_sec"]),
        chunk_threshold_sec=int(whisper_fallback_options["chunk_threshold_sec"]),
        vtt_highlight_words=bool(whisper_fallback_options["vtt_highlight_words"]),
        vtt_max_line_count=int(whisper_fallback_options["vtt_max_line_count"]),
        vtt_max_line_width=int(whisper_fallback_options["vtt_max_line_width"]),
        debug=debug,
    )
    effective_model_name = map_model_name(logical_model, "python")
    execution_backend = "whisper_python"
    if rc == 255:
        rc, _detected_lang = run_whisper_cli(
            audio_path=audio_src,
            out_dir=work_dir,
            language=language,
            model=logical_model,
            whisper_models_dir=str(whisper_fallback_options.get("whisper_models_dir", "")),
            use_gpu=use_gpu,
            gpu_device=int(whisper_fallback_options["gpu_device"]),
            vad_filter=bool(whisper_fallback_options["vad_filter"]),
            timeout_sec=int(whisper_fallback_options["timeout_sec"]),
            debug=debug,
        )
        effective_model_name = map_model_name(logical_model, "cli")
        execution_backend = "whisper_cli"
    return rc, execution_backend, effective_model_name


def _run_legacy_whisper_translation_fallback(
    audio_src: Path,
    work_dir: Path,
    *,
    source_language: str,
    target_language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Fallback to the historical Whisper-only multilingual behavior.

    For targets outside the dedicated local FR/EN translation pipeline, we keep
    backward compatibility by re-running Whisper directly with the requested
    subtitle language. This is explicitly a best-effort path: useful for broad
    language coverage, but less predictable than the local Marian translation
    models used for FR/EN.
    """
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    if not expected_vtt.exists():
        print(f"VTT output not found before Whisper fallback translation: {expected_vtt}")
        metadata = _build_translation_metadata(
            applied=False,
            backend=_TRANSLATION_BACKEND_WHISPER_LEGACY,
            source_language=source_language,
            target_language=target_language,
            model=map_model_name(str(whisper_fallback_options["model"]), "python"),
            use_gpu=bool(whisper_fallback_options["use_gpu"]),
            note="best_effort_multilingual_whisper_fallback",
        )
        return 5, metadata, None

    original_content = expected_vtt.read_text(encoding="utf-8")
    # Even on the legacy fallback path we preserve the already-produced source
    # VTT before asking Whisper for a best-effort output in the requested
    # language.
    source_sidecar_path = _build_source_vtt_sidecar_path(expected_vtt, source_language)
    source_sidecar_path.write_text(original_content, encoding="utf-8")
    expected_vtt.unlink(missing_ok=True)

    rc, execution_backend, effective_model_name = _run_whisper_with_explicit_language(
        audio_src,
        work_dir,
        language=target_language,
        whisper_fallback_options=whisper_fallback_options,
        debug=debug,
    )
    translation_metadata = _build_translation_metadata(
        applied=False,
        backend=_TRANSLATION_BACKEND_WHISPER_LEGACY,
        source_language=source_language,
        target_language=target_language,
        model=effective_model_name,
        use_gpu=bool(whisper_fallback_options["use_gpu"]),
        source_sidecar=str(source_sidecar_path.name),
        execution_backend=execution_backend,
        note="best_effort_multilingual_whisper_fallback",
    )
    if rc != 0:
        expected_vtt.write_text(original_content, encoding="utf-8")
        return rc, translation_metadata, source_language

    rc = _finalize_vtt(
        audio_src,
        work_dir,
        max_line_count=int(whisper_fallback_options["vtt_max_line_count"]),
        max_line_width=int(whisper_fallback_options["vtt_max_line_width"]),
        debug=debug,
    )
    if rc != 0:
        expected_vtt.write_text(original_content, encoding="utf-8")
        return rc, translation_metadata, source_language

    translation_metadata["applied"] = True
    return 0, translation_metadata, _normalize_language_code(target_language)


def _check_translation_input_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language: Optional[str],
    use_gpu: bool,
    debug: bool,
) -> tuple[Optional[Path], Optional[tuple[int, Dict[str, Any], Optional[str]]]]:
    """Validate the finalized VTT before the translation decision tree runs."""
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    if not expected_vtt.exists():
        print(f"VTT output not found before translation: {expected_vtt}")
        return None, (
            5,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_LOCAL,
                source_language=_normalize_language_code(detected_language),
                target_language=requested_language,
                model=_resolve_translation_model_name(
                    _normalize_language_code(detected_language),
                    requested_language,
                    use_gpu=use_gpu,
                ),
                use_gpu=use_gpu,
            ),
            None,
        )

    # Music-only or otherwise non-verbal media can legitimately produce an
    # empty VTT. In that case we keep the empty output as-is instead of failing
    # the translation decision just because no spoken source language exists.
    read_ok, has_cues, _last_end_sec = _read_last_vtt_cue_end_seconds(expected_vtt)
    if read_ok and not has_cues:
        if debug:
            print(
                "Generated VTT contains no subtitle cues; "
                "skipping translation for non-verbal audio"
            )
        return expected_vtt, (
            0,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_NONE,
                source_language=None,
                target_language=requested_language,
                model=None,
                use_gpu=use_gpu,
                note="no_speech_or_non_verbal_audio",
            ),
            requested_language,
        )

    return expected_vtt, None


def _maybe_translate_final_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language: Optional[str],
    whisper_fallback_options: Optional[Dict[str, Any]],
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Translate the finalized VTT only when requested and source/target differ.

    Decision tree:
    - same source/target language: keep the source VTT as-is;
    - FR<->EN pair: use the local text-translation pipeline;
    - any other pair: fall back to the historical Whisper multilingual path.
    """
    normalized_requested_language = _normalize_language_code(requested_language)
    if not normalized_requested_language or normalized_requested_language == "auto":
        return (
            0,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_NONE,
                source_language=detected_language,
                target_language=detected_language,
                model=None,
                use_gpu=use_gpu,
            ),
            _normalize_language_code(detected_language),
        )

    expected_vtt, preflight_response = _check_translation_input_vtt(
        audio_src,
        work_dir,
        requested_language=normalized_requested_language,
        detected_language=detected_language,
        use_gpu=use_gpu,
        debug=debug,
    )
    if preflight_response is not None:
        return preflight_response
    if expected_vtt is None:
        return (
            5,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_LOCAL,
                source_language=_normalize_language_code(detected_language),
                target_language=normalized_requested_language,
                model=_resolve_translation_model_name(
                    _normalize_language_code(detected_language),
                    normalized_requested_language,
                    use_gpu=use_gpu,
                ),
                use_gpu=use_gpu,
            ),
            None,
        )

    normalized_detected_language = _normalize_language_code(detected_language)
    if not normalized_detected_language:
        print(
            "Subtitle translation decision failed: Whisper could not determine the source "
            "language while a target subtitle language was explicitly requested"
        )
        return (
            _TRANSLATION_DECISION_FAILED_RC,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_NONE,
                source_language=None,
                target_language=normalized_requested_language,
                model=None,
                use_gpu=use_gpu,
            ),
            None,
        )

    if normalized_detected_language == normalized_requested_language:
        if debug:
            print(
                "Detected source language matches the requested subtitle language; "
                "translation skipped"
            )
        return (
            0,
            _build_translation_metadata(
                applied=False,
                backend=_TRANSLATION_BACKEND_NONE,
                source_language=normalized_detected_language,
                target_language=normalized_requested_language,
                model=None,
                use_gpu=use_gpu,
            ),
            normalized_detected_language,
        )

    # Dedicated local translation is intentionally limited to the pairs we trust
    # and validate explicitly. Everything else falls back to the historical
    # Whisper behavior for compatibility.
    local_translation_model = _resolve_translation_model_name(
        normalized_detected_language,
        normalized_requested_language,
        use_gpu=use_gpu,
    )
    if local_translation_model is None:
        if debug:
            print(
                "Local subtitle translation is not available for "
                f"{normalized_detected_language}->{normalized_requested_language}; "
                "falling back to legacy Whisper multilingual output"
            )
        if whisper_fallback_options is None:
            return (
                _TRANSLATION_UNSUPPORTED_PAIR_RC,
                _build_translation_metadata(
                    applied=False,
                    backend=_TRANSLATION_BACKEND_WHISPER_LEGACY,
                    source_language=normalized_detected_language,
                    target_language=normalized_requested_language,
                    model=None,
                    use_gpu=use_gpu,
                    note="legacy_whisper_fallback_not_configured",
                ),
                None,
            )
        return _run_legacy_whisper_translation_fallback(
            audio_src,
            work_dir,
            source_language=normalized_detected_language,
            target_language=normalized_requested_language,
            whisper_fallback_options=whisper_fallback_options,
            debug=debug,
        )

    rc, translation_metadata = _translate_vtt_file(
        expected_vtt,
        source_language=normalized_detected_language,
        target_language=normalized_requested_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
    )
    final_language = normalized_requested_language if rc == 0 else normalized_detected_language
    return rc, translation_metadata, final_language


def _load_whisper_runtime_model(
    torch: Any,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    debug: bool,
) -> tuple[int, Optional[Any], str]:
    """Load the Whisper model and transparently fall back to CPU if needed."""
    model_name = map_model_name(model, "python")
    device = "cuda" if use_gpu else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    if debug:
        print(f"Loading whisper model '{model_name}' on {device} (dtype={dtype})")

    wmodel = _load_whisper_model(model_name, device, whisper_models_dir=whisper_models_dir)
    if wmodel is None and device == "cuda":
        print("Retrying whisper model load on CPU")
        device = "cpu"
        wmodel = _load_whisper_model(model_name, device, whisper_models_dir=whisper_models_dir)

    if wmodel is None:
        return 10, None, device
    return 0, wmodel, device


def _prepare_transcription_plan(
    audio_path: Path,
    language: str,
    vad_filter: bool,
    device: str,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    debug: bool,
) -> tuple[float, list[tuple[float, float]], Dict[str, object]]:
    """Build the chunk plan and matching Whisper kwargs for this audio file."""
    input_duration_sec = _probe_duration_seconds(audio_path, debug=debug)
    chunk_plan = _plan_audio_chunks(
        total_duration_sec=input_duration_sec,
        chunk_duration_sec=int(chunk_duration_sec),
        chunk_threshold_sec=int(chunk_threshold_sec),
        chunk_overlap_sec=int(chunk_overlap_sec),
    )
    transcribe_kwargs = _build_transcribe_kwargs(
        language,
        vad_filter,
        device,
        chunked=(len(chunk_plan) > 1),
    )
    if debug:
        print(f"Transcribing: {audio_path} with args {transcribe_kwargs}")
    return input_duration_sec, chunk_plan, transcribe_kwargs


def _build_chunk_transcribe_kwargs(
    transcribe_kwargs: Dict[str, object],
    detected_language: Optional[str],
    explicit_language: bool,
    previous_chunk_text: str,
) -> Dict[str, object]:
    """Apply chunk-to-chunk context before transcribing the next chunk."""
    chunk_kwargs = dict(transcribe_kwargs)
    if detected_language and not explicit_language:
        chunk_kwargs["language"] = detected_language

    initial_prompt = _build_initial_prompt_from_text(previous_chunk_text)
    if initial_prompt:
        chunk_kwargs["initial_prompt"] = initial_prompt
    return chunk_kwargs


def _transcribe_one_audio_chunk(
    wmodel: Any,
    audio_path: Path,
    chunk_dir: Path,
    chunk_index: int,
    chunk_count: int,
    start_sec: float,
    duration_sec: float,
    timeout_sec: int,
    transcribe_kwargs: Dict[str, object],
    detected_language: Optional[str],
    explicit_language: bool,
    previous_chunk_text: str,
    language: str,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str], str]:
    """Extract, transcribe, and post-process one chunk of the input audio."""
    chunk_path = chunk_dir / f"{audio_path.stem}.chunk_{chunk_index:04d}.mp3"
    rc = _extract_audio_chunk(
        audio_path=audio_path,
        chunk_path=chunk_path,
        start_sec=start_sec,
        duration_sec=duration_sec,
        timeout_sec=timeout_sec,
        debug=debug,
    )
    if rc != 0:
        print(f"Whisper chunk extraction failed (chunk {chunk_index + 1}/{chunk_count}, rc={rc})")
        return 22, None, detected_language, previous_chunk_text

    chunk_kwargs = _build_chunk_transcribe_kwargs(
        transcribe_kwargs=transcribe_kwargs,
        detected_language=detected_language,
        explicit_language=explicit_language,
        previous_chunk_text=previous_chunk_text,
    )
    if debug:
        print(
            "Transcribing chunk "
            f"{chunk_index + 1}/{chunk_count}: start={start_sec:.3f}s "
            f"duration={duration_sec:.3f}s"
        )

    chunk_result = _transcribe_audio(wmodel, chunk_path, chunk_kwargs)
    try:
        chunk_path.unlink(missing_ok=True)
    except Exception:
        pass

    if chunk_result is None:
        return 20, None, detected_language, previous_chunk_text

    chunk_expected_language = language if explicit_language else detected_language
    filtered_result = _filter_result_segments(
        chunk_result,
        expected_language=chunk_expected_language,
        debug=debug,
    )
    resolved_language = detected_language or _extract_detected_language(filtered_result)
    next_chunk_text = str(filtered_result.get("text", "")).strip()
    return 0, filtered_result, resolved_language, next_chunk_text


def _run_chunked_whisper_transcription(
    wmodel: Any,
    audio_path: Path,
    out_dir: Path,
    chunk_plan: list[tuple[float, float]],
    input_duration_sec: float,
    transcribe_kwargs: Dict[str, object],
    language: str,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Run Whisper on overlapping chunks and merge the filtered results."""
    if debug:
        print(
            "Using chunked transcription: "
            f"{len(chunk_plan)} chunks, duration={input_duration_sec:.3f}s, "
            f"chunk_duration={int(chunk_duration_sec)}s, "
            f"chunk_overlap={_normalize_chunk_overlap_seconds(int(chunk_duration_sec), int(chunk_overlap_sec))}s"
        )

    merged_results: list[tuple[float, Dict[str, object]]] = []
    keep_windows = [_compute_chunk_keep_window(chunk_plan, i) for i in range(len(chunk_plan))]
    detected_language: Optional[str] = None
    explicit_language = bool(language and language.lower() != "auto")
    previous_chunk_text = ""
    chunk_dir = out_dir / "_chunks"

    for chunk_index, (start_sec, duration_sec) in enumerate(chunk_plan):
        rc, chunk_result, detected_language, previous_chunk_text = _transcribe_one_audio_chunk(
            wmodel=wmodel,
            audio_path=audio_path,
            chunk_dir=chunk_dir,
            chunk_index=chunk_index,
            chunk_count=len(chunk_plan),
            start_sec=start_sec,
            duration_sec=duration_sec,
            timeout_sec=timeout_sec,
            transcribe_kwargs=transcribe_kwargs,
            detected_language=detected_language,
            explicit_language=explicit_language,
            previous_chunk_text=previous_chunk_text,
            language=language,
            debug=debug,
        )
        if rc != 0:
            return rc, None, detected_language

        assert chunk_result is not None
        merged_results.append((start_sec, chunk_result))

    merged_result = _combine_chunk_results(merged_results, keep_windows=keep_windows)
    return 0, merged_result, detected_language


def _run_whisper_python_transcription(
    wmodel: Any,
    audio_path: Path,
    out_dir: Path,
    language: str,
    vad_filter: bool,
    device: str,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
    debug: bool,
) -> tuple[int, Optional[Dict[str, object]], Optional[str]]:
    """Transcribe the file either in one pass or chunk-by-chunk."""
    input_duration_sec, chunk_plan, transcribe_kwargs = _prepare_transcription_plan(
        audio_path=audio_path,
        language=language,
        vad_filter=vad_filter,
        device=device,
        chunk_duration_sec=chunk_duration_sec,
        chunk_overlap_sec=chunk_overlap_sec,
        chunk_threshold_sec=chunk_threshold_sec,
        debug=debug,
    )

    if len(chunk_plan) <= 1:
        result = _transcribe_audio(wmodel, audio_path, transcribe_kwargs)
        if result is None:
            return 20, None, None
        return 0, result, _extract_detected_language(result)

    return _run_chunked_whisper_transcription(
        wmodel=wmodel,
        audio_path=audio_path,
        out_dir=out_dir,
        chunk_plan=chunk_plan,
        input_duration_sec=input_duration_sec,
        transcribe_kwargs=transcribe_kwargs,
        language=language,
        timeout_sec=timeout_sec,
        chunk_duration_sec=chunk_duration_sec,
        chunk_overlap_sec=chunk_overlap_sec,
        debug=debug,
    )


def run_whisper_python(
    audio_path: Path,
    out_dir: Path,
    language: str,
    model: str,
    whisper_models_dir: Optional[str],
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    chunk_duration_sec: int,
    chunk_overlap_sec: int,
    chunk_threshold_sec: int,
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
    rc, wmodel, device = _load_whisper_runtime_model(
        torch=torch,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        debug=debug,
    )
    if rc != 0 or wmodel is None:
        return 10, None

    rc, result, detected_code = _run_whisper_python_transcription(
        wmodel=wmodel,
        audio_path=audio_path,
        out_dir=out_dir,
        language=language,
        vad_filter=vad_filter,
        device=device,
        timeout_sec=timeout_sec,
        chunk_duration_sec=chunk_duration_sec,
        chunk_overlap_sec=chunk_overlap_sec,
        chunk_threshold_sec=chunk_threshold_sec,
        debug=debug,
    )
    if rc != 0:
        return rc, detected_code
    if result is None:
        return 20, detected_code

    expected_language = (
        _normalize_language_code(language)
        if language and language.lower() != "auto"
        else _normalize_language_code(detected_code)
    )
    result = _filter_result_segments(
        result,
        expected_language=expected_language,
        debug=debug,
    )
    detected_code = _normalize_language_code(_extract_detected_language(result) or detected_code)
    word_options: Dict[str, object] = {
        "highlight_words": bool(vtt_highlight_words),
        "max_line_count": int(vtt_max_line_count),
        "max_line_width": int(vtt_max_line_width),
    }
    if not _write_vtt_result(result, audio_path, out_dir, get_writer, word_options, debug):
        return 21, detected_code

    return 0, detected_code


def _probe_duration_seconds(path: Path, debug: bool = False) -> float:
    """Probe the media duration in seconds with ffprobe."""
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
    """Compute a runtime timeout from media duration and CLI settings."""
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
    """Prepare the audio source file used for transcription."""
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
    args: argparse.Namespace,
    audio_src: Path,
    work_dir: Path,
    timeout_sec: int,
    effective_use_gpu: bool,
    debug: bool,
) -> tuple[int, Optional[str]]:
    """Run source-language transcription via the Python API first, then CLI."""
    transcription_language = _resolve_transcription_language(args.language)
    chunk_threshold_sec = _resolve_chunk_threshold_seconds(
        configured_value=args.chunk_threshold_seconds,
        use_gpu=effective_use_gpu,
    )

    rc, detected_lang = run_whisper_python(
        audio_path=audio_src,
        out_dir=work_dir,
        language=transcription_language,
        model=args.model,
        whisper_models_dir=str(args.whisper_models_dir),
        use_gpu=effective_use_gpu,
        gpu_device=int(args.gpu_device),
        vad_filter=(args.vad_filter == "true"),
        timeout_sec=timeout_sec,
        chunk_duration_sec=int(args.chunk_duration_seconds),
        chunk_overlap_sec=int(args.chunk_overlap_seconds),
        chunk_threshold_sec=chunk_threshold_sec,
        vtt_highlight_words=(str(args.vtt_highlight_words).lower() == "true"),
        vtt_max_line_count=int(args.vtt_max_line_count),
        vtt_max_line_width=int(args.vtt_max_line_width),
        debug=debug,
    )
    if rc == 255:
        rc, detected_lang = run_whisper_cli(
            audio_path=audio_src,
            out_dir=work_dir,
            language=transcription_language,
            model=args.model,
            whisper_models_dir=str(args.whisper_models_dir),
            use_gpu=effective_use_gpu,
            gpu_device=int(args.gpu_device),
            vad_filter=(args.vad_filter == "true"),
            timeout_sec=timeout_sec,
            debug=debug,
        )
    if rc != 0:
        print(f"whisper CLI failed with return code {rc}")
    return rc, _normalize_language_code(detected_lang)


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
    """Locate the generated VTT file for a Whisper output stem."""
    for stem in _build_vtt_stem_candidates(audio_src):
        candidate = work_dir / f"{stem}.vtt"
        if candidate.exists():
            return candidate
    return None


def _finalize_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    max_line_count: int = 2,
    max_line_width: int = 40,
    debug: bool = False,
) -> int:
    """Rename the generated VTT file to the expected final output name."""
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    generated_vtt = _find_generated_vtt(audio_src, work_dir)
    try:
        if generated_vtt is None:
            print("VTT output not found after whisper execution")
            return 5

        if generated_vtt != expected_vtt:
            generated_vtt.replace(expected_vtt)

        _postprocess_vtt_file(
            expected_vtt,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            debug=debug,
        )
        print(f"VTT written to: {expected_vtt}")
        return 0
    except Exception as e:
        print(f"Failed to finalize VTT: {e}")
        return 6


def _parse_vtt_timestamp(raw: str) -> Optional[float]:
    """Parse a WebVTT timestamp into seconds."""
    value = raw.strip()
    if not value:
        return None

    # WebVTT commonly uses either MM:SS.mmm or HH:MM:SS.mmm.
    # Some tools also emit commas as decimal markers.
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        hours = 0
        minutes_part, seconds_part = parts
    elif len(parts) == 3:
        hours_part, minutes_part, seconds_part = parts
        try:
            hours = int(hours_part)
        except ValueError:
            return None
    else:
        return None

    if "." not in seconds_part:
        return None

    seconds_whole, millis_part = seconds_part.split(".", 1)
    try:
        minutes = int(minutes_part)
        seconds = int(seconds_whole)
        millis = int(millis_part[:3].ljust(3, "0"))
    except ValueError:
        return None

    return float(hours * 3600 + minutes * 60 + seconds) + (millis / 1000.0)


def _read_last_vtt_cue_end_seconds(vtt_path: Path) -> tuple[bool, bool, Optional[float]]:
    """Inspect a WebVTT file and return read status, cue presence, and last cue end."""
    saw_cue = False
    last_end_sec: Optional[float] = None
    try:
        for line in vtt_path.read_text(encoding="utf-8").splitlines():
            if "-->" not in line:
                continue
            saw_cue = True
            try:
                raw_end = line.split("-->", 1)[1].strip().split()[0]
            except Exception:
                continue
            parsed = _parse_vtt_timestamp(raw_end)
            if parsed is not None:
                last_end_sec = parsed
    except Exception:
        return False, False, None
    return True, saw_cue, last_end_sec


def _validate_vtt_coverage(
    vtt_path: Path,
    reference_duration_sec: float,
    min_coverage_ratio: float,
    max_final_gap_sec: float,
    debug: bool,
) -> int:
    """Fail when the generated VTT is clearly truncated versus the source duration."""
    if reference_duration_sec <= 0:
        return 0

    read_ok, has_cues, last_end_sec = _read_last_vtt_cue_end_seconds(vtt_path)
    if not read_ok:
        print("VTT coverage validation failed: unable to read the generated VTT")
        return 7
    if not has_cues:
        if debug:
            print(
                "VTT coverage: generated VTT contains no subtitle cues; "
                "treating it as a valid no-speech result"
            )
        return 0
    if last_end_sec is None or last_end_sec <= 0:
        print("VTT coverage validation failed: unable to read the last subtitle cue")
        return 7

    coverage_ratio = last_end_sec / float(reference_duration_sec)
    final_gap_sec = max(0.0, float(reference_duration_sec) - last_end_sec)
    if debug:
        print(
            "VTT coverage: "
            f"last_end={last_end_sec:.3f}s, duration={reference_duration_sec:.3f}s, "
            f"coverage_ratio={coverage_ratio:.3f}, final_gap={final_gap_sec:.3f}s"
        )

    if coverage_ratio < min_coverage_ratio and final_gap_sec > max_final_gap_sec:
        print(
            "VTT coverage validation failed: output appears truncated "
            f"(last cue at {last_end_sec:.3f}s for duration {reference_duration_sec:.3f}s, "
            f"coverage={coverage_ratio:.3f}, gap={final_gap_sec:.3f}s)"
        )
        return 7

    return 0


def main() -> int:
    """Run the transcription script end to end and return an exit code."""
    args = parse_args()

    base_dir = Path(args.base_dir)
    input_path = base_dir / args.input_file
    work_dir = base_dir / args.work_dir
    debug = str(args.debug).lower() in ("true", "1", "yes")
    video_identification = _extract_video_identification(args)

    if not input_path.exists():
        print(f"Input file not found: {input_path}")
        return 2

    timeout_sec = _compute_timeout(args, input_path, debug)
    rc, audio_src = _prepare_audio_source(args, input_path, work_dir, timeout_sec, debug)
    if rc != 0 or audio_src is None:
        return rc

    effective_use_gpu = _resolve_effective_use_gpu(args.use_gpu == "true", debug)
    rc, detected_language = _run_transcription(
        args,
        audio_src,
        work_dir,
        timeout_sec,
        effective_use_gpu,
        debug,
    )
    if rc != 0:
        return rc

    rc = _finalize_vtt(
        audio_src,
        work_dir,
        max_line_count=int(args.vtt_max_line_count),
        max_line_width=int(args.vtt_max_line_width),
        debug=debug,
    )
    if rc != 0:
        return rc

    whisper_fallback_options = {
        "model": args.model,
        "whisper_models_dir": str(args.whisper_models_dir),
        "use_gpu": effective_use_gpu,
        "gpu_device": int(args.gpu_device),
        "vad_filter": args.vad_filter == "true",
        "timeout_sec": timeout_sec,
        "chunk_duration_sec": int(args.chunk_duration_seconds),
        "chunk_overlap_sec": int(args.chunk_overlap_seconds),
        "chunk_threshold_sec": _resolve_chunk_threshold_seconds(
            configured_value=args.chunk_threshold_seconds,
            use_gpu=effective_use_gpu,
        ),
        "vtt_highlight_words": str(args.vtt_highlight_words).lower() == "true",
        "vtt_max_line_count": int(args.vtt_max_line_count),
        "vtt_max_line_width": int(args.vtt_max_line_width),
    }
    rc, translation_metadata, final_subtitle_language = _maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language=args.language,
        detected_language=detected_language,
        whisper_fallback_options=whisper_fallback_options,
        use_gpu=effective_use_gpu,
        huggingface_models_dir=args.huggingface_models_dir,
        max_line_count=int(args.vtt_max_line_count),
        max_line_width=int(args.vtt_max_line_width),
        debug=debug,
    )
    if rc != 0:
        return rc

    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    reference_duration_sec = _probe_duration_seconds(audio_src, debug=debug)
    if reference_duration_sec <= 0:
        reference_duration_sec = _probe_duration_seconds(input_path, debug=debug)

    # Keep the coverage validation thresholds internal. They are a defensive
    # quality gate against silently truncated subtitles, not an operator-facing
    # tuning surface.
    rc = _validate_vtt_coverage(
        vtt_path=expected_vtt,
        reference_duration_sec=reference_duration_sec,
        min_coverage_ratio=_MIN_VTT_COVERAGE_RATIO,
        max_final_gap_sec=_MAX_VTT_FINAL_GAP_SECONDS,
        debug=debug,
    )
    if rc != 0:
        return rc

    task_metadata = dict(video_identification)
    task_metadata.update(
        _build_transcription_runtime_metadata(
            requested_language=args.language,
            detected_language=detected_language,
            final_language=final_subtitle_language,
            whisper_model=args.model,
            use_gpu=effective_use_gpu,
            translation=translation_metadata,
        )
    )
    _write_info_video_metadata(work_dir, task_metadata, debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
