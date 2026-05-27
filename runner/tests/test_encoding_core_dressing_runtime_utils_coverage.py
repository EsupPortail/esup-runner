"""Validates asset URL validation, host allowlist checking, and IP resolution validation."""

import importlib
from pathlib import Path

import pytest


def _load_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def test_dressing_filename_and_env_helpers(monkeypatch):
    """Validate Dressing filename and env helpers."""
    dressing = _load_core_module("dressing_runtime_utils")

    assert (
        dressing.safe_filename_from_url(
            "https://example.org/path/logo.png",
            sanitize_filename_fn=lambda name: name,
        )
        == "logo.png"
    )
    assert (
        dressing.safe_filename_from_url(
            "https://example.org/path/",
            sanitize_filename_fn=lambda _name: "",
        )
        == "asset"
    )

    monkeypatch.setenv("DOWNLOAD_ALLOWED_HOSTS", "Example.org., sub.Example.net ")
    assert dressing.download_allowed_hosts_from_env() == ["example.org", "sub.example.net"]

    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "off")
    assert dressing.download_allow_private_networks_from_env() is False

    assert dressing.host_is_allowed("video.example.org", ["example.org"]) is True
    assert dressing.host_is_allowed("other.invalid", ["example.org"]) is False


def test_dressing_validate_host_resolves_to_public_ip(monkeypatch):
    """Validate Dressing validate host resolves to public ip."""
    dressing = _load_core_module("dressing_runtime_utils")

    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dns"))
    )
    with pytest.raises(ValueError, match="cannot be resolved"):
        dressing.validate_host_resolves_to_public_ip("example.org")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("not-an-ip", 0))],
    )
    with pytest.raises(ValueError, match="invalid address"):
        dressing.validate_host_resolves_to_public_ip("example.org")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    with pytest.raises(ValueError, match="private/loopback"):
        dressing.validate_host_resolves_to_public_ip("example.org")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 0))],
    )
    dressing.validate_host_resolves_to_public_ip("example.org")


def test_download_url_to_dir_validations_and_success(monkeypatch, tmp_path):
    """Validate Download url to dir validations and success."""
    dressing = _load_core_module("dressing_runtime_utils")

    with pytest.raises(ValueError, match="Only http/https"):
        dressing.download_url_to_dir(
            "ftp://example.org/file.png",
            str(tmp_path),
            "wm",
            sanitize_filename_fn=lambda name: name,
        )

    with pytest.raises(ValueError, match="Invalid download URL host"):
        dressing.download_url_to_dir(
            "http:///file.png",
            str(tmp_path),
            "wm",
            sanitize_filename_fn=lambda name: name,
        )

    monkeypatch.setattr(dressing, "download_allowed_hosts_from_env", lambda: ["allowed.example"])
    monkeypatch.setattr(dressing, "host_is_allowed", lambda *_a, **_k: False)
    with pytest.raises(ValueError, match="host not allowed"):
        dressing.download_url_to_dir(
            "https://blocked.example/file.png",
            str(tmp_path),
            "wm",
            sanitize_filename_fn=lambda name: name,
        )

    monkeypatch.setattr(dressing, "download_allowed_hosts_from_env", lambda: [])
    monkeypatch.setattr(dressing, "download_allow_private_networks_from_env", lambda: False)
    with pytest.raises(ValueError, match="host not allowed"):
        dressing.download_url_to_dir(
            "https://localhost/file.png",
            str(tmp_path),
            "wm",
            sanitize_filename_fn=lambda name: name,
        )

    validate_calls = []

    def _validate(host: str) -> None:
        validate_calls.append(host)

    urlopen_calls = []

    class _Resp:
        def __init__(self, payload: bytes):
            self.payload = payload

        def read(self) -> bytes:
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def _urlopen(_req, timeout=0):
        assert timeout == 30
        urlopen_calls.append("called")
        return _Resp(b"binary-data")

    monkeypatch.setattr(dressing, "validate_host_resolves_to_public_ip", _validate)
    monkeypatch.setattr(dressing.urllib.request, "urlopen", _urlopen)

    local_path = dressing.download_url_to_dir(
        "https://example.org/assets/logo final.png",
        str(tmp_path),
        "watermark",
        sanitize_filename_fn=lambda name: name.replace(" ", "_"),
    )
    assert Path(local_path).exists()
    assert Path(local_path).read_bytes() == b"binary-data"
    assert validate_calls == ["example.org"]
    assert len(urlopen_calls) == 1

    cached_path = dressing.download_url_to_dir(
        "https://example.org/assets/logo final.png",
        str(tmp_path),
        "watermark",
        sanitize_filename_fn=lambda name: name.replace(" ", "_"),
    )
    assert cached_path == local_path
    assert len(urlopen_calls) == 1


def test_dressing_probe_and_command_helpers(monkeypatch):
    """Validate Dressing probe and command helpers."""
    dressing = _load_core_module("dressing_runtime_utils")

    monkeypatch.setattr(dressing.subprocess, "check_output", lambda *_a, **_k: b"12.5\n")
    assert dressing.probe_duration_seconds("movie.mp4") == 12.5

    monkeypatch.setattr(
        dressing.subprocess,
        "check_output",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ffprobe")),
    )
    assert dressing.probe_duration_seconds("movie.mp4") == 0.0

    monkeypatch.setattr(dressing.subprocess, "check_output", lambda *_a, **_k: b"0\n")
    assert dressing.probe_has_audio("movie.mp4") is True

    monkeypatch.setattr(
        dressing.subprocess,
        "check_output",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("ffprobe")),
    )
    assert dressing.probe_has_audio("movie.mp4") is False

    assert dressing.watermark_overlay_xy("top_left", 10) == ("10", "10")
    assert dressing.watermark_overlay_xy("top_right", 10) == ("main_w-overlay_w-10", "10")
    assert dressing.watermark_overlay_xy("bottom_left", 10) == ("10", "main_h-overlay_h-10")
    assert dressing.watermark_overlay_xy("bottom_right", 10) == (
        "main_w-overlay_w-10",
        "main_h-overlay_h-10",
    )
    assert dressing.watermark_overlay_xy("unknown", 10) == ("main_w-overlay_w-10", "10")

    filter_expr = dressing.build_normalize_1080p_filter("in", "out")
    assert "[in]" in filter_expr
    assert "[out]" in filter_expr

    logs = []
    assert (
        dressing.run_ffmpeg_cmd(
            "ffmpeg ...",
            "cut",
            launch_cmd_fn=lambda *_a, **_k: (1, "ffmpeg output"),
            encode_log_fn=logs.append,
        )
        is True
    )
    assert logs == ["ffmpeg output"]

    recorded_cut_cmd = {"cmd": ""}

    def _run_cut(cmd: str, kind: str) -> bool:
        recorded_cut_cmd["cmd"] = cmd
        assert kind == "cut_intermediate"
        return True

    assert (
        dressing.create_cut_intermediate(
            "in file.mp4",
            "out file.mp4",
            "00:00:01",
            "00:00:02",
            choose_h264_encoder_fn=lambda: ("libx264", ""),
            run_ffmpeg_cmd_fn=_run_cut,
        )
        is True
    )
    assert "-c:v libx264" in recorded_cut_cmd["cmd"]

    recorded_wm_cmd = {"cmd": ""}

    def _run_wm(cmd: str, kind: str) -> bool:
        recorded_wm_cmd["cmd"] = cmd
        assert kind == "dressing_watermark"
        return True

    assert (
        dressing.create_watermarked_intermediate(
            "input.mp4",
            "wm.png",
            "output.mp4",
            "top_right",
            "invalid-opacity",
            choose_h264_encoder_fn=lambda: ("h264", ""),
            watermark_overlay_xy_fn=lambda _p: ("x", "y"),
            build_normalize_1080p_filter_fn=lambda *_a, **_k: "[base]",
            run_ffmpeg_cmd_fn=_run_wm,
        )
        is True
    )
    assert "aa=1.000" in recorded_wm_cmd["cmd"]

    assert (
        dressing.parse_duration_seconds_fallback(
            None,
            timestamp_to_seconds_fn=lambda _s: 99,
        )
        == 0.0
    )
    assert (
        dressing.parse_duration_seconds_fallback(
            " ",
            timestamp_to_seconds_fn=lambda _s: 99,
        )
        == 0.0
    )
    assert (
        dressing.parse_duration_seconds_fallback(
            "12.25",
            timestamp_to_seconds_fn=lambda _s: 99,
        )
        == 12.25
    )
    assert (
        dressing.parse_duration_seconds_fallback(
            "00:00:05",
            timestamp_to_seconds_fn=lambda _s: 5,
        )
        == 5.0
    )


def test_dressing_concat_and_apply_helpers(tmp_path):
    """Validate Dressing concat and apply helpers."""
    dressing = _load_core_module("dressing_runtime_utils")

    main_path = str(tmp_path / "main.mp4")
    opening_path = str(tmp_path / "opening.mp4")
    ending_path = str(tmp_path / "ending.mp4")
    output_path = str(tmp_path / "out.mp4")

    recorded_concat_cmd = {"cmd": ""}

    def _probe_duration(path: str) -> float:
        if path == main_path:
            return 10.0
        return 0.0

    def _probe_has_audio(path: str) -> bool:
        return path == main_path

    def _run_concat(cmd: str, kind: str) -> bool:
        recorded_concat_cmd["cmd"] = cmd
        assert kind == "dressing_credits"
        return True

    assert (
        dressing.create_credits_concat_intermediate(
            main_path=main_path,
            opening_path=opening_path,
            opening_duration_hint="2.5",
            ending_path=ending_path,
            ending_duration_hint="3.5",
            output_path=output_path,
            choose_h264_encoder_fn=lambda: ("libx264", ""),
            probe_duration_seconds_fn=_probe_duration,
            probe_has_audio_fn=_probe_has_audio,
            parse_duration_seconds_fallback_fn=lambda hint: float(hint or 0),
            build_normalize_1080p_filter_fn=lambda src, dst: f"[{src}]scale[{dst}]",
            run_ffmpeg_cmd_fn=_run_concat,
        )
        is True
    )
    assert "concat=n=3:v=1:a=1:unsafe=1" in recorded_concat_cmd["cmd"]
    assert "anullsrc" in recorded_concat_cmd["cmd"]

    cut_path, cut_msg, subtime, effective_duration = dressing.apply_cut_for_dressing(
        current_main_path=main_path,
        base="video",
        has_opening=True,
        has_ending=False,
        cut_config={"start": "00:00:01", "end": "00:00:04"},
        subtime=" -ss 1 -to 4 ",
        effective_duration=3,
        videos_dir=str(tmp_path),
        create_cut_intermediate_fn=lambda *_a, **_k: False,
    )
    assert cut_path == main_path
    assert "Warning: cut intermediate failed" in cut_msg
    assert subtime == " -ss 1 -to 4 "
    assert effective_duration == 3

    _, cut_exc_msg, _, _ = dressing.apply_cut_for_dressing(
        current_main_path=main_path,
        base="video",
        has_opening=True,
        has_ending=False,
        cut_config={"start": "00:00:01", "end": "00:00:04"},
        subtime="x",
        effective_duration=1,
        videos_dir=str(tmp_path),
        create_cut_intermediate_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert "failed (boom)" in cut_exc_msg

    wm_path, wm_msg = dressing.apply_watermark_for_dressing(
        current_main_path=main_path,
        base="video",
        dressing_config={"watermark": "https://example.org/wm.png"},
        assets_dir=str(tmp_path / "assets"),
        videos_dir=str(tmp_path),
        download_url_to_dir_fn=lambda *_a, **_k: str(tmp_path / "wm.png"),
        create_watermarked_intermediate_fn=lambda *_a, **_k: False,
    )
    assert wm_path == main_path
    assert "Warning: watermark dressing failed" in wm_msg

    _, wm_exc_msg = dressing.apply_watermark_for_dressing(
        current_main_path=main_path,
        base="video",
        dressing_config={"watermark": "https://example.org/wm.png"},
        assets_dir=str(tmp_path / "assets"),
        videos_dir=str(tmp_path),
        download_url_to_dir_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("network")),
        create_watermarked_intermediate_fn=lambda *_a, **_k: True,
    )
    assert "failed (network)" in wm_exc_msg

    credits_path, credits_msg = dressing.apply_credits_for_dressing(
        current_main_path=main_path,
        base="video",
        dressing_config={"opening_credits_video": "https://example.org/opening.mp4"},
        assets_dir=str(tmp_path / "assets"),
        videos_dir=str(tmp_path),
        download_url_to_dir_fn=lambda *_a, **_k: opening_path,
        create_credits_concat_intermediate_fn=lambda **_k: False,
    )
    assert credits_path == main_path
    assert "Warning: credits dressing failed" in credits_msg

    _, credits_exc_msg = dressing.apply_credits_for_dressing(
        current_main_path=main_path,
        base="video",
        dressing_config={"opening_credits_video": "https://example.org/opening.mp4"},
        assets_dir=str(tmp_path / "assets"),
        videos_dir=str(tmp_path),
        download_url_to_dir_fn=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("network")),
        create_credits_concat_intermediate_fn=lambda **_k: True,
    )
    assert "failed (network)" in credits_exc_msg
