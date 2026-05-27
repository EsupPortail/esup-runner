"""Translation-focused helpers for transcription runtime.

Hosts utility primitives for model loading and warning management.
Centralizes text normalization helpers used around subtitle translation.
Keeps optional backend concerns isolated from orchestration code.
"""

import logging
import re
import warnings
from pathlib import Path
from typing import Any, Callable, Dict, Optional

SACREMOSES_RECOMMENDED_WARNING = "Recommended: pip install sacremoses."


class HfHubUnauthenticatedWarningFilter(logging.Filter):
    """Drop the noisy informational warning emitted by HF Hub for anonymous access."""

    _needle = "You are sending unauthenticated requests to the HF Hub."

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False only for the noisy anonymous HF Hub warning."""
        try:
            return self._needle not in record.getMessage()
        except Exception:
            return True


HF_HUB_WARNING_FILTER_INSTALLED = False
HF_HUB_WARNING_FILTER = HfHubUnauthenticatedWarningFilter()


def import_translation_modules() -> tuple[Optional[Any], Optional[Any], Optional[Any]]:
    """Import the local subtitle translation backend on demand."""
    try:
        global HF_HUB_WARNING_FILTER_INSTALLED

        # Marian tokenizer emits this recommendation when sacremoses is absent.
        # We keep translation functional without forcing that optional package.
        warnings.filterwarnings(
            "ignore",
            message=re.escape(SACREMOSES_RECOMMENDED_WARNING),
            category=UserWarning,
        )

        # HF Hub emits an informational warning on anonymous requests.
        # Authentication remains documented via HF_TOKEN; we avoid polluting logs.
        if not HF_HUB_WARNING_FILTER_INSTALLED:
            logging.getLogger("huggingface_hub").addFilter(HF_HUB_WARNING_FILTER)
            logging.getLogger("huggingface_hub.utils._http").addFilter(HF_HUB_WARNING_FILTER)
            HF_HUB_WARNING_FILTER_INSTALLED = True

        import torch  # type: ignore
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer  # type: ignore

        return torch, AutoModelForSeq2SeqLM, AutoTokenizer
    except Exception as exc:
        print(
            "Subtitle translation backend unavailable: "
            f"{exc}. Install the transcription extras with translation support."
        )
        return None, None, None


def build_source_vtt_sidecar_path(
    vtt_path: Path,
    source_language: str,
    *,
    normalize_language: Callable[[Optional[str]], Optional[str]],
) -> Path:
    """Return the sidecar path used to preserve the pre-translation source VTT."""
    normalized_source_language = normalize_language(source_language) or "source"
    return vtt_path.with_name(f"{vtt_path.stem}.source-{normalized_source_language}.webvtt.txt")


def prepare_huggingface_models_dir(
    models_dir: Optional[str],
    debug: bool = False,
) -> Optional[str]:
    """Create and return the configured Hugging Face cache directory."""
    normalized_models_dir = str(models_dir or "").strip()
    if not normalized_models_dir:
        return None

    try:
        cache_path = Path(normalized_models_dir)
        cache_path.mkdir(parents=True, exist_ok=True)
        return str(cache_path)
    except Exception as exc:
        if debug:
            print(
                f"Failed to prepare Hugging Face cache directory '{normalized_models_dir}': {exc}"
            )
        return normalized_models_dir


def resolve_translation_model_name(
    source_language: Optional[str],
    target_language: Optional[str],
    use_gpu: bool,
    *,
    normalize_language: Callable[[Optional[str]], Optional[str]],
    cpu_model_map: Dict[tuple[str, str], str],
    gpu_model_map: Dict[tuple[str, str], str],
) -> Optional[str]:
    """Return the internal translation model name for the requested language pair."""
    normalized_source_language = normalize_language(source_language)
    normalized_target_language = normalize_language(target_language)
    if not normalized_source_language or not normalized_target_language:
        return None
    model_map = gpu_model_map if use_gpu else cpu_model_map
    return model_map.get((normalized_source_language, normalized_target_language))


def load_translation_model_objects(
    auto_tokenizer_cls: Any,
    auto_model_cls: Any,
    model_name: str,
    cache_dir: Optional[str],
    *,
    hf_token: str,
) -> tuple[Any, Any]:
    """Load the tokenizer and seq2seq model from Hugging Face."""
    from_pretrained_kwargs: Dict[str, object] = {}
    if cache_dir:
        from_pretrained_kwargs["cache_dir"] = cache_dir
    if hf_token:
        from_pretrained_kwargs["token"] = hf_token
    tokenizer = auto_tokenizer_cls.from_pretrained(model_name, **from_pretrained_kwargs)
    model = auto_model_cls.from_pretrained(model_name, **from_pretrained_kwargs)
    return tokenizer, model


def place_translation_model_on_device(model: Any, device: str) -> Any:
    """Move the translation model to the target device and switch to eval mode."""
    if device == "cuda":
        model = model.to("cuda")
    else:
        model = model.to("cpu")
    model.eval()
    return model


def run_translation_batch(
    texts: list[str],
    *,
    torch: Any,
    tokenizer: Any,
    model: Any,
) -> list[str]:
    """Translate a batch of cue texts while preserving one output per input cue."""
    if not texts:
        return []

    tokenized = tokenizer(
        texts,
        return_tensors="pt",
        padding=True,
        truncation=True,
        max_length=512,
    )
    model_device = getattr(model, "device", None)
    if model_device is not None:
        tokenized = {key: value.to(model_device) for key, value in tokenized.items()}

    with torch.inference_mode():
        generated = model.generate(
            **tokenized,
            max_length=None,
            max_new_tokens=256,
            num_beams=4,
        )

    return [
        " ".join(str(text).split()).strip()
        for text in tokenizer.batch_decode(generated, skip_special_tokens=True)
    ]


def translate_cue_texts(
    cue_texts: list[str],
    *,
    translate_batch: Callable[[list[str]], list[str]],
    batch_size: int,
    normalize_vtt_cue_text: Callable[[str], str],
) -> list[str]:
    """Translate cue texts in small batches and keep empty outputs safe."""
    translated_texts: list[str] = []
    normalized_batch_size = max(1, int(batch_size))

    for index in range(0, len(cue_texts), normalized_batch_size):
        source_batch = cue_texts[index : index + normalized_batch_size]
        translated_batch = translate_batch(source_batch)
        if len(translated_batch) != len(source_batch):
            raise ValueError("subtitle translation returned a different cue count than requested")

        for source_text, translated_text in zip(source_batch, translated_batch):
            normalized_source_text = normalize_vtt_cue_text(source_text)
            normalized_translated_text = normalize_vtt_cue_text(str(translated_text))
            translated_texts.append(normalized_translated_text or normalized_source_text)

    return translated_texts


def translate_vtt_content(
    content: str,
    *,
    translate_batch: Callable[[list[str]], list[str]],
    max_line_width: int,
    max_line_count: int,
    batch_size: int,
    parse_vtt_postprocess_block: Callable[[str], Any],
    normalize_vtt_cue_text: Callable[[str], str],
    translate_cue_texts_fn: Callable[..., list[str]],
    repair_cross_cue_apostrophe_splits: Callable[[list[Any]], None],
    render_postprocessed_vtt_blocks: Callable[..., list[str]],
) -> str:
    """Translate VTT cue texts while preserving timestamps and block structure."""
    parsed_blocks: list[Any] = [
        parse_vtt_postprocess_block(block) for block in (content or "").split("\n\n")
    ]

    cue_block_indexes: list[int] = []
    cue_texts: list[str] = []
    for index, parsed_block in enumerate(parsed_blocks):
        if isinstance(parsed_block, str):
            continue
        _cue_prefix, cue_text = parsed_block
        normalized_cue_text = normalize_vtt_cue_text(cue_text)
        if not normalized_cue_text:
            continue
        cue_block_indexes.append(index)
        cue_texts.append(normalized_cue_text)

    if cue_texts:
        translated_texts = translate_cue_texts_fn(
            cue_texts,
            translate_batch=translate_batch,
            batch_size=batch_size,
        )
        for cue_block_index, translated_text in zip(cue_block_indexes, translated_texts):
            parsed_block = parsed_blocks[cue_block_index]
            if isinstance(parsed_block, str):
                raise ValueError("subtitle translation lost cue structure while applying results")
            cue_prefix, _original_text = parsed_block
            parsed_blocks[cue_block_index] = (cue_prefix, translated_text)

    repair_cross_cue_apostrophe_splits(parsed_blocks)
    processed_blocks = render_postprocessed_vtt_blocks(
        parsed_blocks,
        max_line_width=max_line_width,
        max_line_count=max_line_count,
    )
    return "\n\n".join(processed_blocks).rstrip() + "\n"
