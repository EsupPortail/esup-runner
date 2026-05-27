"""Validates Studio legacy runtime API script helpers for fetch, parse, and time conversion."""

import argparse
import builtins
import importlib.util
import os
import socket
import sys
import types
import urllib.request
from pathlib import Path

import pytest


def _load_studio_script_module():
    """Load the legacy studio runtime API script as a Python module."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "tests" / "studio_legacy_runtime_api.py"
    spec = importlib.util.spec_from_file_location("studio_script_noexclude", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_studio_script_module_forced_fallback_imports(monkeypatch):
    """Load legacy_runtime_api.py while forcing the app.* import branch to fail."""
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "tests" / "studio_legacy_runtime_api.py"
    spec = importlib.util.spec_from_file_location("studio_script_noexclude_fallback", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")

    original_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name.startswith("app.task_handlers.studio.core"):
            raise ModuleNotFoundError("forced fallback import branch")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _guarded_import)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_args(**overrides):
    """Build a default argparse namespace for Studio runtime test scenarios."""
    defaults = {
        "xml_url": "https://example.org/mediapackage.xml",
        "base_dir": "/tmp",
        "work_dir": "work",
        "output_file": "studio.mp4",
        "debug": "false",
        "presenter": None,
        "encoding_type": "CPU",
        "hwaccel_device": "0",
        "cuda_visible_devices": None,
        "cuda_device_order": None,
        "cuda_path": None,
        "force_cpu": "false",
        "studio_crf": None,
        "studio_preset": None,
        "studio_audio_bitrate": None,
        "studio_allow_nvenc": "false",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_studio_fetch_parse_and_time_helpers(monkeypatch):
    """Validate Studio fetch parse and time helpers."""
    studio = _load_studio_script_module()

    class _Response:
        text = "payload"

        @staticmethod
        def raise_for_status():
            return None

    fake_requests = types.SimpleNamespace(get=lambda *_a, **_k: _Response())
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    assert studio.fetch_text("https://example.org/file") == "payload"

    xml_text = """
    <mediapackage xmlns="http://mediapackage.opencastproject.org" presenter="piph">
      <media>
        <track type="presentation/source"><url>https://example.org/pres.mp4</url></track>
        <track type="presenter/source"><url>https://example.org/pers.mp4</url></track>
      </media>
      <metadata>
        <catalog type="smil/cutting"><url>https://example.org/cut.smil</url></catalog>
      </metadata>
    </mediapackage>
    """
    pres_url, pers_url, presenter_layout, smil_url = studio.parse_mediapackage(xml_text)
    assert pres_url == "https://example.org/pres.mp4"
    assert pers_url == "https://example.org/pers.mp4"
    assert presenter_layout == "piph"
    assert smil_url == "https://example.org/cut.smil"

    assert studio.parse_smil_cut(
        "<smil><body><video clipBegin='1.0s' clipEnd='3.5s'/></body></smil>"
    ) == (
        1.0,
        3.5,
    )
    assert studio.parse_smil_cut("<smil><body><text/></body></smil>") == (None, None)
    assert studio.parse_smil_cut("<smil>") == (None, None)

    assert studio.parse_time(None) is None
    assert studio.parse_time("   ") is None
    assert studio.parse_time("4.25s") == pytest.approx(4.25)
    assert studio.parse_time("oopss") is None
    assert studio.parse_time("00:01:02") == pytest.approx(62.0)
    assert studio.parse_time("n/a") is None

    assert studio._first_token(None, "x") == "x"
    assert studio._first_token("  abc def ", "x") == "abc"
    assert studio._looks_like_webm_source("https://cdn/video.webm") is True
    assert studio._looks_like_webm_source("/tmp/video.mp4") is False


def test_studio_download_and_materialize_helpers(monkeypatch, tmp_path):
    """Validate Studio download and materialize helpers."""
    studio = _load_studio_script_module()

    monkeypatch.setenv("DOWNLOAD_ALLOWED_HOSTS", " example.org , test.local. ")
    assert studio._download_allowed_hosts_from_env() == ["example.org", "test.local"]

    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "no")
    assert studio._download_allow_private_networks_from_env() is False
    monkeypatch.setenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "yes")
    assert studio._download_allow_private_networks_from_env() is True

    assert studio._host_is_allowed("a.example.org", ["example.org"]) is True
    assert studio._host_is_allowed("evil.org", ["example.org"]) is False

    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [])
    ok, reason = studio._host_resolves_to_public_ip("example.org")
    assert ok is False
    assert "cannot be resolved" in reason

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("dns error")),
    )
    ok, reason = studio._host_resolves_to_public_ip("example.org")
    assert ok is False
    assert "cannot be resolved" in reason

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    ok, reason = studio._host_resolves_to_public_ip("example.org")
    assert ok is False
    assert "private address" in reason

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("not-an-ip", 0))],
    )
    ok, reason = studio._host_resolves_to_public_ip("example.org")
    assert ok is False
    assert "invalid address" in reason

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_k: [(None, None, None, None, ("8.8.8.8", 0))],
    )
    ok, reason = studio._host_resolves_to_public_ip("example.org")
    assert ok is True
    assert reason == ""

    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"ok")
    parsed = studio.urllib.parse.urlparse("https://example.org/existing.mp4")
    assert studio._download_http_source(
        "https://example.org/existing.mp4", str(tmp_path), "pres", parsed
    ) == str(existing)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return b"binary-data"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse())
    downloaded = studio._download_http_source(
        "https://example.org/new.mp4",
        str(tmp_path),
        "presentation",
        studio.urllib.parse.urlparse("https://example.org/new.mp4"),
    )
    assert downloaded.endswith("new.mp4")
    assert Path(downloaded).exists()

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("network error")),
    )
    fallback = studio._download_http_source(
        "https://example.org/fail.mp4",
        str(tmp_path),
        "presentation",
        studio.urllib.parse.urlparse("https://example.org/fail.mp4"),
    )
    assert fallback == "https://example.org/fail.mp4"

    assert studio._materialize_source(None, str(tmp_path), "presentation") is None
    assert (
        studio._materialize_source("ftp://example.org/file.mp4", str(tmp_path), "presentation")
        is None
    )
    assert (
        studio._materialize_source("/tmp/local.mp4", str(tmp_path), "presentation")
        == "/tmp/local.mp4"
    )
    assert studio._materialize_source("http:///bad", str(tmp_path), "presentation") is None

    monkeypatch.setattr(studio, "_download_allowed_hosts_from_env", lambda: ["allowed.org"])
    monkeypatch.setattr(studio, "_download_allow_private_networks_from_env", lambda: True)
    assert (
        studio._materialize_source("https://blocked.org/file.mp4", str(tmp_path), "presentation")
        is None
    )

    monkeypatch.setattr(studio, "_download_allowed_hosts_from_env", lambda: [])
    monkeypatch.setattr(studio, "_download_allow_private_networks_from_env", lambda: False)
    assert (
        studio._materialize_source("https://localhost/file.mp4", str(tmp_path), "presentation")
        is None
    )

    monkeypatch.setattr(studio, "_host_resolves_to_public_ip", lambda _host: (False, "private"))
    assert (
        studio._materialize_source("https://example.org/file.mp4", str(tmp_path), "presentation")
        is None
    )

    monkeypatch.setattr(studio, "_host_resolves_to_public_ip", lambda _host: (True, ""))
    monkeypatch.setattr(studio, "_download_http_source", lambda *args, **_k: "/tmp/downloaded.mp4")
    assert (
        studio._materialize_source("https://example.org/file.mp4", str(tmp_path), "presentation")
        == "/tmp/downloaded.mp4"
    )


def test_studio_codec_probe_filter_and_core_builders(monkeypatch):
    """Validate Studio codec probe filter and core builders."""
    studio = _load_studio_script_module()

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=" V..... libx264\n"),
    )
    assert studio._has_encoder("libx264") is True

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    assert studio._has_encoder("libx264") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=" V..... h264\n"),
    )
    assert studio._has_encoder("libx264") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("encoder boom")),
    )
    assert studio._has_encoder("libx264") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    assert studio._has_decoder("h264_cuvid") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=" V..... h264_cuvid\n"),
    )
    assert studio._has_decoder("h264_cuvid") is True

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=" V..... other_decoder\n"),
    )
    assert studio._has_decoder("h264_cuvid") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert studio._has_decoder("h264_cuvid") is False

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(
            returncode=0, stdout=b'{"streams":[{"codec_name":"h264"}]}'
        ),
    )
    assert studio.probe_codec("video.mp4") == "h264"

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=b"{bad-json"),
    )
    assert studio.probe_codec("video.mp4") == ""

    monkeypatch.setattr(studio, "probe_codec", lambda _src: "vp9")
    assert studio._is_webm_input_source("/tmp/file.unknown") is True
    assert studio._is_webm_input_source(None) is False

    monkeypatch.setattr(studio, "probe_codec", lambda _src: "h264")
    monkeypatch.setattr(studio, "_has_decoder", lambda name: name == "h264_cuvid")
    assert studio._choose_cuda_decoder_for("video.mp4") == "h264_cuvid"

    monkeypatch.setattr(studio, "_has_decoder", lambda _name: False)
    assert studio._choose_cuda_decoder_for("video.mp4") is None

    monkeypatch.setattr(studio, "probe_codec", lambda _src: "vp9")
    assert studio._choose_cuda_decoder_for("video.webm") is None

    monkeypatch.setattr(studio, "_has_encoder", lambda name: name == "libx264")
    assert studio._choose_h264_encoder() == ("libx264", "")
    monkeypatch.setattr(studio, "_has_encoder", lambda _name: False)
    assert studio._choose_h264_encoder()[0] == "h264"

    studio._nvenc_preflight.cache_clear()
    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=""),
    )
    assert studio._nvenc_preflight() == (True, "")

    studio._nvenc_preflight.cache_clear()
    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=4, stdout="nvenc err"),
    )
    ok, details = studio._nvenc_preflight()
    assert ok is False
    assert "nvenc err" in details

    studio._nvenc_preflight.cache_clear()
    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError("ffmpeg")),
    )
    ok, details = studio._nvenc_preflight()
    assert ok is False
    assert "command not found" in details

    studio._nvenc_preflight.cache_clear()
    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("nvenc boom")),
    )
    ok, details = studio._nvenc_preflight()
    assert ok is False
    assert "exception" in details

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(
            returncode=0, stdout=b'{"streams":[{"height":721}]}'
        ),
    )
    assert studio.probe_height("video.mp4") == 721

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(returncode=0, stdout=b"{bad-json"),
    )
    assert studio.probe_height("video.mp4") == 0

    assert "overlay" in studio.build_filter(1080, 720, "piph")
    assert "hstack" in studio.build_filter(1080, 720, "mid")
    assert studio.build_filter(0, 0, "unknown") == " "

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: types.SimpleNamespace(stdout=b"scale_cuda\noverlay_cuda"),
    )
    assert studio.filter_available("scale_cuda") is True

    monkeypatch.setattr(
        studio.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("filters boom")),
    )
    assert studio.filter_available("scale_cuda") is False


def test_studio_pipeline_builders_and_run_flow(monkeypatch, tmp_path):
    """Validate Studio pipeline builders and run flow."""
    studio = _load_studio_script_module()
    real_build_full_gpu_pipeline = studio._build_full_gpu_pipeline
    real_build_gpu_encode_only_pipeline = studio._build_gpu_encode_only_pipeline
    real_build_cpu_pipeline = studio._build_cpu_pipeline

    args = _make_args(encoding_type="GPU", force_cpu="false", hwaccel_device="2")

    studio._set_cuda_env(
        _make_args(cuda_visible_devices="0", cuda_device_order="PCI_BUS_ID", cuda_path="/cuda")
    )
    assert os.environ.get("CUDA_VISIBLE_DEVICES") == "0"
    assert os.environ.get("CUDA_DEVICE_ORDER") == "PCI_BUS_ID"
    assert os.environ.get("CUDA_PATH") == "/cuda"

    monkeypatch.setattr(studio, "probe_height", lambda src: 1080 if "pres" in src else 720)
    input_args, pres_h, pers_h = studio.build_input_args("pres.mp4", "pers.mp4", args)
    assert '"pres.mp4"' in input_args and '"pers.mp4"' in input_args
    assert (pres_h, pers_h) == (1080, 720)
    assert studio.build_input_args("pres.mp4", None, args)[1] == 1080
    assert studio.build_input_args(None, "pers.mp4", args)[2] == 720
    with pytest.raises(ValueError):
        studio.build_input_args(None, None, args)

    assert studio.build_subtime(1.0, 4.0).startswith("-ss 1.000 -t")
    assert studio.build_subtime(1.0, None) == "-ss 1.000 "
    assert studio.build_subtime(None, 2.0) == "-to 2.000 "
    assert studio.build_subtime(None, None) == ""

    monkeypatch.setattr(studio, "_choose_h264_encoder", lambda: ("h264", "warn"))
    monkeypatch.setattr(studio, "_is_webm_input_source", lambda _src: False)
    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(studio, "_build_gpu_encode_only_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_cpu_pipeline",
        lambda *_a, **_k: ('-i "a" ', "cpu-sub", "", "-map 0:v -map 0:a? "),
    )
    subcmd, video_codec, input_args2, cpu_encoder, map_opts = studio.build_pipeline(
        "pres.mp4", "pers.mp4", 1080, 720, "mid", args, '-i "x" '
    )
    assert subcmd == "cpu-sub"
    assert input_args2 == '-i "a" '
    assert cpu_encoder == "h264"
    assert map_opts == "-map 0:v -map 0:a? "

    monkeypatch.setattr(
        studio,
        "_build_full_gpu_pipeline",
        lambda *_a, **_k: ('-i "gpu" ', "gpu-sub", "-c:v h264_nvenc ", '-map "[vout]" -map 0:a? '),
    )
    subcmd, _, input_args2, _, _ = studio.build_pipeline(
        "pres.mp4", "pers.mp4", 1080, 720, "mid", args, ""
    )
    assert subcmd == "gpu-sub"
    assert input_args2 == '-i "gpu" '

    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_gpu_encode_only_pipeline",
        lambda *_a, **_k: (
            '-i "gpu-enc" ',
            "gpu-enc-sub",
            "-c:v h264_nvenc ",
            "-map 0:v -map 0:a? ",
        ),
    )
    subcmd, _, input_args2, _, _ = studio.build_pipeline(
        "pres.mp4", "pers.mp4", 1080, 720, "mid", args, ""
    )
    assert subcmd == "gpu-enc-sub"
    assert input_args2 == '-i "gpu-enc" '

    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", real_build_full_gpu_pipeline)
    monkeypatch.setattr(
        studio, "_build_gpu_encode_only_pipeline", real_build_gpu_encode_only_pipeline
    )
    monkeypatch.setattr(studio, "_build_cpu_pipeline", real_build_cpu_pipeline)

    # Real CPU builder coverage for warning and non-libx264 mixed-path branch.
    monkeypatch.setattr(studio, "_choose_h264_encoder", lambda: ("h264", "warn"))
    input_args_cpu, subcmd_cpu, video_codec_cpu, map_opts_cpu = studio._build_cpu_pipeline(
        "pres.mp4",
        "pers.mp4",
        1080,
        720,
        "mid",
        _make_args(),
    )
    assert '"pres.mp4"' in input_args_cpu and '"pers.mp4"' in input_args_cpu
    assert "hstack" in subcmd_cpu
    assert "-q:v 23" in video_codec_cpu
    assert map_opts_cpu == '-map "[vout]" -map 0:a? '

    assert studio._is_gpu_requested(_make_args(encoding_type="CPU")) is False
    assert studio._is_gpu_requested(_make_args(encoding_type="GPU", force_cpu="true")) is False
    assert studio._is_gpu_requested(_make_args(encoding_type="GPU", force_cpu="false")) is True

    assert studio._prepare_full_gpu_inputs(None, "pers.mp4", 720, "mid", args) is None

    assert "-rc cbr -cbr 1" in studio._build_nvenc_video_codec(_make_args(), webm_input=True)
    assert studio._even_or_default_height(721, 720) == 722

    monkeypatch.setattr(studio, "_is_gpu_requested", lambda _args: False)
    assert studio._prepare_full_gpu_inputs("pres.mp4", "pers.mp4", 720, "mid", args) is None

    monkeypatch.setattr(studio, "_is_gpu_requested", lambda _args: True)
    monkeypatch.setattr(studio, "_nvenc_preflight", lambda: (False, "nvenc missing"))
    assert studio._prepare_full_gpu_inputs("pres.mp4", "pers.mp4", 720, "mid", args) is None

    monkeypatch.setattr(studio, "_nvenc_preflight", lambda: (True, ""))
    monkeypatch.setattr(studio, "_choose_cuda_decoder_for", lambda _src: None)
    assert studio._prepare_full_gpu_inputs("pres.mp4", "pers.mp4", 720, "mid", args) is None

    monkeypatch.setattr(studio, "_choose_cuda_decoder_for", lambda _src: "h264_cuvid")
    monkeypatch.setattr(studio, "filter_available", lambda name: True)
    prepared = studio._prepare_full_gpu_inputs("pres.mp4", "pers.mp4", 721, "pipb", args)
    assert prepared is not None
    input_args_gpu, height, pip_h, overlay_pos = prepared
    assert "hwaccel cuda" in input_args_gpu
    assert height % 2 == 0
    assert pip_h % 2 == 0
    assert overlay_pos == "W-w-10:H-h-10"

    assert "hstack" in studio._build_full_gpu_filtergraph(
        presenter_layout="mid",
        height=720,
        pip_h=180,
        overlay_pos="W-w-10:10",
    )
    assert "overlay_cuda" in studio._build_full_gpu_filtergraph(
        presenter_layout="piph",
        height=720,
        pip_h=180,
        overlay_pos="W-w-10:10",
    )

    monkeypatch.setattr(studio, "_prepare_full_gpu_inputs", lambda **_k: None)
    assert (
        studio._build_full_gpu_pipeline(
            "pres.mp4", "pers.mp4", 720, 720, "mid", args, webm_input=False
        )
        is None
    )

    monkeypatch.setattr(
        studio,
        "_prepare_full_gpu_inputs",
        lambda **_k: ('-i "pres" -i "pers" ', 720, 180, "W-w-10:10"),
    )
    full_gpu = studio._build_full_gpu_pipeline(
        "pres.mp4", "pers.mp4", 720, 720, "piph", args, webm_input=True
    )
    assert full_gpu is not None
    assert full_gpu[3] == '-map "[vout]" -map 0:a? '

    monkeypatch.setattr(studio, "_is_gpu_requested", lambda _args: False)
    assert (
        studio._build_gpu_encode_only_pipeline(
            "pres.mp4", "pers.mp4", 720, 720, "mid", args, webm_input=False
        )
        is None
    )

    monkeypatch.setattr(studio, "_is_gpu_requested", lambda _args: True)
    monkeypatch.setattr(studio, "_nvenc_preflight", lambda: (False, "no nvenc"))
    assert (
        studio._build_gpu_encode_only_pipeline(
            "pres.mp4", "pers.mp4", 720, 720, "mid", args, webm_input=False
        )
        is None
    )

    monkeypatch.setattr(studio, "_nvenc_preflight", lambda: (True, ""))
    monkeypatch.setattr(studio, "build_filter", lambda *_a, **_k: " -filter_complex mixed ")

    gpu_enc = studio._build_gpu_encode_only_pipeline(
        "pres.mp4", "pers.mp4", 720, 720, "mid", args, webm_input=False
    )
    assert gpu_enc is not None
    assert gpu_enc[3] == '-map "[vout]" -map 0:a? '

    gpu_enc = studio._build_gpu_encode_only_pipeline(
        "pres.mp4", "pers.mp4", 720, 0, "mid", args, webm_input=False
    )
    assert gpu_enc[0] == '-i "pres.mp4" '

    gpu_enc = studio._build_gpu_encode_only_pipeline(
        "pres.mp4", "pers.mp4", 0, 720, "mid", args, webm_input=False
    )
    assert gpu_enc[0] == '-i "pers.mp4" '

    gpu_enc = studio._build_gpu_encode_only_pipeline(
        "pres.mp4", "pers.mp4", 0, 0, "mid", args, webm_input=False
    )
    assert gpu_enc[0] == '-i "pres.mp4" '

    assert (
        studio._build_gpu_encode_only_pipeline(
            "pres.mp4", None, 720, 0, "mid", args, webm_input=False
        )
        is not None
    )
    assert (
        studio._build_gpu_encode_only_pipeline(
            None, "pers.mp4", 0, 720, "mid", args, webm_input=False
        )
        is not None
    )
    assert (
        studio._build_gpu_encode_only_pipeline(None, None, 0, 0, "mid", args, webm_input=False)
        is None
    )


def test_studio_parser_clip_times_run_pipelines_and_main(monkeypatch, tmp_path):
    """Validate Studio parser clip times run pipelines and main."""
    studio = _load_studio_script_module()

    parser = studio._build_arg_parser()
    parsed = parser.parse_args(
        [
            "--xml-url",
            "https://example.org/mp.xml",
            "--base-dir",
            str(tmp_path),
            "--work-dir",
            "work",
            "--output-file",
            "studio.mp4",
        ]
    )
    assert parsed.xml_url == "https://example.org/mp.xml"

    monkeypatch.setattr(studio, "fetch_text", lambda _url: "<xml />")
    monkeypatch.setattr(
        studio,
        "parse_mediapackage",
        lambda _xml: ("pres.mp4", "pers.mp4", "mid", "cut.smil"),
    )
    pres_url, pers_url, layout, smil_url = studio._load_mediapackage_and_layout(
        _make_args(presenter="pipb")
    )
    assert (pres_url, pers_url, layout, smil_url) == ("pres.mp4", "pers.mp4", "pipb", "cut.smil")

    assert studio._load_clip_times(None) == (None, None)
    monkeypatch.setattr(
        studio, "fetch_text", lambda _url: (_ for _ in ()).throw(RuntimeError("smil fail"))
    )
    assert studio._load_clip_times("https://example.org/cut.smil") == (None, None)

    monkeypatch.setattr(studio, "fetch_text", lambda _url: "<smil/>")
    monkeypatch.setattr(studio, "parse_smil_cut", lambda _text: (1.0, 2.0))
    assert studio._load_clip_times("https://example.org/cut.smil") == (1.0, 2.0)

    args = _make_args(encoding_type="GPU", studio_allow_nvenc="false")

    monkeypatch.setattr(
        studio,
        "_build_full_gpu_pipeline",
        lambda *_a, **_k: ('-i "in" ', ' -vf "x" ', "-c:v libx264 ", "-map 0:v -map 0:a? "),
    )
    monkeypatch.setattr(studio, "_build_gpu_encode_only_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_cpu_pipeline",
        lambda *_a, **_k: ('-i "in" ', ' -vf "cpu" ', "-c:v h264 ", "-map 0:v -map 0:a? "),
    )

    call_codes = [1, 0]

    def _run_with_fallback(_cmd):
        return types.SimpleNamespace(returncode=call_codes.pop(0))

    monkeypatch.setattr(studio.subprocess, "run", _run_with_fallback)
    rc = studio._run_pipelines(
        pres_url_local="pres.mp4",
        pers_url_local="pers.mp4",
        pres_h=720,
        pers_h=720,
        presenter_layout="mid",
        args=args,
        studio_allow_nvenc=False,
        webm_input=False,
        subtime="",
        audio_bitrate="192k",
        output_opts="",
        output_path=str(tmp_path / "studio.mp4"),
    )
    assert rc == 0
    assert args.force_cpu == "true"

    # FULL_GPU immediate success path.
    monkeypatch.setattr(
        studio,
        "_build_full_gpu_pipeline",
        lambda *_a, **_k: ('-i "in" ', ' -vf "x" ', "-c:v libx264 ", "-map 0:v -map 0:a? "),
    )
    monkeypatch.setattr(studio.subprocess, "run", lambda _cmd: types.SimpleNamespace(returncode=0))
    rc = studio._run_pipelines(
        pres_url_local="pres.mp4",
        pers_url_local="pers.mp4",
        pres_h=720,
        pers_h=720,
        presenter_layout="mid",
        args=_make_args(encoding_type="GPU", studio_allow_nvenc="true"),
        studio_allow_nvenc=True,
        webm_input=False,
        subtime="",
        audio_bitrate="192k",
        output_opts="",
        output_path=str(tmp_path / "studio.mp4"),
    )
    assert rc == 0

    # GPU_ENC_ONLY failure then CPU fallback path.
    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_gpu_encode_only_pipeline",
        lambda *_a, **_k: ('-i "gpu" ', ' -vf "gpu" ', "-c:v h264_nvenc ", "-map 0:v -map 0:a? "),
    )
    monkeypatch.setattr(
        studio,
        "_build_cpu_pipeline",
        lambda *_a, **_k: ('-i "cpu" ', ' -vf "cpu" ', "-c:v h264 ", "-map 0:v -map 0:a? "),
    )
    codes = [1, 0]
    monkeypatch.setattr(
        studio.subprocess, "run", lambda _cmd: types.SimpleNamespace(returncode=codes.pop(0))
    )
    rc = studio._run_pipelines(
        pres_url_local="pres.mp4",
        pers_url_local="pers.mp4",
        pres_h=720,
        pers_h=720,
        presenter_layout="mid",
        args=_make_args(encoding_type="GPU", studio_allow_nvenc="true"),
        studio_allow_nvenc=True,
        webm_input=False,
        subtime="",
        audio_bitrate="192k",
        output_opts="",
        output_path=str(tmp_path / "studio.mp4"),
    )
    assert rc == 0

    # GPU_ENC_ONLY immediate success path.
    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_gpu_encode_only_pipeline",
        lambda *_a, **_k: ('-i "gpu" ', ' -vf "gpu" ', "-c:v h264_nvenc ", "-map 0:v -map 0:a? "),
    )
    monkeypatch.setattr(studio.subprocess, "run", lambda _cmd: types.SimpleNamespace(returncode=0))
    rc = studio._run_pipelines(
        pres_url_local="pres.mp4",
        pers_url_local="pers.mp4",
        pres_h=720,
        pers_h=720,
        presenter_layout="mid",
        args=_make_args(encoding_type="GPU", studio_allow_nvenc="true"),
        studio_allow_nvenc=True,
        webm_input=False,
        subtime="",
        audio_bitrate="192k",
        output_opts="",
        output_path=str(tmp_path / "studio.mp4"),
    )
    assert rc == 0

    monkeypatch.setattr(studio, "_build_full_gpu_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(studio, "_build_gpu_encode_only_pipeline", lambda *_a, **_k: None)
    monkeypatch.setattr(
        studio,
        "_build_cpu_pipeline",
        lambda *_a, **_k: ('-i "in" ', ' -vf "x" -c:v libx264 ', "", "-map 0:v -map 0:a? "),
    )
    monkeypatch.setattr(studio.subprocess, "run", lambda _cmd: types.SimpleNamespace(returncode=0))
    rc = studio._run_pipelines(
        pres_url_local="pres.mp4",
        pers_url_local="pers.mp4",
        pres_h=720,
        pers_h=720,
        presenter_layout="mid",
        args=_make_args(encoding_type="CPU"),
        studio_allow_nvenc=True,
        webm_input=False,
        subtime="",
        audio_bitrate="192k",
        output_opts="",
        output_path=str(tmp_path / "studio.mp4"),
    )
    assert rc == 0

    common_args = _make_args(base_dir=str(tmp_path), work_dir="work", output_file="studio.mp4")
    monkeypatch.setattr(
        studio,
        "_load_mediapackage_and_layout",
        lambda _args: ("pres.mp4", "pers.mp4", "mid", "cut.smil"),
    )
    monkeypatch.setattr(studio, "_load_clip_times", lambda _smil: (1.0, 3.0))
    monkeypatch.setattr(studio, "_materialize_source", lambda url, *_a: url)
    monkeypatch.setattr(studio, "_is_webm_input_source", lambda _src: True)
    monkeypatch.setattr(
        studio, "build_input_args", lambda *_a: (_ for _ in ()).throw(ValueError("no tracks"))
    )
    assert studio._main_impl(common_args) == 1

    monkeypatch.setattr(studio, "build_input_args", lambda *_a: ('-i "x" ', 720, 720))
    monkeypatch.setattr(studio, "_run_pipelines", lambda **_k: 0)
    assert studio._main_impl(common_args) == 0

    class _Parser:
        @staticmethod
        def parse_args():
            return common_args

    monkeypatch.setattr(studio, "_build_arg_parser", lambda: _Parser())
    monkeypatch.setattr(studio, "_main_impl", lambda _args: 3)
    with pytest.raises(SystemExit) as exc:
        studio.main()
    assert exc.value.code == 3


def test_studio_legacy_runtime_import_fallback_branch(monkeypatch):
    """Validate studio legacy runtime import fallback branch."""
    studio = _load_studio_script_module_forced_fallback_imports(monkeypatch)
    assert studio.MAX_SMIL_TIME_SECONDS > 0
    parser = studio._build_arg_parser()
    assert parser is not None
