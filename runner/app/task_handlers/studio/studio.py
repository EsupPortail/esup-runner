#!/usr/bin/env python3
"""Standalone studio entrypoint for base MP4 generation from mediapackage XML.

This file stays intentionally thin: it parses CLI arguments, then delegates
the end-to-end workflow to the runtime helpers in `core/`.

High-level workflow executed by the delegated runtime:
1. Resolve `base_dir`/`work_dir`/`output_file` and create the task workspace.
2. Fetch and parse the mediapackage XML to resolve:
   - presentation/presenter source URLs;
   - effective presenter layout (`mid`, `piph`, `pipb`);
   - optional SMIL cutting URL.
3. Load optional clip times from SMIL (`clipBegin`/`clipEnd`) when available.
4. Materialize remote HTTP(S) media sources into the workspace with download
   guardrails (allowlist and optional private-network restrictions).
5. Probe source streams/codecs/dimensions and detect WebM-family inputs.
6. Build the studio composition pipeline with ordered fallback:
   - full GPU decode+filter+encode path when CUVID/NVENC and filters are available;
   - GPU encode-only path (CPU decode/filter + NVENC);
   - full CPU path as final fallback.
7. Apply optional clip seek window and encode to CFR MP4 with AAC audio.
8. Return an exit status consumed by the studio handler before downstream encoding.

Usage example:
    python studio.py \
        --xml-url https://example.org/mediapackage.xml \
        --base-dir /tmp/work --work-dir output --output-file studio_base.mp4 \
        --encoding-type GPU --presenter piph
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in {None, ""}:  # pragma: no cover - exercised by subprocess regression tests
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.task_handlers.studio.core import main, parse_args  # noqa: E402

__all__ = ["main", "parse_args"]

if __name__ == "__main__":
    raise SystemExit(main())
