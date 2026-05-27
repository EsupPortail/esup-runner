"""Metadata helpers for transcription runtime.

Extracts optional video-identification fields from args or plain mappings.
Builds structured payloads written alongside generated subtitle artifacts.
Persists `info_video.json` safely with debug-aware logging hooks.
"""

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def extract_video_identification(values: Dict[str, Any]) -> Dict[str, str]:
    """Extract optional video identification metadata from a mapping."""
    return {
        key: str(value) for key, value in values.items() if value is not None and str(value).strip()
    }


def extract_video_identification_from_args(args: Any) -> Dict[str, str]:
    """Extract optional video identification metadata from parsed CLI args."""
    return extract_video_identification(
        {
            "video_id": getattr(args, "video_id", ""),
            "video_slug": getattr(args, "video_slug", ""),
            "video_title": getattr(args, "video_title", ""),
        }
    )


def write_info_video_metadata(
    work_dir: Path, metadata: Dict[str, Any], debug: bool = False
) -> None:
    """Merge task metadata into info_video.json."""
    if not metadata:
        return

    work_dir.mkdir(parents=True, exist_ok=True)
    info_path = work_dir / "info_video.json"
    data: Dict[str, Any] = {}
    try:
        if info_path.exists():
            loaded = json.loads(info_path.read_text(encoding="utf-8") or "{}")
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        data = {}

    data.update(metadata)
    info_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if debug:
        print(f"Task metadata written to: {info_path}")


def write_video_identification_metadata(
    work_dir: Path,
    metadata: Dict[str, str],
    writer: Callable[[Path, Dict[str, Any], bool], None],
    debug: bool = False,
) -> None:
    """Persist optional video identification metadata into info_video.json."""
    if metadata:
        writer(work_dir, metadata, debug)


def translation_hardware_profile(use_gpu: bool) -> str:
    """Return a stable label for metadata/debug logs."""
    return "gpu" if use_gpu else "cpu"


def build_translation_metadata(
    *,
    applied: bool,
    backend: str,
    source_language: Optional[str],
    target_language: Optional[str],
    model: Optional[str],
    use_gpu: bool,
    normalize_language: Callable[[Optional[str]], Optional[str]],
    source_sidecar: Optional[str] = None,
    execution_backend: Optional[str] = None,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Build normalized translation metadata for info_video.json."""
    metadata: Dict[str, Any] = {
        "applied": bool(applied),
        "backend": backend,
        "source_language": normalize_language(source_language),
        "target_language": normalize_language(target_language),
        "model": model,
        "hardware_profile": translation_hardware_profile(use_gpu),
    }
    if source_sidecar:
        metadata["source_sidecar"] = source_sidecar
    if execution_backend:
        metadata["execution_backend"] = execution_backend
    if note:
        metadata["note"] = note
    return metadata


def build_transcription_runtime_metadata(
    *,
    requested_language: str,
    detected_language: Optional[str],
    final_language: Optional[str],
    whisper_model: str,
    use_gpu: bool,
    translation: Dict[str, Any],
    normalize_language: Callable[[Optional[str]], Optional[str]],
    map_model_name: Callable[[str, str], str],
    vtt_internal_gaps: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build stable runtime metadata written to info_video.json."""
    normalized_detected_language = normalize_language(detected_language)
    normalized_requested_language = normalize_language(requested_language)
    normalized_final_language = normalize_language(final_language)
    metadata: Dict[str, Any] = {
        "transcription": {
            "whisper_model": map_model_name(whisper_model, "python"),
            "hardware_profile": translation_hardware_profile(use_gpu),
            "requested_subtitle_language": normalized_requested_language,
            "detected_source_language": normalized_detected_language,
            "final_subtitle_language": normalized_final_language,
            "translation": translation,
        }
    }
    if vtt_internal_gaps is not None:
        metadata["transcription"]["vtt_internal_gaps"] = vtt_internal_gaps
    return metadata
