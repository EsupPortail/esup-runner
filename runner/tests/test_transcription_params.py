"""Validates transcription script argument building with video identification metadata fields."""

import importlib.util
from pathlib import Path

from app.task_handlers.transcription.transcription_handler import TranscriptionHandler


def _load_transcription_script_module():
    """Load the transcription core module without requiring scripts/ to be a package."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "app" / "task_handlers" / "transcription" / "transcription.py"
    spec = importlib.util.spec_from_file_location("transcription_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_script_arguments_includes_video_identification():
    """Validate Build script arguments includes video identification."""
    handler = TranscriptionHandler()

    params = {
        "language": "fr",
        "source_language": "fr",
        "format": "vtt",
        "model": "turbo",
        "video_id": "12345",
        "video_slug": "intro-python",
        "video_title": "Introduction a Python",
    }

    args = handler._build_script_arguments(
        parameters=params,
        base_dir="/tmp/base",
        input_file="input.mp4",
        work_dir="output",
    )

    assert "--video-id" in args
    assert "--video-slug" in args
    assert "--video-title" in args
    assert args[args.index("--video-id") + 1] == params["video_id"]
    assert args[args.index("--video-slug") + 1] == params["video_slug"]
    assert args[args.index("--video-title") + 1] == params["video_title"]
    assert args[args.index("--source-language") + 1] == params["source_language"]
    assert "--whisper-models-dir" in args
    assert "--huggingface-models-dir" in args


def test_validate_parameters_accepts_video_identification_and_compatibility_fields():
    """Validate accepted transcription parameters include compatibility metadata."""
    handler = TranscriptionHandler()

    assert (
        handler.validate_parameters(
            {
                "language": "fr",
                "source_language": "fr",
                "format": "vtt",
                "model": "small",
                "model_type": "WHISPER",
                "duration": 17.0,
                "normalize": False,
                "video_id": "abc123",
                "video_slug": "my-video",
                "video_title": "My Video",
            }
        )
        is True
    )
    assert handler.validate_parameters({"duration": 17.0}) is True
    assert handler.validate_parameters({"model_type": "WHISPER"}) is True
    assert handler.validate_parameters({"unknown": "value"}) is False


def test_transcription_script_parser_accepts_video_identification_flags():
    """Validate Transcription script parser accepts video identification flags."""
    tr = _load_transcription_script_module()

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
            "--video-id",
            "vid-001",
            "--video-slug",
            "sample-video",
            "--video-title",
            "Sample Video",
        ]
    )

    assert args.video_id == "vid-001"
    assert args.video_slug == "sample-video"
    assert args.video_title == "Sample Video"


def test_transcription_script_parser_accepts_source_language_flag():
    """Validate Transcription script parser accepts source language flag."""
    tr = _load_transcription_script_module()

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
            "--source-language",
            "fr",
        ]
    )

    assert args.source_language == "fr"


def test_transcription_script_parser_defaults_source_language_to_auto():
    """Validate source language defaults to auto when not provided."""
    tr = _load_transcription_script_module()

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

    assert args.source_language == "auto"


def test_transcription_script_parser_accepts_huggingface_models_dir_flag():
    """Validate Transcription script parser accepts huggingface models dir flag."""
    tr = _load_transcription_script_module()

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
            "--huggingface-models-dir",
            "/tmp/hf-cache",
        ]
    )

    assert args.huggingface_models_dir == "/tmp/hf-cache"


def test_transcription_script_parser_accepts_whisper_models_dir_flag():
    """Validate Transcription script parser accepts whisper models dir flag."""
    tr = _load_transcription_script_module()

    args = tr.parse_args(
        [
            "--base-dir",
            "/tmp/base",
            "--input-file",
            "input.mp4",
            "--work-dir",
            "output",
            "--whisper-models-dir",
            "/tmp/whisper-cache",
        ]
    )

    assert args.whisper_models_dir == "/tmp/whisper-cache"
