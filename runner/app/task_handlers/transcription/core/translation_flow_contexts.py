"""Translation flow contexts.

Defines dataclass contexts grouping dependencies for translation sub-flows.
These contexts are now the only dependency-injection entrypoint.
Keeps translation orchestration explicit and easy to unit test with stubs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional


@dataclass(frozen=True)
class TranslationRuntimeContext:
    """Dependencies and constants used to load translation runtime objects."""

    translation_unsupported_pair_rc: int
    translation_backend_unavailable_rc: int
    cpu_translation_models: Dict[tuple[str, str], str]
    resolve_translation_model_name_fn: Callable[..., Optional[str]]
    import_translation_modules_fn: Callable[[], tuple[Optional[Any], Optional[Any], Optional[Any]]]
    prepare_huggingface_models_dir_fn: Callable[..., Optional[str]]
    load_translation_model_objects_fn: Callable[..., tuple[Any, Any]]
    place_translation_model_on_device_fn: Callable[[Any, str], Any]


@dataclass(frozen=True)
class TranslateVttFileContext:
    """Dependencies and constants used by `translate_vtt_file`."""

    translation_backend_local: str
    translation_failed_rc: int
    translation_batch_size: int
    build_translation_metadata_fn: Callable[..., Dict[str, Any]]
    load_translation_runtime_fn: Callable[
        ..., tuple[int, Optional[Any], Optional[Any], Optional[str]]
    ]
    build_source_vtt_sidecar_path_fn: Callable[[Path, str], Path]
    run_translation_batch_fn: Callable[..., list[str]]
    translate_vtt_content_fn: Callable[..., str]


@dataclass(frozen=True)
class TranslationDecisionContext:
    """Dependencies and constants used by `maybe_translate_final_vtt`."""

    translation_backend_none: str
    translation_backend_local: str
    translation_backend_whisper_legacy: str
    translation_decision_failed_rc: int
    translation_unsupported_pair_rc: int
    normalize_language_fn: Callable[[Optional[str]], Optional[str]]
    build_translation_metadata_fn: Callable[..., Dict[str, Any]]
    check_translation_input_vtt_fn: Callable[
        ..., tuple[Optional[Path], Optional[tuple[int, Dict[str, Any], Optional[str]]]]
    ]
    resolve_translation_model_name_fn: Callable[..., Optional[str]]
    run_legacy_whisper_translation_fallback_fn: Callable[
        ..., tuple[int, Dict[str, Any], Optional[str]]
    ]
    translate_vtt_file_fn: Callable[..., tuple[int, Dict[str, Any]]]
