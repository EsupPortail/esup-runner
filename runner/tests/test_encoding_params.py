import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

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


def test_build_script_arguments_includes_cut_dressing_and_video_identification():
    handler = VideoEncodingHandler()

    params = {
        "rendition": '{"360": {"resolution": "640x360", "encode_mp4": true}}',
        "cut": '{"start": "00:00:05", "end": "00:00:10", "initial_duration": "00:01:00"}',
        "dressing": '{"watermark": "https://example.org/wm.png", "watermark_position_orig": "top_right", "watermark_opacity": "100"}',
        "video_id": "12345",
        "video_slug": "intro-python",
        "video_title": "Introduction to Python",
    }

    args = handler._build_script_arguments(
        parameters=params,
        base_dir="/tmp/base",
        input_file="input.mp4",
        work_dir="output",
    )

    assert "--cut" in args
    assert "--dressing" in args
    assert "--video-id" in args
    assert "--video-slug" in args
    assert "--video-title" in args
    assert args[args.index("--cut") + 1] == params["cut"]
    assert args[args.index("--dressing") + 1] == params["dressing"]
    assert args[args.index("--video-id") + 1] == params["video_id"]
    assert args[args.index("--video-slug") + 1] == params["video_slug"]
    assert args[args.index("--video-title") + 1] == params["video_title"]


def test_validate_parameters_accepts_video_identification_fields():
    handler = VideoEncodingHandler()

    assert (
        handler.validate_parameters(
            {
                "rendition": "{}",
                "cut": "{}",
                "dressing": "{}",
                "video_id": "abc123",
                "video_slug": "my-video",
                "video_title": "My Video",
            }
        )
        is True
    )
    assert handler.validate_parameters({"unknown": "value"}) is False
    assert handler.get_invalid_parameters({"unknown": "value"}) == ["unknown"]


def test_encoding_script_parser_accepts_video_identification_flags():
    enc = _load_encoding_script_module()
    parser = enc._build_arg_parser()

    args = parser.parse_args(
        [
            "--encoding-type",
            "CPU",
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


def test_bitrate_helpers_cover_error_and_kilobit_formatting():
    enc = _load_encoding_script_module()

    with pytest.raises(ValueError, match="Bitrate must be a string"):
        enc._parse_bitrate_to_bps(123)

    assert enc._format_bitrate_from_bps(875000) == "875k"


def test_infer_video_bitrate_falls_back_on_invalid_default_resolution():
    enc = _load_encoding_script_module()

    enc._DEFAULT_RENDITION_CONFIG = {
        "1080": {
            "resolution": "not-a-resolution",
            "video_bitrate": "3000k",
            "audio_bitrate": "192k",
            "encode_mp4": False,
        }
    }

    assert enc._infer_video_bitrate(1920, 1080) == "3M"


def test_infer_audio_bitrate_covers_invalid_and_matching_tiers():
    enc = _load_encoding_script_module()

    enc._DEFAULT_RENDITION_CONFIG = {
        "bad-key": {"audio_bitrate": "96k"},
    }
    assert enc._infer_audio_bitrate(720) == "128k"

    enc._DEFAULT_RENDITION_CONFIG = {
        "360": {"audio_bitrate": "96k"},
        "720": {"audio_bitrate": "128k"},
        "1080": {"audio_bitrate": "192k"},
    }
    assert enc._infer_audio_bitrate(500) == "128k"


def test_merge_rendition_config_covers_validation_and_null_removal():
    enc = _load_encoding_script_module()

    with pytest.raises(ValueError, match="must be a JSON object"):
        enc._merge_rendition_config([])

    with pytest.raises(ValueError, match="key cannot be empty"):
        enc._merge_rendition_config({" ": {"encode_mp4": True}})

    enc._RENDITION_CONFIG = {"720": {"resolution": "1280x720", "encode_mp4": True}}
    merged = enc._merge_rendition_config({"720": None})
    assert "720" not in merged

    with pytest.raises(ValueError, match="must be an object"):
        enc._merge_rendition_config({"720": "invalid"})

    enc._RENDITION_CONFIG = {"720": "legacy-non-dict"}
    merged_non_dict = enc._merge_rendition_config({"720": {"encode_mp4": False}})
    assert merged_non_dict["720"] == {"encode_mp4": False}


def test_rendition_validation_helpers_cover_error_branches():
    enc = _load_encoding_script_module()

    with pytest.raises(ValueError, match="Invalid rendition key"):
        enc._validate_rendition_key_and_cfg("hd", {})

    with pytest.raises(ValueError, match="must be an object"):
        enc._validate_rendition_key_and_cfg("720", "invalid")

    with pytest.raises(ValueError, match="missing required string field 'resolution'"):
        enc._parse_rendition_resolution("720", {})

    with pytest.raises(ValueError, match="Expected 'WIDTHxHEIGHT'"):
        enc._parse_rendition_resolution("720", {"resolution": "1280-720"})

    with pytest.raises(ValueError, match="must contain positive integers"):
        enc._parse_rendition_resolution("720", {"resolution": "0x720"})

    with pytest.raises(ValueError, match="must match resolution height"):
        enc._parse_rendition_resolution("720", {"resolution": "1280x721"})

    with pytest.raises(ValueError, match="video_bitrate' must be a string"):
        enc._normalize_video_bitrate("720", {"video_bitrate": 42}, 1280, 720)

    with pytest.raises(ValueError, match="audio_bitrate' must be a string"):
        enc._normalize_audio_bitrate("720", {"audio_bitrate": 42}, 720)

    with pytest.raises(ValueError, match="encode_mp4' must be a boolean"):
        enc._normalize_encode_mp4("720", {"encode_mp4": "true"})


def test_validate_and_normalize_config_rejects_invalid_containers():
    enc = _load_encoding_script_module()

    with pytest.raises(ValueError, match="must be an object"):
        enc._validate_and_normalize_rendition_config(["not", "a", "dict"])

    with pytest.raises(ValueError, match="cannot be empty"):
        enc._validate_and_normalize_rendition_config({})


def test_rendition_selection_and_output_helpers_cover_mp4_and_metadata_paths():
    enc = _load_encoding_script_module()

    enc._RENDITION_CONFIG = enc._validate_and_normalize_rendition_config(
        {
            "360": {
                "resolution": "640x360",
                "video_bitrate": "750k",
                "audio_bitrate": "96k",
                "encode_mp4": True,
            },
            "720": {
                "resolution": "1280x720",
                "video_bitrate": "2000k",
                "audio_bitrate": "128k",
                "encode_mp4": True,
            },
            "1080": {
                "resolution": "1920x1080",
                "video_bitrate": "3000k",
                "audio_bitrate": "192k",
                "encode_mp4": False,
            },
        }
    )

    selected_mp4 = enc._select_renditions_for_encode(source_height=1080, output_format="mp4")
    selected_keys = [key for key, _, _ in selected_mp4]
    assert selected_keys == ["360", "720"]

    segment_mp4 = enc._build_video_output_segment(
        output_format="mp4",
        rendition_key="360",
        rendition_cfg=enc._RENDITION_CONFIG["360"],
        output_basename="sample",
    )
    assert '"/tmp/esup-runner/task01/output/360p_sample.mp4"' in segment_mp4
    assert "-movflags faststart" in segment_mp4

    metadata_entries = enc._build_video_metadata_entries(
        output_format="mp4", source_height=1080, output_basename="sample"
    )
    assert metadata_entries == [
        {
            "encoding_format": "video/mp4",
            "rendition": "640x360",
            "filename": "360p_sample.mp4",
        },
        {
            "encoding_format": "video/mp4",
            "rendition": "1280x720",
            "filename": "720p_sample.mp4",
        },
    ]


def test_build_encode_thumbnail_job_limits_size_to_1280x720(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)

    ffmpeg_cmd, title, content, append, _ = enc._build_encode_thumbnail_job(
        file="input.mp4",
        filename="input",
        duration=120,
        thumbnail_index=0,
    )

    assert title == "encode_thumbnail"
    assert append is True
    assert content["filename"] == "input_0.png"
    assert "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease" in ffmpeg_cmd
    assert "-vframes 1" in ffmpeg_cmd


def test_parse_rendition_config_deep_merges_existing_entries_and_adds_2160():
    enc = _load_encoding_script_module()

    args = Mock(
        rendition=(
            '{"720":{"encode_mp4":false,"video_bitrate":"2500k"},'
            '"2160":{"resolution":"3840x2160","video_bitrate":"12000k","audio_bitrate":"192k","encode_mp4":true}}'
        )
    )
    msg = enc._parse_rendition_config(args, "")

    assert "Rendition configuration updated" in msg
    assert enc._RENDITION_CONFIG["720"]["resolution"] == "1280x720"
    assert enc._RENDITION_CONFIG["720"]["video_bitrate"] == "2500k"
    assert enc._RENDITION_CONFIG["720"]["audio_bitrate"] == "128k"
    assert enc._RENDITION_CONFIG["720"]["encode_mp4"] is False
    assert enc._RENDITION_CONFIG["2160"]["resolution"] == "3840x2160"
    assert enc._RENDITION_CONFIG["2160"]["video_bitrate"] == "12000k"
    assert enc._RENDITION_CONFIG["2160"]["audio_bitrate"] == "192k"
    assert enc._RENDITION_CONFIG["2160"]["encode_mp4"] is True


def test_parse_rendition_config_allows_missing_bitrates_for_new_rendition():
    enc = _load_encoding_script_module()

    args = Mock(rendition=('{"2160":{"resolution":"3840x2160","encode_mp4":false}}'))
    msg = enc._parse_rendition_config(args, "")

    assert "Rendition configuration updated" in msg
    assert enc._RENDITION_CONFIG["2160"]["resolution"] == "3840x2160"
    # Auto-inferred defaults for missing bitrates on new renditions.
    assert enc._RENDITION_CONFIG["2160"]["video_bitrate"] == "12M"
    assert enc._RENDITION_CONFIG["2160"]["audio_bitrate"] == "192k"
    assert enc._RENDITION_CONFIG["2160"]["encode_mp4"] is False


def test_parse_rendition_config_rejects_invalid_bitrate_and_restores_defaults():
    enc = _load_encoding_script_module()

    args = Mock(
        rendition=(
            '{"2160":{"resolution":"3840x2160","video_bitrate":"12000","audio_bitrate":"192k","encode_mp4":true}}'
        )
    )
    msg = enc._parse_rendition_config(args, "")

    assert "Warning: Failed to parse rendition parameter" in msg
    assert "Using default rendition configuration" in msg
    assert enc._RENDITION_CONFIG == enc._DEFAULT_RENDITION_CONFIG


def test_get_cmd_cpu_uses_primary_stream_mapping_and_probe_options(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    enc._choose_h264_encoder = Mock(return_value=("libx264", ""))

    cmd = enc.get_cmd_cpu("m3u8", "h264", 360, "input.mp4")

    assert " -probesize 100M -analyzeduration 100M -i " in cmd
    assert "-map 0:v:0? -map 0:a?" in cmd


def test_get_cmd_cpu_uses_1080_rate_ladder_for_hls(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    enc._choose_h264_encoder = Mock(return_value=("libx264", ""))

    cmd = enc.get_cmd_cpu("m3u8", "h264", 1080, "input.mp4")

    assert "1080p_input.m3u8" in cmd
    assert "-b:v 3000k -maxrate 4500k -bufsize 6M" in cmd


def test_get_cmd_cpu_includes_2160_only_when_explicitly_configured(tmp_path):
    enc = _load_encoding_script_module()

    enc._VIDEOS_DIR = str(tmp_path)
    enc._VIDEOS_OUTPUT_DIR = str(tmp_path)
    enc._choose_h264_encoder = Mock(return_value=("libx264", ""))

    default_cmd = enc.get_cmd_cpu("m3u8", "h264", 2160, "input.mp4")
    assert "2160p_input.m3u8" not in default_cmd

    enc._RENDITION_CONFIG = enc._validate_and_normalize_rendition_config(
        {
            **enc._RENDITION_CONFIG,
            "2160": {
                "resolution": "3840x2160",
                "video_bitrate": "12000k",
                "audio_bitrate": "192k",
                "encode_mp4": True,
            },
        }
    )
    cmd_4k = enc.get_cmd_cpu("m3u8", "h264", 2160, "input.mp4")

    assert "2160p_input.m3u8" in cmd_4k
    assert "-b:v 12000k" in cmd_4k
    assert "-b:a 192k" in cmd_4k


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


def test_process_encoding_rejects_zero_second_input(tmp_path):
    enc = _load_encoding_script_module()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "encoding.log").write_text("")

    enc._VIDEOS_OUTPUT_DIR = str(output_dir)
    enc._CUT_CONFIG = {}
    enc.EFFECTIVE_DURATION = 0
    enc._prepare_input_file = Mock(return_value=("input.mp4", "input prepared\n"))
    enc.get_info_video = Mock(
        return_value={
            "duration": 0,
            "codec": "h264",
            "height": 720,
            "has_stream_video": True,
            "has_stream_thumbnail": True,
            "has_stream_audio": False,
        }
    )
    enc.launch_encode = Mock()

    with pytest.raises(enc.EncodingValidationError, match="input video duration is 0 seconds"):
        enc._process_encoding(args=Mock())

    enc.launch_encode.assert_not_called()


def test_process_encoding_rejects_invalid_source_video_file(tmp_path):
    enc = _load_encoding_script_module()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "encoding.log").write_text("")

    enc._VIDEOS_OUTPUT_DIR = str(output_dir)
    enc._prepare_input_file = Mock(return_value=("fake-video.mp4", "input prepared\n"))
    enc.get_info_video = Mock(return_value={})
    enc.launch_encode = Mock()

    with pytest.raises(
        enc.EncodingValidationError,
        match="source file does not appear to be a valid video file",
    ):
        enc._process_encoding(args=Mock())

    enc.launch_encode.assert_not_called()


def test_process_encoding_rejects_zero_second_effective_duration_after_cut(tmp_path):
    enc = _load_encoding_script_module()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    (output_dir / "encoding.log").write_text("")

    enc._VIDEOS_OUTPUT_DIR = str(output_dir)
    enc._CUT_CONFIG = {"start": "00:00:05", "end": "00:00:05"}
    enc.EFFECTIVE_DURATION = 0
    enc._prepare_input_file = Mock(return_value=("input.mp4", "input prepared\n"))
    enc.get_info_video = Mock(
        return_value={
            "duration": 12,
            "codec": "h264",
            "height": 720,
            "has_stream_video": True,
            "has_stream_thumbnail": True,
            "has_stream_audio": False,
        }
    )
    enc.launch_encode = Mock()

    with pytest.raises(
        enc.EncodingValidationError,
        match="effective video duration is 0 seconds after applying cut",
    ):
        enc._process_encoding(args=Mock())

    enc.launch_encode.assert_not_called()
