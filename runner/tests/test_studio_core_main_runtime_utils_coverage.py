"""Validates Studio core main runtime utils module wiring and helper function implementations."""

import types

from app.task_handlers.studio.core import main_runtime_utils, runtime_args_utils


def test_studio_core_main_runtime_utils_wiring(monkeypatch):
    """Validate studio core main runtime utils wiring."""
    calls = {}

    def _fake_build_input_args(pres_url, pers_url, args, *, probe_height_fn):
        calls["build_input_args"] = (pres_url, pers_url, args, probe_height_fn("probe.mp4"))
        return "ia", 11, 22

    monkeypatch.setattr(
        main_runtime_utils.pipeline_building_utils,
        "build_input_args",
        _fake_build_input_args,
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "probe_height",
        lambda _src, subprocess_module: (
            333 if subprocess_module is main_runtime_utils.subprocess else 0
        ),
    )
    assert main_runtime_utils._build_input_args("pres", "pers", object()) == ("ia", 11, 22)
    assert calls["build_input_args"][3] == 333

    def _fake_is_webm_input_source(source, *, probe_codec_fn, looks_like_webm_source_fn=None):
        del looks_like_webm_source_fn
        calls["is_webm_input_source"] = (source, probe_codec_fn("codec.mp4"))
        return True

    monkeypatch.setattr(
        main_runtime_utils.source_utils,
        "is_webm_input_source",
        _fake_is_webm_input_source,
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "probe_codec",
        lambda _src, subprocess_module: (
            "vp9" if subprocess_module is main_runtime_utils.subprocess else ""
        ),
    )
    assert main_runtime_utils._is_webm_input_source("sample.mp4") is True
    assert calls["is_webm_input_source"] == ("sample.mp4", "vp9")

    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "set_cuda_env",
        lambda args, *, os_module: calls.update(
            {"set_cuda_env": (args.token, os_module is main_runtime_utils.os)}
        ),
    )
    main_runtime_utils._set_cuda_env(types.SimpleNamespace(token="ok"))
    assert calls["set_cuda_env"] == ("ok", True)

    main_runtime_utils._nvenc_preflight.cache_clear()
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "nvenc_preflight",
        lambda *, subprocess_module: (
            subprocess_module is main_runtime_utils.subprocess,
            "nvenc-ok",
        ),
    )
    assert main_runtime_utils._nvenc_preflight() == (True, "nvenc-ok")
    main_runtime_utils._nvenc_preflight.cache_clear()

    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "choose_cuda_decoder_for",
        lambda source, *, probe_codec_fn, has_decoder_fn: (
            "h264_cuvid"
            if (
                source == "in.mp4"
                and probe_codec_fn("x.mp4") == "h264"
                and has_decoder_fn("h264_cuvid") is True
            )
            else None
        ),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "probe_codec",
        lambda _src, subprocess_module: (
            "h264" if subprocess_module is main_runtime_utils.subprocess else ""
        ),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "has_decoder",
        lambda decoder, *, subprocess_module: (
            decoder == "h264_cuvid" and subprocess_module is main_runtime_utils.subprocess
        ),
    )
    assert main_runtime_utils._choose_cuda_decoder_for("in.mp4") == "h264_cuvid"

    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "choose_h264_encoder",
        lambda *, has_encoder_fn: (
            ("libx264", "chosen") if has_encoder_fn("libx264") else ("h264", "fallback")
        ),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "has_encoder",
        lambda encoder, *, subprocess_module: (
            encoder == "libx264" and subprocess_module is main_runtime_utils.subprocess
        ),
    )
    assert main_runtime_utils._choose_h264_encoder() == ("libx264", "chosen")

    def _fake_prepare_full_gpu_inputs(
        pres_url,
        pers_url,
        pres_h,
        presenter_layout,
        args,
        **kwargs,
    ):
        del kwargs
        calls["prepare_full_gpu_inputs"] = (
            pres_url,
            pers_url,
            pres_h,
            presenter_layout,
            args.mode,
        )
        return "gpu-ia", 720, 180, "W-w-10:10"

    def _fake_build_full_gpu_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        *,
        prepare_full_gpu_inputs_fn,
        build_full_gpu_filtergraph_fn,
        build_nvenc_video_codec_fn,
    ):
        prepared = prepare_full_gpu_inputs_fn(
            pres_url=pres_url,
            pers_url=pers_url,
            pres_h=pres_h,
            presenter_layout=presenter_layout,
            args=args,
        )
        filt = build_full_gpu_filtergraph_fn(
            presenter_layout=presenter_layout,
            height=prepared[1],
            pip_h=prepared[2],
            overlay_pos=prepared[3],
        )
        codec = build_nvenc_video_codec_fn(webm_input)
        return prepared[0], filt, codec, f"m{pers_h}"

    def _fake_build_gpu_encode_only_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        *,
        is_gpu_requested_fn,
        set_cuda_env_fn,
        nvenc_preflight_fn,
        build_filter_fn,
        build_nvenc_video_codec_fn,
    ):
        del is_gpu_requested_fn, set_cuda_env_fn
        nvenc = nvenc_preflight_fn()
        filt = build_filter_fn(pres_h, pers_h, presenter_layout)
        codec = build_nvenc_video_codec_fn(webm_input)
        return f"enc-{pres_url}-{pers_url}", f"{filt}:{args.mode}:{nvenc[0]}", codec, "map"

    monkeypatch.setattr(
        main_runtime_utils.pipeline_building_utils,
        "prepare_full_gpu_inputs",
        _fake_prepare_full_gpu_inputs,
    )
    monkeypatch.setattr(
        main_runtime_utils.pipeline_building_utils,
        "build_full_gpu_pipeline",
        _fake_build_full_gpu_pipeline,
    )
    monkeypatch.setattr(
        main_runtime_utils.pipeline_building_utils,
        "build_gpu_encode_only_pipeline",
        _fake_build_gpu_encode_only_pipeline,
    )
    monkeypatch.setattr(
        main_runtime_utils.pipeline_building_utils,
        "build_cpu_pipeline",
        lambda *_args, **_kwargs: ("cpu-ia", "cpu-sub", "cpu-vc", "cpu-map"),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "filter_available",
        lambda name, *, subprocess_module: (
            name in {"scale_cuda", "overlay_cuda"}
            and subprocess_module is main_runtime_utils.subprocess
        ),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_runtime_utils,
        "nvenc_preflight",
        lambda *, subprocess_module: (
            subprocess_module is main_runtime_utils.subprocess,
            "ok",
        ),
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_command_utils,
        "build_full_gpu_filtergraph",
        lambda **kwargs: f"fg-{kwargs['height']}-{kwargs['pip_h']}-{kwargs['overlay_pos']}",
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_command_utils,
        "build_filter",
        lambda pres_h, pers_h, presenter_layout: f"f-{pres_h}-{pers_h}-{presenter_layout}",
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_command_utils,
        "build_nvenc_video_codec",
        lambda args, *, webm_input: f"vc-{args.mode}-{webm_input}",
    )
    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_command_utils,
        "even_or_default_height",
        lambda value, default: value or default,
    )

    def _fake_run_pipelines(**kwargs):
        args_obj = types.SimpleNamespace(mode="gpu")
        full = kwargs["build_full_gpu_pipeline_fn"](
            "pres.mp4", "pers.mp4", 1080, 720, "piph", args_obj, True
        )
        enc = kwargs["build_gpu_encode_only_pipeline_fn"](
            "pres.mp4", "pers.mp4", 1080, 720, "mid", args_obj, False
        )
        cpu = kwargs["build_cpu_pipeline_fn"]("pres.mp4", "pers.mp4", 1080, 720, "mid", args_obj)
        calls["run_pipelines"] = (full, enc, cpu, kwargs["shlex_split_fn"]("a b"))
        return 77

    monkeypatch.setattr(
        main_runtime_utils.pipeline_runtime_utils,
        "run_pipelines",
        _fake_run_pipelines,
    )
    assert main_runtime_utils._run_pipelines(test="x") == 77
    assert calls["run_pipelines"][0][0] == "gpu-ia"
    assert calls["run_pipelines"][1][2] == "vc-gpu-False"
    assert calls["run_pipelines"][2] == ("cpu-ia", "cpu-sub", "cpu-vc", "cpu-map")
    assert calls["run_pipelines"][3] == ["a", "b"]

    monkeypatch.setattr(
        main_runtime_utils.ffmpeg_command_utils,
        "build_subtime",
        lambda _start, _end: "sub",
    )
    context = main_runtime_utils.build_main_flow_context()
    assert context.build_subtime_fn(1.0, 2.0) == "sub"
    assert context.build_input_args_fn == main_runtime_utils._build_input_args

    monkeypatch.setattr(main_runtime_utils, "parse_args", lambda: "parsed-args")
    monkeypatch.setattr(main_runtime_utils, "build_main_flow_context", lambda: "ctx")
    monkeypatch.setattr(
        main_runtime_utils.main_orchestration_utils,
        "run_main_flow",
        lambda args, *, context: 123 if args == "parsed-args" and context == "ctx" else 0,
    )
    assert main_runtime_utils.main() == 123


def test_studio_core_runtime_args_parse_args():
    """Validate studio core runtime args parse_args."""
    args = runtime_args_utils.parse_args(
        [
            "--xml-url",
            "https://example.org/mp.xml",
            "--base-dir",
            "/tmp/work",
            "--work-dir",
            "output",
            "--output-file",
            "studio.mp4",
        ]
    )
    assert args.xml_url == "https://example.org/mp.xml"
    assert args.work_dir == "output"
