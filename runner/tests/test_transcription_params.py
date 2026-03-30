import importlib.util
from pathlib import Path

from app.task_handlers.transcription.transcription_handler import TranscriptionHandler


def _load_transcription_script_module():
    """Load transcription.py as a module without requiring scripts/ to be a package."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = (
        repo_root / "app" / "task_handlers" / "transcription" / "scripts" / "transcription.py"
    )
    spec = importlib.util.spec_from_file_location("transcription_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_script_arguments_includes_video_identification():
    handler = TranscriptionHandler()

    params = {
        "language": "fr",
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


def test_validate_parameters_accepts_video_identification_fields():
    handler = TranscriptionHandler()

    assert (
        handler.validate_parameters(
            {
                "language": "fr",
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
    assert handler.validate_parameters({"unknown": "value"}) is False


def test_transcription_script_parser_accepts_video_identification_flags():
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
