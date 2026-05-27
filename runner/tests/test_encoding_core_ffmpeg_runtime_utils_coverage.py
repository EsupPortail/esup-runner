"""Validates ffmpeg command builders and runtime utility branches for encoding workflows."""

import importlib
import types


def _load_core_module(module_name: str):
    module = importlib.import_module(f"app.task_handlers.encoding.core.{module_name}")
    return importlib.reload(module)


def test_ffmpeg_command_utils_job_builders_cover_branches(tmp_path):
    """Validate Ffmpeg command utils job builders cover branches."""
    ffmpeg_cmd = _load_core_module("ffmpeg_command_utils")

    no_rendition = ffmpeg_cmd.build_encode_video_job(
        encoder_type="gpu",
        format="hls",
        codec="h264",
        height=720,
        file="input.mp4",
        filename="input",
        get_cmd_gpu_fn=lambda *_a: "GPU_CMD",
        get_cmd_cpu_fn=lambda *_a: "CPU_CMD",
        build_video_metadata_entries_fn=lambda **_k: [],
    )
    assert no_rendition[0] == ""
    assert no_rendition[4]["skip_execution"] is True

    with_renditions = ffmpeg_cmd.build_encode_video_job(
        encoder_type="cpu",
        format="hls",
        codec="h264",
        height=720,
        file="input.mp4",
        filename="input",
        get_cmd_gpu_fn=lambda *_a: "GPU_CMD",
        get_cmd_cpu_fn=lambda *_a: "CPU_CMD",
        build_video_metadata_entries_fn=lambda **_k: [
            {"filename": "a.ts"},
            {"filename": "b.ts"},
        ],
    )
    assert with_renditions[0] == "CPU_CMD"
    assert with_renditions[2]["filename"] == "a.ts"
    assert with_renditions[4]["additional_renditions"][0]["filename"] == "b.ts"

    mp3_job = ffmpeg_cmd.build_encode_audio_job(
        kind="mp3",
        file="input.mp4",
        filename="sample",
        videos_dir=str(tmp_path),
        videos_output_dir=str(tmp_path / "out"),
        mp3_template="ffmpeg -i {input} {output_dir}/audio_192k_{output}.mp3",
        m4a_template="ffmpeg -i {input} {output_dir}/audio_192k_{output}.m4a",
    )
    assert "audio_192k_sample.mp3" in mp3_job[0]
    assert mp3_job[2]["encoding_format"] == "audio/mp3"

    m4a_job = ffmpeg_cmd.build_encode_audio_job(
        kind="m4a",
        file="input.mp4",
        filename="sample",
        videos_dir=str(tmp_path),
        videos_output_dir=str(tmp_path / "out"),
        mp3_template="ffmpeg -i {input} {output_dir}/audio_192k_{output}.mp3",
        m4a_template="ffmpeg -i {input} {output_dir}/audio_192k_{output}.m4a",
    )
    assert "audio_192k_sample.m4a" in m4a_job[0]
    assert m4a_job[2]["encoding_format"] == "video/mp4"


def test_ffmpeg_runtime_utils_helpers_cover_branches():
    """Validate Ffmpeg runtime utils helpers cover branches."""
    ffmpeg_runtime = _load_core_module("ffmpeg_runtime_utils")

    ffmpeg_runtime.has_encoder.cache_clear()

    class _HasEncoderOkSubprocess:
        PIPE = object()
        DEVNULL = object()

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=0, stdout=" V..... libx264\n")

    assert ffmpeg_runtime.has_encoder("libx264", subprocess_module=_HasEncoderOkSubprocess) is True

    ffmpeg_runtime.has_encoder.cache_clear()

    class _HasEncoderBadSubprocess:
        PIPE = object()
        DEVNULL = object()

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=1, stdout="")

    assert (
        ffmpeg_runtime.has_encoder("libx264", subprocess_module=_HasEncoderBadSubprocess) is False
    )

    ffmpeg_runtime.has_encoder.cache_clear()

    class _HasEncoderNoMatchSubprocess:
        PIPE = object()
        DEVNULL = object()

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=0, stdout=" V..... h264\n")

    assert (
        ffmpeg_runtime.has_encoder("libx264", subprocess_module=_HasEncoderNoMatchSubprocess)
        is False
    )

    ffmpeg_runtime.has_encoder.cache_clear()

    class _HasEncoderExplodingSubprocess:
        PIPE = object()
        DEVNULL = object()

        @staticmethod
        def run(*_a, **_k):
            raise RuntimeError("boom")

    assert (
        ffmpeg_runtime.has_encoder("libx264", subprocess_module=_HasEncoderExplodingSubprocess)
        is False
    )

    assert ffmpeg_runtime.choose_h264_encoder(has_encoder_fn=lambda _e: True) == ("libx264", "")
    assert ffmpeg_runtime.choose_h264_encoder(has_encoder_fn=lambda _e: False)[0] == "h264"

    ffmpeg_runtime.nvenc_preflight.cache_clear()

    class _NvencOkSubprocess:
        PIPE = object()
        STDOUT = object()

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=0, stdout="")

    assert ffmpeg_runtime.nvenc_preflight(subprocess_module=_NvencOkSubprocess) == (True, "")

    ffmpeg_runtime.nvenc_preflight.cache_clear()

    class _NvencFailSubprocess:
        PIPE = object()
        STDOUT = object()

        @staticmethod
        def run(*_a, **_k):
            return types.SimpleNamespace(returncode=7, stdout="nvenc error")

    ok, details = ffmpeg_runtime.nvenc_preflight(subprocess_module=_NvencFailSubprocess)
    assert ok is False
    assert "nvenc error" in details

    ffmpeg_runtime.nvenc_preflight.cache_clear()

    class _NvencMissingSubprocess:
        PIPE = object()
        STDOUT = object()

        @staticmethod
        def run(*_a, **_k):
            raise FileNotFoundError("ffmpeg")

    assert ffmpeg_runtime.nvenc_preflight(subprocess_module=_NvencMissingSubprocess)[0] is False

    ffmpeg_runtime.nvenc_preflight.cache_clear()

    class _NvencExceptionSubprocess:
        PIPE = object()
        STDOUT = object()

        @staticmethod
        def run(*_a, **_k):
            raise RuntimeError("nvenc")

    ok, details = ffmpeg_runtime.nvenc_preflight(subprocess_module=_NvencExceptionSubprocess)
    assert ok is False
    assert "exception" in details

    text_subprocess = types.SimpleNamespace(
        PIPE=object(),
        STDOUT=object(),
        run=lambda *_a, **_k: types.SimpleNamespace(returncode=5, stdout=None),
    )
    assert ffmpeg_runtime.run_and_collect_text(
        ["echo", "x"], subprocess_module=text_subprocess
    ) == (
        5,
        "",
    )

    bytes_subprocess = types.SimpleNamespace(
        PIPE=object(),
        STDOUT=object(),
        run=lambda *_a, **_k: types.SimpleNamespace(returncode=4, stdout=None),
    )
    assert ffmpeg_runtime.run_shell_bytes("echo x", subprocess_module=bytes_subprocess) == (4, b"")
