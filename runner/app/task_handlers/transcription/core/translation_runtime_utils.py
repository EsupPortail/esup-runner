"""Default subtitle-translation runtime wiring.

Connects runtime loading, translation decisions, and VTT translation helpers.
Adapts translation outcomes into metadata and final validation expectations.
Keeps end-to-end translation behavior consistent across CPU/GPU execution modes.
"""

import os
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, cast

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

import language_utils
import metadata_utils
import output_validation_runtime_utils
import runtime_cli_utils
import translation_decision_flow_utils
import translation_flow_contexts
import translation_runtime_flow_utils
import translation_utils
import translation_vtt_file_flow_utils
import vtt_postprocess_utils
import vtt_validation_utils
import whisper_python_runtime_utils
from runtime_args_utils import (
    _CPU_TRANSLATION_MODELS,
    _GPU_TRANSLATION_MODELS,
    _TRANSLATION_BACKEND_LOCAL,
    _TRANSLATION_BACKEND_NONE,
    _TRANSLATION_BACKEND_UNAVAILABLE_RC,
    _TRANSLATION_BACKEND_WHISPER_LEGACY,
    _TRANSLATION_BATCH_SIZE,
    _TRANSLATION_DECISION_FAILED_RC,
    _TRANSLATION_FAILED_RC,
    _TRANSLATION_UNSUPPORTED_PAIR_RC,
)


def build_source_vtt_sidecar_path(vtt_path: Path, source_language: str) -> Path:
    """Return the sidecar path used to preserve the pre-translation source VTT."""
    return cast(
        Path,
        translation_utils.build_source_vtt_sidecar_path(
            vtt_path,
            source_language,
            normalize_language=language_utils.normalize_language_code,
        ),
    )


def resolve_translation_model_name(
    source_language: Optional[str],
    target_language: Optional[str],
    use_gpu: bool,
) -> Optional[str]:
    """Return the internal translation model name for the requested language pair."""
    return cast(
        Optional[str],
        translation_utils.resolve_translation_model_name(
            source_language=source_language,
            target_language=target_language,
            use_gpu=use_gpu,
            normalize_language=language_utils.normalize_language_code,
            cpu_model_map=_CPU_TRANSLATION_MODELS,
            gpu_model_map=_GPU_TRANSLATION_MODELS,
        ),
    )


def build_translation_metadata(
    *,
    applied: bool,
    backend: str,
    source_language: Optional[str],
    target_language: Optional[str],
    model: Optional[str],
    use_gpu: bool,
    source_sidecar: Optional[str] = None,
    execution_backend: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Build normalized translation metadata for info_video.json."""
    return cast(
        Dict[str, Any],
        metadata_utils.build_translation_metadata(
            applied=applied,
            backend=backend,
            source_language=source_language,
            target_language=target_language,
            model=model,
            use_gpu=use_gpu,
            normalize_language=language_utils.normalize_language_code,
            source_sidecar=source_sidecar,
            execution_backend=execution_backend,
            note=note,
        ),
    )


def load_translation_model_objects(
    auto_tokenizer_cls: Any,
    auto_model_cls: Any,
    model_name: str,
    cache_dir: Optional[str],
) -> tuple[Any, Any]:
    """Load tokenizer/model objects using the configured Hugging Face token."""
    hf_token = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_HUB_TOKEN") or "").strip()
    return cast(
        tuple[Any, Any],
        translation_utils.load_translation_model_objects(
            auto_tokenizer_cls,
            auto_model_cls,
            model_name,
            cache_dir,
            hf_token=hf_token,
        ),
    )


def load_translation_runtime(
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    debug: bool,
) -> tuple[int, Optional[Any], Optional[Any], Optional[str]]:
    """Load the internal FR<->EN subtitle translation runtime."""
    context = translation_flow_contexts.TranslationRuntimeContext(
        translation_unsupported_pair_rc=_TRANSLATION_UNSUPPORTED_PAIR_RC,
        translation_backend_unavailable_rc=_TRANSLATION_BACKEND_UNAVAILABLE_RC,
        cpu_translation_models=_CPU_TRANSLATION_MODELS,
        resolve_translation_model_name_fn=resolve_translation_model_name,
        import_translation_modules_fn=translation_utils.import_translation_modules,
        prepare_huggingface_models_dir_fn=translation_utils.prepare_huggingface_models_dir,
        load_translation_model_objects_fn=load_translation_model_objects,
        place_translation_model_on_device_fn=translation_utils.place_translation_model_on_device,
    )
    return cast(
        tuple[int, Optional[Any], Optional[Any], Optional[str]],
        translation_runtime_flow_utils.load_translation_runtime(
            source_language=source_language,
            target_language=target_language,
            use_gpu=use_gpu,
            huggingface_models_dir=huggingface_models_dir,
            debug=debug,
            context=context,
        ),
    )


run_translation_batch = translation_utils.run_translation_batch


def translate_cue_texts(
    cue_texts: list[str],
    *,
    translate_batch: Callable[[list[str]], list[str]],
    batch_size: int,
) -> list[str]:
    """Translate cue texts in small batches and keep empty outputs safe."""
    return cast(
        list[str],
        translation_utils.translate_cue_texts(
            cue_texts,
            translate_batch=translate_batch,
            batch_size=batch_size,
            normalize_vtt_cue_text=vtt_postprocess_utils.normalize_vtt_cue_text,
        ),
    )


def translate_vtt_content(
    content: str,
    *,
    translate_batch: Callable[[list[str]], list[str]],
    max_line_width: int,
    max_line_count: int,
    batch_size: int = _TRANSLATION_BATCH_SIZE,
) -> str:
    """Translate VTT cue texts while preserving timestamps and block structure."""
    return cast(
        str,
        translation_utils.translate_vtt_content(
            content,
            translate_batch=translate_batch,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            batch_size=batch_size,
            parse_vtt_postprocess_block=vtt_postprocess_utils.parse_vtt_postprocess_block,
            normalize_vtt_cue_text=vtt_postprocess_utils.normalize_vtt_cue_text,
            translate_cue_texts_fn=translate_cue_texts,
            repair_cross_cue_apostrophe_splits=lambda blocks: vtt_postprocess_utils.repair_cross_cue_apostrophe_splits_with_defaults(
                blocks,
                parse_vtt_timestamp_fn=vtt_validation_utils.parse_vtt_timestamp,
            ),
            render_postprocessed_vtt_blocks=vtt_postprocess_utils.render_postprocessed_vtt_blocks_with_defaults,
        ),
    )


def build_translate_vtt_file_context() -> translation_flow_contexts.TranslateVttFileContext:
    """Build the default context used by `translate_vtt_file`."""
    return translation_flow_contexts.TranslateVttFileContext(
        translation_backend_local=_TRANSLATION_BACKEND_LOCAL,
        translation_failed_rc=_TRANSLATION_FAILED_RC,
        translation_batch_size=_TRANSLATION_BATCH_SIZE,
        build_translation_metadata_fn=build_translation_metadata,
        load_translation_runtime_fn=load_translation_runtime,
        build_source_vtt_sidecar_path_fn=build_source_vtt_sidecar_path,
        run_translation_batch_fn=run_translation_batch,
        translate_vtt_content_fn=translate_vtt_content,
    )


def translate_vtt_file(
    vtt_path: Path,
    *,
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> tuple[int, Dict[str, Any]]:
    """Translate a finalized VTT with the default local translation runtime."""
    return cast(
        tuple[int, Dict[str, Any]],
        translation_vtt_file_flow_utils.translate_vtt_file(
            vtt_path,
            source_language=source_language,
            target_language=target_language,
            use_gpu=use_gpu,
            huggingface_models_dir=huggingface_models_dir,
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            debug=debug,
            context=build_translate_vtt_file_context(),
        ),
    )


def run_whisper_with_explicit_language(
    audio_src: Path,
    work_dir: Path,
    *,
    language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
) -> tuple[int, str, str]:
    """Run Whisper with an explicit target language for legacy best-effort fallback."""
    return cast(
        tuple[int, str, str],
        translation_decision_flow_utils.run_whisper_with_explicit_language(
            audio_src,
            work_dir,
            language=language,
            whisper_fallback_options=whisper_fallback_options,
            debug=debug,
            run_whisper_python_fn=whisper_python_runtime_utils.run_whisper_python,
            run_whisper_cli_fn=runtime_cli_utils.run_whisper_cli_with_defaults,
            map_model_name_fn=runtime_cli_utils.map_model_name,
        ),
    )


def run_legacy_whisper_translation_fallback(
    audio_src: Path,
    work_dir: Path,
    *,
    source_language: str,
    target_language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Fallback to the historical Whisper-only multilingual behavior."""
    return cast(
        tuple[int, Dict[str, Any], Optional[str]],
        translation_decision_flow_utils.run_legacy_whisper_translation_fallback(
            audio_src,
            work_dir,
            source_language=source_language,
            target_language=target_language,
            whisper_fallback_options=whisper_fallback_options,
            debug=debug,
            translation_backend_whisper_legacy=_TRANSLATION_BACKEND_WHISPER_LEGACY,
            build_translation_metadata_fn=build_translation_metadata,
            map_model_name_fn=runtime_cli_utils.map_model_name,
            build_source_vtt_sidecar_path_fn=build_source_vtt_sidecar_path,
            run_whisper_with_explicit_language_fn=run_whisper_with_explicit_language,
            finalize_vtt_fn=output_validation_runtime_utils.finalize_vtt,
            normalize_language_fn=language_utils.normalize_language_code,
        ),
    )


def check_translation_input_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language: Optional[str],
    use_gpu: bool,
    debug: bool,
) -> tuple[Optional[Path], Optional[tuple[int, Dict[str, Any], Optional[str]]]]:
    """Validate the finalized VTT before the translation decision tree runs."""
    return cast(
        tuple[Optional[Path], Optional[tuple[int, Dict[str, Any], Optional[str]]]],
        translation_decision_flow_utils.check_translation_input_vtt(
            audio_src,
            work_dir,
            requested_language=requested_language,
            detected_language=detected_language,
            use_gpu=use_gpu,
            debug=debug,
            translation_backend_local=_TRANSLATION_BACKEND_LOCAL,
            translation_backend_none=_TRANSLATION_BACKEND_NONE,
            build_translation_metadata_fn=build_translation_metadata,
            normalize_language_fn=language_utils.normalize_language_code,
            resolve_translation_model_name_fn=resolve_translation_model_name,
            read_last_vtt_cue_end_seconds_fn=output_validation_runtime_utils.read_last_vtt_cue_end_seconds,
        ),
    )


def build_translation_decision_context() -> translation_flow_contexts.TranslationDecisionContext:
    """Build the default context for deciding whether/how to translate final subtitles."""
    return translation_flow_contexts.TranslationDecisionContext(
        translation_backend_none=_TRANSLATION_BACKEND_NONE,
        translation_backend_local=_TRANSLATION_BACKEND_LOCAL,
        translation_backend_whisper_legacy=_TRANSLATION_BACKEND_WHISPER_LEGACY,
        translation_decision_failed_rc=_TRANSLATION_DECISION_FAILED_RC,
        translation_unsupported_pair_rc=_TRANSLATION_UNSUPPORTED_PAIR_RC,
        normalize_language_fn=language_utils.normalize_language_code,
        build_translation_metadata_fn=build_translation_metadata,
        check_translation_input_vtt_fn=check_translation_input_vtt,
        resolve_translation_model_name_fn=resolve_translation_model_name,
        run_legacy_whisper_translation_fallback_fn=run_legacy_whisper_translation_fallback,
        translate_vtt_file_fn=translate_vtt_file,
    )


def maybe_translate_final_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language: Optional[str],
    whisper_fallback_options: Optional[Dict[str, Any]],
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    max_line_width: int,
    max_line_count: int,
    debug: bool,
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Translate the finalized VTT only when requested and source/target differ."""
    return cast(
        tuple[int, Dict[str, Any], Optional[str]],
        translation_decision_flow_utils.maybe_translate_final_vtt(
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
            context=build_translation_decision_context(),
        ),
    )
