"""Small binary-signature checks for denied media codecs.

This module intentionally avoids FFmpeg/ffprobe. It only reads a bounded prefix
of the downloaded file and looks for simple container-level signatures.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_SCAN_BYTES = 4 * 1024 * 1024

MAGICYUV_DENY_MESSAGE = (
    "Media rejected: MagicYUV codec is temporarily denied because of "
    "CVE-2026-8461 / PixelSmash.\n"
    "Please convert the file to H.264/H.265/VP9/AV1 before submitting it."
)


@dataclass(frozen=True)
class DeniedMediaMatch:
    """Description of a denied media signature match."""

    codec: str
    message: str


class MediaDeniedError(ValueError):
    """Raised when a media file matches a configured denylist rule."""


def normalize_media_codec_denylist(denylist: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    """Normalize codec names from configuration."""
    return {str(item).strip().lower() for item in denylist if str(item).strip()}


def _read_prefix(path: Path, max_bytes: int = DEFAULT_SCAN_BYTES) -> bytes:
    with path.open("rb") as file_handle:
        return file_handle.read(max_bytes)


def _looks_like_riff_avi(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI "


def _looks_like_matroska(data: bytes) -> bool:
    return data.startswith(b"\x1a\x45\xdf\xa3")


def _looks_like_iso_bmff(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def has_magicyuv_signature(data: bytes) -> bool:
    """Return whether the byte prefix contains known MagicYUV indicators."""
    if b"magicyuv" in data.lower():
        return True
    if b"MAGY" not in data:
        return False
    return _looks_like_riff_avi(data) or _looks_like_matroska(data) or _looks_like_iso_bmff(data)


def detect_denied_media(
    path: str | Path,
    denylist: list[str] | tuple[str, ...] | set[str],
    *,
    max_bytes: int = DEFAULT_SCAN_BYTES,
) -> DeniedMediaMatch | None:
    """Return a denied media match, if the configured denylist detects one."""
    denied = normalize_media_codec_denylist(denylist)
    if "magicyuv" not in denied:
        return None

    data = _read_prefix(Path(path), max_bytes=max_bytes)
    if has_magicyuv_signature(data):
        return DeniedMediaMatch(codec="magicyuv", message=MAGICYUV_DENY_MESSAGE)
    return None


def validate_media_against_denylist(
    path: str | Path,
    denylist: list[str] | tuple[str, ...] | set[str],
    *,
    max_bytes: int = DEFAULT_SCAN_BYTES,
) -> None:
    """Raise MediaDeniedError when a file matches a configured denylist rule."""
    match = detect_denied_media(path, denylist, max_bytes=max_bytes)
    if match is not None:
        raise MediaDeniedError(match.message)
