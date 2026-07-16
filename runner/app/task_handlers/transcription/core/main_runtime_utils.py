"""Default runtime wiring for the top-level transcription flow.

Builds the concrete dependency graph consumed by `run_main_flow`.
Binds orchestration hooks to runtime implementations from sibling modules.
Keeps entrypoint startup minimal by moving wiring concerns into one place.
"""

from typing import cast

from . import (
    gap_repair_runtime_utils,
    main_orchestration_utils,
    metadata_runtime_utils,
    output_validation_runtime_utils,
    runtime_cli_utils,
    runtime_media_utils,
    transcription_runtime_utils,
    translation_runtime_utils,
)
from .runtime_args_utils import (
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
