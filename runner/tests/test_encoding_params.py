import importlib.util
from pathlib import Path
from unittest.mock import Mock

from app.task_handlers.encoding.encoding_handler import VideoEncodingHandler


def _load_encoding_script_module():
    """Load encoding.py as a module without requiring scripts/ to be a package."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "app" / "task_handlers" / "encoding" / "scripts" / "encoding.py"
    spec = importlib.util.spec_from_file_location("encoding_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_script_arguments_includes_cut_and_dressing():
    handler = VideoEncodingHandler()

    params = {
        "rendition": '{"360": {"resolution": "640x360", "encode_mp4": true}}',
        "cut": '{"start": "00:00:05", "end": "00:00:10", "initial_duration": "00:01:00"}',
        "dressing": '{"watermark": "https://example.org/wm.png", "watermark_position_orig": "top_right", "watermark_opacity": "100"}',
    }

    args = handler._build_script_arguments(
        parameters=params,
        base_dir="/tmp/base",
        input_file="input.mp4",
        work_dir="output",
    )

    assert "--cut" in args
    assert "--dressing" in args
    assert args[args.index("--cut") + 1] == params["cut"]
    assert args[args.index("--dressing") + 1] == params["dressing"]


def test_apply_dressing_watermark_only_switches_input(tmp_path):
    enc = _load_encoding_script_module()

    # Arrange workspace
    enc._VIDEOS_DIR = str(tmp_path)
    (tmp_path / "input.mp4").write_bytes(b"fake")
    (tmp_path / "wm.png").write_bytes(b"fake")

    enc._download_url_to_dir = Mock(return_value=str(tmp_path / "wm.png"))
    enc._create_watermarked_intermediate = Mock(return_value=True)
    enc._create_credits_concat_intermediate = Mock(return_value=False)

    dressing = {
        "watermark": "https://example.org/wm.png",
        "watermark_position_orig": "top_right",
        "watermark_opacity": "100",
    }

    new_file, msg = enc.apply_dressing_if_needed("input.mp4", dressing)

    assert new_file.endswith("_dressing_wm.mp4")
    assert "Applying watermark" in msg
    enc._create_watermarked_intermediate.assert_called_once()
    enc._create_credits_concat_intermediate.assert_not_called()


def test_apply_dressing_with_opening_and_cut_resets_global_cut(tmp_path):
    enc = _load_encoding_script_module()

    # Arrange workspace
    enc._VIDEOS_DIR = str(tmp_path)
    (tmp_path / "input.mp4").write_bytes(b"fake")
    (tmp_path / "opening.mp4").write_bytes(b"fake")

    # Simulate a global cut already configured by CLI parsing
    enc.SUBTIME = " -ss 00:00:05 -to 00:00:10 "
    enc.EFFECTIVE_DURATION = 5
    enc._CUT_CONFIG = {"start": "00:00:05", "end": "00:00:10"}

    enc._download_url_to_dir = Mock(return_value=str(tmp_path / "opening.mp4"))
    enc._create_cut_intermediate = Mock(return_value=True)
    enc._create_credits_concat_intermediate = Mock(return_value=True)
    enc._probe_duration_seconds = Mock(return_value=8.0)
    enc._probe_has_audio = Mock(return_value=False)

    dressing = {
        "opening_credits_video": "https://example.org/opening.mp4",
        "opening_credits_video_duration": "8",
    }

    new_file, msg = enc.apply_dressing_if_needed("input.mp4", dressing)

    assert new_file.endswith("_dressing.mp4")
    assert "Applying cut to main video only" in msg
    assert "disabling SUBTIME cut" in msg

    # Cut must be disabled for the final encode so credits aren't truncated
    assert enc.SUBTIME == " "
    assert enc.EFFECTIVE_DURATION == 0

    enc._create_cut_intermediate.assert_called_once()
    enc._create_credits_concat_intermediate.assert_called_once()


def test_apply_dressing_with_opening_and_ending_calls_concat(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    (tmp_path / "input.mp4").write_bytes(b"fake")
    (tmp_path / "opening.mp4").write_bytes(b"fake")
    (tmp_path / "ending.mp4").write_bytes(b"fake")

    def download_side_effect(url, target_dir, prefix):
        if prefix == "opening":
            return str(tmp_path / "opening.mp4")
        if prefix == "ending":
            return str(tmp_path / "ending.mp4")
        raise AssertionError("unexpected prefix")

    enc._download_url_to_dir = Mock(side_effect=download_side_effect)
    enc._create_credits_concat_intermediate = Mock(return_value=True)
    enc._probe_duration_seconds = Mock(return_value=5.0)
    enc._probe_has_audio = Mock(return_value=True)

    dressing = {
        "opening_credits_video": "https://example.org/opening.mp4",
        "opening_credits_video_duration": "5",
        "ending_credits_video": "https://example.org/ending.mp4",
        "ending_credits_video_duration": "5",
    }

    new_file, msg = enc.apply_dressing_if_needed("input.mp4", dressing)

    assert new_file.endswith("_dressing.mp4")
    assert "opening=True" in msg and "ending=True" in msg
    enc._create_credits_concat_intermediate.assert_called_once()


def test_apply_dressing_watermark_plus_credits_watermark_only_on_main(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    (tmp_path / "input.mp4").write_bytes(b"fake")
    (tmp_path / "wm.png").write_bytes(b"fake")
    (tmp_path / "opening.mp4").write_bytes(b"fake")
    (tmp_path / "ending.mp4").write_bytes(b"fake")

    def download_side_effect(url, target_dir, prefix):
        if prefix == "watermark":
            return str(tmp_path / "wm.png")
        if prefix == "opening":
            return str(tmp_path / "opening.mp4")
        if prefix == "ending":
            return str(tmp_path / "ending.mp4")
        raise AssertionError(f"unexpected prefix: {prefix}")

    enc._download_url_to_dir = Mock(side_effect=download_side_effect)
    enc._create_watermarked_intermediate = Mock(return_value=True)
    enc._create_credits_concat_intermediate = Mock(return_value=True)
    enc._probe_duration_seconds = Mock(return_value=5.0)
    enc._probe_has_audio = Mock(return_value=True)

    dressing = {
        "watermark": "https://example.org/wm.png",
        "watermark_position_orig": "top_right",
        "watermark_opacity": "100",
        "opening_credits_video": "https://example.org/opening.mp4",
        "opening_credits_video_duration": "5",
        "ending_credits_video": "https://example.org/ending.mp4",
        "ending_credits_video_duration": "5",
    }

    new_file, msg = enc.apply_dressing_if_needed("input.mp4", dressing)

    assert new_file.endswith("_dressing.mp4")
    assert "Applying watermark" in msg
    assert "Applying credits concat" in msg

    # Watermark is applied first to the main input (not to credits)
    enc._create_watermarked_intermediate.assert_called_once()
    wm_call = enc._create_watermarked_intermediate.call_args
    assert wm_call.args[0].endswith("input.mp4")
    assert wm_call.args[1].endswith("wm.png")
    assert wm_call.args[2].endswith("_dressing_wm.mp4")

    # Concat must use the watermarked main as the 'main_path'
    enc._create_credits_concat_intermediate.assert_called_once()
    cc_kwargs = enc._create_credits_concat_intermediate.call_args.kwargs
    assert cc_kwargs["main_path"].endswith("_dressing_wm.mp4")
    assert cc_kwargs["opening_path"].endswith("opening.mp4")
    assert cc_kwargs["ending_path"].endswith("ending.mp4")
