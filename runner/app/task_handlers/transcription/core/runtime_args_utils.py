"""Runtime constants and CLI argument parsing for transcription script.

Defines guardrail thresholds that protect subtitle quality and runtime stability.
Exposes CLI options controlling transcription, chunking, validation, and translation.
Keeps operational defaults explicit so behavior is reproducible across environments.
"""

import argparse
import os
from pathlib import Path
from typing import Optional

# Internal safety thresholds for the final VTT validation.
# These are intentionally kept in code instead of the public runner configuration:
# they protect against silently truncated subtitles, and loosening them is rarely
# something an operator should tune per deployment.
_MIN_VTT_COVERAGE_RATIO = 0.75
_MAX_VTT_FINAL_GAP_SECONDS = 300.0
# Guardrail for suspicious "holes" inside the subtitle timeline.
# This catches internal jumps that are too large to be considered normal
# subtitle pacing for spoken content and should trigger operator attention.
_MAX_VTT_INTERNAL_GAP_SECONDS = 15.0
_MAX_VTT_INTERNAL_GAP_COUNT = 0
# Best-effort repair knobs for suspicious internal gaps. They stay conservative:
# this is a non-blocking quality improvement pass, not a full second transcription.
_MAX_INTERNAL_GAP_REPAIR_ATTEMPTS = 3
_INTERNAL_GAP_REPAIR_CONTEXT_PADDING_SECONDS = 1.0
_INTERNAL_GAP_REPAIR_CUE_OVERLAP_TOLERANCE_SECONDS = 0.4
_INTERNAL_GAP_REPAIR_MIN_WINDOW_SECONDS = 2.0

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
_DEFAULT_CACHE_DIR = "/home/esup-runner/.cache/esup-runner"
_DEFAULT_WHISPER_MODELS_DIR = f"{_DEFAULT_CACHE_DIR}/whisper-models"
_DEFAULT_HUGGINGFACE_MODELS_DIR = f"{_DEFAULT_CACHE_DIR}/huggingface"

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


def _resolve_default_cache_subdir(subdir: str) -> str:
    """Resolve a default cache subdirectory from CACHE_DIR or the built-in root."""
    cache_dir = os.getenv("CACHE_DIR", _DEFAULT_CACHE_DIR)
    return str((Path(cache_dir).expanduser() / subdir))


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
        "--source-language",
        default="auto",
        help="Spoken source language code or 'auto' to let Whisper detect it",
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
        default=os.getenv("WHISPER_MODELS_DIR", _resolve_default_cache_subdir("whisper-models")),
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
        default=os.getenv("HUGGINGFACE_MODELS_DIR", _resolve_default_cache_subdir("huggingface")),
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
