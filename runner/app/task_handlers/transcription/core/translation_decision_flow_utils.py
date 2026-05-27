"""Translation decision and Whisper fallback flow helpers.

Decides if translation should run from languages, runtime, and VTT availability.
Handles explicit-language Whisper fallbacks when auto-detection is ambiguous.
Normalizes return codes so translation decisions remain traceable by callers.
"""

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, cast

_CORE_DIR = Path(__file__).resolve().parent

if str(_CORE_DIR) not in sys.path:  # pragma: no cover - direct file-spec import guard
    sys.path.insert(0, str(_CORE_DIR))

from translation_flow_contexts import (
    TranslationDecisionContext,
)


def run_whisper_with_explicit_language(
    audio_src: Path,
    work_dir: Path,
    *,
    language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
    run_whisper_python_fn: Callable[..., tuple[int, Optional[str]]],
    run_whisper_cli_fn: Callable[..., tuple[int, Optional[str]]],
    map_model_name_fn: Callable[[str, str], str],
) -> tuple[int, str, str]:
    """Run Whisper with explicit target language for legacy fallback behavior."""
    logical_model = str(whisper_fallback_options["model"])
    use_gpu = bool(whisper_fallback_options["use_gpu"])
    rc, _detected_lang = run_whisper_python_fn(
        audio_path=audio_src,
        out_dir=work_dir,
        language=language,
        model=logical_model,
        whisper_models_dir=str(whisper_fallback_options.get("whisper_models_dir", "")),
        use_gpu=use_gpu,
        gpu_device=int(whisper_fallback_options["gpu_device"]),
        vad_filter=bool(whisper_fallback_options["vad_filter"]),
        timeout_sec=int(whisper_fallback_options["timeout_sec"]),
        chunk_duration_sec=int(whisper_fallback_options["chunk_duration_sec"]),
        chunk_overlap_sec=int(whisper_fallback_options["chunk_overlap_sec"]),
        chunk_threshold_sec=int(whisper_fallback_options["chunk_threshold_sec"]),
        vtt_highlight_words=bool(whisper_fallback_options["vtt_highlight_words"]),
        vtt_max_line_count=int(whisper_fallback_options["vtt_max_line_count"]),
        vtt_max_line_width=int(whisper_fallback_options["vtt_max_line_width"]),
        debug=debug,
    )
    effective_model_name = map_model_name_fn(logical_model, "python")
    execution_backend = "whisper_python"
    if rc == 255:
        rc, _detected_lang = run_whisper_cli_fn(
            audio_path=audio_src,
            out_dir=work_dir,
            language=language,
            model=logical_model,
            whisper_models_dir=str(whisper_fallback_options.get("whisper_models_dir", "")),
            use_gpu=use_gpu,
            gpu_device=int(whisper_fallback_options["gpu_device"]),
            vad_filter=bool(whisper_fallback_options["vad_filter"]),
            timeout_sec=int(whisper_fallback_options["timeout_sec"]),
            debug=debug,
        )
        effective_model_name = map_model_name_fn(logical_model, "cli")
        execution_backend = "whisper_cli"
    return rc, execution_backend, effective_model_name


def run_legacy_whisper_translation_fallback(
    audio_src: Path,
    work_dir: Path,
    *,
    source_language: str,
    target_language: str,
    whisper_fallback_options: Dict[str, Any],
    debug: bool,
    translation_backend_whisper_legacy: str,
    build_translation_metadata_fn: Callable[..., Dict[str, Any]],
    map_model_name_fn: Callable[[str, str], str],
    build_source_vtt_sidecar_path_fn: Callable[[Path, str], Path],
    run_whisper_with_explicit_language_fn: Callable[..., tuple[int, str, str]],
    finalize_vtt_fn: Callable[..., int],
    normalize_language_fn: Callable[[Optional[str]], Optional[str]],
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Fallback to historical Whisper-only multilingual behavior."""
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    if not expected_vtt.exists():
        print(f"VTT output not found before Whisper fallback translation: {expected_vtt}")
        metadata = build_translation_metadata_fn(
            applied=False,
            backend=translation_backend_whisper_legacy,
            source_language=source_language,
            target_language=target_language,
            model=map_model_name_fn(str(whisper_fallback_options["model"]), "python"),
            use_gpu=bool(whisper_fallback_options["use_gpu"]),
            note="best_effort_multilingual_whisper_fallback",
        )
        return 5, metadata, None

    original_content = expected_vtt.read_text(encoding="utf-8")
    source_sidecar_path = build_source_vtt_sidecar_path_fn(expected_vtt, source_language)
    source_sidecar_path.write_text(original_content, encoding="utf-8")
    expected_vtt.unlink(missing_ok=True)

    rc, execution_backend, effective_model_name = run_whisper_with_explicit_language_fn(
        audio_src,
        work_dir,
        language=target_language,
        whisper_fallback_options=whisper_fallback_options,
        debug=debug,
    )
    translation_metadata = build_translation_metadata_fn(
        applied=False,
        backend=translation_backend_whisper_legacy,
        source_language=source_language,
        target_language=target_language,
        model=effective_model_name,
        use_gpu=bool(whisper_fallback_options["use_gpu"]),
        source_sidecar=str(source_sidecar_path.name),
        execution_backend=execution_backend,
        note="best_effort_multilingual_whisper_fallback",
    )
    if rc != 0:
        expected_vtt.write_text(original_content, encoding="utf-8")
        return rc, translation_metadata, source_language

    rc = finalize_vtt_fn(
        audio_src,
        work_dir,
        max_line_count=int(whisper_fallback_options["vtt_max_line_count"]),
        max_line_width=int(whisper_fallback_options["vtt_max_line_width"]),
        debug=debug,
    )
    if rc != 0:
        expected_vtt.write_text(original_content, encoding="utf-8")
        return rc, translation_metadata, source_language

    translation_metadata["applied"] = True
    return 0, translation_metadata, normalize_language_fn(target_language)


def check_translation_input_vtt(
    audio_src: Path,
    work_dir: Path,
    *,
    requested_language: str,
    detected_language: Optional[str],
    use_gpu: bool,
    debug: bool,
    translation_backend_local: str,
    translation_backend_none: str,
    build_translation_metadata_fn: Callable[..., Dict[str, Any]],
    normalize_language_fn: Callable[[Optional[str]], Optional[str]],
    resolve_translation_model_name_fn: Callable[..., Optional[str]],
    read_last_vtt_cue_end_seconds_fn: Callable[[Path], tuple[bool, bool, Optional[float]]],
) -> tuple[Optional[Path], Optional[tuple[int, Dict[str, Any], Optional[str]]]]:
    """Validate the finalized VTT before the translation decision tree runs."""
    expected_vtt = work_dir / f"{Path(audio_src).stem}.vtt"
    if not expected_vtt.exists():
        print(f"VTT output not found before translation: {expected_vtt}")
        return None, (
            5,
            build_translation_metadata_fn(
                applied=False,
                backend=translation_backend_local,
                source_language=normalize_language_fn(detected_language),
                target_language=requested_language,
                model=resolve_translation_model_name_fn(
                    normalize_language_fn(detected_language),
                    requested_language,
                    use_gpu=use_gpu,
                ),
                use_gpu=use_gpu,
            ),
            None,
        )

    read_ok, has_cues, _last_end_sec = read_last_vtt_cue_end_seconds_fn(expected_vtt)
    if read_ok and not has_cues:
        if debug:
            print(
                "Generated VTT contains no subtitle cues; "
                "skipping translation for non-verbal audio"
            )
        return expected_vtt, (
            0,
            build_translation_metadata_fn(
                applied=False,
                backend=translation_backend_none,
                source_language=None,
                target_language=requested_language,
                model=None,
                use_gpu=use_gpu,
                note="no_speech_or_non_verbal_audio",
            ),
            requested_language,
        )

    return expected_vtt, None


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
    context: TranslationDecisionContext,
) -> tuple[int, Dict[str, Any], Optional[str]]:
    """Translate finalized VTT only when requested and source/target differ."""
    resolved_context = context

    normalized_requested_language = resolved_context.normalize_language_fn(requested_language)
    if not normalized_requested_language or normalized_requested_language == "auto":
        return (
            0,
            resolved_context.build_translation_metadata_fn(
                applied=False,
                backend=resolved_context.translation_backend_none,
                source_language=detected_language,
                target_language=detected_language,
                model=None,
                use_gpu=use_gpu,
            ),
            resolved_context.normalize_language_fn(detected_language),
        )

    expected_vtt, preflight_response = resolved_context.check_translation_input_vtt_fn(
        audio_src,
        work_dir,
        requested_language=normalized_requested_language,
        detected_language=detected_language,
        use_gpu=use_gpu,
        debug=debug,
    )
    if preflight_response is not None:
        return cast(tuple[int, Dict[str, Any], Optional[str]], preflight_response)
    if expected_vtt is None:
        return (
            5,
            resolved_context.build_translation_metadata_fn(
                applied=False,
                backend=resolved_context.translation_backend_local,
                source_language=resolved_context.normalize_language_fn(detected_language),
                target_language=normalized_requested_language,
                model=resolved_context.resolve_translation_model_name_fn(
                    resolved_context.normalize_language_fn(detected_language),
                    normalized_requested_language,
                    use_gpu=use_gpu,
                ),
                use_gpu=use_gpu,
            ),
            None,
        )

    normalized_detected_language = resolved_context.normalize_language_fn(detected_language)
    if not normalized_detected_language:
        print(
            "Subtitle translation decision failed: Whisper could not determine the source "
            "language while a target subtitle language was explicitly requested"
        )
        return (
            resolved_context.translation_decision_failed_rc,
            resolved_context.build_translation_metadata_fn(
                applied=False,
                backend=resolved_context.translation_backend_none,
                source_language=None,
                target_language=normalized_requested_language,
                model=None,
                use_gpu=use_gpu,
            ),
            None,
        )

    if normalized_detected_language == normalized_requested_language:
        if debug:
            print(
                "Detected source language matches the requested subtitle language; "
                "translation skipped"
            )
        return (
            0,
            resolved_context.build_translation_metadata_fn(
                applied=False,
                backend=resolved_context.translation_backend_none,
                source_language=normalized_detected_language,
                target_language=normalized_requested_language,
                model=None,
                use_gpu=use_gpu,
            ),
            normalized_detected_language,
        )

    local_translation_model = resolved_context.resolve_translation_model_name_fn(
        normalized_detected_language,
        normalized_requested_language,
        use_gpu=use_gpu,
    )
    if local_translation_model is None:
        if debug:
            print(
                "Local subtitle translation is not available for "
                f"{normalized_detected_language}->{normalized_requested_language}; "
                "falling back to legacy Whisper multilingual output"
            )
        if whisper_fallback_options is None:
            return (
                resolved_context.translation_unsupported_pair_rc,
                resolved_context.build_translation_metadata_fn(
                    applied=False,
                    backend=resolved_context.translation_backend_whisper_legacy,
                    source_language=normalized_detected_language,
                    target_language=normalized_requested_language,
                    model=None,
                    use_gpu=use_gpu,
                    note="legacy_whisper_fallback_not_configured",
                ),
                None,
            )
        return cast(
            tuple[int, Dict[str, Any], Optional[str]],
            resolved_context.run_legacy_whisper_translation_fallback_fn(
                audio_src,
                work_dir,
                source_language=normalized_detected_language,
                target_language=normalized_requested_language,
                whisper_fallback_options=whisper_fallback_options,
                debug=debug,
            ),
        )

    rc, translation_metadata = resolved_context.translate_vtt_file_fn(
        expected_vtt,
        source_language=normalized_detected_language,
        target_language=normalized_requested_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
        debug=debug,
    )
    final_language = normalized_requested_language if rc == 0 else normalized_detected_language
    return rc, translation_metadata, final_language
