"""Static parsing helpers for studio metadata (mediapackage + SMIL)."""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

# Reject absurd/unbounded SMIL clip values beyond 5 days.
MAX_SMIL_TIME_SECONDS = 5 * 24 * 60 * 60


def sanitize_smil_time(seconds: float | None) -> float | None:
    """Return a safe SMIL time value in seconds or None when invalid."""
    if seconds is None:
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    if seconds > MAX_SMIL_TIME_SECONDS:
        return None
    return seconds


def parse_time(value: str | None) -> float | None:
    """Parse a SMIL time value into seconds."""
    if not value:
        return None
    raw_value = value.strip()
    if not raw_value:
        return None
    if raw_value.endswith("s"):
        try:
            return sanitize_smil_time(float(raw_value[:-1]))
        except Exception:
            return None
    match = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", raw_value)
    if match:
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        return sanitize_smil_time(hours * 3600 + minutes * 60 + seconds)
    return None


def parse_smil_cut(smil_text: str) -> tuple[float | None, float | None]:
    """Parse clip begin and end times from a SMIL cutting document."""
    try:
        root = ET.fromstring(smil_text)
        for element in root.iter():
            if element.tag.endswith("video"):
                begin_raw = element.attrib.get("clipBegin")
                end_raw = element.attrib.get("clipEnd")
                return parse_time(begin_raw), parse_time(end_raw)
        return None, None
    except Exception:
        return None, None


def parse_mediapackage(
    xml_text: str,
) -> tuple[str | None, str | None, str, str | None]:
    """Extract track URLs, presenter layout, and SMIL URL from a mediapackage XML."""
    ns = {"mp": "http://mediapackage.opencastproject.org"}
    root = ET.fromstring(xml_text)
    presenter_layout = root.attrib.get("presenter", "mid")

    presentation_url = None
    presenter_url = None
    media = root.find("mp:media", ns)
    if media is not None:
        for track in media.findall("mp:track", ns):
            track_type = track.attrib.get("type", "")
            url_element = track.find("mp:url", ns)
            url_value = url_element.text if url_element is not None else None
            if track_type == "presentation/source":
                presentation_url = url_value
            elif track_type == "presenter/source":
                presenter_url = url_value

    smil_url = None
    metadata = root.find("mp:metadata", ns)
    if metadata is not None:
        for catalog in metadata.findall("mp:catalog", ns):
            if catalog.attrib.get("type") == "smil/cutting":
                url_element = catalog.find("mp:url", ns)
                if url_element is not None and url_element.text:
                    smil_url = url_element.text

    return presentation_url, presenter_url, presenter_layout, smil_url
