"""Segment filtering helpers for Whisper transcription results.

Applies heuristics to drop punctuation-only, credit-like, or script-drift cues.
Targets common hallucination patterns without over-pruning valid speech text.
Improves subtitle readability before VTT rendering and downstream validation.
"""

import re
from typing import Dict, Optional

# Matches segments that contain only punctuation-like filler. Those cues are
# usually produced when Whisper tries to decode silence or very weak speech.
_PUNCT_ONLY_TEXT_RE = re.compile(r"^[\s\.\,\!\?\:\;…'\"`\-\(\)\[\]\{\}/\\|_]+$")

# Catches the most common subtitle-credit hallucinations that models sometimes
# emit on silent tails or noisy stretches.
_SUBTITLE_CREDIT_TEXT_RE = re.compile(
    r"^(?:sous[- ]?titrage|sous[- ]?titres?|subtitles?|captions?)\b",
    re.IGNORECASE,
)

# We use lightweight script detection to reject obviously out-of-place text for
# Latin-script languages such as French or English. This is intentionally narrow:
# it targets strong hallucination signals without penalizing normal punctuation.
_CYRILLIC_TEXT_RE = re.compile(r"[\u0400-\u04FF]")
_CJK_TEXT_RE = re.compile(r"[\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]")

# Languages where a sudden switch to Cyrillic or CJK output is a reliable sign
# of transcription drift rather than valid content.
_LATIN_SCRIPT_LANGUAGES = {
    "ca",
    "cs",
    "da",
    "de",
    "en",
    "es",
    "eu",
    "fi",
    "fr",
    "gl",
    "hr",
    "hu",
    "is",
    "it",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "sv",
    "tr",
}


def is_punctuation_only_text(text: str) -> bool:
    """Return whether text only contains punctuation-like filler."""
    normalized = (text or "").strip()
    if not normalized:
        return True
    return _PUNCT_ONLY_TEXT_RE.fullmatch(normalized) is not None


def safe_float(value: object) -> Optional[float]:
    """Return a float when conversion is possible."""
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def language_uses_latin_script(language: Optional[str]) -> bool:
    """Return whether the expected transcription language normally uses Latin script."""
    if not language:
        return False
    return language.strip().lower() in _LATIN_SCRIPT_LANGUAGES


def contains_unexpected_script(text: str, expected_language: Optional[str]) -> bool:
    """Return whether text contains scripts that are unexpected for the target language."""
    if not language_uses_latin_script(expected_language):
        return False
    return _CYRILLIC_TEXT_RE.search(text) is not None or _CJK_TEXT_RE.search(text) is not None


def looks_like_subtitle_credit(text: str) -> bool:
    """Return whether text looks like a burned-in subtitle credit hallucination."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return False
    return _SUBTITLE_CREDIT_TEXT_RE.search(normalized) is not None


def looks_like_repetition_loop(text: str) -> bool:
    """Return whether text contains a long low-diversity repetition loop."""
    normalized = " ".join((text or "").split()).strip()
    if not normalized:
        return False

    numeric_tokens = re.findall(r"\d+", normalized)
    if len(numeric_tokens) >= 12 and len(set(numeric_tokens)) <= 4:
        compact = re.sub(r"\s+", "", normalized)
        numeric_chars = "".join(ch for ch in compact if ch.isdigit())
        if len(numeric_chars) >= max(12, int(len(compact) * 0.3)):
            return True

    return False


def should_drop_segment(
    segment: Dict[str, object],
    expected_language: Optional[str] = None,
) -> bool:
    """Drop silence or obviously hallucinatory segments from chunked results."""
    text = str(segment.get("text", "")).strip()
    if is_punctuation_only_text(text):
        return True
    if looks_like_subtitle_credit(text):
        return True
    if looks_like_repetition_loop(text):
        return True
    if contains_unexpected_script(text, expected_language):
        return True

    no_speech_prob = safe_float(segment.get("no_speech_prob"))
    avg_logprob = safe_float(segment.get("avg_logprob"))
    compression_ratio = safe_float(segment.get("compression_ratio"))

    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob >= 0.6
        and avg_logprob < -0.8
    ):
        return True

    if (
        compression_ratio is not None
        and avg_logprob is not None
        and compression_ratio > 3.0
        and avg_logprob < -0.8
    ):
        return True

    return False


def extract_detected_language(result: Dict[str, object]) -> Optional[str]:
    """Extract the detected language code from a Whisper Python result."""
    try:
        return str(result.get("language")) if isinstance(result, dict) else None
    except Exception:
        return None


def filter_result_segments(
    result: Dict[str, object],
    expected_language: Optional[str] = None,
    debug: bool = False,
) -> Dict[str, object]:
    """Filter obviously bad segments and rebuild the result text."""
    segments = result.get("segments")
    if not isinstance(segments, list):
        return result
    effective_language = expected_language or extract_detected_language(result)

    filtered_segments: list[Dict[str, object]] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if should_drop_segment(segment, effective_language):
            if debug:
                print(
                    "Dropping suspicious segment: "
                    f"text={segment.get('text', '')!r}, "
                    f"expected_language={effective_language!r}, "
                    f"no_speech_prob={segment.get('no_speech_prob')}, "
                    f"avg_logprob={segment.get('avg_logprob')}, "
                    f"compression_ratio={segment.get('compression_ratio')}"
                )
            continue
        filtered_segments.append(segment)

    filtered_result = dict(result)
    filtered_result["segments"] = filtered_segments
    filtered_result["text"] = " ".join(
        str(segment.get("text", "")).strip()
        for segment in filtered_segments
        if str(segment.get("text", "")).strip()
    )
    return filtered_result
