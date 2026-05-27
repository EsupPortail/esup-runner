"""Validates encoding runtime flow wrapper functions and ffmpeg command building utilities."""

import importlib

import pytest


def _load_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def test_runtime_flow_wrapper_functions_missing_lines(monkeypatch):
    """Validate Runtime flow wrapper functions missing lines."""
    runtime_flow = _load_core_module("runtime_flow_utils")

    runtime_flow._DEBUG = False
    runtime_flow._VIDEOS_DIR = "/tmp/input"
    runtime_flow._VIDEOS_OUTPUT_DIR = "/tmp/output"
    runtime_flow._ENCODING_TYPE = "GPU"
    runtime_flow._HWACCEL_DEVICE = 2
    runtime_flow._RENDITION_CONFIG = {"360": {"resolution": "640x360"}}
    runtime_flow.SUBTIME = " -ss 1 -to 2 "
    runtime_flow.EFFECTIVE_DURATION = 1
    runtime_flow._SOURCE_VIDEO_FPS = 29.97

    assert runtime_flow._DEBUG is False
    assert runtime_flow._VIDEOS_DIR == "/tmp/input"
    assert runtime_flow._VIDEOS_OUTPUT_DIR == "/tmp/output"
    assert runtime_flow._ENCODING_TYPE == "GPU"
    assert runtime_flow._HWACCEL_DEVICE == 2
    assert runtime_flow._RENDITION_CONFIG == {"360": {"resolution": "640x360"}}
    assert runtime_flow.SUBTIME == " -ss 1 -to 2 "
    assert runtime_flow.EFFECTIVE_DURATION == 1
    assert runtime_flow._SOURCE_VIDEO_FPS == pytest.approx(29.97)

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "is_webm_source",
        lambda **kwargs: kwargs["file"] == "a.webm" and kwargs["codec"] == "vp9",
    )
    assert runtime_flow._is_webm_source(file="a.webm", codec="vp9") is True

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_fps_mode_options",
        lambda **kwargs: f"fps:{kwargs['is_webm']}",
    )
    assert runtime_flow._build_fps_mode_options(is_webm_source=True) == "fps:True"

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_nvenc_rate_control_options",
        lambda **kwargs: f"nvenc:{kwargs['is_webm']}",
    )
    assert runtime_flow._build_nvenc_rate_control_options(is_webm_source=False) == "nvenc:False"

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_cpu_quality_options",
        lambda **kwargs: f"cpu:{kwargs['is_webm']}",
    )
    assert runtime_flow._build_cpu_quality_options(is_webm_source=True) == "cpu:True"

    monkeypatch.setattr(
        runtime_flow.rendition_utils,
        "build_rate_control",
        lambda *args, **_kwargs: {"rendition": args[0], "video": args[1]},
    )
    assert runtime_flow._build_rate_control("720", "2000k") == {
        "rendition": "720",
        "video": "2000k",
    }

    monkeypatch.setattr(
        runtime_flow.rendition_utils,
        "build_rendition_rate_options",
        lambda *args, **_kwargs: f"opts:{args[0]}",
    )
    assert (
        runtime_flow._build_rendition_rate_options("720", {"video_bitrate": "2000k"}) == "opts:720"
    )

    monkeypatch.setattr(
        runtime_flow.rendition_utils,
        "normalize_rendition_entry",
        lambda *args, **_kwargs: ("720", {"resolution": "1280x720"}),
    )
    assert runtime_flow._normalize_rendition_entry("720", {"resolution": "1280x720"}) == (
        "720",
        {"resolution": "1280x720"},
    )

    monkeypatch.setattr(
        runtime_flow.media_probe_utils,
        "is_image_codec_name",
        lambda codec_name, **_kwargs: codec_name == "png",
    )
    assert runtime_flow._is_image_codec_name("png") is True
