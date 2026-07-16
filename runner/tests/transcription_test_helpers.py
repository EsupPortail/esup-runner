"""Shared helpers for functional transcription core tests."""

import importlib
import sys
import types
from pathlib import Path
from typing import Any


def transcription_core_dir() -> Path:
    """Return the transcription core utilities directory."""
    return Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "transcription" / "core"


def load_transcription_core_module(
    module_name: str,
    *,
    force_insert_branch: bool = False,
):
    """Import and reload a transcription core module."""
    core_dir = str(transcription_core_dir())
    if force_insert_branch:
        while core_dir in sys.path:
            sys.path.remove(core_dir)

    module = importlib.import_module(f"app.task_handlers.transcription.core.{module_name}")
    return importlib.reload(module)


def make_transcription_args(base_dir: Path, **overrides: Any) -> types.SimpleNamespace:
    """Build common transcription arguments for tests."""
    defaults = {
        "base_dir": str(base_dir),
        "input_file": "input.mp4",
        "work_dir": "work",
        "debug": "false",
        "language": "fr",
        "source_language": "auto",
        "model": "small",
        "whisper_models_dir": "",
        "use_gpu": "false",
        "gpu_device": 0,
        "vad_filter": "false",
        "chunk_duration_seconds": 30,
        "chunk_overlap_seconds": 2,
        "chunk_threshold_seconds": 60,
        "vtt_highlight_words": "false",
        "vtt_max_line_count": 2,
        "vtt_max_line_width": 40,
        "huggingface_models_dir": "",
        "timeout_factor": "8.0",
        "min_timeout": "60",
        "sample_rate": 16000,
        "downmix_mono": "true",
        "audio_stream_index": 0,
        "normalize": "false",
        "normalize_target_level": "-16.0",
        "video_id": "",
        "video_slug": "",
        "video_title": "",
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)
