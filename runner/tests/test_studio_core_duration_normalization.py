"""Validates Studio duration normalization for stable video/audio timelines."""

from __future__ import annotations

from types import SimpleNamespace

from app.task_handlers.studio.core import (
    ffmpeg_command_utils,
    ffmpeg_runtime_utils,
    main_orchestration_utils,
    pipeline_building_utils,
)


def test_filter_builders_add_padding_and_trim_when_target_duration_is_set():
    """Validate filter builders inject tpad+trim guards when a target duration is available."""
    target_duration = 5.6

    mixed_filter = ffmpeg_command_utils.build_filter(
        1080,
        720,
        "piph",
        target_duration=target_duration,
    )
    assert "tpad=stop_mode=clone:stop_duration=5.600" in mixed_filter
    assert "trim=duration=5.600,setpts=PTS-STARTPTS" in mixed_filter

    single_source = ffmpeg_command_utils.build_cpu_single_source_subcmd(
        cpu_encoder="libx264",
        cpu_is_libx264=True,
        target_h=720,
        args=SimpleNamespace(studio_preset=None, studio_crf=None),
        target_duration=target_duration,
    )
    assert "tpad=stop_mode=clone:stop_duration=5.600" in single_source
    assert "trim=duration=5.600,setpts=PTS-STARTPTS" in single_source


def test_compute_target_duration_applies_clip_window_and_fallbacks():
    """Validate target duration computation from source probe values and optional SMIL cuts."""

    def _probe_duration(source: str) -> float | None:
        return {
            "presentation.webm": 5.61,
            "presenter.webm": 5.58,
        }.get(source)

    assert (
        ffmpeg_runtime_utils.compute_target_duration(
            "presentation.webm",
            "presenter.webm",
            None,
            None,
            probe_duration_fn=_probe_duration,
        )
        == 5.61
    )

    assert (
        ffmpeg_runtime_utils.compute_target_duration(
            "presentation.webm",
            "presenter.webm",
            1.0,
            4.0,
            probe_duration_fn=_probe_duration,
        )
        == 3.0
    )

    assert (
        ffmpeg_runtime_utils.compute_target_duration(
            None,
            None,
            None,
            None,
            probe_duration_fn=_probe_duration,
        )
        is None
    )


def test_duration_helpers_cover_zero_positive_and_clip_edge_cases():
    """Validate duration helpers on zero/positive values and clip edge branches."""
    zero_duration_filter = ffmpeg_command_utils.build_filter(
        1080,
        720,
        "mid",
        target_duration=0.0,
    )
    assert "tpad=stop_mode=clone" not in zero_duration_filter
    assert "trim=duration=" not in zero_duration_filter

    gpu_enc = pipeline_building_utils.build_gpu_encode_only_pipeline(
        "presentation.mp4",
        None,
        720,
        0,
        "mid",
        SimpleNamespace(encoding_type="GPU", force_cpu=None),
        webm_input=False,
        target_duration=4.2,
        is_gpu_requested_fn=lambda _args: True,
        set_cuda_env_fn=lambda _args: None,
        nvenc_preflight_fn=lambda: (True, ""),
        build_filter_fn=lambda *_args, **_kwargs: "unused",
        build_nvenc_video_codec_fn=lambda _webm_input: "-c:v h264_nvenc ",
    )
    assert gpu_enc is not None
    assert "tpad=stop_mode=clone:stop_duration=4.200" in gpu_enc[1]
    assert "trim=duration=4.200,setpts=PTS-STARTPTS" in gpu_enc[1]

    def _probe_duration(source: str) -> float | None:
        return {
            "presentation.mp4": 1.0,
        }.get(source)

    assert (
        ffmpeg_runtime_utils.compute_target_duration(
            "presentation.mp4",
            None,
            None,
            0.6,
            probe_duration_fn=_probe_duration,
        )
        == 0.6
    )
    assert (
        ffmpeg_runtime_utils.compute_target_duration(
            "presentation.mp4",
            None,
            2.0,
            None,
            probe_duration_fn=_probe_duration,
        )
        is None
    )


def test_probe_duration_handles_success_missing_and_invalid_payloads():
    """Validate probe_duration behavior for valid, empty, and invalid ffprobe outputs."""

    class _ProbeSubprocess:
        PIPE = object()
        STDOUT = object()

        @staticmethod
        def run(cmd, stdout=None, stderr=None):
            del stdout, stderr
            payload_by_source = {
                "ok.mp4": b'{"format": {"duration": "12.345"}}',
                "missing.mp4": b'{"format": {}}',
                "zero.mp4": b'{"format": {"duration": "0"}}',
                "bad.mp4": b"not-json",
            }
            return SimpleNamespace(stdout=payload_by_source[cmd[-1]])

    assert (
        ffmpeg_runtime_utils.probe_duration("ok.mp4", subprocess_module=_ProbeSubprocess) == 12.345
    )
    assert (
        ffmpeg_runtime_utils.probe_duration("missing.mp4", subprocess_module=_ProbeSubprocess)
        is None
    )
    assert (
        ffmpeg_runtime_utils.probe_duration("zero.mp4", subprocess_module=_ProbeSubprocess) is None
    )
    assert (
        ffmpeg_runtime_utils.probe_duration("bad.mp4", subprocess_module=_ProbeSubprocess) is None
    )


def test_run_main_flow_forwards_computed_target_duration(tmp_path):
    """Validate main flow forwards computed target duration to pipeline execution."""
    captured: dict[str, object] = {}

    def _run_pipelines(**kwargs):
        captured.update(kwargs)
        return 0

    context = main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=lambda _args: (
            "presentation.webm",
            "presenter.webm",
            "mid",
            None,
        ),
        load_clip_times_fn=lambda _smil_url: (None, None),
        materialize_source_fn=lambda source, *_args: source,
        is_webm_input_source_fn=lambda _source: True,
        build_input_args_fn=lambda *_args: (
            '-i "presentation.webm" -i "presenter.webm" ',
            1080,
            720,
        ),
        build_subtime_fn=lambda _start, _end: "",
        run_pipelines_fn=_run_pipelines,
        compute_target_duration_fn=lambda *_args: 5.6,
    )

    args = SimpleNamespace(
        base_dir=str(tmp_path),
        work_dir="output",
        output_file="studio_base.mp4",
        studio_allow_nvenc="true",
        studio_audio_bitrate=None,
    )

    rc = main_orchestration_utils.run_main_flow(args, context=context)
    assert rc == 0
    assert captured["target_duration"] == 5.6


def test_run_main_flow_ignores_target_duration_exceptions(tmp_path):
    """Validate orchestration keeps running if target-duration computation fails."""
    captured: dict[str, object] = {}

    def _run_pipelines(**kwargs):
        captured.update(kwargs)
        return 0

    def _compute_target_duration_raises(*_args):
        raise RuntimeError("probe failure")

    context = main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=lambda _args: (
            "presentation.webm",
            "presenter.webm",
            "mid",
            None,
        ),
        load_clip_times_fn=lambda _smil_url: (None, None),
        materialize_source_fn=lambda source, *_args: source,
        is_webm_input_source_fn=lambda _source: True,
        build_input_args_fn=lambda *_args: (
            '-i "presentation.webm" -i "presenter.webm" ',
            1080,
            720,
        ),
        build_subtime_fn=lambda _start, _end: "",
        run_pipelines_fn=_run_pipelines,
        compute_target_duration_fn=_compute_target_duration_raises,
    )

    args = SimpleNamespace(
        base_dir=str(tmp_path),
        work_dir="output",
        output_file="studio_base.mp4",
        studio_allow_nvenc="true",
        studio_audio_bitrate=None,
    )

    rc = main_orchestration_utils.run_main_flow(args, context=context)
    assert rc == 0
    assert captured["target_duration"] is None
