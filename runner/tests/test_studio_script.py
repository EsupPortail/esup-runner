"""Validates Studio CPU pipeline building and input source selection with layout fallback."""

import argparse
import importlib.util
from pathlib import Path

import pytest

_MAX_DOUBLE_LIKE_SECONDS = (
    "1797693134862315708145274237317043567980705675258449965989174768031572607800285387605895586"
    "3276687817154045895351438246423432132688946418276846754670353751698604991057655128207624549"
    "0090389328944075868508455133942304583236903222948165808559332123348274797826204144723168738"
    "177180919299881250404026184124858368.000s"
)


def _load_studio_script_module():
    """Load studio.py as a module without requiring scripts/ to be a package."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "tests" / "studio_legacy_runtime_api.py"
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
    """Validate Build cpu pipeline with missing video source falls back to single input."""
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
    """Validate Build cpu pipeline with two video sources keeps mixed vout mapping."""
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
    """Validate Select cpu input args with presenter missing video uses presentation."""
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
    """Validate Select cpu input args with missing dimensions falls back to presentation."""
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
    """Validate Select cpu input args with single input and no media cases."""
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
    """Validate Single source height presentation and default."""
    studio = _load_studio_script_module()

    assert studio._single_source_height("presentation", 540, 1080) == 540
    assert studio._single_source_height("unknown", 540, 1080) == 720


def test_build_cpu_single_source_subcmd_uses_qv_for_non_libx264():
    """Validate Build cpu single source subcmd uses qv for non libx264."""
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


def test_parse_time_rejects_unbounded_values():
    """Validate Parse time rejects unbounded values."""
    studio = _load_studio_script_module()

    assert studio.parse_time(_MAX_DOUBLE_LIKE_SECONDS) is None
    assert studio.parse_time("1e309s") is None


def test_parse_time_caps_values_over_five_days():
    """Validate Parse time caps values over five days."""
    studio = _load_studio_script_module()

    assert studio.parse_time("432000s") == pytest.approx(432000.0)
    assert studio.parse_time("432000.001s") is None


def test_parse_smil_cut_keeps_clip_begin_when_clip_end_is_unbounded():
    """Validate Parse smil cut keeps clip begin when clip end is unbounded."""
    studio = _load_studio_script_module()

    smil_text = f"""
    <smil>
      <body>
        <video clipBegin="2.722s" clipEnd="{_MAX_DOUBLE_LIKE_SECONDS}" />
      </body>
    </smil>
    """

    clip_begin, clip_end = studio.parse_smil_cut(smil_text)
    assert clip_begin == pytest.approx(2.722)
    assert clip_end is None


def test_sanitize_smil_time_rejects_none():
    """Validate Sanitize smil time rejects none."""
    studio = _load_studio_script_module()
    assert studio._sanitize_smil_time(None) is None


def test_build_nvenc_video_codec_enables_webm_specific_rate_control():
    """Validate Build nvenc video codec enables webm specific rate control."""
    studio = _load_studio_script_module()
    args = _make_args(studio_preset="p4", studio_crf="23")

    webm_codec = studio._build_nvenc_video_codec(args, webm_input=True)
    mp4_codec = studio._build_nvenc_video_codec(args, webm_input=False)

    assert "-rc cbr -cbr 1 " in webm_codec
    assert "-spatial-aq 1 -aq-strength 8 -temporal-aq 1 " in webm_codec
    assert "-qmin 0 -qmax 35 " in webm_codec
    assert "-rc cbr -cbr 1 " not in mp4_codec


def test_is_webm_input_source_uses_extension_or_codec_probe():
    """Validate Is webm input source uses extension or codec probe."""
    studio = _load_studio_script_module()

    assert studio._is_webm_input_source("https://example.org/video.webm") is True
    assert studio._is_webm_input_source("/tmp/video.webm") is True

    original_probe = studio.probe_codec
    try:
        studio.probe_codec = lambda _src: "vp9"
        assert studio._is_webm_input_source("/tmp/video.unknown") is True
    finally:
        studio.probe_codec = original_probe


def test_looks_like_webm_source_and_is_webm_input_source_cover_false_paths():
    """Validate Looks like webm source and is webm input source cover false paths."""
    studio = _load_studio_script_module()

    assert studio._looks_like_webm_source(None) is False
    assert studio._is_webm_input_source(None) is False
