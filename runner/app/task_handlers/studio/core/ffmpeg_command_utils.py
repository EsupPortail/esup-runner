"""FFmpeg command-fragment helpers for studio pipelines."""

from __future__ import annotations

from typing import Any


def first_token(value: str | None, default: str) -> str:
    """Return the first whitespace-separated token or a default fallback."""
    if not value:
        return default
    token = str(value).strip().split()[0]
    return token or default


def build_filter(pres_h: int, pers_h: int, presenter: str) -> str:
    """Build the FFmpeg filter graph for the requested studio layout."""
    if presenter not in {"mid", "piph", "pipb"}:
        presenter = "mid"
    if presenter in ("piph", "pipb") and pres_h > 0 and pers_h > 0:
        height = pres_h if (pres_h % 2) == 0 else pres_h + 1
        sh = height // 4
        sh = sh if (sh % 2) == 0 else sh + 1
        overlay_pos = "W-w-10:H-h-10" if presenter == "pipb" else "W-w-10:10"
        return (
            " -filter_complex "
            + f'"[0:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{height},setsar=1,format=yuv420p[pres];'
            + f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{sh},setsar=1,format=yuv420p[pip];"
            + f"[pres][pip]overlay={overlay_pos}:eof_action=pass:shortest=0:repeatlast=0[tmp];"
            + "[tmp]format=yuv420p,setsar=1[vout]"
            + '" '
        )
    if presenter == "mid" and pres_h > 0 and pers_h > 0:
        min_h = pres_h if pres_h <= pers_h else pers_h
        height = min_h if (min_h % 2) == 0 else min_h + 1
        return (
            " -filter_complex "
            + f'"[0:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{height},setsar=1,format=yuv420p[left];'
            + f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{height},setsar=1,format=yuv420p[right];"
            + "[left][right]hstack=inputs=2[tmp];"
            + '[tmp]format=yuv420p,setsar=1[vout]" '
        )
    return " "


def build_subtime(clip_begin: float | None, clip_end: float | None) -> str:
    """Build FFmpeg seek arguments from optional clip times."""
    if clip_begin is not None and clip_end is not None and clip_end > clip_begin:
        duration = clip_end - clip_begin
        return f"-ss {clip_begin:.3f} -t {duration:.3f} "
    if clip_begin is not None:
        return f"-ss {clip_begin:.3f} "
    if clip_end is not None:
        return f"-to {clip_end:.3f} "
    return ""


def build_nvenc_video_codec(args: Any, *, webm_input: bool) -> str:
    """Build the NVENC video codec options string for studio encoding."""
    nvenc_preset = first_token(args.studio_preset, "p4")
    nvenc_cq = first_token(args.studio_crf, "")
    rc_opt = f"-preset {nvenc_preset} "
    if nvenc_cq:
        rc_opt += f"-cq {nvenc_cq} "
    if webm_input:
        rc_opt += "-rc cbr -cbr 1 -spatial-aq 1 -aq-strength 8 -temporal-aq 1 -qmin 0 -qmax 35 "
    return f"-c:v h264_nvenc {rc_opt}-profile:v high -pix_fmt yuv420p "


def even_or_default_height(height: int, default: int) -> int:
    """Return an even output height, falling back to a default value."""
    value = height or default
    return value if (value % 2) == 0 else value + 1


def build_full_gpu_filtergraph(
    *,
    presenter_layout: str,
    height: int,
    pip_h: int,
    overlay_pos: str,
) -> str:
    """Build the filter graph used by the full GPU studio pipeline."""
    if presenter_layout == "mid":
        return (
            f' -filter_complex "[0:v]settb=AVTB,setpts=PTS-STARTPTS,scale_cuda=-2:{height}:format=nv12,hwdownload,format=yuv420p,fps=30[l];'
            f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,scale_cuda=-2:{height}:format=nv12,hwdownload,format=yuv420p,fps=30[r];"
            f'[l][r]hstack=inputs=2,format=yuv420p,setsar=1[vout]" '
        )
    return (
        f' -filter_complex "[0:v]settb=AVTB,setpts=PTS-STARTPTS,scale_cuda=-2:{height}:format=nv12[pres];'
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,scale_cuda=-2:{pip_h}:format=nv12[pip];"
        f'[pres][pip]overlay_cuda={overlay_pos}:eof_action=pass:shortest=0:repeatlast=0,hwdownload,format=yuv420p,fps=30,setsar=1[vout]" '
    )


def build_cpu_single_source_subcmd(
    cpu_encoder: str,
    cpu_is_libx264: bool,
    target_h: int,
    args: Any,
) -> str:
    """Build filter and codec options for a single-source CPU path."""
    x264_preset = first_token(args.studio_preset, "slow")
    x264_crf = first_token(args.studio_crf, "20")
    subcmd = (
        f' -vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{target_h},format=yuv420p,setsar=1" '
        f"-c:v {cpu_encoder} "
    )
    if cpu_is_libx264:
        subcmd += f"-preset {x264_preset} -crf {x264_crf} "
    else:
        subcmd += "-q:v 23 "
    return subcmd
