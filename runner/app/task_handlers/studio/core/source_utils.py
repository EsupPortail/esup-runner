"""Source/media-type helpers used by studio pipeline selection."""

from __future__ import annotations

import os
import urllib.parse
from typing import Callable

WEBM_EXTENSIONS = {".webm"}
WEBM_VIDEO_CODECS = {"vp8", "vp9", "av1"}


def looks_like_webm_source(path_or_url: str | None) -> bool:
    """Return whether a source path/URL looks like a WebM media file."""
    if not path_or_url:
        return False
    parsed = urllib.parse.urlparse(path_or_url)
    path = parsed.path if parsed.scheme else path_or_url
    ext = os.path.splitext(str(path))[1].lower()
    return ext in WEBM_EXTENSIONS


def is_webm_input_source(
    source: str | None,
    *,
    looks_like_webm_source_fn: Callable[[str | None], bool] = looks_like_webm_source,
    probe_codec_fn: Callable[[str], str],
) -> bool:
    """Return whether a local/remote source should be treated as WebM input."""
    if looks_like_webm_source_fn(source):
        return True
    if not source:
        return False
    codec = (probe_codec_fn(source) or "").strip().lower()
    return codec in WEBM_VIDEO_CODECS
