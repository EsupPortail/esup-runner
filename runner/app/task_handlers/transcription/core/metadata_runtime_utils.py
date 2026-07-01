"""Default runtime wiring for transcription metadata.

Adapts low-level metadata helpers into runtime-facing callbacks.
Combines language normalization and metadata persistence for final outputs.
Ensures metadata shape remains stable across orchestration paths.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional, cast

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import language_utils
import metadata_utils
import runtime_cli_utils

extract_video_identification = metadata_utils.extract_video_identification_from_args
write_info_video_metadata = metadata_utils.write_info_video_metadata


def build_transcription_runtime_metadata(
    *,
    requested_language: str,
    detected_language: Optional[str],
    final_language: Optional[str],
    whisper_model: str,
    use_gpu: bool,
    translation: Dict[str, Any],
    source_language: Optional[str] = "auto",
    vtt_internal_gaps: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build stable runtime metadata written to info_video.json."""
    return cast(
        Dict[str, Any],
        metadata_utils.build_transcription_runtime_metadata(
            requested_language=requested_language,
            detected_language=detected_language,
            final_language=final_language,
            whisper_model=whisper_model,
            use_gpu=use_gpu,
            translation=translation,
            normalize_language=language_utils.normalize_language_code,
            map_model_name=runtime_cli_utils.map_model_name,
            source_language=source_language,
            vtt_internal_gaps=vtt_internal_gaps,
        ),
    )
