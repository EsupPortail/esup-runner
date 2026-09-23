"""Validate post-encode ffprobe checks for HLS and MP4 renditions."""

from __future__ import annotations

import json
import subprocess
import types

from app.task_handlers.encoding.core import ffmpeg_runtime_utils as ffmpeg_runtime
from app.task_handlers.encoding.core import runtime_flow_utils as runtime_flow


class _ProbeSubprocess:
    PIPE = object()
    response = types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
    error: BaseException | None = None

    @classmethod
    def run(cls, *_args, **_kwargs):
        if cls.error is not None:
            raise cls.error
        return cls.response


def _probe_payload(*, video: list[object], audio: list[object]) -> str:
    streams = [
        *({"codec_type": "video", "duration": duration} for duration in video),
        *({"codec_type": "audio", "duration": duration} for duration in audio),
    ]
    return json.dumps({"streams": streams})


def test_probe_stream_durations_parses_media_streams():
    _ProbeSubprocess.error = None
    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "duration": "100.25"},
                    {"codec_type": "audio", "duration": 99.75},
                    {"codec_type": "audio", "duration": "N/A"},
                    {"codec_type": "subtitle", "duration": "100"},
                    "invalid",
                ]
            }
        ),
        stderr="",
    )

    durations, error = ffmpeg_runtime.probe_stream_durations(
        "output.ts", subprocess_module=_ProbeSubprocess
    )

    assert error == ""
    assert durations == {"video": [100.25], "audio": [99.75]}
    assert ffmpeg_runtime._duration_seconds(None) == 0.0
    assert ffmpeg_runtime._duration_seconds(-1) == 0.0


def test_probe_stream_durations_reports_execution_and_json_errors():
    _ProbeSubprocess.error = None
    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=7,
        stdout="",
        stderr=b" first\nprobe error ",
    )
    durations, error = ffmpeg_runtime.probe_stream_durations(
        "output.ts", subprocess_module=_ProbeSubprocess
    )
    assert durations is None
    assert "code 7: first probe error" in error

    _ProbeSubprocess.response = types.SimpleNamespace(returncode=0, stdout="not-json", stderr="")
    durations, error = ffmpeg_runtime.probe_stream_durations(
        "output.ts", subprocess_module=_ProbeSubprocess
    )
    assert durations is None
    assert "invalid JSON" in error
    assert "output: not-json" in error

    _ProbeSubprocess.response = types.SimpleNamespace(returncode=0, stdout="[]", stderr="")
    durations, error = ffmpeg_runtime.probe_stream_durations(
        "output.ts", subprocess_module=_ProbeSubprocess
    )
    assert durations == {"video": [], "audio": []}
    assert error == ""


def test_probe_stream_durations_reports_subprocess_exceptions():
    exceptions = (
        (subprocess.TimeoutExpired(["ffprobe"], 120), "timed out"),
        (FileNotFoundError("missing"), "executable not found"),
        (OSError("denied"), "execution failed"),
        (RuntimeError("boom"), "unexpected ffprobe failure"),
    )
    for exception, expected_message in exceptions:
        _ProbeSubprocess.error = exception
        durations, error = ffmpeg_runtime.probe_stream_durations(
            "output.ts", subprocess_module=_ProbeSubprocess
        )
        assert durations is None
        assert expected_message in error
    _ProbeSubprocess.error = None


def test_validate_video_outputs_accepts_complete_hls_and_mp4(tmp_path):
    playlist = tmp_path / "360p_video.m3u8"
    transport_stream = tmp_path / "360p_video.ts"
    mp4 = tmp_path / "360p_video.mp4"
    for path in (playlist, transport_stream, mp4):
        path.write_bytes(b"encoded")

    _ProbeSubprocess.error = None
    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=0,
        stdout=_probe_payload(video=[100], audio=[99.5, 98]),
        stderr="",
    )
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(playlist), str(transport_stream)), (str(mp4), str(mp4))],
        expected_stream_durations={"video": [100], "audio": [100, 100]},
        subprocess_module=_ProbeSubprocess,
    )

    assert valid is True
    assert message.count("ffprobe output validation ok") == 2
    assert "audio=99.500s,98.000s" in message

    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=0,
        stdout=_probe_payload(video=[100], audio=[]),
        stderr="",
    )
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(mp4), str(mp4))],
        expected_stream_durations={"video": [100], "audio": []},
        subprocess_module=_ProbeSubprocess,
    )
    assert valid is True
    assert "video=100.000s" in message


def test_validate_video_outputs_rejects_truncated_or_missing_streams(tmp_path):
    output = tmp_path / "720p_video.mp4"
    output.write_bytes(b"encoded")

    _ProbeSubprocess.error = None
    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=0,
        stdout=_probe_payload(video=[120], audio=[53]),
        stderr="",
    )
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(output), str(output))],
        expected_stream_durations={"video": [120], "audio": [120]},
        subprocess_module=_ProbeSubprocess,
    )
    assert valid is False
    assert "audio stream 0 stops at 53.000s" in message
    assert "expected at least 115.000s" in message

    _ProbeSubprocess.response = types.SimpleNamespace(
        returncode=0,
        stdout=_probe_payload(video=[120], audio=[]),
        stderr="",
    )
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(output), str(output))],
        expected_stream_durations={"video": [120], "audio": [120]},
        subprocess_module=_ProbeSubprocess,
    )
    assert valid is False
    assert "expected 1 audio stream(s)" in message


def test_validate_video_outputs_rejects_missing_targets_and_probe_failures(tmp_path):
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [], expected_stream_durations={"video": [10], "audio": []}
    )
    assert valid is False
    assert "no video output was selected" in message

    valid, message = ffmpeg_runtime.validate_video_outputs(
        [], expected_stream_durations={"video": [], "audio": []}
    )
    assert valid is False
    assert "expected video duration is not positive" in message

    missing = tmp_path / "missing.m3u8"
    missing_ts = tmp_path / "missing.ts"
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(missing), str(missing_ts))],
        expected_stream_durations={"video": [10], "audio": []},
    )
    assert valid is False
    assert str(missing) in message
    assert str(missing_ts) in message

    output = tmp_path / "video.mp4"
    output.write_bytes(b"encoded")
    _ProbeSubprocess.error = None
    _ProbeSubprocess.response = types.SimpleNamespace(returncode=2, stdout="", stderr="bad")
    valid, message = ffmpeg_runtime.validate_video_outputs(
        [(str(output), str(output))],
        expected_stream_durations={"video": [10], "audio": []},
        subprocess_module=_ProbeSubprocess,
    )
    assert valid is False
    assert "ffprobe exited with code 2" in message


def test_nonempty_file_handles_filesystem_errors(monkeypatch):
    monkeypatch.setattr(
        ffmpeg_runtime.os.path,
        "isfile",
        lambda _path: (_ for _ in ()).throw(OSError("filesystem error")),
    )
    assert ffmpeg_runtime._is_nonempty_file("output.mp4") is False


def test_runtime_validation_builds_hls_and_mp4_targets(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(runtime_flow, "_VIDEOS_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(
        runtime_flow,
        "_RENDITION_CONFIG",
        {
            "360": {
                "resolution": "640x360",
                "video_bitrate": "750k",
                "audio_bitrate": "96k",
                "encode_mp4": True,
            }
        },
    )

    def _validate(outputs, **kwargs):
        captured["outputs"] = outputs
        captured.update(kwargs)
        return True, "validated\n"

    monkeypatch.setattr(runtime_flow.ffmpeg_runtime_utils, "validate_video_outputs", _validate)

    result = runtime_flow.validate_video_outputs(
        {
            "height": 720,
            "duration": 101,
            "effective_duration": 100,
            "video_duration": 99,
            "has_stream_audio": True,
            "audio_durations": [98, 75],
        },
        "My video.mp4",
    )

    assert result == (True, "validated\n")
    assert captured["outputs"] == [
        (
            str(tmp_path / "360p_My_video.m3u8"),
            str(tmp_path / "360p_My_video.ts"),
        ),
        (str(tmp_path / "360p_My_video.mp4"), str(tmp_path / "360p_My_video.mp4")),
    ]
    assert captured["expected_stream_durations"] == {
        "video": [99.0],
        "audio": [98.0, 75.0],
    }
    assert captured["subprocess_module"] is runtime_flow.subprocess

    runtime_flow.validate_video_outputs(
        {
            "height": 720,
            "duration": 100,
            "video_duration": 100,
            "has_stream_audio": True,
            "audio_durations": "invalid",
        },
        "My video.mp4",
    )
    assert captured["expected_stream_durations"] == {
        "video": [100.0],
        "audio": [100.0],
    }


def test_runtime_validation_accounts_for_cut_stream_overlap(monkeypatch):
    captured = {}
    monkeypatch.setattr(runtime_flow, "SUBTIME", " -ss 00:00:50 -to 00:01:10 ")
    monkeypatch.setattr(
        runtime_flow,
        "_CUT_CONFIG",
        {"start": "00:00:50", "end": "00:01:10"},
    )
    monkeypatch.setattr(runtime_flow, "_video_output_validation_targets", lambda *_a: [("a", "b")])

    def _validate(outputs, **kwargs):
        captured["outputs"] = outputs
        captured.update(kwargs)
        return True, "validated\n"

    monkeypatch.setattr(runtime_flow.ffmpeg_runtime_utils, "validate_video_outputs", _validate)

    result = runtime_flow.validate_video_outputs(
        {
            "duration": 20,
            "effective_duration": 20,
            "video_duration": 65,
            "has_stream_audio": True,
            "audio_durations": [120, 40],
        },
        "video.mp4",
    )

    assert result == (True, "validated\n")
    assert captured["expected_stream_durations"] == {
        "video": [15.0],
        "audio": [20.0],
    }
    assert runtime_flow._expected_output_stream_duration("invalid", 20) == 20
