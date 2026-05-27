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
