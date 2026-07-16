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

import sys
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover - exercised by subprocess regression tests
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.task_handlers.encoding.core import main, parse_args  # noqa: E402

__all__ = ["main", "parse_args"]

if __name__ == "__main__":
    raise SystemExit(main())
