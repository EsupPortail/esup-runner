"""VTT file translation flow helpers.

Translates finalized subtitle cues while preserving cue timing and structure.
Applies batching and line-format constraints to keep VTT output readable.
Wraps backend failures with stable return codes for orchestration callers.
"""

import sys
from pathlib import Path
from typing import Any, Dict, Optional

_CORE_DIR = Path(__file__).resolve().parent

if str(_CORE_DIR) not in sys.path:  # pragma: no cover - direct file-spec import guard
    sys.path.insert(0, str(_CORE_DIR))

from translation_flow_contexts import (
    TranslateVttFileContext,
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
    context: TranslateVttFileContext,
) -> tuple[int, Dict[str, Any]]:
    """Translate a finalized VTT file and preserve the source VTT as a sidecar."""
    resolved_context = context

    rc, torch, runtime, model_name = resolved_context.load_translation_runtime_fn(
        source_language=source_language,
        target_language=target_language,
        use_gpu=use_gpu,
        huggingface_models_dir=huggingface_models_dir,
        debug=debug,
    )
    translation_metadata = resolved_context.build_translation_metadata_fn(
        applied=False,
        backend=resolved_context.translation_backend_local,
        source_language=source_language,
        target_language=target_language,
        model=model_name,
        use_gpu=use_gpu,
    )
    if rc != 0 or torch is None or runtime is None:
        return rc, translation_metadata

    tokenizer, model = runtime
    original_content = vtt_path.read_text(encoding="utf-8")

    try:
        translated_content = resolved_context.translate_vtt_content_fn(
            original_content,
            translate_batch=lambda batch: resolved_context.run_translation_batch_fn(
                batch,
                torch=torch,
                tokenizer=tokenizer,
                model=model,
            ),
            max_line_width=max_line_width,
            max_line_count=max_line_count,
            batch_size=resolved_context.translation_batch_size,
        )
    except Exception as error:
        print(f"Subtitle translation failed: {error}")
        return resolved_context.translation_failed_rc, translation_metadata

    source_sidecar_path = resolved_context.build_source_vtt_sidecar_path_fn(
        vtt_path, source_language
    )
    source_sidecar_path.write_text(original_content, encoding="utf-8")
    vtt_path.write_text(translated_content, encoding="utf-8")
    translation_metadata = resolved_context.build_translation_metadata_fn(
        applied=True,
        backend=resolved_context.translation_backend_local,
        source_language=source_language,
        target_language=target_language,
        model=model_name,
        use_gpu=use_gpu,
        source_sidecar=str(source_sidecar_path.name),
    )

    if debug:
        print(f"Source-language VTT preserved at: {source_sidecar_path}")
        print(
            "Translated VTT written to: "
            f"{vtt_path} (from {source_language} to {target_language})"
        )

    return 0, translation_metadata
