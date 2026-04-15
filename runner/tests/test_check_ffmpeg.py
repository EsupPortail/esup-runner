from scripts import check_ffmpeg as cff


def test_parse_filters_accepts_three_and_four_flag_formats() -> None:
    text = """
Filters:
  T.. = Timeline support
  ... scale_cuda        V->V       GPU accelerated video resizer
  .S.C hwupload_cuda    V->V       Upload system memory frames to a CUDA device.
  TSCX overlay_cuda     VV->V      Overlay one video on top of another using CUDA.
"""
    parsed = cff._parse_filters(text)
    assert "scale_cuda" in parsed
    assert "hwupload_cuda" in parsed
    assert "overlay_cuda" in parsed


def test_parse_filters_ignores_ansi_sequences() -> None:
    text = "\x1b[0;32m...\x1b[0m \x1b[0;36mscale_cuda\x1b[0m V->V GPU accelerated video resizer"
    parsed = cff._parse_filters(text)
    assert "scale_cuda" in parsed


def test_collect_results_marks_scale_and_hwupload_ok_when_preflight_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        cff,
        "check_binaries",
        lambda: [
            cff.CheckResult("binary:ffmpeg", True, True, ""),
            cff.CheckResult("binary:ffprobe", True, True, ""),
        ],
    )
    monkeypatch.setattr(
        cff,
        "check_versions",
        lambda: [
            cff.CheckResult("version:ffmpeg", True, True, ""),
            cff.CheckResult("version:ffprobe", True, True, ""),
        ],
    )
    monkeypatch.setattr(cff, "check_build_configuration", lambda for_studio: [])
    monkeypatch.setattr(
        cff, "preflight_cpu_encode", lambda: cff.CheckResult("preflight:cpu", True, True, "")
    )
    monkeypatch.setattr(
        cff, "check_png_encoder", lambda: cff.CheckResult("cap:png_encoder", True, True, "")
    )
    monkeypatch.setattr(cff, "_build_gpu_env", lambda _cuda_visible: {})
    monkeypatch.setattr(
        cff,
        "check_expected_caps_for_mode",
        lambda mode, env, for_encoding, for_studio: (
            [
                cff.CheckResult("cap:gpu:h264_nvenc", True, True, ""),
                cff.CheckResult("cap:gpu:h264_cuvid", True, True, ""),
                cff.CheckResult("cap:gpu:scale_cuda", False, True, ""),
                cff.CheckResult("cap:gpu:hwupload_cuda", False, False, ""),
            ]
            if mode == "gpu"
            else [cff.CheckResult("cap:cpu:h264_encoder", True, True, "")]
        ),
    )
    monkeypatch.setattr(
        cff,
        "preflight_nvenc_basic",
        lambda env: cff.CheckResult("preflight:gpu:nvenc", True, True, ""),
    )
    monkeypatch.setattr(
        cff,
        "preflight_scale_cuda_to_nvenc",
        lambda env, hwdev: cff.CheckResult("preflight:gpu:scale_cuda+nvenc", True, True, ""),
    )

    results = cff._collect_results(
        modes=["gpu"],
        hwdev=0,
        cuda_visible="0",
        for_encoding=True,
        for_studio=True,
        cfg_err="",
        verbose=False,
    )
    by_name = {r.name: r for r in results}
    assert by_name["cap:gpu:scale_cuda"].ok
    assert by_name["cap:gpu:hwupload_cuda"].ok
    assert by_name["cap:gpu:scale_cuda"].details == "Validated by preflight:gpu:scale_cuda+nvenc"
