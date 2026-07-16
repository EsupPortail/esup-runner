"""Validates transcription parameter validation and video identification metadata support."""

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path

_CPU_TRANSLATION_MODELS = {
    ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
}
_GPU_TRANSLATION_MODELS = {
    ("en", "fr"): "Helsinki-NLP/opus-mt-tc-big-en-fr",
    ("fr", "en"): "Helsinki-NLP/opus-mt-tc-big-fr-en",
}
_TRANSLATION_BATCH_SIZE = 24
_TRANSLATION_UNSUPPORTED_PAIR_RC = 30
_TRANSLATION_FAILED_RC = 32
_TRANSLATION_DECISION_FAILED_RC = 33


def _load_transcription_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "app" / "task_handlers" / "transcription" / "transcription.py"
    spec = importlib.util.spec_from_file_location("transcription_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_transcription_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.transcription.core.{module_name}")
    return importlib.reload(module)


def _postprocess_vtt_content_with_core_utils(
    content: str, *, max_line_width: int, max_line_count: int
) -> str:
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    parse_range = lambda line: vtt_utils.parse_vtt_cue_time_range(  # noqa: E731
        line,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
    )
    cue_gap = (
        lambda prev_line, next_line: vtt_utils.cue_gap_allows_apostrophe_transfer(  # noqa: E731
            prev_line,
            next_line,
            parse_vtt_cue_time_range_fn=parse_range,
        )
    )
    extract_trailing = lambda text: vtt_utils.extract_trailing_token_core(  # noqa: E731
        text,
        normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
    )
    repair_split = (
        lambda prev_text, next_text: vtt_utils.repair_cross_cue_apostrophe_split(  # noqa: E731
            prev_text,
            next_text,
            normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
            extract_trailing_token_core_fn=extract_trailing,
        )
    )
    repair_splits = lambda blocks: vtt_utils.repair_cross_cue_apostrophe_splits(  # noqa: E731
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=cue_gap,
        repair_cross_cue_apostrophe_split_fn=repair_split,
    )
    render_blocks = lambda blocks, *, max_line_width, max_line_count: vtt_utils.render_postprocessed_vtt_blocks(  # noqa: E731
        blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        wrap_vtt_cue_text_fn=vtt_utils.wrap_vtt_cue_text,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
        format_vtt_timestamp_fn=validation_utils.format_vtt_timestamp,
    )

    return vtt_utils.postprocess_vtt_content(
        content,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        parse_vtt_postprocess_block_fn=vtt_utils.parse_vtt_postprocess_block,
        repair_cross_cue_apostrophe_splits_fn=repair_splits,
        render_postprocessed_vtt_blocks_fn=render_blocks,
    )


def _postprocess_vtt_file_with_core_utils(
    vtt_path: Path,
    *,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> None:
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    vtt_utils.postprocess_vtt_file(
        vtt_path,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        postprocess_vtt_content_fn=_postprocess_vtt_content_with_core_utils,
    )


def _combine_chunk_results_with_core_utils(chunk_results, keep_windows=None):
    chunking_utils = _load_transcription_core_module("chunking_utils")
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    def offset_segment_timestamps_fn(segment, segment_id, offset_sec):
        return chunking_utils.offset_segment_timestamps(
            segment,
            segment_id,
            offset_sec,
            offset_timestamp_fn=chunking_utils.offset_timestamp,
        )

    def trim_segment_to_time_window_fn(segment, keep_start_sec, keep_end_sec):
        return chunking_utils.trim_segment_to_time_window(
            segment,
            keep_start_sec,
            keep_end_sec,
            safe_float_fn=segment_filter_utils.safe_float,
        )

    def merge_adjacent_identical_segment_fn(merged_segments, next_segment):
        return chunking_utils.merge_adjacent_identical_segment(
            merged_segments,
            next_segment,
            safe_float_fn=segment_filter_utils.safe_float,
        )

    def append_chunk_segments_fn(
        merged_segments, chunk_segments, next_segment_id, offset_sec, keep_window
    ):
        return chunking_utils.append_chunk_segments(
            merged_segments,
            chunk_segments,
            next_segment_id,
            offset_sec,
            keep_window,
            offset_segment_timestamps_fn=offset_segment_timestamps_fn,
            trim_segment_to_time_window_fn=trim_segment_to_time_window_fn,
            merge_adjacent_identical_segment_fn=merge_adjacent_identical_segment_fn,
        )

    return chunking_utils.combine_chunk_results(
        chunk_results,
        keep_windows=keep_windows,
        extract_detected_language_fn=segment_filter_utils.extract_detected_language,
        append_chunk_segments_fn=append_chunk_segments_fn,
        resolve_keep_window_fn=chunking_utils.resolve_keep_window,
        build_merged_result_text_fn=chunking_utils.build_merged_result_text,
    )


def _translate_vtt_content_with_core_utils(
    content: str,
    *,
    translate_batch,
    max_line_width: int,
    max_line_count: int,
    batch_size: int,
) -> str:
    translation_utils = _load_transcription_core_module("translation_utils")
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    parse_range = lambda line: vtt_utils.parse_vtt_cue_time_range(  # noqa: E731
        line,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
    )
    cue_gap = (
        lambda prev_line, next_line: vtt_utils.cue_gap_allows_apostrophe_transfer(  # noqa: E731
            prev_line,
            next_line,
            parse_vtt_cue_time_range_fn=parse_range,
        )
    )
    extract_trailing = lambda text: vtt_utils.extract_trailing_token_core(  # noqa: E731
        text,
        normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
    )
    repair_split = (
        lambda prev_text, next_text: vtt_utils.repair_cross_cue_apostrophe_split(  # noqa: E731
            prev_text,
            next_text,
            normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
            extract_trailing_token_core_fn=extract_trailing,
        )
    )
    repair_splits = lambda blocks: vtt_utils.repair_cross_cue_apostrophe_splits(  # noqa: E731
        blocks,
        cue_gap_allows_apostrophe_transfer_fn=cue_gap,
        repair_cross_cue_apostrophe_split_fn=repair_split,
    )
    render_blocks = lambda blocks, *, max_line_width, max_line_count: vtt_utils.render_postprocessed_vtt_blocks(  # noqa: E731
        blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        wrap_vtt_cue_text_fn=vtt_utils.wrap_vtt_cue_text,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
        format_vtt_timestamp_fn=validation_utils.format_vtt_timestamp,
    )

    return translation_utils.translate_vtt_content(
        content,
        translate_batch=translate_batch,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        batch_size=batch_size,
        parse_vtt_postprocess_block=vtt_utils.parse_vtt_postprocess_block,
        normalize_vtt_cue_text=vtt_utils.normalize_vtt_cue_text,
        translate_cue_texts_fn=lambda cue_texts, *, translate_batch, batch_size: translation_utils.translate_cue_texts(  # noqa: E731
            cue_texts,
            translate_batch=translate_batch,
            batch_size=batch_size,
            normalize_vtt_cue_text=vtt_utils.normalize_vtt_cue_text,
        ),
        repair_cross_cue_apostrophe_splits=repair_splits,
        render_postprocessed_vtt_blocks=render_blocks,
    )


def _translate_vtt_file_with_core_utils(
    vtt_path: Path,
    *,
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: str,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    load_translation_runtime_fn,
    run_translation_batch_fn,
):
    flow_utils = _load_transcription_core_module("translation_flow_utils")
    translation_utils = _load_transcription_core_module("translation_utils")
    language_utils = _load_transcription_core_module("language_utils")
    metadata_utils = _load_transcription_core_module("metadata_utils")

    def build_translation_metadata_fn(**kwargs):
        return metadata_utils.build_translation_metadata(
            **kwargs,
            normalize_language=language_utils.normalize_language_code,
        )

    context = flow_utils.TranslateVttFileContext(
        translation_backend_local="local_translation",
        translation_failed_rc=_TRANSLATION_FAILED_RC,
        translation_batch_size=_TRANSLATION_BATCH_SIZE,
        build_translation_metadata_fn=build_translation_metadata_fn,
        load_translation_runtime_fn=load_translation_runtime_fn,
        build_source_vtt_sidecar_path_fn=lambda path, language: translation_utils.build_source_vtt_sidecar_path(  # noqa: E731
            path,
            language,
            normalize_language=language_utils.normalize_language_code,
        ),
        run_translation_batch_fn=run_translation_batch_fn,
        translate_vtt_content_fn=lambda content, *, translate_batch, max_line_width, max_line_count, batch_size: _translate_vtt_content_with_core_utils(  # noqa: E731
            content,
            translate_batch=translate_batch,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            batch_size=batch_size,
        ),
    )

    return flow_utils.translate_vtt_file(
        vtt_path,
        source_language=source_language,
        target_language=target_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        context=context,
    )


def _maybe_translate_final_vtt_with_core_utils(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language,
    whisper_fallback_options,
    use_gpu: bool,
    huggingface_models_dir: str,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    run_legacy_whisper_translation_fallback_fn=None,
    translate_vtt_file_fn=None,
):
    flow_utils = _load_transcription_core_module("translation_flow_utils")
    translation_utils = _load_transcription_core_module("translation_utils")
    language_utils = _load_transcription_core_module("language_utils")
    metadata_utils = _load_transcription_core_module("metadata_utils")
    vtt_validation_utils = _load_transcription_core_module("vtt_validation_utils")

    def normalize_language_fn(language):
        return language_utils.normalize_language_code(language)

    def build_translation_metadata_fn(**kwargs):
        return metadata_utils.build_translation_metadata(
            **kwargs,
            normalize_language=normalize_language_fn,
        )

    def resolve_translation_model_name_fn(source_language, target_language, use_gpu):
        return translation_utils.resolve_translation_model_name(
            source_language=source_language,
            target_language=target_language,
            use_gpu=use_gpu,
            normalize_language=normalize_language_fn,
            cpu_model_map=_CPU_TRANSLATION_MODELS,
            gpu_model_map=_GPU_TRANSLATION_MODELS,
        )

    def read_last_vtt_cue_end_seconds_fn(vtt_path):
        return vtt_validation_utils.read_last_vtt_cue_end_seconds(
            vtt_path,
            parse_timestamp=vtt_validation_utils.parse_vtt_timestamp,
        )

    def check_translation_input_vtt_fn(audio_src, work_dir, **kwargs):
        return flow_utils.check_translation_input_vtt(
            audio_src,
            work_dir,
            requested_language=kwargs["requested_language"],
            detected_language=kwargs["detected_language"],
            use_gpu=kwargs["use_gpu"],
            debug=kwargs["debug"],
            translation_backend_local="local_translation",
            translation_backend_none="none",
            build_translation_metadata_fn=build_translation_metadata_fn,
            normalize_language_fn=normalize_language_fn,
            resolve_translation_model_name_fn=resolve_translation_model_name_fn,
            read_last_vtt_cue_end_seconds_fn=read_last_vtt_cue_end_seconds_fn,
        )

    if run_legacy_whisper_translation_fallback_fn is None:

        def run_legacy_whisper_translation_fallback_fn(*_args, **_kwargs):
            raise AssertionError("legacy Whisper fallback should not be called in this scenario")

    if translate_vtt_file_fn is None:

        def translate_vtt_file_fn(*_args, **_kwargs):
            raise AssertionError("local VTT translation should not be called in this scenario")

    context = flow_utils.TranslationDecisionContext(
        translation_backend_none="none",
        translation_backend_local="local_translation",
        translation_backend_whisper_legacy="whisper_legacy_fallback",
        translation_decision_failed_rc=_TRANSLATION_DECISION_FAILED_RC,
        translation_unsupported_pair_rc=_TRANSLATION_UNSUPPORTED_PAIR_RC,
        normalize_language_fn=normalize_language_fn,
        build_translation_metadata_fn=build_translation_metadata_fn,
        check_translation_input_vtt_fn=check_translation_input_vtt_fn,
        resolve_translation_model_name_fn=resolve_translation_model_name_fn,
        run_legacy_whisper_translation_fallback_fn=run_legacy_whisper_translation_fallback_fn,
        translate_vtt_file_fn=translate_vtt_file_fn,
    )

    return flow_utils.maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language=requested_language,
        detected_language=detected_language,
        whisper_fallback_options=whisper_fallback_options,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        context=context,
    )


def _load_translation_runtime_with_core_utils(
    *,
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: str,
    debug: bool,
    import_translation_modules_fn,
):
    flow_utils = _load_transcription_core_module("translation_flow_utils")
    translation_utils = _load_transcription_core_module("translation_utils")
    language_utils = _load_transcription_core_module("language_utils")

    def resolve_translation_model_name_fn(source_language, target_language, use_gpu):
        return translation_utils.resolve_translation_model_name(
            source_language=source_language,
            target_language=target_language,
            use_gpu=use_gpu,
            normalize_language=language_utils.normalize_language_code,
            cpu_model_map=_CPU_TRANSLATION_MODELS,
            gpu_model_map=_GPU_TRANSLATION_MODELS,
        )

    context = flow_utils.TranslationRuntimeContext(
        translation_unsupported_pair_rc=_TRANSLATION_UNSUPPORTED_PAIR_RC,
        translation_backend_unavailable_rc=31,
        cpu_translation_models=_CPU_TRANSLATION_MODELS,
        resolve_translation_model_name_fn=resolve_translation_model_name_fn,
        import_translation_modules_fn=import_translation_modules_fn,
        prepare_huggingface_models_dir_fn=translation_utils.prepare_huggingface_models_dir,
        load_translation_model_objects_fn=lambda auto_tokenizer_cls, auto_model_cls, model_name, cache_dir: translation_utils.load_translation_model_objects(  # noqa: E731
            auto_tokenizer_cls,
            auto_model_cls,
            model_name,
            cache_dir,
            hf_token="",
        ),
        place_translation_model_on_device_fn=translation_utils.place_translation_model_on_device,
    )

    return flow_utils.load_translation_runtime(
        source_language=source_language,
        target_language=target_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        debug=debug,
        context=context,
    )


def _read_vtt_cue_time_ranges_with_core_utils(vtt_path: Path):
    validation_utils = _load_transcription_core_module("vtt_validation_utils")
    return validation_utils.read_vtt_cue_time_ranges(
        vtt_path,
        parse_timestamp=validation_utils.parse_vtt_timestamp,
    )


def _detect_vtt_internal_gaps_with_core_utils(
    vtt_path: Path,
    max_internal_gap_sec: float,
    *,
    read_vtt_cue_time_ranges_fn=None,
):
    validation_utils = _load_transcription_core_module("vtt_validation_utils")
    read_ranges = read_vtt_cue_time_ranges_fn or _read_vtt_cue_time_ranges_with_core_utils
    return validation_utils.detect_vtt_internal_gaps(
        vtt_path,
        max_internal_gap_sec,
        read_cue_time_ranges=read_ranges,
    )


def _validate_vtt_coverage_with_core_utils(
    *,
    vtt_path: Path,
    reference_duration_sec: float,
    min_coverage_ratio: float,
    max_final_gap_sec: float,
    debug: bool,
):
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    def read_last_cue_end_seconds(path: Path):
        return validation_utils.read_last_vtt_cue_end_seconds(
            path,
            parse_timestamp=validation_utils.parse_vtt_timestamp,
        )

    return validation_utils.validate_vtt_coverage(
        vtt_path=vtt_path,
        reference_duration_sec=reference_duration_sec,
        min_coverage_ratio=min_coverage_ratio,
        max_final_gap_sec=max_final_gap_sec,
        debug=debug,
        read_last_cue_end_seconds=read_last_cue_end_seconds,
    )


def _validate_vtt_internal_gaps_with_core_utils(
    *,
    vtt_path: Path,
    max_internal_gap_sec: float,
    max_internal_gap_count: int,
    debug: bool,
    detect_vtt_internal_gaps_fn=None,
):
    validation_utils = _load_transcription_core_module("vtt_validation_utils")
    detect = detect_vtt_internal_gaps_fn or _detect_vtt_internal_gaps_with_core_utils
    return validation_utils.validate_vtt_internal_gaps(
        vtt_path=vtt_path,
        max_internal_gap_sec=max_internal_gap_sec,
        max_internal_gap_count=max_internal_gap_count,
        debug=debug,
        detect_vtt_internal_gaps_fn=detect,
    )


def _read_vtt_cues_with_core_utils(vtt_path: Path):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    parse_range = lambda line: vtt_utils.parse_vtt_cue_time_range(  # noqa: E731
        line,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
    )
    return gap_utils.read_vtt_cues(
        vtt_path,
        parse_vtt_postprocess_block_fn=vtt_utils.parse_vtt_postprocess_block,
        parse_vtt_cue_time_range_fn=parse_range,
        normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
    )


def _dedupe_sorted_vtt_cues_with_core_utils(cues):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    return gap_utils.dedupe_sorted_vtt_cues(
        cues,
        normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
    )


def _render_vtt_from_cues_with_core_utils(
    cues,
    *,
    max_line_width: int,
    max_line_count: int,
):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    validation_utils = _load_transcription_core_module("vtt_validation_utils")
    return gap_utils.render_vtt_from_cues(
        cues,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        format_vtt_timestamp_fn=validation_utils.format_vtt_timestamp,
        wrap_vtt_cue_text_fn=vtt_utils.wrap_vtt_cue_text,
        split_vtt_cue_text_fn=vtt_utils.split_vtt_cue_text,
    )


def _run_gap_window_rerun_with_core_utils(
    *,
    audio_src: Path,
    out_dir: Path,
    model: str,
    whisper_models_dir: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    transcription_language: str,
    start_sec: float,
    duration_sec: float,
    gap_start_sec: float,
    gap_end_sec: float,
    overlap_tolerance_sec: float,
    debug: bool,
    extract_audio_chunk_fn,
    run_whisper_python_fn,
    run_whisper_cli_fn,
    find_generated_vtt_fn,
    read_vtt_cues_fn,
):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    return gap_utils.run_gap_window_rerun(
        audio_src=audio_src,
        out_dir=out_dir,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        gpu_device=gpu_device,
        vad_filter=vad_filter,
        timeout_sec=timeout_sec,
        transcription_language=transcription_language,
        start_sec=start_sec,
        duration_sec=duration_sec,
        gap_start_sec=gap_start_sec,
        gap_end_sec=gap_end_sec,
        overlap_tolerance_sec=overlap_tolerance_sec,
        debug=debug,
        extract_audio_chunk_fn=extract_audio_chunk_fn,
        run_whisper_python_fn=run_whisper_python_fn,
        run_whisper_cli_fn=run_whisper_cli_fn,
        find_generated_vtt_fn=find_generated_vtt_fn,
        read_vtt_cues_fn=read_vtt_cues_fn,
    )


def _attempt_best_effort_gap_repair_with_core_utils(
    *,
    vtt_path: Path,
    audio_src: Path,
    work_dir: Path,
    model: str,
    whisper_models_dir: str,
    use_gpu: bool,
    gpu_device: int,
    vad_filter: bool,
    timeout_sec: int,
    detected_language,
    max_internal_gap_sec: float,
    max_repair_attempts: int,
    max_line_width: int,
    max_line_count: int,
    debug: bool,
    detect_vtt_internal_gaps_fn=None,
    read_vtt_cues_fn=None,
    probe_duration_seconds_fn=None,
    normalize_language_fn=None,
    resolve_transcription_language_fn=None,
    run_gap_window_rerun_fn=None,
    dedupe_sorted_vtt_cues_fn=None,
    render_vtt_from_cues_fn=None,
    postprocess_vtt_file_fn=None,
):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    language_utils = _load_transcription_core_module("language_utils")

    detect = detect_vtt_internal_gaps_fn or _detect_vtt_internal_gaps_with_core_utils
    read_cues = read_vtt_cues_fn or _read_vtt_cues_with_core_utils
    probe = probe_duration_seconds_fn or (lambda _path: 0.0)
    normalize = normalize_language_fn or language_utils.normalize_language_code
    resolve_language = resolve_transcription_language_fn or (
        lambda requested_language: "auto" if requested_language == "auto" else requested_language
    )
    run_gap = run_gap_window_rerun_fn or (lambda **_kwargs: (False, []))
    dedupe = dedupe_sorted_vtt_cues_fn or _dedupe_sorted_vtt_cues_with_core_utils
    render = render_vtt_from_cues_fn or (
        lambda cues, *, max_line_width, max_line_count: _render_vtt_from_cues_with_core_utils(
            cues,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
        )
    )
    postprocess = postprocess_vtt_file_fn or (
        lambda *args, **kwargs: _postprocess_vtt_file_with_core_utils(*args, **kwargs)
    )
    context = gap_utils.AttemptGapRepairContext(
        context_padding_seconds=1.0,
        min_window_seconds=2.0,
        overlap_tolerance_seconds=0.4,
        detect_vtt_internal_gaps_fn=detect,
        read_vtt_cues_fn=read_cues,
        probe_duration_seconds_fn=probe,
        normalize_language_fn=normalize,
        resolve_transcription_language_fn=resolve_language,
        run_gap_window_rerun_fn=run_gap,
        dedupe_sorted_vtt_cues_fn=dedupe,
        render_vtt_from_cues_fn=render,
        postprocess_vtt_file_fn=postprocess,
    )

    return gap_utils.attempt_best_effort_vtt_internal_gap_repair(
        vtt_path=vtt_path,
        audio_src=audio_src,
        work_dir=work_dir,
        model=model,
        whisper_models_dir=whisper_models_dir,
        use_gpu=use_gpu,
        gpu_device=gpu_device,
        vad_filter=vad_filter,
        timeout_sec=timeout_sec,
        detected_language=detected_language,
        max_internal_gap_sec=max_internal_gap_sec,
        max_repair_attempts=max_repair_attempts,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
        context=context,
    )


def _default_non_blocking_internal_gap_metadata_with_core_utils(
    *,
    note: str,
    error=None,
    threshold_seconds: float = 15.0,
    allowed_gap_count: int = 0,
):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    return gap_utils.default_non_blocking_internal_gap_metadata(
        note=note,
        threshold_seconds=threshold_seconds,
        allowed_gap_count=allowed_gap_count,
        error=error,
    )


def _run_non_blocking_internal_gap_repair_with_core_utils(
    *,
    expected_vtt: Path,
    audio_src: Path,
    work_dir: Path,
    args,
    timeout_sec: int,
    effective_use_gpu: bool,
    detected_language,
    vtt_max_line_width: int,
    vtt_max_line_count: int,
    debug: bool,
    attempt_best_effort_vtt_internal_gap_repair_fn,
):
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    context = gap_utils.NonBlockingGapRepairContext(
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
        max_internal_gap_repair_attempts=3,
        attempt_best_effort_vtt_internal_gap_repair_fn=attempt_best_effort_vtt_internal_gap_repair_fn,
        default_non_blocking_internal_gap_metadata_fn=_default_non_blocking_internal_gap_metadata_with_core_utils,
    )
    return gap_utils.run_non_blocking_internal_gap_repair(
        expected_vtt=expected_vtt,
        audio_src=audio_src,
        work_dir=work_dir,
        args=args,
        timeout_sec=timeout_sec,
        effective_use_gpu=effective_use_gpu,
        detected_language=detected_language,
        vtt_max_line_width=vtt_max_line_width,
        vtt_max_line_count=vtt_max_line_count,
        debug=debug,
        context=context,
    )


def _build_transcription_runtime_metadata_with_core_utils(
    *,
    requested_language: str,
    detected_language,
    final_language,
    whisper_model: str,
    use_gpu: bool,
    translation,
    vtt_internal_gaps=None,
):
    metadata_utils = _load_transcription_core_module("metadata_utils")
    language_utils = _load_transcription_core_module("language_utils")
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")
    return metadata_utils.build_transcription_runtime_metadata(
        requested_language=requested_language,
        detected_language=detected_language,
        final_language=final_language,
        whisper_model=whisper_model,
        use_gpu=use_gpu,
        translation=translation,
        normalize_language=language_utils.normalize_language_code,
        map_model_name=runtime_cli_utils.map_model_name,
        vtt_internal_gaps=vtt_internal_gaps,
    )


def _write_info_video_metadata_with_core_utils(
    work_dir: Path,
    metadata,
    *,
    debug: bool,
):
    metadata_utils = _load_transcription_core_module("metadata_utils")
    metadata_utils.write_info_video_metadata(work_dir, metadata, debug=debug)


def test_finalize_vtt_accepts_truncated_stem_from_whisper_cli(tmp_path):
    """Validate Finalize vtt accepts truncated stem from whisper cli."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    whisper_cli_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.vtt"
    whisper_cli_vtt.write_text("WEBVTT\n\n")

    rc = output_utils.finalize_vtt(
        audio_src,
        work_dir,
        max_line_count=2,
        max_line_width=40,
        debug=False,
        find_generated_vtt_fn=lambda a, w: output_utils.find_generated_vtt(
            a,
            w,
            build_vtt_stem_candidates_fn=output_utils.build_vtt_stem_candidates,
        ),
        postprocess_vtt_file_fn=_postprocess_vtt_file_with_core_utils,
    )

    expected_vtt = work_dir / "audio_192k_test_2026-02-17_14-42-56.189832.vtt"
    assert rc == 0
    assert expected_vtt.exists()
    assert not whisper_cli_vtt.exists()


def test_finalize_vtt_fails_when_no_vtt_found(tmp_path):
    """Validate Finalize vtt fails when no vtt found."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test_2026-02-17_14-42-56.189832.mp3"
    audio_src.write_bytes(b"fake-audio")

    rc = output_utils.finalize_vtt(
        audio_src,
        work_dir,
        max_line_count=2,
        max_line_width=40,
        debug=False,
        find_generated_vtt_fn=lambda a, w: output_utils.find_generated_vtt(
            a,
            w,
            build_vtt_stem_candidates_fn=output_utils.build_vtt_stem_candidates,
        ),
        postprocess_vtt_file_fn=_postprocess_vtt_file_with_core_utils,
    )

    assert rc == 5


def test_finalize_vtt_postprocesses_apostrophe_wrapping(tmp_path):
    """Validate Finalize vtt postprocesses apostrophe wrapping."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")

    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")

    generated_vtt = work_dir / "audio_192k_test.vtt"
    generated_vtt.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Je parle de l\n"
        "'usage responsable aujourd'hui.\n",
        encoding="utf-8",
    )

    rc = output_utils.finalize_vtt(
        audio_src,
        work_dir,
        max_line_count=2,
        max_line_width=24,
        debug=False,
        find_generated_vtt_fn=lambda a, w: output_utils.find_generated_vtt(
            a,
            w,
            build_vtt_stem_candidates_fn=output_utils.build_vtt_stem_candidates,
        ),
        postprocess_vtt_file_fn=_postprocess_vtt_file_with_core_utils,
    )

    assert rc == 0
    processed = generated_vtt.read_text(encoding="utf-8")
    assert "l'usage" in processed
    assert "l\n'usage" not in processed


def test_postprocess_vtt_file_with_defaults_repairs_apostrophe_wrapping(tmp_path):
    """Validate Postprocess vtt file with defaults repairs apostrophe wrapping."""
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    vtt_path = tmp_path / "sample.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:05.000\n" "Je parle de l\n" "'usage responsable.\n",
        encoding="utf-8",
    )

    vtt_utils.postprocess_vtt_file_with_defaults(
        vtt_path,
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    processed = vtt_path.read_text(encoding="utf-8")
    assert "l'usage" in processed
    assert "l\n'usage" not in processed


def test_run_transcription_uses_auto_source_language_for_requested_target_language(
    monkeypatch, tmp_path
):
    """Validate Run transcription uses auto source language for requested target language."""
    flow_utils = _load_transcription_core_module("transcription_flow_utils")
    args = types.SimpleNamespace(
        language="en",
        chunk_threshold_seconds="",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        gpu_device=0,
        vad_filter="true",
        chunk_duration_seconds="300",
        chunk_overlap_seconds="4",
        vtt_highlight_words="false",
        vtt_max_line_count="2",
        vtt_max_line_width="40",
    )

    captured = {}

    def fake_run_whisper_python(**kwargs):
        captured["language"] = kwargs["language"]
        return 0, "fr"

    context = flow_utils.TranscriptionFlowContext(
        resolve_transcription_language_fn=lambda _requested: "auto",
        resolve_chunk_threshold_seconds_fn=lambda **_kwargs: 777,
        run_whisper_python_fn=fake_run_whisper_python,
        run_whisper_cli_fn=lambda **_kwargs: (255, None),
        normalize_language_code_fn=lambda language: language,
    )

    rc, detected_language = flow_utils.run_transcription(
        args,
        tmp_path / "audio.mp3",
        tmp_path / "output",
        60,
        False,
        False,
        context=context,
    )

    assert rc == 0
    assert detected_language == "fr"
    assert captured["language"] == "auto"


def test_run_transcription_uses_explicit_source_language_when_provided(tmp_path):
    """Validate explicit source language guides Whisper without relying on auto-detect."""
    flow_utils = _load_transcription_core_module("transcription_flow_utils")
    args = types.SimpleNamespace(
        language="fr",
        source_language="fr",
        chunk_threshold_seconds="",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        gpu_device=0,
        vad_filter="true",
        chunk_duration_seconds="300",
        chunk_overlap_seconds="4",
        vtt_highlight_words="false",
        vtt_max_line_count="2",
        vtt_max_line_width="40",
    )

    captured = {}

    def fake_run_whisper_python(**kwargs):
        captured["language"] = kwargs["language"]
        return 0, "en"

    context = flow_utils.TranscriptionFlowContext(
        resolve_transcription_language_fn=lambda requested: requested,
        resolve_chunk_threshold_seconds_fn=lambda **_kwargs: 777,
        run_whisper_python_fn=fake_run_whisper_python,
        run_whisper_cli_fn=lambda **_kwargs: (255, None),
        normalize_language_code_fn=lambda language: language,
    )

    rc, detected_language = flow_utils.run_transcription(
        args,
        tmp_path / "audio.mp3",
        tmp_path / "output",
        60,
        True,
        False,
        context=context,
    )

    assert rc == 0
    assert captured["language"] == "fr"
    assert detected_language == "fr"


def test_runtime_media_compute_timeout_falls_back_to_defaults_on_invalid_args(tmp_path):
    """Validate Runtime media compute timeout falls back to defaults on invalid args."""
    runtime_media_utils = _load_transcription_core_module("runtime_media_utils")
    args = types.SimpleNamespace(timeout_factor="oops", min_timeout="bad")

    timeout_sec = runtime_media_utils.compute_timeout(
        args,
        tmp_path / "input.mp4",
        debug=False,
        probe_duration_seconds_fn=lambda _path, _debug: 10.0,
    )

    assert timeout_sec == 80


def test_runtime_media_prepare_audio_source_uses_ffmpeg_then_normalize(tmp_path):
    """Validate Runtime media prepare audio source uses ffmpeg then normalize."""
    runtime_media_utils = _load_transcription_core_module("runtime_media_utils")
    args = types.SimpleNamespace(
        input_file="input.mp4",
        sample_rate="16000",
        downmix_mono="true",
        audio_stream_index="0",
        normalize="true",
        normalize_target_level="-23",
    )

    recorded = {"ffmpeg_calls": 0, "normalize_calls": 0}
    expected_norm_path = tmp_path / "output" / "input_norm.mp3"

    def fake_run_ffmpeg_to_mp3(**kwargs):
        recorded["ffmpeg_calls"] += 1
        assert kwargs["sample_rate"] == 16000
        assert kwargs["downmix_mono"] is True
        assert kwargs["audio_index"] == 0
        return 0

    def fake_normalize_mp3_with_ffmpeg_normalize(**kwargs):
        recorded["normalize_calls"] += 1
        assert kwargs["target_level"] == "-23"
        return expected_norm_path

    rc, audio_src = runtime_media_utils.prepare_audio_source(
        args,
        tmp_path / "input.mp4",
        tmp_path / "output",
        timeout_sec=120,
        debug=False,
        run_ffmpeg_to_mp3_fn=fake_run_ffmpeg_to_mp3,
        normalize_mp3_with_ffmpeg_normalize_fn=fake_normalize_mp3_with_ffmpeg_normalize,
    )

    assert rc == 0
    assert audio_src == expected_norm_path
    assert recorded["ffmpeg_calls"] == 1
    assert recorded["normalize_calls"] == 1


def test_run_whisper_cli_reports_resolution_hint_when_binary_is_missing(
    monkeypatch, tmp_path, capsys
):
    """Validate Run whisper cli reports resolution hint when binary is missing."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    def fake_subprocess_run(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "whisper")

    rc, detected_language = runtime_cli_utils.run_whisper_cli(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "output",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        debug=False,
        map_model_name_fn=runtime_cli_utils.map_model_name,
        build_whisper_command_fn=lambda **kwargs: ["whisper"],
        prepare_whisper_env_fn=runtime_cli_utils.prepare_whisper_env,
        detect_language_from_stdout_fn=lambda _stdout, _language: None,
        print_transcription_dependency_resolution_hint_fn=lambda **_kwargs: print(
            "make sync-transcription-cpu"
        ),
        subprocess_run=fake_subprocess_run,
    )

    captured = capsys.readouterr().out
    assert rc == 127
    assert detected_language is None
    assert "Unable to run whisper CLI: command not found in PATH: whisper" in captured
    assert "make sync-transcription-cpu" in captured


def test_run_whisper_cli_with_defaults_reports_resolution_hint_when_binary_is_missing(
    tmp_path, capsys
):
    """Validate Run whisper cli with defaults reports resolution hint when binary is missing."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    def fake_subprocess_run(*_args, **_kwargs):
        raise FileNotFoundError(2, "No such file or directory", "whisper")

    rc, detected_language = runtime_cli_utils.run_whisper_cli_with_defaults(
        audio_path=tmp_path / "audio.mp3",
        out_dir=tmp_path / "output",
        language="auto",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        debug=False,
        subprocess_run=fake_subprocess_run,
    )

    captured = capsys.readouterr().out
    assert rc == 127
    assert detected_language is None
    assert "Unable to run whisper CLI: command not found in PATH: whisper" in captured
    assert "make sync-transcription-cpu" in captured


def test_runner_project_dir_returns_fallback_on_resolution_error(monkeypatch):
    """Validate Runner project dir returns fallback on resolution error."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    class BrokenPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise RuntimeError("path resolution failed")

    monkeypatch.setattr(runtime_cli_utils, "Path", BrokenPath)

    assert runtime_cli_utils.runner_project_dir() == "<runner-dir>"


def test_dependency_resolution_hint_prints_missing_python_module(monkeypatch, capsys):
    """Validate Dependency resolution hint prints missing python module."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    runtime_cli_utils.print_transcription_dependency_resolution_hint(
        use_gpu=True,
        missing_python_module="torch",
        runner_project_dir_fn=lambda: "/opt/esup-runner/runner",
    )

    captured = capsys.readouterr().out
    assert "- Missing Python module: torch" in captured
    assert "make sync-transcription-gpu" in captured


def test_plan_audio_chunks_splits_long_audio():
    """Validate Plan audio chunks splits long audio."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    chunks = chunking_utils.plan_audio_chunks(
        total_duration_sec=2100.0,
        chunk_duration_sec=600,
        chunk_threshold_sec=1200,
        chunk_overlap_sec=0,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
    )

    assert chunks == [
        (0.0, 600.0),
        (600.0, 600.0),
        (1200.0, 600.0),
        (1800.0, 300.0),
    ]


def test_plan_audio_chunks_uses_overlap_stride():
    """Validate Plan audio chunks uses overlap stride."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    chunks = chunking_utils.plan_audio_chunks(
        total_duration_sec=605.0,
        chunk_duration_sec=300,
        chunk_threshold_sec=120,
        chunk_overlap_sec=3,
        normalize_chunk_overlap_seconds_fn=chunking_utils.normalize_chunk_overlap_seconds,
    )

    assert chunks == [
        (0.0, 300.0),
        (297.0, 300.0),
        (594.0, 11.0),
    ]


def test_resolve_chunk_threshold_seconds_uses_cpu_default():
    """Validate Resolve chunk threshold seconds uses cpu default."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    threshold = chunking_utils.resolve_chunk_threshold_seconds(
        configured_value=None,
        use_gpu=False,
        cpu_threshold_seconds=800,
        gpu_threshold_seconds=1800,
    )

    assert threshold == 800


def test_resolve_chunk_threshold_seconds_uses_gpu_default():
    """Validate Resolve chunk threshold seconds uses gpu default."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    threshold = chunking_utils.resolve_chunk_threshold_seconds(
        configured_value=None,
        use_gpu=True,
        cpu_threshold_seconds=800,
        gpu_threshold_seconds=1800,
    )

    assert threshold == 1800


def test_resolve_chunk_threshold_seconds_keeps_explicit_override():
    """Validate Resolve chunk threshold seconds keeps explicit override."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    threshold = chunking_utils.resolve_chunk_threshold_seconds(
        configured_value="1200",
        use_gpu=False,
        cpu_threshold_seconds=800,
        gpu_threshold_seconds=1800,
    )

    assert threshold == 1200


def test_parse_args_uses_internal_chunk_defaults(monkeypatch):
    """Validate Parse args uses internal chunk defaults."""
    tr = _load_transcription_script_module()

    monkeypatch.setenv("WHISPER_CHUNK_DURATION_SECONDS", "999")
    monkeypatch.setenv("WHISPER_CHUNK_OVERLAP_SECONDS", "9")

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.chunk_duration_seconds == "300"
    assert args.chunk_overlap_seconds == "3"


def test_parse_args_uses_default_huggingface_models_dir(monkeypatch):
    """Validate Parse args uses default huggingface models dir."""
    tr = _load_transcription_script_module()

    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.delenv("CACHE_DIR", raising=False)

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.huggingface_models_dir == "/home/esup-runner/.cache/esup-runner/huggingface"


def test_parse_args_huggingface_default_follows_cache_dir(monkeypatch):
    """Validate Parse args huggingface default follows cache dir."""
    tr = _load_transcription_script_module()

    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.setenv("CACHE_DIR", "/tmp/esup-cache")

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
        ]
    )

    assert args.huggingface_models_dir == "/tmp/esup-cache/huggingface"


def test_load_whisper_model_uses_configured_download_root(monkeypatch, tmp_path):
    """Validate Load whisper model uses configured download root."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    captured = {}

    def fake_load_model(model_name, device=None, download_root=None):
        captured["model_name"] = model_name
        captured["device"] = device
        captured["download_root"] = download_root
        return object()

    fake_whisper = types.SimpleNamespace(load_model=fake_load_model)
    monkeypatch.setitem(sys.modules, "whisper", fake_whisper)

    target_dir = tmp_path / "whisper-cache"
    loaded = runtime_cli_utils.load_whisper_model(
        "small", "cpu", whisper_models_dir=str(target_dir)
    )

    assert loaded is not None
    assert captured["model_name"] == "small"
    assert captured["device"] == "cpu"
    assert captured["download_root"] == str(target_dir)
    assert target_dir.is_dir()


def test_prepare_huggingface_models_dir_creates_directory(tmp_path):
    """Validate Prepare huggingface models dir creates directory."""
    translation_utils = _load_transcription_core_module("translation_utils")

    cache_dir = tmp_path / "hf-cache"

    resolved = translation_utils.prepare_huggingface_models_dir(str(cache_dir), debug=False)

    assert resolved == str(cache_dir)
    assert cache_dir.is_dir()


def test_resolve_translation_model_name_uses_cpu_profile():
    """Validate Resolve translation model name uses cpu profile."""
    translation_utils = _load_transcription_core_module("translation_utils")
    language_utils = _load_transcription_core_module("language_utils")

    model_name = translation_utils.resolve_translation_model_name(
        source_language="fr",
        target_language="en",
        use_gpu=False,
        normalize_language=language_utils.normalize_language_code,
        cpu_model_map={
            ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
            ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
        },
        gpu_model_map={
            ("en", "fr"): "Helsinki-NLP/opus-mt-tc-big-en-fr",
            ("fr", "en"): "Helsinki-NLP/opus-mt-tc-big-fr-en",
        },
    )

    assert model_name == "Helsinki-NLP/opus-mt-fr-en"


def test_resolve_translation_model_name_uses_gpu_profile():
    """Validate Resolve translation model name uses gpu profile."""
    translation_utils = _load_transcription_core_module("translation_utils")
    language_utils = _load_transcription_core_module("language_utils")

    model_name = translation_utils.resolve_translation_model_name(
        source_language="fr",
        target_language="en",
        use_gpu=True,
        normalize_language=language_utils.normalize_language_code,
        cpu_model_map={
            ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
            ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
        },
        gpu_model_map={
            ("en", "fr"): "Helsinki-NLP/opus-mt-tc-big-en-fr",
            ("fr", "en"): "Helsinki-NLP/opus-mt-tc-big-fr-en",
        },
    )

    assert model_name == "Helsinki-NLP/opus-mt-tc-big-fr-en"


def test_load_translation_runtime_passes_cache_dir(tmp_path):
    """Validate Load translation runtime passes cache dir."""
    captured = {}

    class FakeTorch:
        @staticmethod
        def inference_mode():
            class _Ctx:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _Ctx()

    class FakeTokenizerCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["tokenizer_model_name"] = model_name
            captured["tokenizer_cache_dir"] = kwargs.get("cache_dir")
            return object()

    class FakeModel:
        device = "cpu"

        def to(self, _device):
            return self

        def eval(self):
            return None

    class FakeModelCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["model_model_name"] = model_name
            captured["model_cache_dir"] = kwargs.get("cache_dir")
            return FakeModel()

    cache_dir = tmp_path / "hf-cache"
    rc, _torch, runtime, model_name = _load_translation_runtime_with_core_utils(
        source_language="fr",
        target_language="en",
        use_gpu=False,
        huggingface_models_dir=str(cache_dir),
        debug=False,
        import_translation_modules_fn=lambda: (FakeTorch(), FakeModelCls, FakeTokenizerCls),
    )

    assert rc == 0
    assert runtime is not None
    assert model_name == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["tokenizer_cache_dir"] == str(cache_dir)
    assert captured["model_cache_dir"] == str(cache_dir)


def test_load_translation_model_objects_passes_hf_token():
    """Validate Load translation model objects passes hf token."""
    translation_utils = _load_transcription_core_module("translation_utils")

    captured = {}

    class FakeTokenizerCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["tokenizer_model_name"] = model_name
            captured["tokenizer_kwargs"] = kwargs
            return object()

    class FakeModelCls:
        @staticmethod
        def from_pretrained(model_name, **kwargs):
            captured["model_model_name"] = model_name
            captured["model_kwargs"] = kwargs
            return object()

    _tokenizer, _model = translation_utils.load_translation_model_objects(
        FakeTokenizerCls,
        FakeModelCls,
        "Helsinki-NLP/opus-mt-fr-en",
        "/tmp/hf-cache",
        hf_token="hf_test_token",
    )

    assert captured["tokenizer_model_name"] == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["model_model_name"] == "Helsinki-NLP/opus-mt-fr-en"
    assert captured["tokenizer_kwargs"]["cache_dir"] == "/tmp/hf-cache"
    assert captured["model_kwargs"]["cache_dir"] == "/tmp/hf-cache"
    assert captured["tokenizer_kwargs"]["token"] == "hf_test_token"
    assert captured["model_kwargs"]["token"] == "hf_test_token"


def test_run_translation_batch_sets_max_length_none_to_avoid_generation_warning():
    """Validate Run translation batch sets max length none to avoid generation warning."""
    translation_utils = _load_transcription_core_module("translation_utils")

    class FakeTensor:
        def to(self, _device):
            return self

    class FakeTokenizer:
        def __call__(self, texts, return_tensors, padding, truncation, max_length):
            assert texts == ["Bonjour."]
            assert return_tensors == "pt"
            assert padding is True
            assert truncation is True
            assert max_length == 512
            return {"input_ids": FakeTensor(), "attention_mask": FakeTensor()}

        def batch_decode(self, generated, skip_special_tokens):
            assert generated == ["GEN"]
            assert skip_special_tokens is True
            return ["Hello."]

    class FakeModel:
        device = "cpu"

        def __init__(self):
            self.kwargs = None

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return ["GEN"]

    class FakeTorch:
        @staticmethod
        def inference_mode():
            class _Ctx:
                def __enter__(self):
                    return None

                def __exit__(self, exc_type, exc, tb):
                    return False

            return _Ctx()

    fake_model = FakeModel()
    translated = translation_utils.run_translation_batch(
        ["Bonjour."],
        torch=FakeTorch(),
        tokenizer=FakeTokenizer(),
        model=fake_model,
    )

    assert translated == ["Hello."]
    assert fake_model.kwargs is not None
    assert fake_model.kwargs["max_length"] is None
    assert fake_model.kwargs["max_new_tokens"] == 256
    assert fake_model.kwargs["num_beams"] == 4


def test_combine_chunk_results_offsets_segments_and_words():
    """Validate Combine chunk results offsets segments and words."""
    merged = _combine_chunk_results_with_core_utils(
        [
            (
                0.0,
                {
                    "text": "bonjour",
                    "language": "fr",
                    "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": "bonjour"}],
                },
            ),
            (
                600.0,
                {
                    "text": "tout le monde",
                    "segments": [
                        {
                            "id": 0,
                            "start": 0.5,
                            "end": 2.0,
                            "text": "tout le monde",
                            "words": [{"word": "tout", "start": 0.5, "end": 0.8}],
                        }
                    ],
                },
            ),
        ]
    )

    assert merged["language"] == "fr"
    assert merged["text"] == "bonjour tout le monde"
    assert len(merged["segments"]) == 2
    assert merged["segments"][1]["id"] == 1
    assert merged["segments"][1]["start"] == 600.5
    assert merged["segments"][1]["end"] == 602.0
    assert merged["segments"][1]["words"][0]["start"] == 600.5


def test_combine_chunk_results_splits_overlap_without_duplicate_cue():
    """Validate Combine chunk results splits overlap without duplicate cue."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    chunk_plan = [(0.0, 300.0), (297.0, 300.0)]
    keep_windows = [
        chunking_utils.compute_chunk_keep_window(chunk_plan, 0),
        chunking_utils.compute_chunk_keep_window(chunk_plan, 1),
    ]
    merged = _combine_chunk_results_with_core_utils(
        [
            (
                0.0,
                {
                    "text": "bonjour",
                    "segments": [{"id": 0, "start": 296.0, "end": 300.0, "text": "bonjour"}],
                },
            ),
            (
                297.0,
                {
                    "text": "bonjour",
                    "segments": [{"id": 0, "start": 0.0, "end": 3.0, "text": "bonjour"}],
                },
            ),
        ],
        keep_windows=keep_windows,
    )

    assert len(merged["segments"]) == 1
    assert merged["segments"][0]["text"] == "bonjour"
    assert merged["segments"][0]["start"] == 296.0
    assert merged["segments"][0]["end"] == 300.0


def test_validate_vtt_coverage_rejects_truncated_output(tmp_path):
    """Validate Validate vtt coverage rejects truncated output."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:15:02.000\n" "Texte\n\n",
        encoding="utf-8",
    )

    rc = _validate_vtt_coverage_with_core_utils(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 7


def test_validate_vtt_coverage_accepts_small_trailing_gap(tmp_path):
    """Validate Validate vtt coverage accepts small trailing gap."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:32:30.000\n" "Texte\n\n",
        encoding="utf-8",
    )

    rc = _validate_vtt_coverage_with_core_utils(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 0


def test_validate_vtt_coverage_accepts_empty_vtt_for_no_speech_audio(tmp_path):
    """Validate Validate vtt coverage accepts empty vtt for no speech audio."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text("WEBVTT\n\n", encoding="utf-8")

    rc = _validate_vtt_coverage_with_core_utils(
        vtt_path=vtt_path,
        reference_duration_sec=2100.0,
        min_coverage_ratio=0.75,
        max_final_gap_sec=300.0,
        debug=False,
    )

    assert rc == 0


def test_validate_vtt_internal_gaps_rejects_suspicious_hole(tmp_path):
    """Validate Validate vtt internal gaps rejects suspicious hole."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Bonjour\n\n"
        "00:00:30.200 --> 00:00:34.000\n"
        "Reprise\n\n",
        encoding="utf-8",
    )

    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=vtt_path,
        max_internal_gap_sec=15.0,
        max_internal_gap_count=0,
        debug=False,
    )

    assert rc == 8


def test_validate_vtt_internal_gaps_accepts_short_pause(tmp_path):
    """Validate Validate vtt internal gaps accepts short pause."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Bonjour\n\n"
        "00:00:07.000 --> 00:00:09.000\n"
        "Suite\n\n",
        encoding="utf-8",
    )

    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=vtt_path,
        max_internal_gap_sec=15.0,
        max_internal_gap_count=0,
        debug=False,
    )

    assert rc == 0


def test_validate_vtt_internal_gaps_accepts_when_gap_count_within_budget(tmp_path):
    """Validate Validate vtt internal gaps accepts when gap count within budget."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Bonjour\n\n"
        "00:00:30.000 --> 00:00:33.000\n"
        "Suite\n\n",
        encoding="utf-8",
    )

    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=vtt_path,
        max_internal_gap_sec=15.0,
        max_internal_gap_count=1,
        debug=False,
    )

    assert rc == 0


def test_detect_vtt_internal_gaps_reports_expected_metrics(tmp_path):
    """Validate Detect vtt internal gaps reports expected metrics."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Bonjour\n\n"
        "00:00:30.200 --> 00:00:34.000\n"
        "Reprise\n\n",
        encoding="utf-8",
    )

    analysis = _detect_vtt_internal_gaps_with_core_utils(vtt_path, max_internal_gap_sec=15.0)

    assert analysis["read_ok"] is True
    assert analysis["cue_count"] == 2
    assert analysis["gap_count"] == 1
    assert round(float(analysis["largest_gap_sec"]), 3) == 25.2
    assert int(analysis["gaps"][0]["line_number"]) == 6


def test_attempt_best_effort_gap_repair_skips_when_no_suspicious_gap(tmp_path):
    """Validate Attempt best effort gap repair skips when no suspicious gap."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "Bonjour\n\n"
        "00:00:06.000 --> 00:00:10.000\n"
        "Suite\n\n",
        encoding="utf-8",
    )

    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=vtt_path,
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=3,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        probe_duration_seconds_fn=lambda _path: 0.0,
    )

    assert metadata["detected_before_count"] == 0
    assert metadata["rerun_attempted"] is False
    assert metadata["note"] == "no_suspicious_gap_detected"


def test_read_vtt_cue_time_ranges_ignores_malformed_invalid_and_reversed_cues(tmp_path):
    """Validate Read vtt cue time ranges ignores malformed invalid and reversed cues."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 -->\n"
        "Missing end\n\n"
        "XX:YY --> 00:00:03.000\n"
        "Invalid start\n\n"
        "00:00:05.000 --> 00:00:04.000\n"
        "Reversed\n\n"
        "00:00:01.000 --> 00:00:02.000\n"
        "Valid\n\n",
        encoding="utf-8",
    )

    read_ok, cues = _read_vtt_cue_time_ranges_with_core_utils(vtt_path)

    assert read_ok is True
    assert cues == [(1.0, 2.0, 12)]


def test_read_vtt_cue_time_ranges_returns_false_when_read_fails(tmp_path):
    """Validate Read vtt cue time ranges returns false when read fails."""
    read_ok, cues = _read_vtt_cue_time_ranges_with_core_utils(tmp_path / "missing.vtt")

    assert read_ok is False
    assert cues == []


def test_detect_vtt_internal_gaps_handles_unreadable_vtt(monkeypatch, tmp_path):
    """Validate Detect vtt internal gaps handles unreadable vtt."""
    analysis = _detect_vtt_internal_gaps_with_core_utils(
        tmp_path / "missing.vtt",
        max_internal_gap_sec=12.0,
        read_vtt_cue_time_ranges_fn=lambda _p: (False, []),
    )

    assert analysis["read_ok"] is False
    assert analysis["gap_threshold_sec"] == 12.0
    assert analysis["gap_count"] == 0


def test_format_vtt_timestamp_clamps_negative_values():
    """Validate Format vtt timestamp clamps negative values."""
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    assert validation_utils.format_vtt_timestamp(-2.345) == "00:00:00.000"
    assert validation_utils.format_vtt_timestamp(3661.2) == "01:01:01.200"


def test_read_vtt_cues_filters_invalid_ranges_and_empty_text(tmp_path):
    """Validate Read vtt cues filters invalid ranges and empty text."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:00.500\n"
        "Reversed\n\n"
        "AA:BB --> 00:00:02.000\n"
        "Invalid\n\n"
        "00:00:03.000 --> 00:00:04.000\n"
        "\n\n"
        "00:00:05.000 --> 00:00:06.000\n"
        "Texte valide\n\n",
        encoding="utf-8",
    )

    read_ok, cues = _read_vtt_cues_with_core_utils(vtt_path)

    assert read_ok is True
    assert cues == [(5.0, 6.0, "Texte valide")]


def test_read_vtt_cues_skips_blocks_with_empty_cue_prefix(monkeypatch, tmp_path):
    """Validate Read vtt cues skips blocks with empty cue prefix."""
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    vtt_utils = _load_transcription_core_module("vtt_postprocess_utils")
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nTexte\n", encoding="utf-8")

    parse_range = lambda line: vtt_utils.parse_vtt_cue_time_range(  # noqa: E731
        line,
        parse_vtt_timestamp_fn=validation_utils.parse_vtt_timestamp,
    )
    read_ok, cues = gap_utils.read_vtt_cues(
        vtt_path,
        parse_vtt_postprocess_block_fn=lambda _block: ([], "Texte"),
        parse_vtt_cue_time_range_fn=parse_range,
        normalize_vtt_cue_text_fn=vtt_utils.normalize_vtt_cue_text,
    )

    assert read_ok is True
    assert cues == []


def test_read_vtt_cues_returns_false_when_file_missing(tmp_path):
    """Validate Read vtt cues returns false when file missing."""
    read_ok, cues = _read_vtt_cues_with_core_utils(tmp_path / "missing.vtt")

    assert read_ok is False
    assert cues == []


def test_dedupe_sorted_vtt_cues_dedupes_and_merges_adjacent_identical_texts():
    """Validate Dedupe sorted vtt cues dedupes and merges adjacent identical texts."""
    merged = _dedupe_sorted_vtt_cues_with_core_utils(
        [
            (2.00, 3.00, "Hello"),
            (1.00, 2.00, "Hello"),
            (1.00, 2.04, "Hello"),  # almost same window -> drop duplicate
            (4.00, 3.00, "bad"),  # invalid
            (5.00, 5.50, "   "),  # empty normalized text
            (3.50, 4.00, "Bye"),
        ]
    )

    assert merged == [(1.0, 3.0, "Hello"), (3.5, 4.0, "Bye")]


def test_render_vtt_from_cues_keeps_header_and_skips_empty_wrapped_cues():
    """Validate Render vtt from cues keeps header and skips empty wrapped cues."""
    rendered = _render_vtt_from_cues_with_core_utils(
        [(0.0, 1.0, ""), (1.0, 2.0, "bonjour tout le monde")],
        max_line_width=12,
        max_line_count=2,
    )

    assert rendered.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in rendered
    assert "bonjour" in rendered


def test_run_gap_window_rerun_returns_false_when_chunk_extraction_fails(tmp_path):
    """Validate Run gap window rerun returns false when chunk extraction fails."""
    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 22,
        run_whisper_python_fn=lambda **kwargs: (0, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: None,
        read_vtt_cues_fn=lambda _path: (True, []),
    )

    assert ok is False
    assert cues == []


def test_run_gap_window_rerun_returns_false_when_transcribe_rc_is_non_zero(tmp_path):
    """Validate Run gap window rerun returns false when transcribe rc is non zero."""
    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 0,
        run_whisper_python_fn=lambda **kwargs: (42, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: None,
        read_vtt_cues_fn=lambda _path: (True, []),
    )

    assert ok is False
    assert cues == []


def test_run_gap_window_rerun_uses_non_dotted_unique_clip_stem(tmp_path):
    """Validate Run gap window rerun uses non dotted unique clip stem."""
    captured = {}
    generated_vtt = tmp_path / "out" / "audio_name_gap_0000010000_0008000.vtt"
    generated_vtt.parent.mkdir(parents=True, exist_ok=True)
    generated_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    def fake_extract_audio_chunk(**kwargs):
        captured["chunk_path"] = kwargs["chunk_path"]
        return 0

    def fake_find_generated_vtt(audio_path, _out_dir):
        captured["audio_path"] = audio_path
        return generated_vtt

    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.name.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=fake_extract_audio_chunk,
        run_whisper_python_fn=lambda **kwargs: (0, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=fake_find_generated_vtt,
        read_vtt_cues_fn=lambda _path: (True, [(2.0, 3.0, "inside")]),
    )

    assert ok is True
    assert cues == [(12.0, 13.0, "inside")]
    assert captured["chunk_path"].stem == "audio_name_gap_0000010000_0008000"
    assert captured["audio_path"].stem == "audio_name_gap_0000010000_0008000"
    assert "." not in captured["chunk_path"].stem


def test_run_gap_window_rerun_uses_cli_fallback_and_filters_out_of_gap_cues(tmp_path):
    """Validate Run gap window rerun uses cli fallback and filters out of gap cues."""
    generated_vtt = tmp_path / "out" / "clip.vtt"
    generated_vtt.parent.mkdir(parents=True, exist_ok=True)
    generated_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 0,
        run_whisper_python_fn=lambda **kwargs: (255, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: generated_vtt,
        read_vtt_cues_fn=lambda _path: (
            True,
            [
                (0.0, 1.0, "too early"),
                (1.5, 2.5, "inside"),
                (4.6, 5.0, "too late"),
            ],
        ),
    )

    assert ok is True
    assert cues == [(11.5, 12.5, "inside")]


def test_run_gap_window_rerun_skips_recovered_cues_with_non_positive_duration(tmp_path):
    """Validate Run gap window rerun skips recovered cues with non positive duration."""
    generated_vtt = tmp_path / "out" / "clip.vtt"
    generated_vtt.parent.mkdir(parents=True, exist_ok=True)
    generated_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=11.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 0,
        run_whisper_python_fn=lambda **kwargs: (0, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: generated_vtt,
        read_vtt_cues_fn=lambda _path: (
            True,
            [
                (2.0, 2.0, "invalid"),
                (2.0, 3.0, "valid"),
            ],
        ),
    )

    assert ok is True
    assert cues == [(12.0, 13.0, "valid")]


def test_run_gap_window_rerun_returns_false_when_generated_vtt_is_missing(tmp_path):
    """Validate Run gap window rerun returns false when generated vtt is missing."""
    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 0,
        run_whisper_python_fn=lambda **kwargs: (0, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: None,
        read_vtt_cues_fn=lambda _path: (True, []),
    )

    assert ok is False
    assert cues == []


def test_run_gap_window_rerun_returns_false_when_local_cues_cannot_be_read(tmp_path):
    """Validate Run gap window rerun returns false when local cues cannot be read."""
    generated_vtt = tmp_path / "out" / "clip.vtt"
    generated_vtt.parent.mkdir(parents=True, exist_ok=True)
    generated_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    ok, cues = _run_gap_window_rerun_with_core_utils(
        audio_src=tmp_path / "audio.mp3",
        out_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        transcription_language="auto",
        start_sec=10.0,
        duration_sec=8.0,
        gap_start_sec=12.0,
        gap_end_sec=14.0,
        overlap_tolerance_sec=0.4,
        debug=False,
        extract_audio_chunk_fn=lambda **kwargs: 0,
        run_whisper_python_fn=lambda **kwargs: (0, None),
        run_whisper_cli_fn=lambda **kwargs: (0, None),
        find_generated_vtt_fn=lambda _audio_path, _out_dir: generated_vtt,
        read_vtt_cues_fn=lambda _path: (False, []),
    )

    assert ok is False
    assert cues == []


def test_attempt_best_effort_gap_repair_reports_vtt_read_failure(tmp_path):
    """Validate Attempt best effort gap repair reports vtt read failure."""
    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=3,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {
            "read_ok": False,
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        },
    )

    assert metadata["note"] == "vtt_read_failed"


def test_attempt_best_effort_gap_repair_reports_vtt_cue_parse_failure(tmp_path):
    """Validate Attempt best effort gap repair reports vtt cue parse failure."""
    analyses = [
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 18.0,
            "gaps": [
                {
                    "gap_sec": 18.0,
                    "previous_end_sec": 10.0,
                    "next_start_sec": 28.0,
                    "line_number": 42,
                }
            ],
        }
    ]
    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=3,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=lambda _p, _s: analyses[0],
        read_vtt_cues_fn=lambda _p: (False, []),
    )

    assert metadata["note"] == "vtt_cue_parse_failed"


def test_attempt_best_effort_gap_repair_skips_too_short_window(tmp_path):
    """Validate Attempt best effort gap repair skips too short window."""
    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=3,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 18.0,
            "gaps": [
                {
                    "gap_sec": 18.0,
                    "previous_end_sec": 5.0,
                    "next_start_sec": 5.0,
                    "line_number": 42,
                }
            ],
        },
        read_vtt_cues_fn=lambda _path: (True, [(0.0, 1.0, "Intro")]),
        probe_duration_seconds_fn=lambda _path: 4.5,
        run_gap_window_rerun_fn=lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("Should not be called")
        ),
    )

    assert metadata["rerun_attempted"] is False
    assert metadata["rerun_attempts"] == 0


def test_attempt_best_effort_gap_repair_continues_when_rerun_returns_no_cues(tmp_path):
    """Validate Attempt best effort gap repair continues when rerun returns no cues."""
    analyses = [
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 18.0,
            "gaps": [
                {
                    "gap_sec": 18.0,
                    "previous_end_sec": 5.0,
                    "next_start_sec": 8.0,
                    "line_number": 42,
                }
            ],
        },
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 18.0,
            "gaps": [],
        },
    ]
    call_index = {"i": 0}

    def fake_detect(_path, _threshold):
        idx = min(call_index["i"], len(analyses) - 1)
        call_index["i"] += 1
        return analyses[idx]

    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=3,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=fake_detect,
        read_vtt_cues_fn=lambda _path: (True, [(0.0, 1.0, "Intro")]),
        probe_duration_seconds_fn=lambda _path: 50.0,
        run_gap_window_rerun_fn=lambda **kwargs: (False, []),
    )

    assert metadata["rerun_attempted"] is True
    assert metadata["rerun_attempts"] == 1
    assert metadata["rerun_successes"] == 0
    assert metadata["inserted_cue_count"] == 0


def test_attempt_best_effort_gap_repair_merges_recovered_cues(tmp_path):
    """Validate Attempt best effort gap repair merges recovered cues."""
    vtt_path = tmp_path / "subtitles.vtt"
    vtt_path.write_text("WEBVTT\n\n", encoding="utf-8")

    analyses = [
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 15.0,
            "gaps": [
                {
                    "gap_sec": 15.0,
                    "previous_end_sec": 5.0,
                    "next_start_sec": 20.0,
                    "line_number": 10,
                }
            ],
        },
        {
            "read_ok": True,
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        },
    ]

    call_index = {"i": 0}

    def fake_detect(_path, _threshold):
        idx = min(call_index["i"], len(analyses) - 1)
        call_index["i"] += 1
        return analyses[idx]

    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=vtt_path,
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=2,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=fake_detect,
        read_vtt_cues_fn=lambda _path: (True, [(0.0, 5.0, "Intro"), (20.0, 22.0, "Suite")]),
        probe_duration_seconds_fn=lambda _path: 30.0,
        run_gap_window_rerun_fn=lambda **kwargs: (True, [(6.0, 7.0, "Recup")]),
        render_vtt_from_cues_fn=lambda cues, **kwargs: "WEBVTT\n\n",
        postprocess_vtt_file_fn=lambda *args, **kwargs: None,
    )

    assert metadata["rerun_attempted"] is True
    assert metadata["rerun_attempts"] == 1
    assert metadata["rerun_successes"] == 1
    assert metadata["inserted_cue_count"] == 1
    assert metadata["detected_after_count"] == 0
    assert metadata["repair_improved_or_equal"] is True


def test_attempt_best_effort_gap_repair_sets_repair_improved_or_equal_false_when_gap_count_worsens(
    tmp_path,
):
    """Validate Attempt best effort gap repair sets repair improved or equal false when gap count worsens."""
    analyses = [
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 15.0,
            "gaps": [
                {
                    "gap_sec": 15.0,
                    "previous_end_sec": 5.0,
                    "next_start_sec": 20.0,
                    "line_number": 10,
                }
            ],
        },
        {
            "read_ok": True,
            "gap_count": 2,
            "largest_gap_sec": 22.0,
            "gaps": [],
        },
    ]
    call_index = {"i": 0}

    def fake_detect(_path, _threshold):
        idx = min(call_index["i"], len(analyses) - 1)
        call_index["i"] += 1
        return analyses[idx]

    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=2,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=fake_detect,
        read_vtt_cues_fn=lambda _path: (True, [(0.0, 5.0, "Intro")]),
        probe_duration_seconds_fn=lambda _path: 30.0,
        run_gap_window_rerun_fn=lambda **kwargs: (False, []),
    )

    assert metadata["repair_improved_or_equal"] is False


def test_attempt_best_effort_gap_repair_reverts_when_repair_worsens_analysis(tmp_path):
    """Validate Attempt best effort gap repair reverts when repair worsens analysis."""
    vtt_path = tmp_path / "subtitles.vtt"
    original_content = "WEBVTT\n\n00:00:00.000 --> 00:00:05.000\nIntro\n\n"
    vtt_path.write_text(original_content, encoding="utf-8")

    analyses = [
        {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 15.0,
            "gaps": [
                {
                    "gap_sec": 15.0,
                    "previous_end_sec": 5.0,
                    "next_start_sec": 20.0,
                    "line_number": 10,
                }
            ],
        },
        {
            "read_ok": True,
            "gap_count": 2,
            "largest_gap_sec": 22.0,
            "gaps": [],
        },
    ]
    call_index = {"i": 0}

    def fake_detect(_path, _threshold):
        idx = min(call_index["i"], len(analyses) - 1)
        call_index["i"] += 1
        return analyses[idx]

    metadata = _attempt_best_effort_gap_repair_with_core_utils(
        vtt_path=vtt_path,
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        use_gpu=False,
        gpu_device=0,
        vad_filter=True,
        timeout_sec=30,
        detected_language="fr",
        max_internal_gap_sec=15.0,
        max_repair_attempts=2,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        detect_vtt_internal_gaps_fn=fake_detect,
        read_vtt_cues_fn=lambda _path: (True, [(0.0, 5.0, "Intro")]),
        probe_duration_seconds_fn=lambda _path: 30.0,
        run_gap_window_rerun_fn=lambda **kwargs: (True, [(6.0, 7.0, "Bad repair")]),
        render_vtt_from_cues_fn=lambda cues, **kwargs: "WEBVTT\n\nworse\n",
        postprocess_vtt_file_fn=lambda *args, **kwargs: None,
    )

    assert metadata["repair_improved_or_equal"] is False
    assert metadata["repair_reverted"] is True
    assert metadata["detected_after_count"] == 1
    assert vtt_path.read_text(encoding="utf-8") == original_content


def test_write_repaired_vtt_from_cues_continues_when_original_read_fails(monkeypatch, tmp_path):
    """Validate Write repaired vtt from cues continues when original read fails."""
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    vtt_path = tmp_path / "subtitles.vtt"

    monkeypatch.setattr(
        gap_utils.Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read failed")),
    )

    context = gap_utils.AttemptGapRepairContext(
        context_padding_seconds=1.0,
        min_window_seconds=2.0,
        overlap_tolerance_seconds=0.4,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {},
        read_vtt_cues_fn=lambda _path: (True, []),
        probe_duration_seconds_fn=lambda _path: 0.0,
        normalize_language_fn=lambda language: language,
        resolve_transcription_language_fn=lambda _requested: "auto",
        run_gap_window_rerun_fn=lambda **_kwargs: (False, []),
        dedupe_sorted_vtt_cues_fn=lambda cues: cues,
        render_vtt_from_cues_fn=lambda _cues, **_kwargs: "WEBVTT\n\nrepaired\n",
        postprocess_vtt_file_fn=lambda *_args, **_kwargs: None,
    )

    original_content = gap_utils.write_repaired_vtt_from_cues(
        vtt_path=vtt_path,
        existing_cues=[],
        inserted_cues=[(0.0, 1.0, "repaired")],
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=context,
    )

    assert original_content is None
    assert vtt_path.read_bytes() == b"WEBVTT\n\nrepaired\n"


def test_validate_vtt_internal_gaps_returns_zero_when_threshold_is_disabled(tmp_path):
    """Validate Validate vtt internal gaps returns zero when threshold is disabled."""
    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        max_internal_gap_sec=0.0,
        max_internal_gap_count=0,
        debug=False,
    )

    assert rc == 0


def test_validate_vtt_internal_gaps_returns_error_when_vtt_cannot_be_read(tmp_path):
    """Validate Validate vtt internal gaps returns error when vtt cannot be read."""
    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        max_internal_gap_sec=15.0,
        max_internal_gap_count=0,
        debug=False,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {
            "read_ok": False,
            "cue_count": 0,
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        },
    )

    assert rc == 8


def test_validate_vtt_internal_gaps_accepts_when_there_is_less_than_two_cues(tmp_path):
    """Validate Validate vtt internal gaps accepts when there is less than two cues."""
    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        max_internal_gap_sec=15.0,
        max_internal_gap_count=0,
        debug=False,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {
            "read_ok": True,
            "cue_count": 1,
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        },
    )

    assert rc == 0


def test_validate_vtt_internal_gaps_debug_mode_logs_metrics(tmp_path, capsys):
    """Validate Validate vtt internal gaps debug mode logs metrics."""
    rc = _validate_vtt_internal_gaps_with_core_utils(
        vtt_path=tmp_path / "subtitles.vtt",
        max_internal_gap_sec=15.0,
        max_internal_gap_count=0,
        debug=True,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {
            "read_ok": True,
            "cue_count": 2,
            "gap_count": 0,
            "largest_gap_sec": 0.0,
            "gaps": [],
        },
    )

    captured = capsys.readouterr().out
    assert rc == 0
    assert "VTT internal-gap validation:" in captured


def test_default_non_blocking_internal_gap_metadata_sets_error_only_when_provided():
    """Validate Default non blocking internal gap metadata sets error only when provided."""
    without_error = _default_non_blocking_internal_gap_metadata_with_core_utils(note="ok")
    with_error = _default_non_blocking_internal_gap_metadata_with_core_utils(
        note="boom",
        error="stack",
    )

    assert without_error["note"] == "ok"
    assert "error" not in without_error
    assert with_error["error"] == "stack"


def test_run_non_blocking_internal_gap_repair_returns_warning_payload_when_repair_raises(tmp_path):
    """Validate Run non blocking internal gap repair returns warning payload when repair raises."""

    def boom(**kwargs):
        raise RuntimeError("repair failed")

    args = types.SimpleNamespace(
        model="turbo",
        whisper_models_dir=str(tmp_path / "models"),
        gpu_device=0,
        vad_filter="true",
    )
    metadata = _run_non_blocking_internal_gap_repair_with_core_utils(
        expected_vtt=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        args=args,
        timeout_sec=30,
        effective_use_gpu=False,
        detected_language="fr",
        vtt_max_line_width=40,
        vtt_max_line_count=2,
        debug=False,
        attempt_best_effort_vtt_internal_gap_repair_fn=boom,
    )

    assert metadata["note"] == "pre_translation_repair_exception"
    assert "repair failed" in metadata["error"]


def test_run_non_blocking_internal_gap_repair_logs_when_gaps_were_detected(tmp_path, capsys):
    """Validate Run non blocking internal gap repair logs when gaps were detected."""
    metadata = _run_non_blocking_internal_gap_repair_with_core_utils(
        expected_vtt=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        work_dir=tmp_path / "out",
        args=types.SimpleNamespace(
            model="turbo",
            whisper_models_dir=str(tmp_path / "models"),
            gpu_device=0,
            vad_filter="true",
        ),
        timeout_sec=30,
        effective_use_gpu=False,
        detected_language="fr",
        vtt_max_line_width=40,
        vtt_max_line_count=2,
        debug=False,
        attempt_best_effort_vtt_internal_gap_repair_fn=lambda **kwargs: {
            "detected_before_count": 2,
            "inserted_cue_count": 1,
            "detected_after_count": 1,
        },
    )

    captured = capsys.readouterr().out
    assert metadata["detected_before_count"] == 2
    assert "VTT internal-gap check (non-blocking)" in captured


def test_build_whisper_fallback_options_exposes_expected_runtime_values(monkeypatch):
    """Validate Build whisper fallback options exposes expected runtime values."""
    flow_utils = _load_transcription_core_module("transcription_flow_utils")
    args = types.SimpleNamespace(
        model="turbo",
        whisper_models_dir="/tmp/w",
        gpu_device=3,
        vad_filter="true",
        chunk_duration_seconds="300",
        chunk_overlap_seconds="4",
        chunk_threshold_seconds="",
        vtt_highlight_words="false",
    )

    context = flow_utils.TranscriptionFlowContext(
        resolve_transcription_language_fn=lambda _requested: "auto",
        resolve_chunk_threshold_seconds_fn=lambda **_kwargs: 777,
        run_whisper_python_fn=lambda **_kwargs: (0, None),
        run_whisper_cli_fn=lambda **_kwargs: (0, None),
        normalize_language_code_fn=lambda language: language,
    )

    options = flow_utils.build_whisper_fallback_options(
        args=args,
        effective_use_gpu=True,
        timeout_sec=91,
        vtt_max_line_count=2,
        vtt_max_line_width=44,
        context=context,
    )

    assert options["model"] == "turbo"
    assert options["whisper_models_dir"] == "/tmp/w"
    assert options["use_gpu"] is True
    assert options["gpu_device"] == 3
    assert options["chunk_threshold_sec"] == 777
    assert options["timeout_sec"] == 91


def test_validate_final_vtt_and_collect_gap_analysis_returns_coverage_error_early(tmp_path):
    """Validate Validate final vtt and collect gap analysis returns coverage error early."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")

    calls = {"n": 0}

    def fake_probe(_path, debug=False):
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 42.0

    context = output_utils.FinalVttValidationContext(
        min_vtt_coverage_ratio=0.75,
        max_vtt_final_gap_seconds=300.0,
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
        probe_duration_seconds_fn=fake_probe,
        validate_vtt_coverage_fn=lambda **_kwargs: 7,
        validate_vtt_internal_gaps_fn=lambda **_kwargs: 0,
        detect_vtt_internal_gaps_fn=lambda _path, _threshold: {},
    )

    rc, analysis = output_utils.validate_final_vtt_and_collect_gap_analysis(
        expected_vtt=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        input_path=tmp_path / "input.mp3",
        debug=False,
        context=context,
    )

    assert rc == 7
    assert analysis == {}
    assert calls["n"] == 2


def test_validate_final_vtt_and_collect_gap_analysis_logs_non_blocking_warning(tmp_path, capsys):
    """Validate Validate final vtt and collect gap analysis logs non blocking warning."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")

    calls = {"n": 0}

    def fake_probe(_path, debug=False):
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 42.0

    context = output_utils.FinalVttValidationContext(
        min_vtt_coverage_ratio=0.75,
        max_vtt_final_gap_seconds=300.0,
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
        probe_duration_seconds_fn=fake_probe,
        validate_vtt_coverage_fn=lambda **_kwargs: 0,
        validate_vtt_internal_gaps_fn=lambda **_kwargs: 8,
        detect_vtt_internal_gaps_fn=lambda _vtt_path, _threshold: {
            "read_ok": True,
            "cue_count": 4,
            "gap_count": 2,
            "largest_gap_sec": 20.0,
            "gaps": [],
        },
    )

    rc, analysis = output_utils.validate_final_vtt_and_collect_gap_analysis(
        expected_vtt=tmp_path / "subtitles.vtt",
        audio_src=tmp_path / "audio.mp3",
        input_path=tmp_path / "input.mp3",
        debug=False,
        context=context,
    )

    captured = capsys.readouterr().out
    assert rc == 0
    assert analysis["gap_count"] == 2
    assert "VTT internal-gap warning (non-blocking)" in captured
    assert calls["n"] == 2


def test_parse_vtt_timestamp_accepts_minutes_seconds_format():
    """Validate Parse vtt timestamp accepts minutes seconds format."""
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    parsed = validation_utils.parse_vtt_timestamp("19:55.154")

    assert parsed == 1195.154


def test_parse_vtt_timestamp_accepts_comma_decimal_marker():
    """Validate Parse vtt timestamp accepts comma decimal marker."""
    validation_utils = _load_transcription_core_module("vtt_validation_utils")

    parsed = validation_utils.parse_vtt_timestamp("01:02:03,456")

    assert parsed == 3723.456


def test_hf_hub_warning_filter_drops_unauthenticated_hub_warning():
    """Validate Hf hub warning filter drops unauthenticated hub warning."""
    translation_utils = _load_transcription_core_module("translation_utils")

    matching = types.SimpleNamespace(
        getMessage=lambda: "You are sending unauthenticated requests to the HF Hub."
    )
    other = types.SimpleNamespace(getMessage=lambda: "another warning")

    assert translation_utils.HF_HUB_WARNING_FILTER.filter(matching) is False
    assert translation_utils.HF_HUB_WARNING_FILTER.filter(other) is True


def test_hf_hub_warning_filter_returns_true_when_record_message_fails():
    """Validate Hf hub warning filter returns true when record message fails."""
    translation_utils = _load_transcription_core_module("translation_utils")

    class _BrokenRecord:
        @staticmethod
        def getMessage():
            raise RuntimeError("boom")

    assert translation_utils.HF_HUB_WARNING_FILTER.filter(_BrokenRecord()) is True


def test_apply_runtime_cuda_environment_prefers_explicit_cuda_visible_devices_env(monkeypatch):
    """Validate Apply runtime cuda environment prefers explicit cuda visible devices env."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    monkeypatch.setenv("GPU_CUDA_VISIBLE_DEVICES", "2,3")
    monkeypatch.setenv("GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "9")
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)

    runtime_cli_utils.apply_runtime_cuda_environment(gpu_device=0)

    assert os.getenv("CUDA_VISIBLE_DEVICES") == "2,3"
    assert os.getenv("CUDA_DEVICE_ORDER") == "PCI_BUS_ID"


def test_apply_runtime_cuda_environment_falls_back_to_gpu_device_when_env_missing(monkeypatch):
    """Validate Apply runtime cuda environment falls back to gpu device when env missing."""
    runtime_cli_utils = _load_transcription_core_module("runtime_cli_utils")

    monkeypatch.delenv("GPU_CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("GPU_CUDA_DEVICE_ORDER", raising=False)
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("CUDA_DEVICE_ORDER", raising=False)

    runtime_cli_utils.apply_runtime_cuda_environment(gpu_device=7)

    assert os.getenv("CUDA_VISIBLE_DEVICES") == "7"
    assert os.getenv("CUDA_DEVICE_ORDER") is None


def test_build_transcribe_kwargs_disables_previous_text_conditioning_for_chunked_runs():
    """Validate Build transcribe kwargs disables previous text conditioning for chunked runs."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    kwargs = chunking_utils.build_transcribe_kwargs(
        "fr",
        vad_filter=True,
        device="cpu",
        chunked=True,
    )

    assert kwargs["condition_on_previous_text"] is False
    assert kwargs["temperature"] == 0.0
    assert kwargs["word_timestamps"] is True
    assert kwargs["hallucination_silence_threshold"] == 2.0


def test_build_transcribe_kwargs_keeps_previous_text_conditioning_for_single_pass_runs():
    """Validate Build transcribe kwargs keeps previous text conditioning for single pass runs."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    kwargs = chunking_utils.build_transcribe_kwargs(
        "fr",
        vad_filter=True,
        device="cpu",
        chunked=False,
    )

    assert kwargs["condition_on_previous_text"] is True
    assert kwargs["word_timestamps"] is True


def test_filter_result_segments_drops_punctuation_only_filler():
    """Validate Filter result segments drops punctuation only filler."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "... Bonjour ...",
        "segments": [
            {"id": 0, "text": "...", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Bonjour", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result)

    assert filtered["text"] == "Bonjour"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Bonjour"


def test_filter_result_segments_drops_silence_hallucination_segment():
    """Validate Filter result segments drops silence hallucination segment."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "A Blood說, bon terrain sérieux. Salut",
        "segments": [
            {
                "id": 0,
                "text": "A Blood說, bon terrain sérieux.",
                "start": 0.0,
                "end": 2.0,
                "no_speech_prob": 0.91,
                "avg_logprob": -1.2,
                "compression_ratio": 3.5,
            },
            {
                "id": 1,
                "text": "Salut",
                "start": 2.0,
                "end": 3.0,
                "no_speech_prob": 0.05,
                "avg_logprob": -0.2,
                "compression_ratio": 1.4,
            },
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result)

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_unexpected_script_for_french():
    """Validate Filter result segments drops unexpected script for french."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "Sneкая Bonjour",
        "segments": [
            {"id": 0, "text": "Sneкая", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Bonjour", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Bonjour"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Bonjour"


def test_filter_result_segments_drops_subtitle_credit_hallucination():
    """Validate Filter result segments drops subtitle credit hallucination."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "Sous-titrage ST' 501 Salut",
        "segments": [
            {"id": 0, "text": "Sous-titrage ST' 501", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Salut", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_subtitle_credit_without_digits():
    """Validate Filter result segments drops subtitle credit without digits."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "Sous-titrage Société Radio-Canada Salut",
        "segments": [
            {"id": 0, "text": "Sous-titrage Société Radio-Canada", "start": 0.0, "end": 1.0},
            {"id": 1, "text": "Salut", "start": 1.0, "end": 2.0},
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_filter_result_segments_drops_numeric_repetition_loop():
    """Validate Filter result segments drops numeric repetition loop."""
    segment_filter_utils = _load_transcription_core_module("segment_filter_utils")

    result = {
        "text": "On voit les yeux 1,2,3,1,2,3,1,2,3,1,2,3,1,2,3,1,2,3",
        "segments": [
            {
                "id": 0,
                "text": "On voit les yeux 1,2,3,1,2,3,1,2,3,1,2,3,1,2,3,1,2,3",
                "start": 0.0,
                "end": 5.0,
            },
            {"id": 1, "text": "Salut", "start": 5.0, "end": 6.0},
        ],
    }

    filtered = segment_filter_utils.filter_result_segments(result, expected_language="fr")

    assert filtered["text"] == "Salut"
    assert len(filtered["segments"]) == 1
    assert filtered["segments"][0]["text"] == "Salut"


def test_build_initial_prompt_from_text_keeps_tail():
    """Validate Build initial prompt from text keeps tail."""
    chunking_utils = _load_transcription_core_module("chunking_utils")

    prompt = chunking_utils.build_initial_prompt_from_text("un deux trois quatre", max_chars=6)

    assert prompt == "quatre"


def test_postprocess_vtt_content_rewraps_cue_text_after_elision_fix():
    """Validate Postprocess vtt content rewraps cue text after elision fix."""
    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:05.000\n"
        "si on s\n"
        "'est déjà vus. Aujourd'hui on parle.\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=22,
        max_line_count=2,
    )

    assert "s'est" in processed
    assert "s\n'" not in processed
    cue_lines = processed.strip().split("\n\n")[1].splitlines()[1:]
    assert len(cue_lines) <= 2


def test_postprocess_vtt_content_splits_long_cues_to_two_lines_of_forty_chars():
    """Validate Postprocess vtt content splits long cues to display-safe blocks."""
    content = (
        "WEBVTT\n\n"
        "05:01.900 --> 05:07.220\n"
        "quand même revenir en premier lieu sur\n"
        "les questions de la recherche, puisque\n"
        "l'Université\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=40,
        max_line_count=2,
    )

    cue_blocks = [
        block for block in processed.strip().split("\n\n") if "-->" in block.splitlines()[0]
    ]
    assert len(cue_blocks) == 2
    assert "l'Université" in processed
    for cue_block in cue_blocks:
        cue_lines = cue_block.splitlines()[1:]
        assert 1 <= len(cue_lines) <= 2
        assert all(len(line) <= 40 for line in cue_lines)


def test_postprocess_vtt_content_repairs_french_apostrophe_split_across_cues():
    """Validate Postprocess vtt content repairs french apostrophe split across cues."""
    content = (
        "WEBVTT\n\n"
        "00:10.420 --> 00:14.480\n"
        "je suis ravie de vous retrouver si on s\n\n"
        "00:14.480 --> 00:19.580\n"
        "'est déjà vus. Enchantée pour les autres.\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=40,
        max_line_count=2,
    )

    assert "s'est" in processed
    assert "\n'est déjà" not in processed


def test_postprocess_vtt_content_repairs_english_contraction_split_across_cues():
    """Validate Postprocess vtt content repairs english contraction split across cues."""
    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "we\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "'re ready to go.\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=40,
        max_line_count=2,
    )

    assert "we're" in processed
    assert "\n're ready" not in processed


def test_postprocess_vtt_content_drops_duplicate_french_apostrophe_overlap_across_cues():
    """Validate Postprocess vtt content drops duplicate french apostrophe overlap across cues."""
    content = (
        "WEBVTT\n\n"
        "01:04:19.900 --> 01:04:22.500\n"
        "veut dire qu'au sein de l'institution,\n\n"
        "01:04:22.500 --> 01:04:26.060\n"
        "'institution, on propose de ne pas réinventer.\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=48,
        max_line_count=2,
    )

    assert "l'institution" in processed
    assert "'institution, on propose" not in processed


def test_postprocess_vtt_content_drops_duplicate_english_contraction_overlap_across_cues():
    """Validate Postprocess vtt content drops duplicate english contraction overlap across cues."""
    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "and we're\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "'re still checking the last details.\n"
    )

    processed = _postprocess_vtt_content_with_core_utils(
        content,
        max_line_width=48,
        max_line_count=2,
    )

    assert "and we're" in processed
    assert "'re still checking" not in processed


def test_translate_vtt_content_preserves_timestamps_and_translates_cues():
    """Validate Translate vtt content preserves timestamps and translates cues."""
    content = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.000\n"
        "Bonjour tout le monde.\n\n"
        "00:00:02.000 --> 00:00:04.000\n"
        "N'hésitez pas.\n"
    )

    translated = _translate_vtt_content_with_core_utils(
        content,
        translate_batch=lambda batch: [
            "Hello everyone." if "Bonjour" in text else "Don't hesitate." for text in batch
        ],
        max_line_width=32,
        max_line_count=2,
        batch_size=1,
    )

    assert "00:00:00.000 --> 00:00:02.000" in translated
    assert "00:00:02.000 --> 00:00:04.000" in translated
    assert "Hello everyone." in translated
    assert "Don't hesitate." in translated


def test_translate_vtt_file_rewrites_final_vtt_and_keeps_source_sidecar(tmp_path):
    """Validate Translate vtt file rewrites final vtt and keeps source sidecar."""
    vtt_path = tmp_path / "audio_192k_test.vtt"
    vtt_path.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Bonjour.\n",
        encoding="utf-8",
    )

    def fake_load_translation_runtime(**_kwargs):
        return (
            0,
            object(),
            ("fake-tokenizer", "fake-model"),
            "Helsinki-NLP/opus-mt-fr-en",
        )

    def fake_run_translation_batch(texts, *, torch, tokenizer, model):
        assert tokenizer == "fake-tokenizer"
        assert model == "fake-model"
        return ["Hello." for _text in texts]

    rc, translation_metadata = _translate_vtt_file_with_core_utils(
        vtt_path,
        source_language="fr",
        target_language="en",
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=32,
        max_line_count=2,
        debug=False,
        load_translation_runtime_fn=fake_load_translation_runtime,
        run_translation_batch_fn=fake_run_translation_batch,
    )

    source_sidecar = tmp_path / "audio_192k_test.source-fr.webvtt.txt"
    assert rc == 0
    assert translation_metadata["applied"] is True
    assert translation_metadata["model"] == "Helsinki-NLP/opus-mt-fr-en"
    assert translation_metadata["hardware_profile"] == "cpu"
    assert source_sidecar.exists()
    assert "Bonjour." in source_sidecar.read_text(encoding="utf-8")
    assert "Hello." in vtt_path.read_text(encoding="utf-8")


def test_maybe_translate_final_vtt_skips_when_requested_language_matches_detected(tmp_path):
    """Validate Maybe translate final vtt skips when requested language matches detected."""
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Hello.\n",
        encoding="utf-8",
    )

    rc, translation_metadata, final_language = _maybe_translate_final_vtt_with_core_utils(
        audio_src,
        work_dir,
        requested_language="en",
        detected_language="en",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    assert rc == 0
    assert final_language == "en"
    assert translation_metadata["applied"] is False
    assert not (work_dir / "audio_192k_test.source-en.webvtt.txt").exists()


def test_maybe_translate_final_vtt_accepts_empty_vtt_for_non_verbal_audio(tmp_path):
    """Validate Maybe translate final vtt accepts empty vtt for non verbal audio."""
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text("WEBVTT\n\n", encoding="utf-8")

    rc, translation_metadata, final_language = _maybe_translate_final_vtt_with_core_utils(
        audio_src,
        work_dir,
        requested_language="en",
        detected_language=None,
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
    )

    assert rc == 0
    assert final_language == "en"
    assert translation_metadata["applied"] is False
    assert translation_metadata["backend"] == "none"
    assert translation_metadata["note"] == "no_speech_or_non_verbal_audio"
    assert not (work_dir / "audio_192k_test.source-en.webvtt.txt").exists()


def test_maybe_translate_final_vtt_uses_whisper_legacy_fallback_for_unsupported_pair(tmp_path):
    """Validate Maybe translate final vtt uses whisper legacy fallback for unsupported pair."""
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    audio_src = tmp_path / "audio_192k_test.mp3"
    audio_src.write_bytes(b"fake-audio")
    expected_vtt = work_dir / "audio_192k_test.vtt"
    expected_vtt.write_text(
        "WEBVTT\n\n" "00:00:00.000 --> 00:00:02.000\n" "Bonjour.\n",
        encoding="utf-8",
    )

    def fake_run_legacy_whisper_translation_fallback(*_args, **_kwargs):
        return (
            0,
            {
                "applied": True,
                "backend": "whisper_legacy_fallback",
                "source_language": "fr",
                "target_language": "de",
                "model": "large-v3",
                "hardware_profile": "gpu",
            },
            "de",
        )

    rc, translation_metadata, final_language = _maybe_translate_final_vtt_with_core_utils(
        audio_src,
        work_dir,
        requested_language="de",
        detected_language="fr",
        whisper_fallback_options={
            "model": "turbo",
            "use_gpu": True,
            "gpu_device": 0,
            "vad_filter": True,
            "timeout_sec": 60,
            "chunk_duration_sec": 300,
            "chunk_overlap_sec": 3,
            "chunk_threshold_sec": 1800,
            "vtt_highlight_words": False,
            "vtt_max_line_count": 2,
            "vtt_max_line_width": 40,
        },
        use_gpu=True,
        huggingface_models_dir="/tmp/hf-cache",
        max_line_width=40,
        max_line_count=2,
        debug=False,
        run_legacy_whisper_translation_fallback_fn=fake_run_legacy_whisper_translation_fallback,
    )

    assert rc == 0
    assert final_language == "de"
    assert translation_metadata["applied"] is True
    assert translation_metadata["backend"] == "whisper_legacy_fallback"
    assert translation_metadata["model"] == "large-v3"


def test_build_transcription_runtime_metadata_includes_translation_model():
    """Validate Build transcription runtime metadata includes translation model."""
    metadata = _build_transcription_runtime_metadata_with_core_utils(
        requested_language="en",
        detected_language="fr",
        final_language="en",
        whisper_model="turbo",
        use_gpu=True,
        translation={
            "applied": True,
            "backend": "local_translation",
            "source_language": "fr",
            "target_language": "en",
            "model": "Helsinki-NLP/opus-mt-tc-big-fr-en",
            "hardware_profile": "gpu",
        },
    )

    assert metadata["transcription"]["whisper_model"] == "turbo"
    assert metadata["transcription"]["requested_source_language"] == "auto"
    assert metadata["transcription"]["detected_source_language"] == "fr"
    assert metadata["transcription"]["final_subtitle_language"] == "en"
    assert metadata["transcription"]["translation"]["backend"] == "local_translation"
    assert metadata["transcription"]["translation"]["model"] == "Helsinki-NLP/opus-mt-tc-big-fr-en"


def test_build_transcription_runtime_metadata_includes_internal_gap_report():
    """Validate Build transcription runtime metadata includes internal gap report."""
    metadata = _build_transcription_runtime_metadata_with_core_utils(
        requested_language="auto",
        detected_language="fr",
        final_language="fr",
        whisper_model="turbo",
        use_gpu=False,
        translation={
            "applied": False,
            "backend": "none",
            "source_language": "fr",
            "target_language": "fr",
            "model": None,
            "hardware_profile": "cpu",
        },
        vtt_internal_gaps={"enabled": True, "blocking": False, "final_output": {"gap_count": 2}},
    )

    assert metadata["transcription"]["vtt_internal_gaps"]["enabled"] is True
    assert metadata["transcription"]["vtt_internal_gaps"]["blocking"] is False
    assert metadata["transcription"]["vtt_internal_gaps"]["final_output"]["gap_count"] == 2


def test_write_info_video_metadata_merges_runtime_details(tmp_path):
    """Validate Write info video metadata merges runtime details."""
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)

    _write_info_video_metadata_with_core_utils(work_dir, {"video_id": "abc123"}, debug=False)
    _write_info_video_metadata_with_core_utils(
        work_dir,
        {
            "transcription": {
                "translation": {
                    "applied": True,
                    "backend": "local_translation",
                    "model": "Helsinki-NLP/opus-mt-fr-en",
                }
            }
        },
        debug=False,
    )

    info_content = (work_dir / "info_video.json").read_text(encoding="utf-8")
    assert '"video_id": "abc123"' in info_content
    assert '"model": "Helsinki-NLP/opus-mt-fr-en"' in info_content


def test_extract_video_identification_from_args_ignores_empty_values():
    """Validate Extract video identification from args ignores empty values."""
    metadata_utils = _load_transcription_core_module("metadata_utils")
    args = types.SimpleNamespace(video_id="abc123", video_slug="", video_title="  ")

    metadata = metadata_utils.extract_video_identification_from_args(args)

    assert metadata == {"video_id": "abc123"}


def test_transcription_flow_context_mode_runs_python_path(tmp_path):
    """Validate Transcription flow context mode runs python path."""
    flow_utils = _load_transcription_core_module("transcription_flow_utils")
    args = types.SimpleNamespace(
        language="en",
        chunk_threshold_seconds="",
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        gpu_device=0,
        vad_filter="true",
        chunk_duration_seconds="300",
        chunk_overlap_seconds="4",
        vtt_highlight_words="false",
        vtt_max_line_count="2",
        vtt_max_line_width="40",
    )

    seen = {"python_called": 0, "cli_called": 0}

    context = flow_utils.TranscriptionFlowContext(
        resolve_transcription_language_fn=lambda _requested: "auto",
        resolve_chunk_threshold_seconds_fn=lambda **kwargs: 777,
        run_whisper_python_fn=lambda **kwargs: (
            seen.__setitem__("python_called", seen["python_called"] + 1) or 0,
            "fr",
        ),
        run_whisper_cli_fn=lambda **kwargs: (
            seen.__setitem__("cli_called", seen["cli_called"] + 1) or 255,
            None,
        ),
        normalize_language_code_fn=lambda language: language,
    )

    rc, detected_language = flow_utils.run_transcription(
        args,
        tmp_path / "audio.mp3",
        tmp_path / "output",
        60,
        False,
        False,
        context=context,
    )
    options = flow_utils.build_whisper_fallback_options(
        args=args,
        effective_use_gpu=False,
        timeout_sec=60,
        vtt_max_line_count=2,
        vtt_max_line_width=40,
        context=context,
    )

    assert rc == 0
    assert detected_language == "fr"
    assert seen["python_called"] == 1
    assert seen["cli_called"] == 0
    assert options["chunk_threshold_sec"] == 777


def test_output_validation_context_mode_collects_analysis(tmp_path):
    """Validate Output validation context mode collects analysis."""
    output_utils = _load_transcription_core_module("output_validation_flow_utils")
    vtt_path = tmp_path / "audio.vtt"
    vtt_path.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nok\n", encoding="utf-8")
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"fake-audio")

    context = output_utils.FinalVttValidationContext(
        min_vtt_coverage_ratio=0.75,
        max_vtt_final_gap_seconds=300.0,
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
        probe_duration_seconds_fn=lambda _path, _debug: 10.0,
        validate_vtt_coverage_fn=lambda **kwargs: 0,
        validate_vtt_internal_gaps_fn=lambda **kwargs: 0,
        detect_vtt_internal_gaps_fn=lambda _vtt, _threshold: {
            "read_ok": True,
            "gap_count": 1,
            "largest_gap_sec": 12.0,
            "gaps": [{"gap_sec": 12.0}],
        },
    )

    rc, analysis = output_utils.validate_final_vtt_and_collect_gap_analysis(
        expected_vtt=vtt_path,
        audio_src=audio_path,
        input_path=audio_path,
        debug=False,
        context=context,
    )

    assert rc == 0
    assert analysis["read_ok"] is True
    assert analysis["gap_count"] == 1


def test_translation_flow_context_mode_auto_language_short_circuit(tmp_path):
    """Validate Translation flow context mode auto language short circuit."""
    flow_utils = _load_transcription_core_module("translation_flow_utils")

    context = flow_utils.TranslationDecisionContext(
        translation_backend_none="none",
        translation_backend_local="local_translation",
        translation_backend_whisper_legacy="whisper_legacy_fallback",
        translation_decision_failed_rc=33,
        translation_unsupported_pair_rc=30,
        normalize_language_fn=lambda language: language,
        build_translation_metadata_fn=lambda **kwargs: kwargs,
        check_translation_input_vtt_fn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("preflight must not run when requested_language is auto")
        ),
        resolve_translation_model_name_fn=lambda *_args, **_kwargs: None,
        run_legacy_whisper_translation_fallback_fn=lambda *_args, **_kwargs: (
            0,
            {"applied": False},
            None,
        ),
        translate_vtt_file_fn=lambda *_args, **_kwargs: (0, {"applied": True}),
    )

    audio_src = tmp_path / "audio.mp3"
    audio_src.write_bytes(b"fake-audio")
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)

    rc, metadata, final_language = flow_utils.maybe_translate_final_vtt(
        audio_src,
        work_dir,
        requested_language="auto",
        detected_language="fr",
        whisper_fallback_options=None,
        use_gpu=False,
        huggingface_models_dir=None,
        max_line_width=40,
        max_line_count=2,
        debug=False,
        context=context,
    )

    assert rc == 0
    assert final_language == "fr"
    assert metadata["backend"] == "none"
    assert metadata["applied"] is False


def test_gap_repair_context_mode_returns_attempt_metadata(tmp_path):
    """Validate Gap repair context mode returns attempt metadata."""
    gap_utils = _load_transcription_core_module("gap_repair_utils")
    expected_vtt = tmp_path / "audio.vtt"
    expected_vtt.write_text("WEBVTT\n", encoding="utf-8")
    audio_src = tmp_path / "audio.mp3"
    audio_src.write_bytes(b"fake-audio")
    work_dir = tmp_path / "output"
    work_dir.mkdir(parents=True)
    args = types.SimpleNamespace(
        model="small",
        whisper_models_dir=str(tmp_path / "models"),
        gpu_device="0",
        vad_filter="false",
    )

    context = gap_utils.NonBlockingGapRepairContext(
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
        max_internal_gap_repair_attempts=3,
        attempt_best_effort_vtt_internal_gap_repair_fn=lambda **kwargs: {
            "detected_before_count": 2,
            "inserted_cue_count": 1,
            "detected_after_count": 1,
        },
        default_non_blocking_internal_gap_metadata_fn=lambda **kwargs: kwargs,
    )

    metadata = gap_utils.run_non_blocking_internal_gap_repair(
        expected_vtt=expected_vtt,
        audio_src=audio_src,
        work_dir=work_dir,
        args=args,
        timeout_sec=120,
        effective_use_gpu=False,
        detected_language="fr",
        vtt_max_line_width=40,
        vtt_max_line_count=2,
        debug=False,
        context=context,
    )

    assert metadata["detected_before_count"] == 2
    assert metadata["inserted_cue_count"] == 1
    assert metadata["detected_after_count"] == 1


def test_main_orchestration_context_mode_runs_end_to_end(tmp_path, capsys):
    """Validate Main orchestration context mode runs end to end."""
    main_utils = _load_transcription_core_module("main_orchestration_utils")
    base_dir = tmp_path / "base"
    base_dir.mkdir(parents=True)
    input_path = base_dir / "input.mp4"
    input_path.write_bytes(b"fake-video")
    work_dir = base_dir / "work"
    work_dir.mkdir(parents=True)

    args = types.SimpleNamespace(
        base_dir=str(base_dir),
        input_file="input.mp4",
        work_dir="work",
        debug="false",
        vtt_max_line_count="2",
        vtt_max_line_width="40",
        use_gpu="false",
        gpu_device="0",
        language="en",
        huggingface_models_dir="",
        model="small",
    )

    context = main_utils.MainFlowContext(
        extract_video_identification_fn=lambda _args: {"video_id": "abc"},
        compute_timeout_fn=lambda _args, _input, _debug: 30,
        prepare_audio_source_fn=lambda _args, input_file, _work, _timeout, _debug: (0, input_file),
        resolve_effective_use_gpu_fn=lambda _requested, _gpu_device, _debug: False,
        run_transcription_fn=lambda *_args, **_kwargs: (0, "fr"),
        finalize_vtt_fn=lambda *_args, **_kwargs: 0,
        run_non_blocking_internal_gap_repair_fn=lambda **_kwargs: {
            "detected_before_count": 0,
            "inserted_cue_count": 0,
            "detected_after_count": 0,
        },
        build_whisper_fallback_options_fn=lambda **_kwargs: {},
        maybe_translate_final_vtt_fn=lambda *_args, **_kwargs: (
            0,
            {
                "applied": True,
                "backend": "local_translation",
                "source_language": "fr",
                "target_language": "en",
            },
            "en",
        ),
        validate_final_vtt_and_collect_gap_analysis_fn=lambda **_kwargs: (
            0,
            {"read_ok": True, "gap_count": 0, "largest_gap_sec": 0.0, "gaps": []},
        ),
        build_transcription_runtime_metadata_fn=lambda **_kwargs: {"transcription": {}},
        write_info_video_metadata_fn=lambda *_args, **_kwargs: None,
        max_vtt_internal_gap_seconds=15.0,
        max_vtt_internal_gap_count=0,
    )

    rc = main_utils.run_main_flow(args, context=context)
    output = capsys.readouterr().out
    assert rc == 0
    assert "Resolved source audio language: fr" in output
    assert (
        "Subtitle processing mode: translation " "(source_language=fr, target_language=en)"
    ) in output
