"""Runtime helpers for studio metadata loading (HTTP + parse orchestration)."""

from __future__ import annotations

import argparse
from typing import Callable

from . import metadata_utils


def fetch_text(url: str) -> str:
    """Fetch and return text content from a URL."""
    import requests  # type: ignore[import-untyped]

    response = requests.get(url, timeout=(10, 180))
    response.raise_for_status()
    return str(response.text)


def load_mediapackage_and_layout(
    args: argparse.Namespace,
    *,
    fetch_text_fn: Callable[[str], str] = fetch_text,
    parse_mediapackage_fn: Callable[[str], tuple[str | None, str | None, str, str | None]] = (
        metadata_utils.parse_mediapackage
    ),
) -> tuple[str | None, str | None, str, str | None]:
    """Load mediapackage metadata and resolve the effective presenter layout."""
    xml_text = fetch_text_fn(args.xml_url)
    pres_url, pers_url, presenter_layout, smil_url = parse_mediapackage_fn(xml_text)
    if args.presenter:
        presenter_layout = args.presenter
    return pres_url, pers_url, presenter_layout, smil_url


def load_clip_times(
    smil_url: str | None,
    *,
    fetch_text_fn: Callable[[str], str] = fetch_text,
    parse_smil_cut_fn: Callable[[str], tuple[float | None, float | None]] = (
        metadata_utils.parse_smil_cut
    ),
) -> tuple[float | None, float | None]:
    """Load optional clip begin and end times from a SMIL URL."""
    if not smil_url:
        return None, None
    try:
        smil_text = fetch_text_fn(smil_url)
        return parse_smil_cut_fn(smil_text)
    except Exception:
        return None, None
