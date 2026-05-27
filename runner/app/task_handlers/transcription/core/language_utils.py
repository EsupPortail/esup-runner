"""Language normalization helpers for transcription runtime.

Maps Whisper language names and aliases to canonical short language codes.
Normalizes requested and detected values before transcription flow decisions.
Provides safe fallbacks when language information is missing or noisy.
"""

from typing import Optional


def map_language_name_to_code(name: str) -> Optional[str]:
    """Best-effort mapping from language names printed by whisper to ISO-639-1 codes."""
    if not name:
        return None
    n = name.strip().lower()
    common = {
        "english": "en",
        "french": "fr",
        "spanish": "es",
        "german": "de",
        "italian": "it",
        "portuguese": "pt",
        "chinese": "zh",
        "cantonese": "yue",
        "japanese": "ja",
        "korean": "ko",
        "russian": "ru",
        "arabic": "ar",
        "hindi": "hi",
        "dutch": "nl",
        "polish": "pl",
        "turkish": "tr",
    }
    return common.get(n)


def normalize_language_code(language: Optional[str]) -> Optional[str]:
    """Normalize a runner/Whisper language value to a stable short code."""
    if language is None:
        return None

    normalized = str(language).strip().lower()
    if not normalized:
        return None
    if normalized == "auto":
        return "auto"

    mapped_code = map_language_name_to_code(normalized)
    if mapped_code:
        return mapped_code

    return normalized.replace("_", "-").split("-", 1)[0]
