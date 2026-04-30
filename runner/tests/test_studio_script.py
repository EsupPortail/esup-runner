import argparse
import importlib.util
from pathlib import Path


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
