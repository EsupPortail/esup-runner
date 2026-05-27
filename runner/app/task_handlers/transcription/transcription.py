#!/usr/bin/env python3
"""Standalone transcription entrypoint for VTT subtitle generation.

This file stays intentionally thin: it parses CLI arguments, then delegates
the end-to-end workflow to the runtime helpers in `core/`.

High-level workflow executed by the delegated runtime:
1. Resolve `base_dir`/`input_file`/`work_dir` and fail fast if the input is missing.
2. Probe media duration and compute a timeout budget from CLI guardrails.
3. Prepare source audio:
   - keep the input as-is when it is already MP3;
   - otherwise extract MP3 (mono/16kHz by default) with ffmpeg;
   - optionally run ffmpeg-normalize when normalization is requested.
4. Run source transcription in Whisper auto-detect mode:
   - prefer Whisper Python API;
   - fall back to Whisper CLI when Python runtime is unavailable;
   - apply chunking strategy for long audio based on CPU/GPU thresholds.
5. Finalize subtitles by locating the generated VTT, renaming to `<stem>.vtt`,
   and post-processing cue text/line wrapping.
6. Run a non-blocking internal-gap repair pass (best effort) that can re-transcribe
   targeted windows and merge repaired cues back into the VTT timeline.
7. Decide whether translation is needed:
   - skip when requested language is `auto`, matches detected language, or no cues exist;
   - use local FR<->EN translation models when available;
   - otherwise use legacy Whisper multilingual fallback and preserve the pre-translation
     source VTT as `<stem>.source-<lang>.webvtt.txt`.
8. Validate final output quality (coverage and final/internal gap guardrails), while
   keeping internal-gap warnings non-blocking for delivery.
9. Write `info_video.json` with runtime metadata (detected/final language, translation
   backend, sidecar name when present, and internal-gap analysis/repair details).

Usage example:
    python transcription.py \
        --base-dir /tmp/work --input-file input.mp4 --work-dir output \
        --language auto --model small \
        --format vtt --use-gpu false
"""

import importlib
import sys
from argparse import Namespace
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import cast

_SCRIPT_DIR = Path(__file__).resolve().parent
_CORE_DIR = _SCRIPT_DIR / "core"


def _evict_mismatched_core_package() -> None:
    """Drop stale `core*` modules when they point to a different task handler."""
    loaded_core = sys.modules.get("core")
    if loaded_core is None:
        return

    loaded_core_file = getattr(loaded_core, "__file__", "")
    loaded_core_dir = Path(loaded_core_file).resolve().parent if loaded_core_file else None
    if loaded_core_dir == _CORE_DIR:
        return

    for module_name in list(sys.modules):
        if module_name == "core" or module_name.startswith("core."):
            sys.modules.pop(module_name, None)


def _load_core_module() -> ModuleType:
    if __package__:
        return importlib.import_module(".core", __package__)

    if str(_SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPT_DIR))
    _evict_mismatched_core_package()
    return importlib.import_module("core")


_core_module = _load_core_module()
main = cast(Callable[[], int], getattr(_core_module, "main"))
parse_args = cast(Callable[..., Namespace], getattr(_core_module, "parse_args"))
__all__ = ["main", "parse_args"]

if __name__ == "__main__":
    raise SystemExit(main())
