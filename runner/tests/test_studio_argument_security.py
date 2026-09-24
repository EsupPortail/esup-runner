"""Studio parameters and paths must retain their FFmpeg argument boundaries."""

import shlex
from types import SimpleNamespace

import pytest

from app.task_handlers.studio.core import main_runtime_utils, pipeline_building_utils
from app.task_handlers.studio.core.runtime_args_utils import parse_args
from app.task_handlers.studio.studio_handler import StudioEncodingHandler


@pytest.mark.parametrize(
    "bitrate",
    [
        "192k -f mp4 /tmp/outside.mp4",
        '192k" -y',
        "192k\n-i input",
        "-i",
        "",
        None,
        128000,
        True,
        [],
        "9" * 400 + "k",
    ],
)
def test_invalid_audio_bitrate_rejected_at_admission(bitrate):
    handler = StudioEncodingHandler()
    assert not handler.validate_parameters({"studio_audio_bitrate": bitrate})
    assert handler.last_invalid_parameters == ["studio_audio_bitrate"]


@pytest.mark.parametrize("bitrate", ["128k", "192k", "1.5M", " 192k ", "128000", "192000.5"])
def test_valid_bitrates_remain_supported(bitrate):
    handler = StudioEncodingHandler()
    assert handler.validate_parameters({"studio_audio_bitrate": bitrate})
    argv = handler._build_studio_args(
        "https://example.org/package.xml",
        "/tmp/base",
        "output",
        "studio.mp4",
        None,
        {"studio_audio_bitrate": bitrate},
    )
    assert parse_args(argv).studio_audio_bitrate == bitrate.strip()


def test_bitrate_injection_rejected_by_cli_and_direct_execution(monkeypatch):
    malicious = "192k -f mp4 /tmp/outside.mp4"
    handler = StudioEncodingHandler()
    with pytest.raises(ValueError, match="studio_audio_bitrate"):
        handler._build_studio_args(
            "xml", "/tmp", "out", "video.mp4", None, {"studio_audio_bitrate": malicious}
        )
    with pytest.raises(SystemExit) as exc:
        parse_args(
            [
                "--xml-url",
                "xml",
                "--base-dir",
                "/tmp",
                "--work-dir",
                "out",
                "--output-file",
                "video.mp4",
                "--studio-audio-bitrate",
                malicious,
            ]
        )
    assert exc.value.code == 2
    monkeypatch.setattr(
        main_runtime_utils.subprocess,
        "run",
        lambda *_a, **_k: pytest.fail("No process may be started"),
    )
    with pytest.raises(ValueError, match="studio_audio_bitrate"):
        main_runtime_utils._run_pipelines(
            pres_url_local="input.mp4",
            pers_url_local=None,
            pres_h=720,
            pers_h=0,
            presenter_layout="mid",
            args=SimpleNamespace(),
            studio_allow_nvenc=True,
            webm_input=False,
            subtime="",
            audio_bitrate=malicious,
            output_opts="-f mp4 ",
            output_path="output.mp4",
        )


@pytest.mark.parametrize("source_case", ["mixed", "presentation", "presenter", "no-video"])
@pytest.mark.parametrize("gpu", [False, True])
def test_paths_and_options_keep_boundaries_through_pipeline_fallbacks(
    monkeypatch, source_case, gpu
):
    presentation = "/tmp/a space/it's \" -i injected.mp4 \\presentation.mp4"
    presenter = '/tmp/presenter\n" -f mp4 injected.mp4 " $name.mp4'
    output = '/tmp/output " -f mp4 injected.mp4 " \\ end.mp4'
    pres = None if source_case == "presenter" else presentation
    pers = None if source_case == "presentation" else presenter
    pres_h = 0 if source_case == "no-video" else 720
    pers_h = 0 if source_case == "no-video" else 720
    args = SimpleNamespace(
        encoding_type="GPU" if gpu else "CPU",
        force_cpu="false",
        studio_preset='medium"\\x',
        studio_crf="23'",
        hwaccel_device="0",
    )
    calls = []
    monkeypatch.setattr(main_runtime_utils, "_set_cuda_env", lambda _: None)
    monkeypatch.setattr(main_runtime_utils, "_nvenc_preflight", lambda: (True, ""))
    monkeypatch.setattr(main_runtime_utils, "_choose_cuda_decoder_for", lambda _: "h264_cuvid")
    monkeypatch.setattr(main_runtime_utils, "_choose_h264_encoder", lambda: ("libx264", ""))
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils, "filter_available", lambda *_a, **_k: True
    )

    def run(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if "h264_nvenc" in argv else 0)

    monkeypatch.setattr(main_runtime_utils.subprocess, "run", run)
    result = main_runtime_utils._run_pipelines(
        pres_url_local=pres,
        pers_url_local=pers,
        pres_h=pres_h,
        pers_h=pers_h,
        presenter_layout="mid",
        args=args,
        studio_allow_nvenc=True,
        webm_input=False,
        subtime="-ss 1.000 -t 2.000 ",
        audio_bitrate="192k",
        output_opts="-f mp4 ",
        output_path=output,
    )
    assert result == 0
    assert len(calls) == (3 if gpu and pres and pers else 2 if gpu else 1)
    for argv in calls:
        inputs = [argv[i + 1] for i, value in enumerate(argv) if value == "-i"]
        expected = [source for source in (pres, pers) if source]
        if source_case == "no-video" and "-hwaccel" not in argv:
            expected = [presentation]
        assert inputs == expected
        assert argv[-1] == output
        assert argv.count("-f") == 1
        assert argv[argv.index("-b:a") + 1] == "192k"
        assert argv[argv.index("-preset") + 1] == args.studio_preset
        quality = "-cq" if "h264_nvenc" in argv else "-crf"
        assert argv[argv.index(quality) + 1] == args.studio_crf

    fragment, _, _ = pipeline_building_utils.build_input_args(
        pres, pers, args, probe_height_fn=lambda _: 720
    )
    assert shlex.split(fragment) == [arg for path in (pres, pers) if path for arg in ("-i", path)]
