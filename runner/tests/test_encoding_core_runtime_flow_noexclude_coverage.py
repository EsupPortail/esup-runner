"""Validates encoding runtime flow conversion branches and ffmpeg encoder selection logic."""

import importlib
import os
import types
from pathlib import Path

import pytest


def _load_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def _make_args(tmp_path, **overrides):
    defaults = {
        "debug": "false",
        "encoding_type": "CPU",
        "base_dir": str(tmp_path),
        "work_dir": "output",
        "input_file": "input video.mp4",
        "hwaccel_device": "0",
        "cuda_visible_devices": None,
        "cuda_device_order": None,
        "cuda_path": None,
        "rendition": None,
        "cut": None,
        "dressing": None,
        "video_id": None,
        "video_slug": None,
        "video_title": None,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def test_runtime_flow_wrapper_and_conversion_branches(monkeypatch, tmp_path):
    """Validate Runtime flow wrapper and conversion branches."""
    runtime_flow = _load_core_module("runtime_flow_utils")

    assert runtime_flow.timestamp_to_seconds("01:02:03") == 3723
    assert runtime_flow.timestamp_to_seconds("02:03") == 123
    assert runtime_flow.timestamp_to_seconds("17") == 17
    assert runtime_flow.timestamp_to_seconds("not-a-time") == 0

    monkeypatch.setattr(
        runtime_flow.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    ok, msg = runtime_flow._convert_file("a.png", "b.png")
    assert ok is True
    assert msg == ""

    monkeypatch.setattr(
        runtime_flow.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=4, stdout="convert failed", stderr=""),
    )
    ok, msg = runtime_flow._convert_file("a.png", "b.png")
    assert ok is False
    assert "convert failed" in msg

    monkeypatch.setattr(
        runtime_flow.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("convert")),
    )
    ok, msg = runtime_flow._convert_file("a.png", "b.png")
    assert ok is False
    assert "command not found" in msg

    monkeypatch.setattr(
        runtime_flow.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    ok, msg = runtime_flow._convert_file("a.png", "b.png")
    assert ok is False
    assert "convert exception" in msg

    runtime_flow._has_encoder.cache_clear()
    runtime_flow._nvenc_preflight.cache_clear()

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils,
        "has_encoder",
        lambda encoder, **_k: encoder == "libx264",
    )
    assert runtime_flow._has_encoder("libx264") is True

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils,
        "choose_h264_encoder",
        lambda **_k: ("libx264", ""),
    )
    assert runtime_flow._choose_h264_encoder() == ("libx264", "")

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils,
        "nvenc_preflight",
        lambda **_k: (True, "ok"),
    )
    assert runtime_flow._nvenc_preflight() == (True, "ok")

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils,
        "launch_cmd",
        lambda *_a, **_k: (True, "launch ok"),
    )
    assert runtime_flow.launch_cmd("ffmpeg -i in out", "cpu", "mp4") == (True, "launch ok")

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "safe_filename_from_url",
        lambda url, **_k: f"safe-{url}",
    )
    assert runtime_flow._safe_filename_from_url("x.png") == "safe-x.png"

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "download_allowed_hosts_from_env",
        lambda: ["example.org"],
    )
    assert runtime_flow._download_allowed_hosts_from_env() == ["example.org"]

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "download_allow_private_networks_from_env",
        lambda: False,
    )
    assert runtime_flow._download_allow_private_networks_from_env() is False

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "host_is_allowed",
        lambda host, allowed_hosts: host in allowed_hosts,
    )
    assert runtime_flow._host_is_allowed("example.org", ["example.org"]) is True

    calls = {"validated": None}
    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "validate_host_resolves_to_public_ip",
        lambda host: calls.__setitem__("validated", host),
    )
    runtime_flow._validate_host_resolves_to_public_ip("example.org")
    assert calls["validated"] == "example.org"

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "download_url_to_dir",
        lambda url, *_a, **_k: f"/tmp/{url.split('/')[-1]}",
    )
    assert runtime_flow._download_url_to_dir("https://x/y.png", "/tmp", "wm") == "/tmp/y.png"

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils, "probe_duration_seconds", lambda *_a: 12.5
    )
    assert runtime_flow._probe_duration_seconds("x.mp4") == 12.5

    monkeypatch.setattr(runtime_flow.dressing_runtime_utils, "probe_has_audio", lambda *_a: True)
    assert runtime_flow._probe_has_audio("x.mp4") is True

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "watermark_overlay_xy",
        lambda *_a, **_k: ("10", "20"),
    )
    assert runtime_flow._watermark_overlay_xy("top_right") == ("10", "20")

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "build_normalize_1080p_filter",
        lambda *_a: "normalize-filter",
    )
    assert runtime_flow._build_normalize_1080p_filter("in", "out") == "normalize-filter"

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "run_ffmpeg_cmd",
        lambda *_a, **_k: True,
    )
    assert runtime_flow._run_ffmpeg_cmd("ffmpeg -i x y", "wm") is True

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "create_cut_intermediate",
        lambda *_a, **_k: True,
    )
    assert (
        runtime_flow._create_cut_intermediate("in.mp4", "out.mp4", "00:00:01", "00:00:03") is True
    )

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "create_watermarked_intermediate",
        lambda *_a, **_k: True,
    )
    assert (
        runtime_flow._create_watermarked_intermediate(
            "in.mp4", "wm.png", "out.mp4", "top_right", "100"
        )
        is True
    )

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "parse_duration_seconds_fallback",
        lambda value, **_k: 3.0 if value else 0.0,
    )
    assert runtime_flow._parse_duration_seconds_fallback("00:00:03") == 3.0

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "create_credits_concat_intermediate",
        lambda *_a, **_k: True,
    )
    assert (
        runtime_flow._create_credits_concat_intermediate(
            "main.mp4",
            "open.mp4",
            "2",
            "end.mp4",
            "3",
            "out.mp4",
        )
        is True
    )

    monkeypatch.setattr(
        runtime_flow.dressing_runtime_utils,
        "apply_dressing_if_needed",
        lambda *_a, **_k: ("dressed.mp4", "dressed\n"),
    )
    assert runtime_flow.apply_dressing_if_needed("input.mp4", {"watermark": "x"}) == (
        "dressed.mp4",
        "dressed\n",
    )


def test_runtime_flow_encode_overview_media_and_metadata_wrappers(monkeypatch):
    """Validate Runtime flow encode overview media and metadata wrappers."""
    runtime_flow = _load_core_module("runtime_flow_utils")

    monkeypatch.setattr(runtime_flow.encoding_flow_utils, "encode_with_gpu", lambda *_a, **_k: True)
    assert runtime_flow.encode_with_gpu("m3u8", "h264", 720, "in.mp4") is True

    monkeypatch.setattr(
        runtime_flow.encoding_flow_utils,
        "encode_without_gpu",
        lambda *_a, **_k: True,
    )
    assert runtime_flow.encode_without_gpu("m3u8", "h264", 720, "in.mp4") is True

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils, "run_and_collect_text", lambda *_a, **_k: (0, "ok")
    )
    assert runtime_flow._run_and_collect_text(["ffmpeg"]) == (0, "ok")

    monkeypatch.setattr(
        runtime_flow.ffmpeg_runtime_utils, "run_shell_bytes", lambda *_a, **_k: (0, b"ok")
    )
    assert runtime_flow._run_shell_bytes("echo ok") == (0, b"ok")

    monkeypatch.setattr(
        runtime_flow.overview_utils,
        "try_sprite_imagemagick_append",
        lambda **_k: (True, "im-ok"),
    )
    assert runtime_flow._try_sprite_imagemagick_append(
        temp_thumb_dir="/tmp",
        num_thumbnails=3,
        sprite_path="/tmp/overview.png",
    ) == (True, "im-ok")

    monkeypatch.setattr(
        runtime_flow.overview_utils,
        "get_overview_max_single_row_thumbnails",
        lambda *_a, **_k: 8,
    )
    assert runtime_flow._get_overview_max_single_row_thumbnails(160, 90) == 8

    monkeypatch.setattr(
        runtime_flow.overview_utils,
        "compute_overview_single_row_plan",
        lambda *_a, **_k: (2, 5, 6, 8),
    )
    assert runtime_flow._compute_overview_single_row_plan(10, 1, 160, 90) == (2, 5, 6, 8)

    monkeypatch.setattr(
        runtime_flow.overview_utils,
        "format_overview_thumbnail_plan_msg",
        lambda *_a: "plan-msg",
    )
    assert runtime_flow._format_overview_thumbnail_plan_msg(1, 1, 1, 1) == "plan-msg"

    monkeypatch.setattr(
        runtime_flow.overview_utils,
        "build_overview_generation_result_msg",
        lambda *_a: (True, "generated", 3),
    )
    assert runtime_flow._build_overview_generation_result_msg("/tmp", 3) == (True, "generated", 3)

    monkeypatch.setattr(runtime_flow.encoding_flow_utils, "encode", lambda *_a, **_k: True)
    assert runtime_flow.encode("cpu", "mp4", "h264", 720, "in.mp4") is True

    monkeypatch.setattr(
        runtime_flow.overview_utils, "generate_overview", lambda *_a, **_k: (True, "ok")
    )
    assert runtime_flow.generate_overview("in.mp4", 10) == (True, "ok")

    monkeypatch.setattr(
        runtime_flow.media_probe_utils, "get_info_from_video", lambda *_a, **_k: ({}, "ok")
    )
    assert runtime_flow.get_info_from_video("ffprobe") == ({}, "ok")

    monkeypatch.setattr(runtime_flow.media_probe_utils, "seconds_from_timestamp", lambda value: 1.5)
    assert runtime_flow._seconds_from_timestamp("00:00:01.5") == 1.5

    monkeypatch.setattr(
        runtime_flow.media_probe_utils, "duration_seconds_from_value", lambda value: 2.0
    )
    assert runtime_flow._duration_seconds_from_value("2") == 2.0

    monkeypatch.setattr(
        runtime_flow.encoding_flow_utils, "launch_encode_video", lambda *_a, **_k: (True, True)
    )
    assert runtime_flow.launch_encode_video({"codec": "h264", "height": 720}, "in.mp4") == (
        True,
        True,
    )

    monkeypatch.setattr(
        runtime_flow.encoding_flow_utils, "launch_encode_audio", lambda *_a, **_k: (True, "ok")
    )
    assert runtime_flow.launch_encode_audio({"has_stream_audio": True}, "in.mp4") == (True, "ok")

    monkeypatch.setattr(runtime_flow.encoding_flow_utils, "launch_encode", lambda *_a, **_k: True)
    assert runtime_flow.launch_encode({"has_stream_video": True}, "in.mp4") is True

    recorded = []
    monkeypatch.setattr(
        runtime_flow.metadata_runtime_utils,
        "add_info_video",
        lambda key, value, **_k: recorded.append((key, value)),
    )
    runtime_flow.add_info_video("k", "v")
    assert recorded == [("k", "v")]

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_encode_video_job",
        lambda **_k: ("cmd", "encode_video", {"filename": "a"}, False, {}),
    )
    assert (
        runtime_flow._build_encode_video_job(
            encoder_type="cpu",
            format="mp4",
            codec="h264",
            height=720,
            file="in.mp4",
            filename="in",
        )[0]
        == "cmd"
    )

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_encode_audio_job",
        lambda **_k: ("cmd", "encode_audio", {"filename": "a"}, False, {}),
    )
    assert (
        runtime_flow._build_encode_audio_job(kind="mp3", file="in.mp4", filename="in")[0] == "cmd"
    )

    monkeypatch.setattr(
        runtime_flow.ffmpeg_command_utils,
        "build_encode_thumbnail_job",
        lambda **_k: ("cmd", "thumbnail", {"filename": "a"}, False, {}),
    )
    assert (
        runtime_flow._build_encode_thumbnail_job(
            file="in.mp4",
            filename="in",
            duration=10,
            thumbnail_index=0,
        )[0]
        == "cmd"
    )


def test_runtime_flow_parse_and_apply_cli_config_branches(monkeypatch, tmp_path):
    """Validate Runtime flow parse and apply cli config branches."""
    runtime_flow = _load_core_module("runtime_flow_utils")

    args_valid = _make_args(
        tmp_path,
        debug="true",
        encoding_type="GPU",
        rendition='{"360": {"resolution": "640x360", "video_bitrate": "700k", "audio_bitrate": "96k", "encode_mp4": true}}',
        cut='{"start": "00:00:02", "end": "00:00:10", "initial_duration": "00:00:05"}',
        dressing='{"watermark": "https://example.org/wm.png"}',
        video_id="42",
        video_slug="slug-42",
        video_title="Title",
        cuda_visible_devices="0,1",
        hwaccel_device="3",
    )

    msg = runtime_flow._apply_cli_config(args_valid)
    assert "Rendition configuration updated" in msg
    assert "Cut configuration applied" in msg
    assert "Warning: end time" in msg
    assert "Dressing configuration received" in msg
    assert "Video identification metadata received" in msg
    assert runtime_flow._DEBUG is True
    assert runtime_flow._HWACCEL_DEVICE == 3
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == "0,1"

    out_dir = Path(runtime_flow._VIDEOS_OUTPUT_DIR)
    assert (out_dir / "encoding.log").exists()
    assert (out_dir / "info_video.json").exists()

    (out_dir / "encoding.log").write_text("pre-existing\n", encoding="utf-8")
    msg = runtime_flow._apply_cli_config(args_valid)
    assert "ENCODING STAGE" in (out_dir / "encoding.log").read_text(encoding="utf-8")

    args_invalid = _make_args(
        tmp_path,
        rendition="{not-json",
        cut='{"start": "00:00:10"}',
        dressing="{bad",
        video_id="   ",
        video_slug="",
        video_title=None,
    )
    msg = runtime_flow._apply_cli_config(args_invalid)
    assert "Failed to parse rendition" in msg
    assert "Cut configuration incomplete" in msg
    assert "Failed to parse dressing" in msg

    args_cut_error = _make_args(tmp_path, cut="{bad")
    cut_msg = runtime_flow._parse_cut_config(args_cut_error, "")
    assert "Cut configuration ignored" in cut_msg

    args_dressing_error = _make_args(tmp_path, dressing="{bad")
    dressing_msg = runtime_flow._parse_dressing_config(args_dressing_error, "")
    assert "Dressing configuration ignored" in dressing_msg

    args_ids = _make_args(tmp_path, video_id="900", video_slug="slug", video_title="T")
    id_msg = runtime_flow._parse_video_identification(args_ids, "")
    assert "Video identification metadata received" in id_msg

    args_bad_hwaccel = _make_args(
        tmp_path,
        encoding_type="GPU",
        cuda_visible_devices="0,1",
        hwaccel_device="not-an-int",
    )
    runtime_flow._apply_cli_config(args_bad_hwaccel)
    assert runtime_flow._HWACCEL_DEVICE == 0

    args_single_gpu = _make_args(
        tmp_path,
        encoding_type="GPU",
        cuda_visible_devices="0",
        hwaccel_device="7",
    )
    runtime_flow._apply_cli_config(args_single_gpu)
    assert runtime_flow._HWACCEL_DEVICE == 0

    # Force append failure to exercise truncation fallback branch.
    append_target = str(out_dir / "encoding.log")
    real_open = open

    def _open_with_append_failure(path, mode="r", *args, **kwargs):
        if str(path) == append_target and mode == "a":
            raise OSError("append denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _open_with_append_failure)
    runtime_flow._apply_cli_config(args_valid)
    assert (out_dir / "encoding.log").exists()


def test_runtime_flow_prepare_validate_and_process_branches(monkeypatch, tmp_path):
    """Validate Runtime flow prepare validate and process branches."""
    runtime_flow = _load_core_module("runtime_flow_utils")

    runtime_flow._VIDEOS_DIR = str(tmp_path)
    runtime_flow._VIDEOS_OUTPUT_DIR = str(tmp_path / "out")
    runtime_flow._DRESSING_CONFIG = {}

    source_file = tmp_path / "input video.mp4"
    source_file.write_bytes(b"content")

    filename, prep_msg = runtime_flow._prepare_input_file(
        _make_args(tmp_path, input_file=source_file.name)
    )
    assert filename == "input_video.mp4"
    assert "Encoding file" in prep_msg
    assert (tmp_path / "input_video.mp4").exists()

    runtime_flow._DRESSING_CONFIG = {"watermark": "https://example.org/wm.png"}
    recorded_add_info = []
    monkeypatch.setattr(
        runtime_flow,
        "apply_dressing_if_needed",
        lambda *_a: ("input_video_dressed.mp4", "dressing applied\n"),
    )
    monkeypatch.setattr(
        runtime_flow,
        "add_info_video",
        lambda key, value, append=False: recorded_add_info.append((key, value, append)),
    )
    filename, prep_msg = runtime_flow._prepare_input_file(
        _make_args(tmp_path, input_file="input_video.mp4")
    )
    assert filename == "input_video_dressed.mp4"
    assert "dressing applied" in prep_msg
    assert any(k == "dressing" for k, _, _ in recorded_add_info)

    with pytest.raises(runtime_flow.EncodingValidationError):
        runtime_flow._prepare_input_file(_make_args(tmp_path, input_file="missing.mp4"))

    runtime_flow._CUT_CONFIG = {"start": "00:00:01", "end": "00:00:05"}
    runtime_flow.EFFECTIVE_DURATION = 4
    duration, msg = runtime_flow._compute_working_duration({"duration": 10})
    assert duration == 4
    assert "Using effective duration from cut" in msg

    runtime_flow._CUT_CONFIG = {}
    runtime_flow._DEBUG = False
    runtime_flow.SUBTIME = " "
    duration, msg = runtime_flow._compute_working_duration({"duration": 12})
    assert duration == 12
    assert runtime_flow.SUBTIME == " -ss 0 -to 12 "

    with pytest.raises(runtime_flow.EncodingValidationError):
        runtime_flow._validate_source_media_info({})

    with pytest.raises(runtime_flow.EncodingValidationError):
        runtime_flow._validate_source_media_info(
            {
                "has_stream_video": False,
                "has_stream_audio": False,
                "has_stream_thumbnail": True,
            }
        )

    runtime_flow._validate_working_duration(1)

    runtime_flow._CUT_CONFIG = {"start": "00:00:01", "end": "00:00:01"}
    with pytest.raises(runtime_flow.EncodingValidationError, match="effective video duration"):
        runtime_flow._validate_working_duration(0)

    runtime_flow._CUT_CONFIG = {}
    with pytest.raises(runtime_flow.EncodingValidationError, match="input video duration"):
        runtime_flow._validate_working_duration(0)

    runtime_flow.EFFECTIVE_DURATION = 3
    runtime_flow._VIDEO_IDENTIFICATION = {"video_id": "42"}
    runtime_flow._SOURCE_VIDEO_FPS = 0.0

    recorded_add_info.clear()
    monkeypatch.setattr(runtime_flow, "_prepare_input_file", lambda _args: ("input.mp4", "prep\n"))
    monkeypatch.setattr(
        runtime_flow,
        "get_info_video",
        lambda _filename: {
            "duration": 12,
            "source_fps": 29.97,
            "has_stream_video": True,
            "has_stream_audio": True,
            "codec": "h264",
            "height": 720,
            "has_stream_thumbnail": True,
        },
    )
    monkeypatch.setattr(runtime_flow, "_compute_working_duration", lambda _info: (12, "duration\n"))
    monkeypatch.setattr(runtime_flow, "_validate_source_media_info", lambda _info: None)
    monkeypatch.setattr(runtime_flow, "_validate_working_duration", lambda _duration: None)
    monkeypatch.setattr(runtime_flow, "launch_encode", lambda _info, _file: True)
    monkeypatch.setattr(
        runtime_flow,
        "add_info_video",
        lambda key, value, append=False: recorded_add_info.append((key, value, append)),
    )

    process_msg = runtime_flow._process_encoding(_make_args(tmp_path))
    assert "Using source fps estimate for encode decisions" in process_msg
    assert "End of encoding" in process_msg
    assert any(key == "encode_result" and value is True for key, value, _ in recorded_add_info)

    monkeypatch.setattr(runtime_flow, "_prepare_input_file", lambda _args: ("", "invalid file"))
    with pytest.raises(runtime_flow.EncodingValidationError):
        runtime_flow._process_encoding(_make_args(tmp_path))
