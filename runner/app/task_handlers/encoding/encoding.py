#!/usr/bin/env python3
"""Standalone encoding entrypoint for FFmpeg-based delivery assets.

This file stays intentionally thin: it parses CLI arguments, then delegates
the end-to-end workflow to the runtime helpers in `core/`.

High-level workflow executed by the delegated runtime:
1. Resolve `base_dir`/`input_file`/`work_dir` and fail fast if input is missing.
2. Apply runtime configuration from CLI flags:
   - CPU/GPU mode and CUDA hints;
   - rendition ladder overrides (`rendition`);
   - optional `cut`, `dressing`, and video identification metadata.
3. Normalize and sanitize the input filename in the task workspace.
4. Optionally apply dressing transformations before final encode:
   - cut-on-main when credits are present;
   - optional watermark render;
   - optional opening/ending credits concatenation.
5. Probe media streams/duration/fps and validate that source media is readable.
6. Compute effective working duration (including cut handling) and persist
   source metadata to `info_video.json`.
7. Launch encoding jobs:
   - video renditions (HLS + optional MP4 per rendition);
   - audio derivatives (MP3 and optional M4A for audio-only sources);
   - static thumbnails and overview artifacts (sprite + VTT).
8. Use GPU paths when requested and available, with NVENC preflight and
   per-format fallback to CPU when needed.
9. Append runtime logs to `encoding.log` and finalize `encode_result` metadata.

Usage example:
    python encoding.py \
        --encoding-type CPU \
        --base-dir /tmp/work --input-file input.mp4 --work-dir output \
        --rendition '{"720":{"encode_mp4":true}}'
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
