"""Stable translation flow API for transcription runtime.

Exposes a single import surface over runtime loading, decision, and VTT translation.
Re-exports typed contexts and flow functions.
Keeps module boundaries explicit.
"""

import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:  # pragma: no cover - direct file-spec import guard
    sys.path.insert(0, str(_CORE_DIR))

from translation_decision_flow_utils import (  # noqa: F401
    check_translation_input_vtt,
    maybe_translate_final_vtt,
    run_legacy_whisper_translation_fallback,
    run_whisper_with_explicit_language,
)
from translation_flow_contexts import (  # noqa: F401
    TranslateVttFileContext,
    TranslationDecisionContext,
    TranslationRuntimeContext,
)
from translation_runtime_flow_utils import load_translation_runtime  # noqa: F401
from translation_vtt_file_flow_utils import translate_vtt_file  # noqa: F401
