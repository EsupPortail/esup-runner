"""Translation model runtime loading flow helpers.

Validates source and target pairs and resolves compatible model names.
Loads tokenizer/model objects and places them on the requested device.
Returns structured runtime objects consumed by VTT translation steps.
"""

import sys
from pathlib import Path
from typing import Any, Optional

_CORE_DIR = Path(__file__).resolve().parent

if str(_CORE_DIR) not in sys.path:  # pragma: no cover - direct file-spec import guard
    sys.path.insert(0, str(_CORE_DIR))

from translation_flow_contexts import (
    TranslationRuntimeContext,
)


def load_translation_runtime(
    *,
    source_language: str,
    target_language: str,
    use_gpu: bool,
    huggingface_models_dir: Optional[str],
    debug: bool,
    context: TranslationRuntimeContext,
) -> tuple[int, Optional[Any], Optional[Any], Optional[str]]:
    """Load the internal FR<->EN subtitle translation runtime."""
    resolved_context = context

    model_name = resolved_context.resolve_translation_model_name_fn(
        source_language,
        target_language,
        use_gpu=use_gpu,
    )
    if not model_name:
        print(
            "Subtitle translation is only supported for the following language pairs: "
            + ", ".join(
                f"{src}->{dst}"
                for src, dst in sorted(resolved_context.cpu_translation_models.keys())
            )
        )
        return resolved_context.translation_unsupported_pair_rc, None, None, None

    torch, auto_model_cls, auto_tokenizer_cls = resolved_context.import_translation_modules_fn()
    if not torch or not auto_model_cls or not auto_tokenizer_cls:
        return resolved_context.translation_backend_unavailable_rc, None, None, model_name

    device = "cuda" if use_gpu else "cpu"
    cache_dir = resolved_context.prepare_huggingface_models_dir_fn(
        huggingface_models_dir,
        debug=debug,
    )
    if debug:
        print(f"Loading subtitle translation model '{model_name}' on {device}")
        if cache_dir:
            print(f"Using Hugging Face translation cache dir: {cache_dir}")

    try:
        tokenizer, model = resolved_context.load_translation_model_objects_fn(
            auto_tokenizer_cls,
            auto_model_cls,
            model_name,
            cache_dir,
        )
        if device == "cuda":
            try:
                model = resolved_context.place_translation_model_on_device_fn(model, "cuda")
            except Exception as cuda_error:
                print(
                    "Subtitle translation model failed to start on CUDA; "
                    f"retrying on CPU ({cuda_error})"
                )
                model = resolved_context.place_translation_model_on_device_fn(model, "cpu")
        else:
            model = resolved_context.place_translation_model_on_device_fn(model, "cpu")
        return 0, torch, (tokenizer, model), model_name
    except Exception as error:
        print(f"Failed to load subtitle translation model '{model_name}': {error}")
        return resolved_context.translation_backend_unavailable_rc, None, None, model_name
