"""Default runtime wiring for the top-level transcription flow.

Builds the concrete dependency graph consumed by `run_main_flow`.
Binds orchestration hooks to runtime implementations from sibling modules.
Keeps entrypoint startup minimal by moving wiring concerns into one place.
"""

import sys
from pathlib import Path
from typing import cast

_CORE_DIR = Path(__file__).resolve().parent
_CORE_DIR_STR = str(_CORE_DIR)
if _CORE_DIR_STR in sys.path:
    sys.path.remove(_CORE_DIR_STR)
sys.path.insert(0, _CORE_DIR_STR)

_RUNTIME_ARGS_MODULE = sys.modules.get("runtime_args_utils")
if _RUNTIME_ARGS_MODULE is not None:
    _runtime_args_file = getattr(_RUNTIME_ARGS_MODULE, "__file__", "")
    if not _runtime_args_file or Path(_runtime_args_file).resolve().parent != _CORE_DIR:
        sys.modules.pop("runtime_args_utils", None)

import gap_repair_runtime_utils
import main_orchestration_utils
import metadata_runtime_utils
import output_validation_runtime_utils
import runtime_cli_utils
import runtime_media_utils
import transcription_runtime_utils
import translation_runtime_utils
from runtime_args_utils import (
    _MAX_VTT_INTERNAL_GAP_COUNT,
    _MAX_VTT_INTERNAL_GAP_SECONDS,
    parse_args,
)


def build_main_flow_context() -> main_orchestration_utils.MainFlowContext:
    """Build default dependencies for the end-to-end transcription flow."""
    return main_orchestration_utils.MainFlowContext(
        extract_video_identification_fn=metadata_runtime_utils.extract_video_identification,
        compute_timeout_fn=runtime_media_utils.compute_timeout_with_defaults,
        prepare_audio_source_fn=runtime_media_utils.prepare_audio_source_with_defaults,
        resolve_effective_use_gpu_fn=runtime_cli_utils.resolve_effective_use_gpu,
        run_transcription_fn=transcription_runtime_utils.run_transcription,
        finalize_vtt_fn=output_validation_runtime_utils.finalize_vtt,
        run_non_blocking_internal_gap_repair_fn=gap_repair_runtime_utils.run_non_blocking_internal_gap_repair,
        build_whisper_fallback_options_fn=transcription_runtime_utils.build_whisper_fallback_options,
        maybe_translate_final_vtt_fn=translation_runtime_utils.maybe_translate_final_vtt,
        validate_final_vtt_and_collect_gap_analysis_fn=output_validation_runtime_utils.validate_final_vtt_and_collect_gap_analysis,
        build_transcription_runtime_metadata_fn=metadata_runtime_utils.build_transcription_runtime_metadata,
        write_info_video_metadata_fn=metadata_runtime_utils.write_info_video_metadata,
        max_vtt_internal_gap_seconds=_MAX_VTT_INTERNAL_GAP_SECONDS,
        max_vtt_internal_gap_count=_MAX_VTT_INTERNAL_GAP_COUNT,
    )


def main() -> int:
    """Run the transcription script end to end and return an exit code."""
    return cast(
        int,
        main_orchestration_utils.run_main_flow(
            parse_args(),
            context=build_main_flow_context(),
        ),
    )
