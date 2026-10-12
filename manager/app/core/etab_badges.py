"""Helpers for displaying establishment badges in the manager web UI."""

from __future__ import annotations

import re
import unicodedata
from hashlib import sha256

_BADGE_CLASS_COUNT = 5
_COMMON_WORDS = {"a", "au", "aux", "d", "de", "des", "du", "la", "le", "les"}
_UNIVERSITY_WORDS = {"universite", "university"}
_KNOWN_BADGES = {
    "universite de lorraine": ("UL", 0),
    "universite de montpellier": ("UM", 1),
    "la rochelle universite": ("LR", 2),
    "universite cote d'azur": ("UC", 3),
    "esup-portail": ("EP", 4),
}


def _normalize_etab_name(etab_name: str) -> str:
    """Normalize an establishment name for matching and stable hashing."""
    decomposed = unicodedata.normalize("NFKD", etab_name)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_accents.casefold().split())


def _fallback_initials(normalized_name: str) -> str:
    """Build two readable initials when an establishment is not in the known list."""
    words: list[str] = re.findall(r"[a-z0-9]+", normalized_name)
    if not words:
        return "??"

    significant_words = [
        word for word in words if word not in _COMMON_WORDS and word not in _UNIVERSITY_WORDS
    ]
    if words[0] in _UNIVERSITY_WORDS and significant_words:
        return (words[0][0] + significant_words[0][0]).upper()
    if len(significant_words) >= 2:
        return (significant_words[0][0] + significant_words[1][0]).upper()
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


def build_etab_badge(etab_name: str | None) -> dict[str, str] | None:
    """Return the display data for an establishment badge."""
    display_name = " ".join(str(etab_name or "").split())
    if not display_name:
        return None

    normalized_name = _normalize_etab_name(display_name)
    known_badge = _KNOWN_BADGES.get(normalized_name)
    if known_badge:
        initials, color_index = known_badge
    else:
        initials = _fallback_initials(normalized_name)
        digest = sha256(normalized_name.encode("utf-8")).hexdigest()
        color_index = int(digest[:8], 16) % _BADGE_CLASS_COUNT

    return {
        "name": display_name,
        "initials": initials,
        "color_class": f"etab-badge-{color_index}",
    }
