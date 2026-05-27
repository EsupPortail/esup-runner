"""Validates encoding flow branches and transcription runtime CLI command building."""

import importlib
import json
import shutil
import subprocess
import types
from pathlib import Path


def _load_encoding_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def _load_transcription_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.transcription.core.{module_name}")
    return importlib.reload(module)


def test_encoding_flow_utils_core_branches():
    """Validate Encoding flow utils core branches."""
    flow = _load_encoding_core_module("encoding_flow_utils")

    logs = []

    assert (
        flow.encode_with_gpu(
            "m3u8",
            "h264",
            720,
            "video.mp4",
            encode_fn=lambda kind, *_a: kind == "gpu",
            encode_log_fn=logs.append,
        )
        is True
    )
    assert "Encode GPU m3u8 ok" in logs[-1]

    assert (
        flow.encode_with_gpu(
            "mp4",
            "h264",
            720,
            "video.mp4",
            encode_fn=lambda kind, *_a: kind == "cpu",
            encode_log_fn=logs.append,
        )
        is True
    )
    assert "Encode CPU mp4 ok" in logs[-1]

    assert (
        flow.encode_with_gpu(
            "mp4",
            "h264",
            720,
            "video.mp4",
            encode_fn=lambda *_a: False,
            encode_log_fn=logs.append,
        )
        is False
    )
    assert "ERROR ENCODING mp4" in logs[-1]

    assert (
        flow.encode_without_gpu(
            "m3u8",
            "h264",
            720,
            "video.mp4",
            encode_fn=lambda kind, *_a: kind == "cpu",
            encode_log_fn=logs.append,
        )
        is True
    )
    assert (
        flow.encode_without_gpu(
            "m3u8",
            "h264",
            720,
            "video.mp4",
            encode_fn=lambda *_a: False,
            encode_log_fn=logs.append,
        )
        is False
    )

    assert (
        flow.encode(
            "unknown",
            "mp4",
            "h264",
            720,
            "video.mp4",
            sanitize_filename_fn=lambda v: v,
            build_encode_video_job_fn=lambda **_k: ("", "", {}, False, {}),
            build_encode_audio_job_fn=lambda **_k: ("", "", {}, False, {}),
            build_encode_thumbnail_job_fn=lambda **_k: ("", "", {}, False, {}),
            launch_cmd_fn=lambda *_a: (True, "ok"),
            add_info_video_fn=lambda *_a: None,
            encode_log_fn=logs.append,
        )
        is False
    )
    assert "Unknown encoding type" in logs[-1]

    added = []
    launched = []

    def _build_encode_video_job(**_kwargs):
        return (
            "ffmpeg -i input output",
            "encode_video",
            {"filename": "v_720.mp4", "encoding_format": "video/mp4"},
            False,
            {"additional_renditions": [{"filename": "v_360.mp4", "encoding_format": "video/mp4"}]},
        )

    assert (
        flow.encode(
            "cpu",
            "mp4",
            "h264",
            720,
            "/tmp/My Video.mp4",
            sanitize_filename_fn=lambda v: v.replace(" ", "_"),
            build_encode_video_job_fn=_build_encode_video_job,
            build_encode_audio_job_fn=lambda **_k: (
                "",
                "",
                {},
                False,
                {"skip_execution": True, "skip_reason": "skip-audio"},
            ),
            build_encode_thumbnail_job_fn=lambda **_k: (
                "",
                "",
                {},
                False,
                {"skip_execution": True, "skip_reason": "skip-thumb"},
            ),
            launch_cmd_fn=lambda cmd, typ, fmt: (
                launched.append((cmd, typ, fmt)) or True,
                "launch-ok\n",
            ),
            add_info_video_fn=lambda title, content, append=False: added.append(
                (title, content, append)
            ),
            encode_log_fn=logs.append,
        )
        is True
    )
    assert launched[-1][1:] == ("cpu", "mp4")
    assert added[0][0] == "encode_video"
    assert added[1][2] is True

    assert (
        flow.encode(
            "mp3",
            "mp3",
            "",
            0,
            "/tmp/audio.mp4",
            sanitize_filename_fn=lambda v: v,
            build_encode_video_job_fn=lambda **_k: ("", "", {}, False, {}),
            build_encode_audio_job_fn=lambda **_k: (
                "",
                "",
                {},
                False,
                {"skip_execution": True, "skip_reason": "audio-skip"},
            ),
            build_encode_thumbnail_job_fn=lambda **_k: ("", "", {}, False, {}),
            launch_cmd_fn=lambda *_a: (True, "unused"),
            add_info_video_fn=lambda *_a: None,
            encode_log_fn=logs.append,
        )
        is True
    )
    assert "audio-skip" in logs[-1]

    assert (
        flow.encode(
            "thumbnail",
            "png",
            "",
            0,
            "/tmp/input.mp4",
            duration=20,
            thumbnail_index=2,
            sanitize_filename_fn=lambda v: v,
            build_encode_video_job_fn=lambda **_k: ("", "", {}, False, {}),
            build_encode_audio_job_fn=lambda **_k: ("", "", {}, False, {}),
            build_encode_thumbnail_job_fn=lambda **_k: (
                "ffmpeg-thumb",
                "thumbnail",
                {"filename": "t.png"},
                False,
                {},
            ),
            launch_cmd_fn=lambda cmd, typ, fmt: (
                launched.append((cmd, typ, fmt)) or True,
                "thumb-ok\n",
            ),
            add_info_video_fn=lambda *_a: None,
            encode_log_fn=logs.append,
        )
        is True
    )
    assert launched[-1][1] == "thumbnail"

    video_calls = []
    no_gpu_calls = []
    yes_gpu_calls = []
    launch_video_logs = []

    encode_m3u8, encode_mp4 = flow.launch_encode_video(
        {"codec": "h264", "height": 720},
        "video.mp4",
        encoding_type="GPU",
        list_codec=("h264",),
        select_renditions_for_encode_fn=lambda **_k: [("720", {}, 720)],
        nvenc_preflight_fn=lambda: (False, "nvenc-unavailable"),
        encode_with_gpu_fn=lambda fmt, *_a: yes_gpu_calls.append(fmt) or True,
        encode_without_gpu_fn=lambda fmt, *_a: no_gpu_calls.append(fmt) or True,
        encode_log_fn=launch_video_logs.append,
    )
    video_calls.append((encode_m3u8, encode_mp4))
    assert video_calls[-1] == (True, True)
    assert no_gpu_calls == ["m3u8", "mp4"]
    assert "NVENC unavailable" in launch_video_logs[-1]

    no_gpu_calls.clear()
    yes_gpu_calls.clear()
    encode_m3u8, encode_mp4 = flow.launch_encode_video(
        {"codec": "h264", "height": 360},
        "video.mp4",
        encoding_type="GPU",
        list_codec=("h264",),
        select_renditions_for_encode_fn=lambda **_k: [],
        nvenc_preflight_fn=lambda: (True, ""),
        encode_with_gpu_fn=lambda fmt, *_a: yes_gpu_calls.append(fmt) or True,
        encode_without_gpu_fn=lambda fmt, *_a: no_gpu_calls.append(fmt) or True,
        encode_log_fn=launch_video_logs.append,
    )
    assert (encode_m3u8, encode_mp4) == (True, True)
    assert yes_gpu_calls == ["m3u8"]
    assert "Skipping mp4 encode" in launch_video_logs[-1]

    no_gpu_calls.clear()
    encode_m3u8, encode_mp4 = flow.launch_encode_video(
        {"codec": "vp9", "height": 480},
        "video.mp4",
        encoding_type="CPU",
        list_codec=("h264",),
        select_renditions_for_encode_fn=lambda **_k: [("360", {}, 360)],
        nvenc_preflight_fn=lambda: (True, ""),
        encode_with_gpu_fn=lambda *_a: False,
        encode_without_gpu_fn=lambda fmt, *_a: no_gpu_calls.append(fmt) or True,
        encode_log_fn=launch_video_logs.append,
    )
    assert (encode_m3u8, encode_mp4) == (True, True)
    assert no_gpu_calls == ["m3u8", "mp4"]

    encode_audio, msg = flow.launch_encode_audio(
        {"has_stream_video": False},
        "video.mp4",
        encode_fn=lambda kind, *_a: False if kind == "m4a" else True,
    )
    assert encode_audio is False
    assert "error m4a" in msg

    encode_audio, msg = flow.launch_encode_audio(
        {"has_stream_video": False},
        "video.mp4",
        encode_fn=lambda kind, *_a: True,
    )
    assert encode_audio is True
    assert "encode m4a ok" in msg

    encode_audio, msg = flow.launch_encode_audio(
        {"has_stream_video": True},
        "video.mp4",
        encode_fn=lambda kind, *_a: False if kind == "mp3" else True,
    )
    assert encode_audio is False
    assert "error mp3" in msg

    orchestration_logs = []
    metadata_added = []

    result = flow.launch_encode(
        {
            "has_stream_video": True,
            "has_stream_thumbnail": True,
            "has_stream_audio": True,
            "duration": 30,
        },
        "video.mp4",
        encode_fn=lambda typ, *_a: typ != "thumbnail" or _a[-1] != 1,
        launch_encode_video_fn=lambda *_a: (True, True),
        launch_encode_audio_fn=lambda *_a: (False, "audio-failed\n"),
        generate_overview_fn=lambda *_a: (True, "overview-msg\n"),
        add_info_video_fn=lambda title, value, append=False: metadata_added.append(
            (title, value, append)
        ),
        encode_log_fn=orchestration_logs.append,
    )
    assert result is False
    assert any(title == "encode_overview" for title, _, _ in metadata_added)
    assert "error thumbnail 1" in orchestration_logs[-1]

    result = flow.launch_encode(
        {
            "has_stream_video": True,
            "has_stream_thumbnail": False,
            "has_stream_audio": False,
            "duration": 10,
        },
        "video.mp4",
        encode_fn=lambda *_a: True,
        launch_encode_video_fn=lambda *_a: (True, True),
        launch_encode_audio_fn=lambda *_a: (True, ""),
        generate_overview_fn=lambda *_a: (False, "overview-failed\n"),
        add_info_video_fn=lambda *_a, **_k: None,
        encode_log_fn=orchestration_logs.append,
    )
    assert result is False
    assert "error generating overview" in orchestration_logs[-1]


def test_launch_encode_uses_video_duration_for_thumbnail_timestamps():
    """Validate thumbnail extraction uses primary video duration, not audio/container duration."""
    flow = _load_encoding_core_module("encoding_flow_utils")

    thumbnail_calls = []
    overview_calls = []

    def _encode_fn(typ, _fmt, _codec, _height, _file, duration=0, thumbnail_index=0):
        if typ == "thumbnail":
            thumbnail_calls.append((duration, thumbnail_index))
        return True

    result = flow.launch_encode(
        {
            "has_stream_video": True,
            "has_stream_thumbnail": True,
            "has_stream_audio": False,
            "duration": 5,
            "video_duration": 0.533333,
        },
        "studio_base.mp4",
        encode_fn=_encode_fn,
        launch_encode_video_fn=lambda *_a: (True, True),
        launch_encode_audio_fn=lambda *_a: (True, ""),
        generate_overview_fn=lambda _file, duration: (
            overview_calls.append(duration) or (True, "overview-ok\n")
        ),
        add_info_video_fn=lambda *_a, **_k: None,
        encode_log_fn=lambda _msg: None,
    )

    assert result is True
    assert thumbnail_calls == [(0.533333, 0), (0.533333, 1), (0.533333, 2)]
    assert overview_calls == [5]


def test_encode_thumbnail_fails_when_ffmpeg_writes_no_png(tmp_path):
    """Validate thumbnail encode does not report success without an actual PNG."""
    flow = _load_encoding_core_module("encoding_flow_utils")

    output_path = tmp_path / "studio_base_0.png"
    logs = []

    result = flow.encode(
        "thumbnail",
        "png",
        "",
        0,
        "studio_base.mp4",
        duration=5,
        thumbnail_index=0,
        sanitize_filename_fn=lambda value: value,
        build_encode_video_job_fn=lambda **_k: ("", "", {}, False, {}),
        build_encode_audio_job_fn=lambda **_k: ("", "", {}, False, {}),
        build_encode_thumbnail_job_fn=lambda **_k: (
            "ffmpeg thumb",
            "encode_thumbnail",
            {"filename": output_path.name},
            True,
            {"output_path": str(output_path)},
        ),
        launch_cmd_fn=lambda *_a: (True, "Output file is empty, nothing was encoded\n"),
        add_info_video_fn=lambda *_a, **_k: None,
        encode_log_fn=logs.append,
    )

    assert result is False
    assert "Thumbnail output missing or empty" in logs[-1]


def test_thumbnail_helpers_cover_fallback_and_file_errors(monkeypatch, tmp_path):
    """Validate thumbnail fallback and file error guards."""
    flow = _load_encoding_core_module("encoding_flow_utils")

    output_path = tmp_path / "studio_base_0.png"
    calls = []

    def _launch(cmd, *_args):
        calls.append(cmd)
        if cmd == "fallback":
            output_path.write_bytes(b"png")
        return True, f"{cmd}\n"

    ok, msg = flow._launch_thumbnail_job(
        "primary",
        "png",
        extra={"output_path": str(output_path), "fallback_cmd": "fallback"},
        launch_cmd_fn=_launch,
    )

    assert ok is True
    assert calls == ["primary", "fallback"]
    assert "retrying at timestamp 0" in msg

    monkeypatch.setattr(flow.os.path, "getsize", lambda _path: (_ for _ in ()).throw(OSError))
    assert flow._is_nonempty_file(str(output_path)) is False

    monkeypatch.setattr(flow.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(flow.os, "unlink", lambda _path: (_ for _ in ()).throw(OSError))
    flow._remove_file_if_exists(str(output_path))


def test_ffmpeg_runtime_utils_launch_cmd_missing_branches():
    """Validate Ffmpeg runtime utils launch cmd missing branches."""
    ffmpeg_runtime = _load_encoding_core_module("ffmpeg_runtime_utils")

    class _RunOkSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=0, stdout=b"ffmpeg ok")

    ok, msg = ffmpeg_runtime.launch_cmd(
        "ffmpeg -i in.mp4 out.mp4", "cpu", "mp4", subprocess_module=_RunOkSubprocess
    )
    assert ok is True
    assert "Encode file in" in msg
    assert "ffmpeg ok" in msg

    class _RunDecodeErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=7, stdout=b"\xff")

    ok, msg = ffmpeg_runtime.launch_cmd(
        "ffmpeg -i in.mp4 out.mp4", "cpu", "mp4", subprocess_module=_RunDecodeErrorSubprocess
    )
    assert ok is False
    assert "ERROR RETURN CODE" in msg

    class _RunCalledProcessErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*_a, **_k):
            raise subprocess.CalledProcessError(2, "ffmpeg")

    ok, msg = ffmpeg_runtime.launch_cmd(
        "ffmpeg -i in.mp4 out.mp4",
        "cpu",
        "mp4",
        subprocess_module=_RunCalledProcessErrorSubprocess,
    )
    assert ok is False
    assert "Runtime Error" in msg

    class _RunOSErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*_a, **_k):
            raise OSError("missing ffmpeg")

    ok, msg = ffmpeg_runtime.launch_cmd(
        "ffmpeg -i in.mp4 out.mp4", "cpu", "mp4", subprocess_module=_RunOSErrorSubprocess
    )
    assert ok is False
    assert "OS error" in msg

    class _RunGenericErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def run(*_a, **_k):
            raise RuntimeError("boom")

    ok, msg = ffmpeg_runtime.launch_cmd(
        "ffmpeg -i in.mp4 out.mp4", "cpu", "mp4", subprocess_module=_RunGenericErrorSubprocess
    )
    assert ok is False
    assert "Unexpected error" in msg


def test_metadata_runtime_utils_encode_log_and_add_info_video(capsys, tmp_path):
    """Validate Metadata runtime utils encode log and add info video."""
    metadata_runtime = _load_encoding_core_module("metadata_runtime_utils")

    out_dir = str(tmp_path)
    metadata_runtime.encode_log("hello-log", debug=True, videos_output_dir=out_dir)
    assert "hello-log" in capsys.readouterr().out
    assert "hello-log" in (tmp_path / "encoding.log").read_text(encoding="utf-8")

    metadata_runtime.add_info_video(
        "encode_video",
        {"filename": "v1.mp4"},
        append=False,
        videos_output_dir=out_dir,
    )
    metadata_runtime.add_info_video(
        "encode_video",
        {"filename": "v2.mp4"},
        append=True,
        videos_output_dir=out_dir,
    )
    metadata_runtime.add_info_video(
        "encode_video",
        {"filename": "v3.mp4"},
        append=True,
        videos_output_dir=out_dir,
    )
    metadata_runtime.add_info_video(
        "thumbnails",
        {"filename": "thumb.png"},
        append=True,
        videos_output_dir=out_dir,
    )

    data = json.loads((tmp_path / "info_video.json").read_text(encoding="utf-8"))
    assert isinstance(data["encode_video"], list)
    assert len(data["encode_video"]) == 3
    assert data["thumbnails"] == [{"filename": "thumb.png"}]

    (tmp_path / "info_video.json").write_text("{invalid-json", encoding="utf-8")
    metadata_runtime.add_info_video(
        "recovered",
        {"ok": True},
        append=False,
        videos_output_dir=out_dir,
    )
    recovered = json.loads((tmp_path / "info_video.json").read_text(encoding="utf-8"))
    assert recovered["recovered"] == {"ok": True}


def test_media_probe_utils_missing_noexclude_branches(monkeypatch, capsys, tmp_path):
    """Validate Media probe utils missing noexclude branches."""
    media_probe = _load_encoding_core_module("media_probe_utils")

    class _ProbeOkSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def check_output(*_a, **_k):
            return b'{"streams": [{"codec_name": "h264"}]}'

    info, msg = media_probe.get_info_from_video(
        "ffprobe -show_streams input.mp4", subprocess_module=_ProbeOkSubprocess
    )
    assert info == {"streams": [{"codec_name": "h264"}]}
    assert msg == ""

    class _ProbeCalledErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def check_output(*_a, **_k):
            raise subprocess.CalledProcessError(1, "ffprobe")

    info, msg = media_probe.get_info_from_video(
        "ffprobe -show_streams input.mp4", subprocess_module=_ProbeCalledErrorSubprocess
    )
    assert info is None
    assert "Runtime Error" in msg

    class _ProbeOSErrorSubprocess:
        PIPE = object()
        STDOUT = object()
        CalledProcessError = subprocess.CalledProcessError

        @staticmethod
        def check_output(*_a, **_k):
            raise OSError("ffprobe missing")

    info, msg = media_probe.get_info_from_video(
        "ffprobe -show_streams input.mp4", subprocess_module=_ProbeOSErrorSubprocess
    )
    assert info is None
    assert "OS error" in msg

    logs = []
    result = media_probe.get_info_video(
        "input.mp4",
        debug=True,
        videos_dir=str(tmp_path),
        image_codecs=["png"],
        webm_video_codecs={"vp9"},
        encode_log_fn=logs.append,
        get_info_from_video_fn=lambda _cmd: (None, "probe-failed"),
        analyze_streams_fn=lambda _streams, image_codecs: (
            False,
            False,
            False,
            "",
            0,
            0.0,
            "",
        ),
        extract_duration_from_probe_fn=lambda _info: 0,
        refine_source_fps_fn=lambda **_k: (0.0, ""),
        probe_packet_based_fps_fn=lambda *_a: 0.0,
    )
    assert result == {}
    debug_out = capsys.readouterr().out
    assert "Probe_cmd :" in debug_out
    assert "return_msg : probe-failed" in debug_out

    logs.clear()
    result = media_probe.get_info_video(
        "input.mp4",
        debug=False,
        videos_dir=str(tmp_path),
        image_codecs=["png"],
        webm_video_codecs={"vp9"},
        encode_log_fn=logs.append,
        get_info_from_video_fn=lambda _cmd: ({"streams": []}, ""),
        analyze_streams_fn=lambda _streams, image_codecs: (
            False,
            False,
            False,
            "",
            0,
            0.0,
            "stream-log\n",
        ),
        extract_duration_from_probe_fn=lambda _info: 0,
        refine_source_fps_fn=lambda **_k: (0.0, "unused\n"),
        probe_packet_based_fps_fn=lambda *_a: 0.0,
    )
    assert result["duration"] == 0
    assert "Warning: duration unavailable" in logs[-1]


def test_overview_utils_missing_noexclude_branches(monkeypatch, tmp_path):
    """Validate Overview utils missing noexclude branches."""
    overview = _load_encoding_core_module("overview_utils")

    ok, msg, count = overview.generate_overview_thumbnails(
        "video.mp4",
        10,
        str(tmp_path),
        videos_dir=str(tmp_path),
        overview_config={"enabled": False},
        run_and_collect_text_fn=lambda *_a: (0, ""),
    )
    assert ok is True
    assert count == 0
    assert "disabled" in msg

    ok, msg, count = overview.generate_overview_thumbnails(
        "video.mp4",
        10,
        str(tmp_path),
        videos_dir=str(tmp_path),
        overview_config={
            "enabled": True,
            "interval": 1,
            "thumbnail_width": 160,
            "thumbnail_height": 90,
        },
        run_and_collect_text_fn=lambda *_a: (0, ""),
        compute_overview_single_row_plan_fn=lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("bad-plan")
        ),
    )
    assert ok is False
    assert count == 0
    assert "Error planning overview thumbnails" in msg

    ok, msg, count = overview.generate_overview_thumbnails(
        "video.mp4",
        10,
        str(tmp_path),
        videos_dir=str(tmp_path),
        overview_config={
            "enabled": True,
            "interval": 1,
            "thumbnail_width": 160,
            "thumbnail_height": 90,
        },
        run_and_collect_text_fn=lambda *_a: (9, "ffmpeg-err"),
    )
    assert ok is False
    assert count == 0
    assert "Error generating overview thumbnails" in msg
    assert "ffmpeg-err" in msg

    ok, msg, count = overview.generate_overview_thumbnails(
        "video.mp4",
        10,
        str(tmp_path),
        videos_dir=str(tmp_path),
        overview_config={
            "enabled": True,
            "interval": 1,
            "thumbnail_width": 160,
            "thumbnail_height": 90,
        },
        run_and_collect_text_fn=lambda *_a: (_ for _ in ()).throw(RuntimeError("explode")),
    )
    assert ok is False
    assert count == 0
    assert "Exception generating overview thumbnails" in msg

    ok, msg = overview.create_overview_sprite(
        str(tmp_path),
        3,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        run_shell_bytes_fn=lambda *_a: (0, b""),
        try_sprite_imagemagick_append_fn=lambda **_k: (False, "im-failed\n"),
        get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: (_ for _ in ()).throw(
            ValueError("bad-dims")
        ),
    )
    assert ok is False
    assert "Error creating sprite sheet" in msg

    ok, msg = overview.create_overview_sprite(
        str(tmp_path),
        4,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        run_shell_bytes_fn=lambda *_a: (0, b""),
        try_sprite_imagemagick_append_fn=lambda **_k: (False, "im-failed\n"),
        get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: 2,
    )
    assert ok is False
    assert "exceed single-row capacity" in msg

    ok, msg = overview.create_overview_sprite(
        str(tmp_path),
        2,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        run_shell_bytes_fn=lambda *_a: (5, b"\xff"),
        try_sprite_imagemagick_append_fn=lambda **_k: (True, "im-ok\n"),
        get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: 10,
    )
    assert ok is True
    assert "im-ok" in msg

    ok, msg = overview.create_overview_sprite(
        str(tmp_path),
        2,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        run_shell_bytes_fn=lambda *_a: (5, b"decode-me"),
        try_sprite_imagemagick_append_fn=lambda **_k: (False, "im-failed\n"),
        get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: 10,
    )
    assert ok is False
    assert "im-failed" in msg

    ok, msg = overview.create_overview_sprite(
        str(tmp_path),
        2,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        run_shell_bytes_fn=lambda *_a: (_ for _ in ()).throw(RuntimeError("boom")),
        try_sprite_imagemagick_append_fn=lambda **_k: (False, ""),
        get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: 10,
    )
    assert ok is False
    assert "Exception creating sprite sheet" in msg

    (tmp_path / "overview_temp").mkdir(exist_ok=True)
    original_rmtree = shutil.rmtree

    def _failing_rmtree(*_a, **_k):
        raise OSError("cannot cleanup")

    monkeypatch.setattr(shutil, "rmtree", _failing_rmtree)
    try:
        ok, msg = overview.create_overview_sprite(
            str(tmp_path),
            1,
            overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
            run_shell_bytes_fn=lambda *_a: (0, b"ok"),
            try_sprite_imagemagick_append_fn=lambda **_k: (False, ""),
            get_overview_max_single_row_thumbnails_fn=lambda *_a, **_k: 10,
        )
    finally:
        monkeypatch.setattr(shutil, "rmtree", original_rmtree)
    assert ok is True
    assert "Sprite sheet created" in msg

    ok, msg = overview.generate_overview_vtt(
        str(tmp_path),
        10,
        0,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        format_vtt_timestamp_fn=overview.format_vtt_timestamp,
    )
    assert ok is False
    assert "no thumbnails" in msg

    ok, msg = overview.generate_overview_vtt(
        str(tmp_path),
        0,
        2,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        format_vtt_timestamp_fn=overview.format_vtt_timestamp,
    )
    assert ok is True
    content = (tmp_path / "overview.vtt").read_text(encoding="utf-8")
    assert "WEBVTT" in content

    missing_dir = tmp_path / "missing" / "subdir"
    ok, msg = overview.generate_overview_vtt(
        str(missing_dir),
        10,
        2,
        overview_config={"thumbnail_width": 160, "thumbnail_height": 90},
        format_vtt_timestamp_fn=overview.format_vtt_timestamp,
    )
    assert ok is False
    assert "Error creating VTT file" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        10,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": False},
        generate_overview_thumbnails_fn=lambda *_a: (True, "", 1),
        create_overview_sprite_fn=lambda *_a: (True, ""),
        generate_overview_vtt_fn=lambda *_a: (True, ""),
    )
    assert ok is True
    assert "disabled" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        0,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": True},
        generate_overview_thumbnails_fn=lambda *_a: (True, "", 1),
        create_overview_sprite_fn=lambda *_a: (True, ""),
        generate_overview_vtt_fn=lambda *_a: (True, ""),
    )
    assert ok is True
    assert "Video too short" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        10,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": True},
        generate_overview_thumbnails_fn=lambda *_a: (False, "thumb-failed\n", 0),
        create_overview_sprite_fn=lambda *_a: (True, ""),
        generate_overview_vtt_fn=lambda *_a: (True, ""),
    )
    assert ok is False
    assert "Failed to generate overview thumbnails" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        10,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": True},
        generate_overview_thumbnails_fn=lambda *_a: (True, "thumb-ok\n", 2),
        create_overview_sprite_fn=lambda *_a: (False, "sprite-failed\n"),
        generate_overview_vtt_fn=lambda *_a: (True, ""),
    )
    assert ok is False
    assert "Failed to create sprite sheet" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        10,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": True},
        generate_overview_thumbnails_fn=lambda *_a: (True, "thumb-ok\n", 2),
        create_overview_sprite_fn=lambda *_a: (True, "sprite-ok\n"),
        generate_overview_vtt_fn=lambda *_a: (False, "vtt-failed\n"),
    )
    assert ok is False
    assert "Failed to generate VTT file" in msg

    ok, msg = overview.generate_overview(
        "video.mp4",
        10,
        videos_output_dir=str(tmp_path),
        overview_config={"enabled": True},
        generate_overview_thumbnails_fn=lambda *_a: (True, "thumb-ok\n", 2),
        create_overview_sprite_fn=lambda *_a: (True, "sprite-ok\n"),
        generate_overview_vtt_fn=lambda *_a: (True, "vtt-ok\n"),
    )
    assert ok is True
    assert "Overview generation complete" in msg


def test_runtime_args_dressing_and_gap_repair_remaining_lines(monkeypatch):
    """Validate Runtime args dressing and gap repair remaining lines."""
    runtime_args = _load_encoding_core_module("runtime_args_utils")
    args = runtime_args.parse_args(
        [
            "--encoding-type",
            "CPU",
            "--base-dir",
            "/tmp/in",
            "--input-file",
            "in.mp4",
            "--work-dir",
            "out",
        ]
    )
    assert args.encoding_type == "CPU"
    assert args.input_file == "in.mp4"

    dressing = _load_encoding_core_module("dressing_runtime_utils")
    same_name, msg = dressing.apply_dressing_if_needed(
        "input.mp4",
        {},
        videos_dir="/tmp",
        sanitize_filename_fn=lambda s: s,
        apply_cut_for_dressing_fn=lambda *_a: (_a[0], ""),
        apply_watermark_for_dressing_fn=lambda *_a: (_a[0], ""),
        apply_credits_for_dressing_fn=lambda *_a: (_a[0], ""),
    )
    assert same_name == "input.mp4"
    assert "apply_dressing_if_needed" in msg

    same_name, msg = dressing.apply_dressing_if_needed(
        "input.mp4",
        {"watermark": "", "opening_credits_video": "", "ending_credits_video": ""},
        videos_dir="/tmp",
        sanitize_filename_fn=lambda s: s,
        apply_cut_for_dressing_fn=lambda *_a: (_a[0], ""),
        apply_watermark_for_dressing_fn=lambda *_a: (_a[0], ""),
        apply_credits_for_dressing_fn=lambda *_a: (_a[0], ""),
    )
    assert same_name == "input.mp4"
    assert "No dressing operations detected" in msg

    gap_runtime = _load_transcription_core_module("gap_repair_runtime_utils")
    expected = Path("/tmp/generated.vtt")
    monkeypatch.setattr(
        gap_runtime.output_validation_flow_utils,
        "find_generated_vtt",
        lambda *_a, **_k: expected,
    )
    found = gap_runtime._find_generated_vtt(Path("/tmp/audio.mp3"), Path("/tmp"))
    assert found == expected
