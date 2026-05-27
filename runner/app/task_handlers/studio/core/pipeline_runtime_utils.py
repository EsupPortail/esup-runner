"""Runtime execution helpers for studio pipeline attempts and fallback order."""

from __future__ import annotations

from typing import Any, Callable


def run_pipelines(
    *,
    pres_url_local: str | None,
    pers_url_local: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: Any,
    studio_allow_nvenc: bool,
    webm_input: bool,
    subtime: str,
    audio_bitrate: str,
    output_opts: str,
    output_path: str,
    build_full_gpu_pipeline_fn: Callable[..., tuple[str, str, str, str] | None],
    build_gpu_encode_only_pipeline_fn: Callable[..., tuple[str, str, str, str] | None],
    build_cpu_pipeline_fn: Callable[..., tuple[str, str, str, str]],
    shlex_split_fn: Callable[[str], list[str]],
    subprocess_run_fn: Callable[[list[str]], Any],
    target_duration: float | None = None,
) -> int:
    """Execute studio pipeline attempts in fallback order until one succeeds."""

    def run_attempt(
        label: str,
        input_args: str,
        subcmd: str,
        video_codec: str,
        map_opts: str,
    ) -> int:
        vc = video_codec
        sc = subcmd
        if "libx264" in (vc or ""):
            vc = (
                vc
                + "-profile:v high -pix_fmt yuv420p "
                + '-sc_threshold 0 -force_key_frames "expr:gte(t,n_forced*1)" '
            )
        if vc == "" and "-c:v libx264" in sc:
            sc = sc.replace(
                "-c:v libx264 ",
                '-c:v libx264 -profile:v high -pix_fmt yuv420p -sc_threshold 0 -force_key_frames "expr:gte(t,n_forced*1)" ',
            )

        ffmpeg_cmd = (
            f"ffmpeg -hide_banner -y -threads 0 "
            f"{input_args}{subtime}{sc} "
            f"{map_opts}{vc}-c:a aac -ar 48000 -b:a {audio_bitrate} "
            f'{output_opts}"{output_path}"'
        )
        print(f"[{label}] {ffmpeg_cmd}")
        result = subprocess_run_fn(shlex_split_fn(ffmpeg_cmd))
        return int(result.returncode)

    if not studio_allow_nvenc and (args.encoding_type or "CPU").upper() == "GPU":
        args.force_cpu = "true"

    full_gpu = build_full_gpu_pipeline_fn(
        pres_url_local,
        pers_url_local,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        target_duration=target_duration,
    )
    if full_gpu is not None:
        ia, sc, vc, mo = full_gpu
        rc = run_attempt("FULL_GPU", ia, sc, vc, mo)
        if rc == 0:
            return 0
        print("FULL_GPU failed; retrying with CPU decode + NVENC encode")

    gpu_enc = build_gpu_encode_only_pipeline_fn(
        pres_url_local,
        pers_url_local,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        target_duration=target_duration,
    )
    if gpu_enc is not None:
        ia, sc, vc, mo = gpu_enc
        rc = run_attempt("GPU_ENC_ONLY", ia, sc, vc, mo)
        if rc == 0:
            return 0
        print("GPU_ENC_ONLY failed; retrying full CPU pipeline")

    ia, sc, vc, mo = build_cpu_pipeline_fn(
        pres_url_local,
        pers_url_local,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        target_duration=target_duration,
    )
    return run_attempt("CPU", ia, sc, vc, mo)
