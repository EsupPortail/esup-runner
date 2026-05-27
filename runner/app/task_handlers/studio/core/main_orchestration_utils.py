"""Top-level orchestration helpers for the studio runtime flow."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class MainFlowContext:
    """Dependencies required to run the studio main flow."""

    load_mediapackage_and_layout_fn: Callable[[Any], tuple[str | None, str | None, str, str | None]]
    load_clip_times_fn: Callable[[str | None], tuple[float | None, float | None]]
    materialize_source_fn: Callable[[str | None, str, str], str | None]
    is_webm_input_source_fn: Callable[[str | None], bool]
    build_input_args_fn: Callable[..., tuple[str, int, int]]
    build_subtime_fn: Callable[[float | None, float | None], str]
    run_pipelines_fn: Callable[..., int]
    compute_target_duration_fn: (
        Callable[
            [str | None, str | None, float | None, float | None],
            float | None,
        ]
        | None
    ) = None
    make_dirs_fn: Callable[..., None] = os.makedirs
    path_join_fn: Callable[..., str] = os.path.join


def run_main_flow(args: Any, *, context: MainFlowContext) -> int:
    """Run the end-to-end studio workflow for parsed CLI args."""
    base_dir = args.base_dir
    work_dir = context.path_join_fn(base_dir, args.work_dir)
    context.make_dirs_fn(work_dir, exist_ok=True)

    pres_url, pers_url, presenter_layout, smil_url = context.load_mediapackage_and_layout_fn(args)
    clip_begin, clip_end = context.load_clip_times_fn(smil_url)

    pres_url_local = context.materialize_source_fn(pres_url, work_dir, "presentation")
    pers_url_local = context.materialize_source_fn(pers_url, work_dir, "presenter")
    webm_input = context.is_webm_input_source_fn(pres_url_local) or context.is_webm_input_source_fn(
        pers_url_local
    )
    if webm_input:
        print("WebM source detected: enabling WebM-specific NVENC rate-control profile")

    studio_allow_nvenc = (args.studio_allow_nvenc or "").lower() in ("true", "1", "yes")

    try:
        _, pres_h, pers_h = context.build_input_args_fn(pres_url_local, pers_url_local, args)
    except ValueError:
        print("ERROR: No media tracks in mediapackage")
        return 1

    subtime = context.build_subtime_fn(clip_begin, clip_end)
    target_duration = None
    if context.compute_target_duration_fn is not None:
        try:
            target_duration = context.compute_target_duration_fn(
                pres_url_local,
                pers_url_local,
                clip_begin,
                clip_end,
            )
        except Exception:
            target_duration = None
    output_path = context.path_join_fn(work_dir, args.output_file)
    audio_bitrate = args.studio_audio_bitrate or "192k"
    output_opts = "-movflags +faststart -f mp4 -fps_mode cfr -r 30 -max_muxing_queue_size 4000 "
    return int(
        context.run_pipelines_fn(
            pres_url_local=pres_url_local,
            pers_url_local=pers_url_local,
            pres_h=pres_h,
            pers_h=pers_h,
            presenter_layout=presenter_layout,
            args=args,
            studio_allow_nvenc=studio_allow_nvenc,
            webm_input=webm_input,
            subtime=subtime,
            audio_bitrate=audio_bitrate,
            output_opts=output_opts,
            output_path=output_path,
            target_duration=target_duration,
        )
    )
