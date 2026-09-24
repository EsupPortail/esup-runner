"""Bound media downloads by received bytes and publish complete files only."""

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from app.core.config import config


class DownloadSizeExceeded(ValueError):
    """A download exceeded the configured size; retrying cannot fix it."""


def validate_download_size(size: int) -> None:
    """Apply the media size limit, keeping zero as unlimited."""
    if config.MAX_VIDEO_SIZE_GB > 0 and size > config.MAX_VIDEO_SIZE_GB * 1024**3:
        raise DownloadSizeExceeded(
            f"Downloaded data exceeds the maximum allowed size of {config.MAX_VIDEO_SIZE_GB} GB."
        )


def limited_download_chunks(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Reject an oversized body before writing the chunk crossing the limit."""
    received = 0
    for chunk in chunks:
        received += len(chunk)
        validate_download_size(received)
        yield chunk


def save_download(chunks: Iterable[bytes], destination: str) -> None:
    """Stream into a temporary file, then publish it atomically for cache reuse."""
    part_path = Path(destination + ".part")
    try:
        with part_path.open("wb") as output:
            for chunk in limited_download_chunks(chunks):
                output.write(chunk)
        os.replace(part_path, destination)
    finally:
        part_path.unlink(missing_ok=True)
