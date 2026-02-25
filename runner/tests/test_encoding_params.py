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


def test_generate_overview_thumbnails_returns_actual_generated_count(tmp_path):
    enc = _load_encoding_script_module()

    video_dir = tmp_path / "videos"
    output_dir = tmp_path / "out"
    video_dir.mkdir()
    output_dir.mkdir()
    (video_dir / "input.mp4").write_bytes(b"fake")

    enc._VIDEOS_DIR = str(video_dir)

    def fake_run_and_collect(_cmd):
        temp_dir = output_dir / "overview_temp"
        for idx in range(3):
            (temp_dir / f"thumb_{idx + 1:04d}.png").write_bytes(b"png")
        return 0, ""

    enc._run_and_collect_text = fake_run_and_collect

    success, msg, count = enc.generate_overview_thumbnails(
        file="input.mp4",
        duration=10,
        output_dir=str(output_dir),
    )

    assert success is True
    assert count == 3
    assert "requested 10" in msg


def test_generate_overview_thumbnails_adjusts_interval_for_single_row_capacity(tmp_path):
    enc = _load_encoding_script_module()

    video_dir = tmp_path / "videos"
    output_dir = tmp_path / "out"
    video_dir.mkdir()
    output_dir.mkdir()
    (video_dir / "input.mp4").write_bytes(b"fake")

    enc._VIDEOS_DIR = str(video_dir)
    enc._OVERVIEW_CONFIG["interval"] = 1
    enc._OVERVIEW_CONFIG["thumbnail_width"] = 160
    enc._OVERVIEW_CONFIG["thumbnail_height"] = 90
    enc._OVERVIEW_CONFIG["max_sprite_width"] = 320
    enc._OVERVIEW_CONFIG["max_sprite_height"] = 900

    def fake_run_and_collect(_cmd):
        temp_dir = output_dir / "overview_temp"
        for idx in range(2):
            (temp_dir / f"thumb_{idx + 1:04d}.png").write_bytes(b"png")
        return 0, ""

    enc._run_and_collect_text = fake_run_and_collect

    success, msg, count = enc.generate_overview_thumbnails(
        file="input.mp4",
        duration=10,
        output_dir=str(output_dir),
    )

    assert success is True
    assert count == 2
    assert "requested 10, max 2" in msg
    assert "interval=4s" in msg
    assert "fps=1/4" in msg


def test_create_overview_sprite_uses_single_row_layout(tmp_path):
    enc = _load_encoding_script_module()

    output_dir = tmp_path / "out"
    temp_dir = output_dir / "overview_temp"
    temp_dir.mkdir(parents=True)
    for idx in range(5):
        (temp_dir / f"thumb_{idx + 1:04d}.png").write_bytes(b"png")

    captured = {}

    def fake_run_shell_bytes(cmd):
        captured["cmd"] = cmd
        return 0, b""

    enc._run_shell_bytes = fake_run_shell_bytes
    enc._OVERVIEW_CONFIG["thumbnail_width"] = 160
    enc._OVERVIEW_CONFIG["thumbnail_height"] = 90
    enc._OVERVIEW_CONFIG["max_sprite_width"] = 1600
    enc._OVERVIEW_CONFIG["max_sprite_height"] = 900

    success, msg = enc.create_overview_sprite(str(output_dir), num_thumbnails=5)

    assert success is True
    assert "1 horizontal row" in msg
    assert "tile=5x1" in captured["cmd"]


def test_generate_overview_vtt_uses_single_row_coordinates(tmp_path):
    enc = _load_encoding_script_module()

    output_dir = tmp_path / "out"
    output_dir.mkdir()

    enc._OVERVIEW_CONFIG["thumbnail_width"] = 160
    enc._OVERVIEW_CONFIG["thumbnail_height"] = 90

    success, _ = enc.generate_overview_vtt(str(output_dir), duration=5, num_thumbnails=5)

    assert success is True
    vtt_content = (output_dir / "overview.vtt").read_text()
    assert "overview.png#xywh=0,0,160,90" in vtt_content
    assert "overview.png#xywh=160,0,160,90" in vtt_content
    assert "overview.png#xywh=320,0,160,90" in vtt_content


def test_get_info_video_handles_missing_format_duration(tmp_path):
    enc = _load_encoding_script_module()

    enc._DEBUG = False
    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    (tmp_path / "encoding.log").write_text("")

    probe_info = {
        "format": {"tags": {"major_brand": "isom"}},
        "streams": [
            {"codec_type": "audio", "codec_name": "aac"},
            {
                "codec_type": "video",
                "codec_name": "h264",
                "height": 720,
                "tags": {"DURATION": "00:00:12.500"},
            },
        ],
    }
    enc.get_info_from_video = Mock(return_value=(probe_info, ""))

    info = enc.get_info_video("input.mp4")

    assert info["duration"] == 12
    assert info["codec"] == "h264"
    assert info["height"] == 720
    assert info["has_stream_video"] is True
    assert info["has_stream_audio"] is True


def test_get_cmd_gpu_uses_primary_stream_mapping_and_probe_options(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)

    cmd = enc.get_cmd_gpu("m3u8", "h264", 720, "input.mp4")

    assert "-probesize 100M -analyzeduration 100M -c:v:0 h264_cuvid -i" in cmd
    assert cmd.count("-map 0:v:0? -map 0:a?") >= 2
    assert " -c:v h264_cuvid " not in cmd


def test_get_cmd_cpu_uses_primary_stream_mapping_and_probe_options(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    enc._choose_h264_encoder = Mock(return_value=("libx264", ""))

    cmd = enc.get_cmd_cpu("m3u8", "h264", 360, "input.mp4")

    assert " -probesize 100M -analyzeduration 100M -i " in cmd
    assert "-map 0:v:0? -map 0:a?" in cmd


def test_get_info_video_keeps_first_non_image_video_stream_as_primary(tmp_path):
    enc = _load_encoding_script_module()

    enc._DEBUG = False
    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    (tmp_path / "encoding.log").write_text("")

    probe_info = {
        "format": {"duration": "12.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "height": 1080},
            # Secondary video stream should not override the primary one.
            {"codec_type": "video", "codec_name": "h264", "height": 720},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    enc.get_info_from_video = Mock(return_value=(probe_info, ""))

    info = enc.get_info_video("input.mp4")

    assert info["codec"] == "h264"
    assert info["height"] == 1080
    assert info["has_stream_video"] is True
    assert info["has_stream_audio"] is True
