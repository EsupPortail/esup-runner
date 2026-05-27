"""Pipeline-building helpers for studio CPU/GPU selection."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class BuildFullGpuFiltergraphFn(Protocol):
    """Callable signature for full-GPU filtergraph builders."""

    def __call__(
        self,
        *,
        presenter_layout: str,
        height: int,
        pip_h: int,
        overlay_pos: str,
        target_duration: float | None = None,
    ) -> str: ...


class BuildFilterFn(Protocol):
    """Callable signature for CPU/GPU mixed filter builders."""

    def __call__(
        self,
        pres_h: int,
        pers_h: int,
        presenter: str,
        target_duration: float | None = None,
    ) -> str: ...


class BuildCpuSingleSourceSubcmdFn(Protocol):
    """Callable signature for single-source CPU subcommand builders."""

    def __call__(
        self,
        *,
        cpu_encoder: str,
        cpu_is_libx264: bool,
        target_h: int,
        args: Any,
        target_duration: float | None = None,
    ) -> str: ...


def _duration_filter_opts(target_duration: float | None) -> str:
    """Build optional tpad/trim suffix used to normalize video duration."""
    if target_duration is None or target_duration <= 0:
        return ""
    duration_str = f"{target_duration:.3f}"
    return (
        f",tpad=stop_mode=clone:stop_duration={duration_str},"
        f"trim=duration={duration_str},setpts=PTS-STARTPTS"
    )


def _build_single_source_scale_subcmd(target_h: int, target_duration: float | None) -> str:
    """Build scale filter command for single-source GPU encode-only fallback paths."""
    duration_opts = _duration_filter_opts(target_duration)
    return (
        f' -vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{target_h},'
        f'format=yuv420p{duration_opts},setsar=1" '
    )


def _select_gpu_encode_only_single_source(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
) -> tuple[str, int] | None:
    """Pick the best single-source fallback input and target height."""
    if pres_url and pers_url:
        if pres_h > 0:
            print(
                "Presenter source has no video stream; falling back to presentation-only pipeline"
            )
            return f'-i "{pres_url}" ', pres_h
        if pers_h > 0:
            print(
                "Presentation source has no video stream; falling back to presenter-only pipeline"
            )
            return f'-i "{pers_url}" ', pers_h
        print(
            "Could not detect video dimensions for studio mix; falling back to presentation-only pipeline"
        )
        return f'-i "{pres_url}" ', 720

    if pres_url:
        return f'-i "{pres_url}" ', (pres_h or 720)
    if pers_url:
        return f'-i "{pers_url}" ', (pers_h or 720)
    return None


def build_input_args(
    pres_url: str | None,
    pers_url: str | None,
    args: Any,
    *,
    probe_height_fn: Callable[[str], int],
) -> tuple[str, int, int]:
    """Build FFmpeg input arguments and probe source heights."""
    del args
    input_args = ""
    pres_h = pers_h = 0
    if pres_url and pers_url:
        input_args = f'-i "{pres_url}" -i "{pers_url}" '
        pres_h = probe_height_fn(pres_url)
        pers_h = probe_height_fn(pers_url)
    elif pres_url:
        input_args = f'-i "{pres_url}" '
        pres_h = probe_height_fn(pres_url)
    elif pers_url:
        input_args = f'-i "{pers_url}" '
        pers_h = probe_height_fn(pers_url)
    else:
        raise ValueError("No media tracks")
    return input_args, pres_h, pers_h


def is_gpu_requested(args: Any) -> bool:
    """Return whether GPU processing is requested and not force-disabled."""
    if (args.encoding_type or "CPU").upper() != "GPU":
        return False
    return (args.force_cpu or "").lower() not in ("true", "1", "yes")


def prepare_full_gpu_inputs(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    presenter_layout: str,
    args: Any,
    *,
    is_gpu_requested_fn: Callable[[Any], bool],
    set_cuda_env_fn: Callable[[Any], None],
    nvenc_preflight_fn: Callable[[], tuple[bool, str]],
    choose_cuda_decoder_for_fn: Callable[[str], str | None],
    filter_available_fn: Callable[[str], bool],
    even_or_default_height_fn: Callable[[int, int], int],
) -> tuple[str, int, int, str] | None:
    """Prepare inputs and sizing values for a full GPU studio pipeline."""
    if not (pres_url and pers_url):
        return None
    if not is_gpu_requested_fn(args):
        return None

    set_cuda_env_fn(args)
    nvenc_ok, nvenc_details = nvenc_preflight_fn()
    if not nvenc_ok:
        if nvenc_details:
            print(nvenc_details.strip())
        return None

    pres_dec = choose_cuda_decoder_for_fn(pres_url)
    pers_dec = choose_cuda_decoder_for_fn(pers_url)
    have_scale_cuda = filter_available_fn("scale_cuda")
    need_overlay = presenter_layout != "mid"
    have_overlay_cuda = (not need_overlay) or filter_available_fn("overlay_cuda")
    if not (pres_dec and pers_dec and have_scale_cuda and have_overlay_cuda):
        return None

    hwdev = int(args.hwaccel_device or 0)
    input_args = (
        f"-hwaccel_device {hwdev} -hwaccel cuda -hwaccel_output_format cuda "
        f'-c:v {pres_dec} -i "{pres_url}" '
        f"-hwaccel_device {hwdev} -hwaccel cuda -hwaccel_output_format cuda "
        f'-c:v {pers_dec} -i "{pers_url}" '
    )

    height = even_or_default_height_fn(pres_h, 720)
    pip_h = (height // 4) if presenter_layout in ("piph", "pipb") else height
    pip_h = even_or_default_height_fn(pip_h, pip_h)
    overlay_pos = "W-w-10:H-h-10" if presenter_layout == "pipb" else "W-w-10:10"
    return input_args, height, pip_h, overlay_pos


def build_full_gpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: Any,
    webm_input: bool,
    target_duration: float | None = None,
    *,
    prepare_full_gpu_inputs_fn: Callable[..., tuple[str, int, int, str] | None],
    build_full_gpu_filtergraph_fn: BuildFullGpuFiltergraphFn,
    build_nvenc_video_codec_fn: Callable[[bool], str],
) -> tuple[str, str, str, str] | None:
    """Build a pipeline using GPU decode (CUVID) + GPU encode (NVENC) when possible."""
    del pers_h
    prepared = prepare_full_gpu_inputs_fn(
        pres_url=pres_url,
        pers_url=pers_url,
        pres_h=pres_h,
        presenter_layout=presenter_layout,
        args=args,
    )
    if prepared is None:
        return None
    input_args, height, pip_h, overlay_pos = prepared
    gpu_filter = build_full_gpu_filtergraph_fn(
        presenter_layout=presenter_layout,
        height=height,
        pip_h=pip_h,
        overlay_pos=overlay_pos,
        target_duration=target_duration,
    )
    map_opts = '-map "[vout]" -map 0:a? '
    return input_args, gpu_filter, build_nvenc_video_codec_fn(webm_input), map_opts


def build_gpu_encode_only_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: Any,
    webm_input: bool,
    target_duration: float | None = None,
    *,
    is_gpu_requested_fn: Callable[[Any], bool],
    set_cuda_env_fn: Callable[[Any], None],
    nvenc_preflight_fn: Callable[[], tuple[bool, str]],
    build_filter_fn: BuildFilterFn,
    build_nvenc_video_codec_fn: Callable[[bool], str],
) -> tuple[str, str, str, str] | None:
    """Build a CPU decode/filter + NVENC encode pipeline."""
    if not is_gpu_requested_fn(args):
        return None

    set_cuda_env_fn(args)
    nvenc_ok, nvenc_details = nvenc_preflight_fn()
    if not nvenc_ok:
        if nvenc_details:
            print(nvenc_details.strip())
        return None

    map_opts = "-map 0:v -map 0:a? "
    if pres_url and pers_url and pres_h > 0 and pers_h > 0:
        input_args = f'-i "{pres_url}" -i "{pers_url}" '
        subcmd = build_filter_fn(
            pres_h,
            pers_h,
            presenter_layout,
            target_duration=target_duration,
        )
        map_opts = '-map "[vout]" -map 0:a? '
    else:
        selected = _select_gpu_encode_only_single_source(
            pres_url,
            pers_url,
            pres_h,
            pers_h,
        )
        if selected is None:
            return None
        input_args, target_h = selected
        subcmd = _build_single_source_scale_subcmd(target_h, target_duration)

    return input_args, subcmd, build_nvenc_video_codec_fn(webm_input), map_opts


def select_cpu_input_args(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
) -> tuple[str, str, str]:
    """Select CPU input args and mapping."""
    if pres_url and pers_url:
        if pres_h > 0 and pers_h > 0:
            return f'-i "{pres_url}" -i "{pers_url}" ', '-map "[vout]" -map 0:a? ', "mixed"
        if pres_h > 0:
            print(
                "Presenter source has no video stream; falling back to presentation-only pipeline"
            )
            return f'-i "{pres_url}" ', "-map 0:v -map 0:a? ", "presentation"
        if pers_h > 0:
            print(
                "Presentation source has no video stream; falling back to presenter-only pipeline"
            )
            return f'-i "{pers_url}" ', "-map 0:v -map 0:a? ", "presenter"
        print(
            "Could not detect video dimensions for studio mix; falling back to presentation-only pipeline"
        )
        return f'-i "{pres_url}" ', "-map 0:v -map 0:a? ", "presentation"
    if pres_url:
        return f'-i "{pres_url}" ', "-map 0:v -map 0:a? ", "presentation"
    if pers_url:
        return f'-i "{pers_url}" ', "-map 0:v -map 0:a? ", "presenter"
    raise ValueError("No media tracks")


def single_source_height(source_kind: str, pres_h: int, pers_h: int) -> int:
    """Return target height for a single-source pipeline."""
    if source_kind == "presentation":
        return pres_h or 720
    if source_kind == "presenter":
        return pers_h or 720
    return 720


def build_cpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: Any,
    target_duration: float | None = None,
    *,
    select_cpu_input_args_fn: Callable[..., tuple[str, str, str]],
    choose_h264_encoder_fn: Callable[[], tuple[str, str]],
    build_filter_fn: BuildFilterFn,
    first_token_fn: Callable[[str | None, str], str],
    single_source_height_fn: Callable[[str, int, int], int],
    build_cpu_single_source_subcmd_fn: BuildCpuSingleSourceSubcmdFn,
) -> tuple[str, str, str, str]:
    """Build a full CPU pipeline (decode + filter + encode)."""
    input_args, map_opts, source_kind = select_cpu_input_args_fn(
        pres_url=pres_url,
        pers_url=pers_url,
        pres_h=pres_h,
        pers_h=pers_h,
    )

    cpu_encoder, enc_warn = choose_h264_encoder_fn()
    if enc_warn:
        print(enc_warn.strip())
    cpu_is_libx264 = cpu_encoder == "libx264"

    if source_kind == "mixed":
        subcmd = build_filter_fn(
            pres_h,
            pers_h,
            presenter_layout,
            target_duration=target_duration,
        )
        x264_preset = first_token_fn(args.studio_preset, "medium")
        x264_crf = first_token_fn(args.studio_crf, "23")
        if cpu_is_libx264:
            video_codec = f"-c:v {cpu_encoder} -preset {x264_preset} -crf {x264_crf} "
        else:
            video_codec = f"-c:v {cpu_encoder} -q:v 23 "
        return input_args, subcmd, video_codec, map_opts

    target_h = single_source_height_fn(source_kind, pres_h, pers_h)
    subcmd = build_cpu_single_source_subcmd_fn(
        cpu_encoder=cpu_encoder,
        cpu_is_libx264=cpu_is_libx264,
        target_h=target_h,
        args=args,
        target_duration=target_duration,
    )
    return input_args, subcmd, "", map_opts


def build_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: Any,
    input_args: str,
    target_duration: float | None = None,
    *,
    choose_h264_encoder_fn: Callable[[], tuple[str, str]],
    is_webm_input_source_fn: Callable[[str | None], bool],
    build_full_gpu_pipeline_fn: Callable[..., tuple[str, str, str, str] | None],
    build_gpu_encode_only_pipeline_fn: Callable[..., tuple[str, str, str, str] | None],
    build_cpu_pipeline_fn: Callable[..., tuple[str, str, str, str]],
) -> tuple[str, str, str, str, str]:
    """Choose the best available studio pipeline and return its components."""
    del input_args
    cpu_encoder, enc_warn = choose_h264_encoder_fn()
    if enc_warn:
        print(enc_warn.strip())
    webm_input = is_webm_input_source_fn(pres_url) or is_webm_input_source_fn(pers_url)

    full_gpu = build_full_gpu_pipeline_fn(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input=webm_input,
        target_duration=target_duration,
    )
    if full_gpu is not None:
        input_args2, subcmd, video_codec, map_opts = full_gpu
        return subcmd, video_codec, input_args2, cpu_encoder, map_opts

    gpu_enc = build_gpu_encode_only_pipeline_fn(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input=webm_input,
        target_duration=target_duration,
    )
    if gpu_enc is not None:
        input_args2, subcmd, video_codec, map_opts = gpu_enc
        return subcmd, video_codec, input_args2, cpu_encoder, map_opts

    input_args2, subcmd, video_codec, map_opts = build_cpu_pipeline_fn(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        target_duration=target_duration,
    )
    return subcmd, video_codec, input_args2, cpu_encoder, map_opts
