import argparse
import importlib.util
from pathlib import Path

import pytest


def _load_studio_script_module():
    """Load studio.py as a module without requiring scripts/ to be a package."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "app" / "task_handlers" / "studio" / "scripts" / "studio.py"
    spec = importlib.util.spec_from_file_location("studio_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(**overrides):
    defaults = {
        "encoding_type": "CPU",
        "force_cpu": "false",
        "studio_preset": None,
        "studio_crf": None,
        "hwaccel_device": "0",
        "cuda_visible_devices": None,
        "cuda_device_order": None,
        "cuda_path": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_build_cpu_pipeline_with_missing_video_source_falls_back_to_single_input():
    studio = _load_studio_script_module()
    args = _make_args()

    input_args, subcmd, video_codec, map_opts = studio._build_cpu_pipeline(
        pres_url="presentation.mp4",
        pers_url="presenter.mp4",
        pres_h=0,
        pers_h=1080,
        presenter_layout="mid",
        args=args,
    )

    assert input_args == '-i "presenter.mp4" '
    assert map_opts == "-map 0:v -map 0:a? "
    assert "[vout]" not in subcmd
    assert "scale=-2:1080" in subcmd
    assert video_codec == ""


def test_build_cpu_pipeline_with_two_video_sources_keeps_mixed_vout_mapping():
    studio = _load_studio_script_module()
    args = _make_args()

    input_args, subcmd, video_codec, map_opts = studio._build_cpu_pipeline(
        pres_url="presentation.mp4",
        pers_url="presenter.mp4",
        pres_h=1080,
        pers_h=720,
        presenter_layout="mid",
        args=args,
    )

    assert input_args == '-i "presentation.mp4" -i "presenter.mp4" '
    assert map_opts == '-map "[vout]" -map 0:a? '
    assert "[vout]" in subcmd
    assert video_codec.startswith("-c:v ")


def test_select_cpu_input_args_with_presenter_missing_video_uses_presentation():
    studio = _load_studio_script_module()

    input_args, map_opts, source_kind = studio._select_cpu_input_args(
        pres_url="presentation.mp4",
        pers_url="presenter.mp4",
        pres_h=1080,
        pers_h=0,
    )

    assert input_args == '-i "presentation.mp4" '
    assert map_opts == "-map 0:v -map 0:a? "
    assert source_kind == "presentation"


def test_select_cpu_input_args_with_missing_dimensions_falls_back_to_presentation():
    studio = _load_studio_script_module()

    input_args, map_opts, source_kind = studio._select_cpu_input_args(
        pres_url="presentation.mp4",
        pers_url="presenter.mp4",
        pres_h=0,
        pers_h=0,
    )

    assert input_args == '-i "presentation.mp4" '
    assert map_opts == "-map 0:v -map 0:a? "
    assert source_kind == "presentation"


def test_select_cpu_input_args_with_single_input_and_no_media_cases():
    studio = _load_studio_script_module()

    input_args, map_opts, source_kind = studio._select_cpu_input_args(
        pres_url="presentation.mp4",
        pers_url=None,
        pres_h=0,
        pers_h=0,
    )
    assert input_args == '-i "presentation.mp4" '
    assert map_opts == "-map 0:v -map 0:a? "
    assert source_kind == "presentation"

    input_args, map_opts, source_kind = studio._select_cpu_input_args(
        pres_url=None,
        pers_url="presenter.mp4",
        pres_h=0,
        pers_h=0,
    )
    assert input_args == '-i "presenter.mp4" '
    assert map_opts == "-map 0:v -map 0:a? "
    assert source_kind == "presenter"

    with pytest.raises(ValueError, match="No media tracks"):
        studio._select_cpu_input_args(
            pres_url=None,
            pers_url=None,
            pres_h=0,
            pers_h=0,
        )


def test_single_source_height_presentation_and_default():
    studio = _load_studio_script_module()

    assert studio._single_source_height("presentation", 540, 1080) == 540
    assert studio._single_source_height("unknown", 540, 1080) == 720


def test_build_cpu_single_source_subcmd_uses_qv_for_non_libx264():
    studio = _load_studio_script_module()
    args = _make_args(studio_preset="slow", studio_crf="21")

    subcmd = studio._build_cpu_single_source_subcmd(
        cpu_encoder="h264",
        cpu_is_libx264=False,
        target_h=720,
        args=args,
    )

    assert (
        '-vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:720,format=yuv420p,setsar=1" '
        in subcmd
    )
    assert "-c:v h264 " in subcmd
    assert "-q:v 23 " in subcmd
