#!/usr/bin/env python3
"""
Studio video generator.
Given an OpenCast mediapackage XML URL, optionally a presenter layout override,
and optional SMIL cutting, produce a single MP4 that serves as input to the encoding pipeline.
"""

import argparse
import os
import shlex
import subprocess
import sys
import urllib.parse
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

_SCRIPT_DIR = Path(__file__).resolve().parents[1] / "app" / "task_handlers" / "studio"
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

if TYPE_CHECKING:
    from app.task_handlers.studio.core import (
        download_runtime_utils,
        ffmpeg_command_utils,
        ffmpeg_runtime_utils,
        main_orchestration_utils,
        metadata_runtime_utils,
        metadata_utils,
        pipeline_building_utils,
        pipeline_runtime_utils,
        source_utils,
    )
    from app.task_handlers.studio.core.runtime_args_utils import (
        build_arg_parser as _core_build_arg_parser,
    )
else:
    try:
        from app.task_handlers.studio.core import (
            download_runtime_utils,
            ffmpeg_command_utils,
            ffmpeg_runtime_utils,
            main_orchestration_utils,
            metadata_runtime_utils,
            metadata_utils,
            pipeline_building_utils,
            pipeline_runtime_utils,
            source_utils,
        )
        from app.task_handlers.studio.core.runtime_args_utils import (
            build_arg_parser as _core_build_arg_parser,
        )
    except Exception:
        _loaded_core_module = sys.modules.get("core")
        if _loaded_core_module is not None:
            _core_file = getattr(_loaded_core_module, "__file__", "")
            _core_dir = Path(_core_file).resolve().parent if _core_file else None
            expected_core_dir = _SCRIPT_DIR / "core"
            if _core_dir != expected_core_dir:
                for module_name in list(sys.modules):
                    if module_name == "core" or module_name.startswith("core."):
                        sys.modules.pop(module_name, None)

        import core.download_runtime_utils as download_runtime_utils
        import core.ffmpeg_command_utils as ffmpeg_command_utils
        import core.ffmpeg_runtime_utils as ffmpeg_runtime_utils
        import core.main_orchestration_utils as main_orchestration_utils
        import core.metadata_runtime_utils as metadata_runtime_utils
        import core.metadata_utils as metadata_utils
        import core.pipeline_building_utils as pipeline_building_utils
        import core.pipeline_runtime_utils as pipeline_runtime_utils
        import core.source_utils as source_utils
        from core.runtime_args_utils import build_arg_parser as _core_build_arg_parser

MAX_SMIL_TIME_SECONDS = metadata_utils.MAX_SMIL_TIME_SECONDS
_WEBM_EXTENSIONS = source_utils.WEBM_EXTENSIONS
_WEBM_VIDEO_CODECS = source_utils.WEBM_VIDEO_CODECS


def _sanitize_smil_time(seconds: float | None) -> float | None:
    """Return a safe SMIL time value in seconds or None when invalid."""
    return metadata_utils.sanitize_smil_time(seconds)


def fetch_text(url: str) -> str:
    """Fetch and return text content from a URL."""
    return metadata_runtime_utils.fetch_text(url)


def parse_mediapackage(
    xml_text: str,
) -> tuple[str | None, str | None, str, str | None]:
    """Extract track URLs, presenter layout, and SMIL URL from a mediapackage XML."""
    return metadata_utils.parse_mediapackage(xml_text)


def parse_smil_cut(smil_text: str) -> tuple[float | None, float | None]:
    """Parse clip begin and end times from a SMIL cutting document."""
    return metadata_utils.parse_smil_cut(smil_text)


def parse_time(val: str | None) -> float | None:
    """Parse a SMIL time value into seconds."""
    return metadata_utils.parse_time(val)


def _first_token(val: str | None, default: str) -> str:
    """Return the first whitespace-separated token or a default fallback."""
    return ffmpeg_command_utils.first_token(val, default)


def _looks_like_webm_source(path_or_url: str | None) -> bool:
    """Return whether a source path/URL looks like a WebM media file."""
    return source_utils.looks_like_webm_source(path_or_url)


def _download_allowed_hosts_from_env() -> list[str]:
    """Return the configured allowlist of download hosts."""
    return download_runtime_utils.download_allowed_hosts_from_env()


def _download_allow_private_networks_from_env() -> bool:
    """Return whether private-network downloads are allowed."""
    return download_runtime_utils.download_allow_private_networks_from_env()


def _host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    """Return whether a host matches the configured allowlist."""
    return download_runtime_utils.host_is_allowed(host, allowed_hosts)


def _host_resolves_to_public_ip(host: str) -> tuple[bool, str]:
    """Check whether a host resolves only to public IP addresses."""
    return download_runtime_utils.host_resolves_to_public_ip(host)


def _download_http_source(
    url: str, work_dir: str, label: str, parsed: urllib.parse.ParseResult
) -> str:
    """Download an HTTP(S) source into the work directory."""
    return download_runtime_utils.download_http_source(url, work_dir, label, parsed)


def _materialize_source(url: str | None, work_dir: str, label: str) -> str | None:
    """Download remote URL to work_dir if it is HTTP(S); otherwise return original path."""
    return download_runtime_utils.materialize_source(
        url,
        work_dir,
        label,
        download_allowed_hosts_from_env_fn=_download_allowed_hosts_from_env,
        download_allow_private_networks_from_env_fn=_download_allow_private_networks_from_env,
        host_is_allowed_fn=_host_is_allowed,
        host_resolves_to_public_ip_fn=_host_resolves_to_public_ip,
        download_http_source_fn=_download_http_source,
    )


def _has_encoder(encoder: str) -> bool:
    """Return True if ffmpeg exposes the given encoder."""
    return ffmpeg_runtime_utils.has_encoder(encoder, subprocess_module=subprocess)


def _has_decoder(decoder: str) -> bool:
    """Return True if ffmpeg exposes the given decoder."""
    return ffmpeg_runtime_utils.has_decoder(decoder, subprocess_module=subprocess)


def probe_codec(source: str) -> str:
    """Probe the primary video codec name for a source file."""
    return ffmpeg_runtime_utils.probe_codec(source, subprocess_module=subprocess)


def _is_webm_input_source(source: str | None) -> bool:
    """Return whether a local/remote source should be treated as WebM input."""
    return source_utils.is_webm_input_source(
        source,
        looks_like_webm_source_fn=_looks_like_webm_source,
        probe_codec_fn=probe_codec,
    )


def _choose_cuda_decoder_for(source: str) -> str | None:
    """Return the best *_cuvid decoder for the given source, or None."""
    return ffmpeg_runtime_utils.choose_cuda_decoder_for(
        source,
        probe_codec_fn=probe_codec,
        has_decoder_fn=_has_decoder,
    )


def _choose_h264_encoder() -> tuple[str, str]:
    """Prefer libx264, fallback to builtin h264 with a warning message."""
    return ffmpeg_runtime_utils.choose_h264_encoder(has_encoder_fn=_has_encoder)


@lru_cache(maxsize=1)
def _nvenc_preflight() -> tuple[bool, str]:
    """Return (ok, details) for NVENC availability on this host.

    Catches common production failures like driver/API mismatches.
    """
    return ffmpeg_runtime_utils.nvenc_preflight(subprocess_module=subprocess)


def probe_height(source: str) -> int:
    """Probe the primary video height for a source file."""
    return ffmpeg_runtime_utils.probe_height(source, subprocess_module=subprocess)


def build_filter(pres_h: int, pers_h: int, presenter: str) -> str:
    """Build the FFmpeg filter graph for the requested studio layout."""
    return ffmpeg_command_utils.build_filter(pres_h, pers_h, presenter)


def filter_available(name: str) -> bool:
    """Return whether FFmpeg exposes a given filter."""
    return ffmpeg_runtime_utils.filter_available(name, subprocess_module=subprocess)


def _set_cuda_env(args: argparse.Namespace) -> None:
    """Populate CUDA-related environment variables from CLI arguments."""
    ffmpeg_runtime_utils.set_cuda_env(args, os_module=os)


def build_input_args(pres_url, pers_url, args):
    """Build FFmpeg input arguments and probe source heights."""
    return pipeline_building_utils.build_input_args(
        pres_url,
        pers_url,
        args,
        probe_height_fn=probe_height,
    )


def build_subtime(clip_begin, clip_end):
    """Build FFmpeg seek arguments from optional clip times."""
    return ffmpeg_command_utils.build_subtime(clip_begin, clip_end)


def build_pipeline(pres_url, pers_url, pres_h, pers_h, presenter_layout, args, input_args):
    """Choose the best available studio pipeline and return its components."""
    return pipeline_building_utils.build_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        input_args,
        choose_h264_encoder_fn=_choose_h264_encoder,
        is_webm_input_source_fn=_is_webm_input_source,
        build_full_gpu_pipeline_fn=_build_full_gpu_pipeline,
        build_gpu_encode_only_pipeline_fn=_build_gpu_encode_only_pipeline,
        build_cpu_pipeline_fn=_build_cpu_pipeline,
    )


def _is_gpu_requested(args: argparse.Namespace) -> bool:
    """Return whether GPU processing is requested and not force-disabled."""
    return pipeline_building_utils.is_gpu_requested(args)


def _build_nvenc_video_codec(args: argparse.Namespace, *, webm_input: bool) -> str:
    """Build the NVENC video codec options string for studio encoding."""
    return ffmpeg_command_utils.build_nvenc_video_codec(args, webm_input=webm_input)


def _even_or_default_height(height: int, default: int) -> int:
    """Return an even output height, falling back to a default value."""
    return ffmpeg_command_utils.even_or_default_height(height, default)


def _prepare_full_gpu_inputs(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, int, int, str] | None:
    """Prepare inputs and sizing values for a full GPU studio pipeline."""
    return pipeline_building_utils.prepare_full_gpu_inputs(
        pres_url,
        pers_url,
        pres_h,
        presenter_layout,
        args,
        is_gpu_requested_fn=_is_gpu_requested,
        set_cuda_env_fn=_set_cuda_env,
        nvenc_preflight_fn=_nvenc_preflight,
        choose_cuda_decoder_for_fn=_choose_cuda_decoder_for,
        filter_available_fn=filter_available,
        even_or_default_height_fn=_even_or_default_height,
    )


def _build_full_gpu_filtergraph(
    *, presenter_layout: str, height: int, pip_h: int, overlay_pos: str
) -> str:
    """Build the filter graph used by the full GPU studio pipeline."""
    return ffmpeg_command_utils.build_full_gpu_filtergraph(
        presenter_layout=presenter_layout,
        height=height,
        pip_h=pip_h,
        overlay_pos=overlay_pos,
    )


def _build_full_gpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
    webm_input: bool,
) -> tuple[str, str, str, str] | None:
    """Build a pipeline that uses GPU decode (CUVID) + GPU encode (NVENC) when possible.

    Returns (input_args, subcmd, video_codec, map_opts) or None if not possible.
    """
    return pipeline_building_utils.build_full_gpu_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        prepare_full_gpu_inputs_fn=_prepare_full_gpu_inputs,
        build_full_gpu_filtergraph_fn=_build_full_gpu_filtergraph,
        build_nvenc_video_codec_fn=lambda webm_input: _build_nvenc_video_codec(
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
    args: argparse.Namespace,
    webm_input: bool,
) -> tuple[str, str, str, str] | None:
    """Build a CPU decode/filter + NVENC encode pipeline.

    Returns (input_args, subcmd, video_codec, map_opts) or None if NVENC not available.
    """
    return pipeline_building_utils.build_gpu_encode_only_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        webm_input,
        is_gpu_requested_fn=_is_gpu_requested,
        set_cuda_env_fn=_set_cuda_env,
        nvenc_preflight_fn=_nvenc_preflight,
        build_filter_fn=build_filter,
        build_nvenc_video_codec_fn=lambda webm_input: _build_nvenc_video_codec(
            args,
            webm_input=webm_input,
        ),
    )


def _load_mediapackage_and_layout(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, str, str | None]:
    """Load mediapackage metadata and resolve the effective presenter layout."""
    return metadata_runtime_utils.load_mediapackage_and_layout(
        args,
        fetch_text_fn=fetch_text,
        parse_mediapackage_fn=parse_mediapackage,
    )


def _select_cpu_input_args(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
) -> tuple[str, str, str]:
    """Select CPU input args and mapping.

    Returns: (input_args, map_opts, source_kind) where source_kind is one of
    "mixed", "presentation", or "presenter".
    """
    return pipeline_building_utils.select_cpu_input_args(pres_url, pers_url, pres_h, pers_h)


def _single_source_height(source_kind: str, pres_h: int, pers_h: int) -> int:
    """Return target height for a single-source pipeline."""
    return pipeline_building_utils.single_source_height(source_kind, pres_h, pers_h)


def _build_cpu_single_source_subcmd(
    cpu_encoder: str, cpu_is_libx264: bool, target_h: int, args: argparse.Namespace
) -> str:
    """Build filter and codec options for a single-source CPU path."""
    return ffmpeg_command_utils.build_cpu_single_source_subcmd(
        cpu_encoder=cpu_encoder,
        cpu_is_libx264=cpu_is_libx264,
        target_h=target_h,
        args=args,
    )


def _build_cpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, str, str, str]:
    """Build a full CPU pipeline (decode + filter + encode)."""
    return pipeline_building_utils.build_cpu_pipeline(
        pres_url,
        pers_url,
        pres_h,
        pers_h,
        presenter_layout,
        args,
        select_cpu_input_args_fn=_select_cpu_input_args,
        choose_h264_encoder_fn=_choose_h264_encoder,
        build_filter_fn=build_filter,
        first_token_fn=_first_token,
        single_source_height_fn=_single_source_height,
        build_cpu_single_source_subcmd_fn=_build_cpu_single_source_subcmd,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the studio generator script."""
    return _core_build_arg_parser()


def _load_clip_times(smil_url: str | None) -> tuple[float | None, float | None]:
    """Load optional clip begin and end times from a SMIL URL."""
    return metadata_runtime_utils.load_clip_times(
        smil_url,
        fetch_text_fn=fetch_text,
        parse_smil_cut_fn=parse_smil_cut,
    )


def _run_pipelines(
    *,
    pres_url_local: str | None,
    pers_url_local: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
    studio_allow_nvenc: bool,
    webm_input: bool,
    subtime: str,
    audio_bitrate: str,
    output_opts: str,
    output_path: str,
) -> int:
    """Execute studio pipeline attempts in fallback order until one succeeds."""
    return pipeline_runtime_utils.run_pipelines(
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
        build_full_gpu_pipeline_fn=_build_full_gpu_pipeline,
        build_gpu_encode_only_pipeline_fn=_build_gpu_encode_only_pipeline,
        build_cpu_pipeline_fn=_build_cpu_pipeline,
        shlex_split_fn=shlex.split,
        subprocess_run_fn=subprocess.run,
    )


def _main_impl(args: argparse.Namespace) -> int:
    """Run the studio generation workflow and return an exit code."""
    context = main_orchestration_utils.MainFlowContext(
        load_mediapackage_and_layout_fn=_load_mediapackage_and_layout,
        load_clip_times_fn=_load_clip_times,
        materialize_source_fn=_materialize_source,
        is_webm_input_source_fn=_is_webm_input_source,
        build_input_args_fn=build_input_args,
        build_subtime_fn=build_subtime,
        run_pipelines_fn=_run_pipelines,
        make_dirs_fn=os.makedirs,
        path_join_fn=os.path.join,
    )
    return main_orchestration_utils.run_main_flow(args, context=context)


def main() -> None:
    """Parse CLI arguments and exit with the studio workflow status."""
    args = _build_arg_parser().parse_args()
    sys.exit(_main_impl(args))


if __name__ == "__main__":
    main()
