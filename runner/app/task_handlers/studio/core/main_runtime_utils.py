"""Default runtime wiring for the top-level studio flow."""

from __future__ import annotations

import os
import shlex
import subprocess
from functools import lru_cache
from typing import Any, cast

from . import (
    download_runtime_utils,
    ffmpeg_command_utils,
    ffmpeg_runtime_utils,
    main_orchestration_utils,
    metadata_runtime_utils,
    pipeline_building_utils,
    pipeline_runtime_utils,
    source_utils,
)
from .runtime_args_utils import parse_args


def _build_input_args(
    pres_url: str | None,
    pers_url: str | None,
    args: Any,
) -> tuple[str, int, int]:
    return pipeline_building_utils.build_input_args(
        pres_url,
        pers_url,
        args,
        probe_height_fn=lambda src: ffmpeg_runtime_utils.probe_height(
            src, subprocess_module=subprocess
        ),
    )


def _is_webm_input_source(source: str | None) -> bool:
    return source_utils.is_webm_input_source(
        source,
        probe_codec_fn=lambda src: ffmpeg_runtime_utils.probe_codec(
            src, subprocess_module=subprocess
        ),
    )


def _set_cuda_env(args: Any) -> None:
    ffmpeg_runtime_utils.set_cuda_env(args, os_module=os)


@lru_cache(maxsize=1)
def _nvenc_preflight() -> tuple[bool, str]:
    return ffmpeg_runtime_utils.nvenc_preflight(subprocess_module=subprocess)


def _choose_cuda_decoder_for(source: str) -> str | None:
    return ffmpeg_runtime_utils.choose_cuda_decoder_for(
        source,
        probe_codec_fn=lambda src: ffmpeg_runtime_utils.probe_codec(
            src, subprocess_module=subprocess
        ),
        has_decoder_fn=lambda decoder: ffmpeg_runtime_utils.has_decoder(
            decoder,
            subprocess_module=subprocess,
        ),
    )


def _choose_h264_encoder() -> tuple[str, str]:
    return ffmpeg_runtime_utils.choose_h264_encoder(
        has_encoder_fn=lambda encoder: ffmpeg_runtime_utils.has_encoder(
            encoder,
            subprocess_module=subprocess,
        )
    )


def _run_pipelines(**kwargs: Any) -> int:
    def _build_full_gpu_pipeline(
        pres_url: str | None,
        pers_url: str | None,
        pres_h: int,
        pers_h: int,
        presenter_layout: str,
        args: Any,
        webm_input: bool,
    ) -> tuple[str, str, str, str] | None:
        return pipeline_building_utils.build_full_gpu_pipeline(
            pres_url,
            pers_url,
            pres_h,
            pers_h,
            presenter_layout,
            args,
            webm_input,
            prepare_full_gpu_inputs_fn=lambda **prep_kwargs: pipeline_building_utils.prepare_full_gpu_inputs(
                prep_kwargs["pres_url"],
                prep_kwargs["pers_url"],
                prep_kwargs["pres_h"],
                prep_kwargs["presenter_layout"],
                prep_kwargs["args"],
                is_gpu_requested_fn=pipeline_building_utils.is_gpu_requested,
                set_cuda_env_fn=_set_cuda_env,
                nvenc_preflight_fn=_nvenc_preflight,
                choose_cuda_decoder_for_fn=_choose_cuda_decoder_for,
                filter_available_fn=lambda name: ffmpeg_runtime_utils.filter_available(
                    name,
                    subprocess_module=subprocess,
                ),
                even_or_default_height_fn=ffmpeg_command_utils.even_or_default_height,
            ),
            build_full_gpu_filtergraph_fn=ffmpeg_command_utils.build_full_gpu_filtergraph,
            build_nvenc_video_codec_fn=lambda webm_input: ffmpeg_command_utils.build_nvenc_video_codec(
                args,
                webm_input=webm_input,
            ),
        )

    def _build_gpu_encode_only_pipeline(
        pres_url: str | None,
        pers_url: str | None,
        pres_h: int,
        pers_h: int,
        presenter_layout: str,
        args: Any,
        webm_input: bool,
    ) -> tuple[str, str, str, str] | None:
        return pipeline_building_utils.build_gpu_encode_only_pipeline(
            pres_url,
            pers_url,
            pres_h,
            pers_h,
            presenter_layout,
            args,
            webm_input,
            is_gpu_requested_fn=pipeline_building_utils.is_gpu_requested,
            set_cuda_env_fn=_set_cuda_env,
            nvenc_preflight_fn=_nvenc_preflight,
            build_filter_fn=ffmpeg_command_utils.build_filter,
            build_nvenc_video_codec_fn=lambda webm_input: ffmpeg_command_utils.build_nvenc_video_codec(
                args,
                webm_input=webm_input,
            ),
        )

    def _build_cpu_pipeline(
        pres_url: str | None,
        pers_url: str | None,
        pres_h: int,
        pers_h: int,
        presenter_layout: str,
        args: Any,
    ) -> tuple[str, str, str, str]:
        return pipeline_building_utils.build_cpu_pipeline(
            pres_url,
            pers_url,
            pres_h,
            pers_h,
            presenter_layout,
            args,
            select_cpu_input_args_fn=pipeline_building_utils.select_cpu_input_args,
            choose_h264_encoder_fn=_choose_h264_encoder,
            build_filter_fn=ffmpeg_command_utils.build_filter,
            first_token_fn=ffmpeg_command_utils.first_token,
            single_source_height_fn=pipeline_building_utils.single_source_height,
            build_cpu_single_source_subcmd_fn=ffmpeg_command_utils.build_cpu_single_source_subcmd,
        )

    return pipeline_runtime_utils.run_pipelines(
        **kwargs,
        build_full_gpu_pipeline_fn=_build_full_gpu_pipeline,
        build_gpu_encode_only_pipeline_fn=_build_gpu_encode_only_pipeline,
        build_cpu_pipeline_fn=_build_cpu_pipeline,
        shlex_split_fn=shlex.split,
        subprocess_run_fn=subprocess.run,
    )


def build_main_flow_context() -> main_orchestration_utils.MainFlowContext:
    """Build default dependencies for the end-to-end studio flow."""
    return main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=metadata_runtime_utils.load_mediapackage_and_layout,
        load_clip_times_fn=metadata_runtime_utils.load_clip_times,
        materialize_source_fn=download_runtime_utils.materialize_source,
        is_webm_input_source_fn=_is_webm_input_source,
        build_input_args_fn=_build_input_args,
        build_subtime_fn=ffmpeg_command_utils.build_subtime,
        run_pipelines_fn=_run_pipelines,
    )


def main() -> int:
    """Run the studio script end to end and return an exit code."""
    return cast(
        int,
        main_orchestration_utils.run_main_flow(
            parse_args(),
            context=build_main_flow_context(),
        ),
    )
