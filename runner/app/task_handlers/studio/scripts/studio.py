#!/usr/bin/env python3
"""
Studio video generator.
Given an OpenCast mediapackage XML URL, optionally a presenter layout override,
and optional SMIL cutting, produce a single MP4 that serves as input to the encoding pipeline.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from functools import lru_cache


def fetch_text(url: str) -> str:
    import requests  # type: ignore[import-untyped]

    r = requests.get(url, timeout=(10, 180))
    r.raise_for_status()
    return str(r.text)


def parse_mediapackage(
    xml_text: str,
) -> tuple[str | None, str | None, str, str | None]:
    ns = {"mp": "http://mediapackage.opencastproject.org"}
    root = ET.fromstring(xml_text)
    presenter_layout = root.attrib.get("presenter", "mid")

    presentation_url = None
    presenter_url = None
    media = root.find("mp:media", ns)
    if media is not None:
        for track in media.findall("mp:track", ns):
            t = track.attrib.get("type", "")
            url_el = track.find("mp:url", ns)
            url_val = url_el.text if url_el is not None else None
            if t == "presentation/source":
                presentation_url = url_val
            elif t == "presenter/source":
                presenter_url = url_val

    smil_url = None
    metadata = root.find("mp:metadata", ns)
    if metadata is not None:
        for cat in metadata.findall("mp:catalog", ns):
            if cat.attrib.get("type") == "smil/cutting":
                url_el = cat.find("mp:url", ns)
                if url_el is not None and url_el.text:
                    smil_url = url_el.text

    return presentation_url, presenter_url, presenter_layout, smil_url


def parse_smil_cut(smil_text: str) -> tuple[float | None, float | None]:
    try:
        root = ET.fromstring(smil_text)
        # Find first <video clipBegin="..." clipEnd="..." />
        for el in root.iter():
            if el.tag.endswith("video"):
                begin_raw = el.attrib.get("clipBegin")
                end_raw = el.attrib.get("clipEnd")
                return parse_time(begin_raw), parse_time(end_raw)
        return None, None
    except Exception:
        return None, None


def parse_time(val: str | None) -> float | None:
    if not val:
        return None
    if val.endswith("s"):
        try:
            return float(val[:-1])
        except Exception:
            return None
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", val)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        s = float(m.group(3))
        return h * 3600 + mi * 60 + s
    return None


def _first_token(val: str | None, default: str) -> str:
    """Return the first whitespace-separated token or a default fallback."""
    if not val:
        return default
    token = str(val).strip().split()[0]
    return token or default


def _download_allowed_hosts_from_env() -> list[str]:
    allowed_hosts_raw = os.getenv("DOWNLOAD_ALLOWED_HOSTS", "")
    return [h.strip().lower().rstrip(".") for h in allowed_hosts_raw.split(",") if h.strip()]


def _download_allow_private_networks_from_env() -> bool:
    allow_private_raw = os.getenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS", "true")
    return allow_private_raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _host_is_allowed(host: str, allowed_hosts: list[str]) -> bool:
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _host_resolves_to_public_ip(host: str) -> tuple[bool, str]:
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = sorted({info[4][0] for info in infos if info and info[4]})
    except Exception:
        ips = []

    if not ips:
        return False, "Download host cannot be resolved"

    for ip in ips:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False, f"Download host resolved to invalid address: {ip}"
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False, f"Download host resolves to private address: {ip}"

    return True, ""


def _download_http_source(
    url: str, work_dir: str, label: str, parsed: urllib.parse.ParseResult
) -> str:
    os.makedirs(work_dir, exist_ok=True)
    base = os.path.basename(parsed.path) or f"{label}.mp4"
    base = base.split("?")[0] or f"{label}.mp4"
    local_path = os.path.join(work_dir, base)

    if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path

    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        with open(local_path, "wb") as f:
            f.write(data)
        print(f"Downloaded remote source to {local_path}")
        return local_path
    except Exception as exc:
        print(f"Failed to download remote source {url}: {exc}")
        return url


def _materialize_source(url: str | None, work_dir: str, label: str) -> str | None:
    """Download remote URL to work_dir if it is HTTP(S); otherwise return original path."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme not in ("http", "https"):
        print(f"Unsupported URL scheme for {label}: {scheme}")
        return None
    if scheme not in ("http", "https"):
        return url

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        print(f"Invalid source URL host for {label}")
        return None

    allowed_hosts = _download_allowed_hosts_from_env()
    if allowed_hosts and not _host_is_allowed(host, allowed_hosts):
        print(f"Download host not allowed for {label}: {host}")
        return None

    allow_private = _download_allow_private_networks_from_env()
    if not allow_private and host in {"localhost"}:
        print(f"Download host not allowed for {label}: {host}")
        return None

    if not allow_private:
        ok, reason = _host_resolves_to_public_ip(host)
        if not ok:
            print(f"{reason} for {label}: {host}")
            return None

    return _download_http_source(url, work_dir, label, parsed)


def _has_encoder(encoder: str) -> bool:
    """Return True if ffmpeg exposes the given encoder."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if res.returncode != 0 or not res.stdout:
            return False
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == encoder:
                return True
    except Exception:
        return False
    return False


def _has_decoder(decoder: str) -> bool:
    """Return True if ffmpeg exposes the given decoder."""
    try:
        res = subprocess.run(
            ["ffmpeg", "-hide_banner", "-decoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if res.returncode != 0 or not res.stdout:
            return False
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == decoder:
                return True
    except Exception:
        return False
    return False


def probe_codec(source: str) -> str:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "json",
        source,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        info = json.loads(res.stdout.decode("utf-8"))
        if info.get("streams"):
            return str(info["streams"][0].get("codec_name") or "")
    except Exception:
        pass
    return ""


def _choose_cuda_decoder_for(source: str) -> str | None:
    """Return the best *_cuvid decoder for the given source, or None."""
    codec = (probe_codec(source) or "").lower()
    # Be conservative: CUVID decode for VP8/VP9/AV1 is frequently less stable across
    # driver/FFmpeg builds and can produce green/pink corruption. Prefer CPU decode.
    if codec in {"vp8", "vp9", "av1"}:
        return None
    mapping = {
        "h264": "h264_cuvid",
        "hevc": "hevc_cuvid",
        "mpeg2video": "mpeg2_cuvid",
    }
    dec = mapping.get(codec)
    if dec and _has_decoder(dec):
        return dec
    return None


def _choose_h264_encoder() -> tuple[str, str]:
    """Prefer libx264, fallback to builtin h264 with a warning message."""
    if _has_encoder("libx264"):
        return "libx264", ""
    return "h264", "libx264 missing; using h264 fallback\n"


@lru_cache(maxsize=1)
def _nvenc_preflight() -> tuple[bool, str]:
    """Return (ok, details) for NVENC availability on this host.

    Catches common production failures like driver/API mismatches.
    """
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        # Keep a reasonable frame size to avoid NVENC minimum-dimension constraints.
        "color=c=black:s=640x360:r=30",
        "-t",
        "0.1",
        "-an",
        "-pix_fmt",
        "yuv420p",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        out = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if out.returncode == 0:
            return True, ""
        details = "NVENC preflight failed (ffmpeg exit %s)\n" % out.returncode
        if out.stdout:
            details += out.stdout
        return False, details
    except FileNotFoundError:
        return False, "ffmpeg command not found; cannot use NVENC\n"
    except Exception as exc:
        return False, f"NVENC preflight exception: {exc}\n"


def probe_height(source: str) -> int:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=height",
        "-of",
        "json",
        source,
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        info = json.loads(res.stdout.decode("utf-8"))
        if info.get("streams"):
            return int(info["streams"][0].get("height") or 0)
    except Exception:
        pass
    return 0


def build_filter(pres_h: int, pers_h: int, presenter: str) -> str:
    if presenter not in {"mid", "piph", "pipb"}:
        presenter = "mid"
    if presenter in ("piph", "pipb") and pres_h > 0 and pers_h > 0:
        height = pres_h if (pres_h % 2) == 0 else pres_h + 1
        sh = height // 4
        # yuv420p requires even dimensions; keep the PiP height even.
        sh = sh if (sh % 2) == 0 else sh + 1
        overlay_pos = "W-w-10:H-h-10" if presenter == "pipb" else "W-w-10:10"
        return (
            " -filter_complex "
            + f'"[0:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{height},setsar=1,format=yuv420p[pres];'
            + f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{sh},setsar=1,format=yuv420p[pip];"
            + f"[pres][pip]overlay={overlay_pos}:shortest=1[tmp];"
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


def filter_available(name: str) -> bool:
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        text = out.stdout.decode(errors="ignore")
        return name in text
    except Exception:
        return False


def _set_cuda_env(args: argparse.Namespace) -> None:
    if args.cuda_visible_devices:
        os.environ["CUDA_VISIBLE_DEVICES"] = args.cuda_visible_devices
    if args.cuda_device_order:
        os.environ["CUDA_DEVICE_ORDER"] = args.cuda_device_order
    if args.cuda_path:
        os.environ["CUDA_PATH"] = args.cuda_path


def build_input_args(pres_url, pers_url, args):
    input_args = ""
    pres_h = pers_h = 0
    if pres_url and pers_url:
        input_args = f'-i "{pres_url}" -i "{pers_url}" '
        pres_h = probe_height(pres_url)
        pers_h = probe_height(pers_url)
    elif pres_url:
        input_args = f'-i "{pres_url}" '
        pres_h = probe_height(pres_url)
    elif pers_url:
        input_args = f'-i "{pers_url}" '
        pers_h = probe_height(pers_url)
    else:
        raise ValueError("No media tracks")
    return input_args, pres_h, pers_h


def build_subtime(clip_begin, clip_end):
    if clip_begin is not None and clip_end is not None and clip_end > clip_begin:
        duration = clip_end - clip_begin
        return f"-ss {clip_begin:.3f} -t {duration:.3f} "
    if clip_begin is not None:
        return f"-ss {clip_begin:.3f} "
    if clip_end is not None:
        return f"-to {clip_end:.3f} "
    return ""


def build_pipeline(pres_url, pers_url, pres_h, pers_h, presenter_layout, args, input_args):
    cpu_encoder, enc_warn = _choose_h264_encoder()
    if enc_warn:
        print(enc_warn.strip())

    full_gpu = _build_full_gpu_pipeline(pres_url, pers_url, pres_h, pers_h, presenter_layout, args)
    if full_gpu is not None:
        input_args2, subcmd, video_codec = full_gpu
        return subcmd, video_codec, input_args2, cpu_encoder

    gpu_enc = _build_gpu_encode_only_pipeline(
        pres_url, pers_url, pres_h, pers_h, presenter_layout, args
    )
    if gpu_enc is not None:
        input_args2, subcmd, video_codec = gpu_enc
        return subcmd, video_codec, input_args2, cpu_encoder

    input_args2, subcmd, video_codec = _build_cpu_pipeline(
        pres_url, pers_url, pres_h, pers_h, presenter_layout, args
    )
    return subcmd, video_codec, input_args2, cpu_encoder


def _is_gpu_requested(args: argparse.Namespace) -> bool:
    if (args.encoding_type or "CPU").upper() != "GPU":
        return False
    return (args.force_cpu or "").lower() not in ("true", "1", "yes")


def _build_nvenc_video_codec(args: argparse.Namespace) -> str:
    nvenc_preset = _first_token(args.studio_preset, "p4")
    nvenc_cq = _first_token(args.studio_crf, "")
    rc_opt = f"-preset {nvenc_preset} "
    if nvenc_cq:
        rc_opt += f"-cq {nvenc_cq} "
    return f"-c:v h264_nvenc {rc_opt}-profile:v high -pix_fmt yuv420p "


def _even_or_default_height(height: int, default: int) -> int:
    h = height or default
    return h if (h % 2) == 0 else h + 1


def _prepare_full_gpu_inputs(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, int, int, str] | None:
    if not (pres_url and pers_url):
        return None
    if not _is_gpu_requested(args):
        return None

    _set_cuda_env(args)
    nvenc_ok, nvenc_details = _nvenc_preflight()
    if not nvenc_ok:
        if nvenc_details:
            print(nvenc_details.strip())
        return None

    pres_dec = _choose_cuda_decoder_for(pres_url)
    pers_dec = _choose_cuda_decoder_for(pers_url)
    have_scale_npp = filter_available("scale_npp")
    need_overlay = presenter_layout != "mid"
    have_overlay_cuda = (not need_overlay) or filter_available("overlay_cuda")
    if not (pres_dec and pers_dec and have_scale_npp and have_overlay_cuda):
        return None

    hwdev = int(args.hwaccel_device or 0)
    input_args = (
        f"-hwaccel_device {hwdev} -hwaccel cuda -hwaccel_output_format cuda "
        f'-c:v {pres_dec} -i "{pres_url}" '
        f"-hwaccel_device {hwdev} -hwaccel cuda -hwaccel_output_format cuda "
        f'-c:v {pers_dec} -i "{pers_url}" '
    )

    height = _even_or_default_height(pres_h, 720)
    pip_h = (height // 4) if presenter_layout in ("piph", "pipb") else height
    pip_h = _even_or_default_height(pip_h, pip_h)
    overlay_pos = "W-w-10:H-h-10" if presenter_layout == "pipb" else "W-w-10:10"
    return input_args, height, pip_h, overlay_pos


def _build_full_gpu_filtergraph(
    *, presenter_layout: str, height: int, pip_h: int, overlay_pos: str
) -> str:
    # Filtergraph:
    # - For mid: still uses software hstack after hwdownload.
    # - For piph/pipb: uses overlay_cuda then hwdownload.
    if presenter_layout == "mid":
        return (
            f' -filter_complex "[0:v]settb=AVTB,setpts=PTS-STARTPTS,scale_npp=-2:{height}:format=nv12,hwdownload,format=yuv420p,fps=30[l];'
            f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,scale_npp=-2:{height}:format=nv12,hwdownload,format=yuv420p,fps=30[r];"
            f'[l][r]hstack=inputs=2,format=yuv420p,setsar=1[vout]" '
        )
    return (
        f' -filter_complex "[0:v]settb=AVTB,setpts=PTS-STARTPTS,scale_npp=-2:{height}:format=nv12[pres];'
        f"[1:v]settb=AVTB,setpts=PTS-STARTPTS,scale_npp=-2:{pip_h}:format=nv12[pip];"
        f'[pres][pip]overlay_cuda={overlay_pos}:shortest=1,hwdownload,format=yuv420p,fps=30,setsar=1[vout]" '
    )


def _build_full_gpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, str, str] | None:
    """Build a pipeline that uses GPU decode (CUVID) + GPU encode (NVENC) when possible.

    Returns (input_args, subcmd, video_codec) or None if not possible.
    """
    prepared = _prepare_full_gpu_inputs(
        pres_url=pres_url,
        pers_url=pers_url,
        pres_h=pres_h,
        presenter_layout=presenter_layout,
        args=args,
    )
    if prepared is None:
        return None
    input_args, height, pip_h, overlay_pos = prepared
    gpu_filter = _build_full_gpu_filtergraph(
        presenter_layout=presenter_layout,
        height=height,
        pip_h=pip_h,
        overlay_pos=overlay_pos,
    )
    return input_args, gpu_filter, _build_nvenc_video_codec(args)


def _build_gpu_encode_only_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, str, str] | None:
    """Build a CPU decode/filter + NVENC encode pipeline.

    Returns (input_args, subcmd, video_codec) or None if NVENC not available.
    """

    if not _is_gpu_requested(args):
        return None

    _set_cuda_env(args)
    nvenc_ok, nvenc_details = _nvenc_preflight()
    if not nvenc_ok:
        if nvenc_details:
            print(nvenc_details.strip())
        return None

    input_args = ""
    if pres_url and pers_url:
        input_args = f'-i "{pres_url}" -i "{pers_url}" '
        subcmd = build_filter(pres_h, pers_h, presenter_layout)
    elif pres_url:
        input_args = f'-i "{pres_url}" '
        target_h = pres_h or 720
        subcmd = f' -vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{target_h},format=yuv420p,setsar=1" '
    elif pers_url:
        input_args = f'-i "{pers_url}" '
        target_h = pers_h or 720
        subcmd = f' -vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{target_h},format=yuv420p,setsar=1" '
    else:
        return None

    return input_args, subcmd, _build_nvenc_video_codec(args)


def _load_mediapackage_and_layout(
    args: argparse.Namespace,
) -> tuple[str | None, str | None, str, str | None]:
    xml_text = fetch_text(args.xml_url)
    pres_url, pers_url, presenter_layout, smil_url = parse_mediapackage(xml_text)
    if args.presenter:
        presenter_layout = args.presenter
    return pres_url, pers_url, presenter_layout, smil_url


def _build_map_opts(pres_url_local: str | None, pers_url_local: str | None) -> str:
    if pres_url_local and pers_url_local:
        return '-map "[vout]" -map 0:a? '
    return "-map 0:v -map 0:a? "


def _build_cpu_pipeline(
    pres_url: str | None,
    pers_url: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
) -> tuple[str, str, str]:
    """Build a full CPU pipeline (decode + filter + encode)."""

    input_args = ""
    if pres_url and pers_url:
        input_args = f'-i "{pres_url}" -i "{pers_url}" '
    elif pres_url:
        input_args = f'-i "{pres_url}" '
    elif pers_url:
        input_args = f'-i "{pers_url}" '
    else:
        raise ValueError("No media tracks")

    cpu_encoder, enc_warn = _choose_h264_encoder()
    if enc_warn:
        print(enc_warn.strip())
    cpu_is_libx264 = cpu_encoder == "libx264"

    if pres_url and pers_url:
        subcmd = build_filter(pres_h, pers_h, presenter_layout)
        x264_preset = _first_token(args.studio_preset, "medium")
        x264_crf = _first_token(args.studio_crf, "23")
    else:
        target_h = (pres_h or pers_h) or 720
        x264_preset = _first_token(args.studio_preset, "slow")
        x264_crf = _first_token(args.studio_crf, "20")
        subcmd = (
            f' -vf "settb=AVTB,setpts=PTS-STARTPTS,fps=30,scale=-2:{target_h},format=yuv420p,setsar=1" '
            f"-c:v {cpu_encoder} "
        )
        if cpu_is_libx264:
            subcmd += f"-preset {x264_preset} -crf {x264_crf} "
        else:
            subcmd += "-q:v 23 "
        return input_args, subcmd, ""

    if cpu_is_libx264:
        video_codec = f"-c:v {cpu_encoder} -preset {x264_preset} -crf {x264_crf} "
    else:
        video_codec = f"-c:v {cpu_encoder} -q:v 23 "
    return input_args, subcmd, video_codec


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Studio base video generator")
    parser.add_argument("--xml-url", required=True, help="Mediapackage XML URL")
    parser.add_argument("--base-dir", required=True, help="Base directory for input files")
    parser.add_argument(
        "--work-dir", required=True, help="Working directory for intermediate files"
    )
    parser.add_argument("--output-file", required=True, help="Output video file path")
    parser.add_argument("--debug", required=False, help="Run script in debug mode")
    parser.add_argument(
        "--presenter", required=False, help="Override presenter layout (mid, piph, pipb)"
    )
    parser.add_argument(
        "--encoding-type", required=False, help="CPU or GPU encoding type (Ex: CPU)"
    )
    parser.add_argument(
        "--hwaccel-device", required=False, help="HW acceleration device index (Ex: 0)"
    )
    parser.add_argument(
        "--cuda-visible-devices", required=False, help="CUDA visible devices (Ex: 0,1)"
    )
    parser.add_argument(
        "--cuda-device-order", required=False, help="CUDA device order (Ex: PCI_BUS_ID)"
    )
    parser.add_argument("--cuda-path", required=False, help="CUDA installation path")
    parser.add_argument(
        "--force-cpu", required=False, help="Force CPU pipeline even if GPU requested"
    )
    parser.add_argument("--studio-crf", required=False, help="CRF for libx264/NVENC if applicable")
    parser.add_argument("--studio-preset", required=False, help="x264 preset or NVENC preset")
    parser.add_argument("--studio-audio-bitrate", required=False, help="Audio bitrate, e.g., 128k")
    parser.add_argument(
        "--studio-allow-nvenc",
        required=False,
        help="Allow NVENC in studio generation even for WebM/VP8/VP9/AV1 inputs (default: false)",
    )
    return parser


def _load_clip_times(smil_url: str | None) -> tuple[float | None, float | None]:
    if not smil_url:
        return None, None
    try:
        smil_text = fetch_text(smil_url)
        return parse_smil_cut(smil_text)
    except Exception:
        return None, None


def _run_pipelines(
    *,
    pres_url_local: str | None,
    pers_url_local: str | None,
    pres_h: int,
    pers_h: int,
    presenter_layout: str,
    args: argparse.Namespace,
    studio_allow_nvenc: bool,
    subtime: str,
    map_opts: str,
    audio_bitrate: str,
    output_opts: str,
    output_path: str,
) -> int:
    def _run_attempt(label: str, input_args: str, subcmd: str, video_codec: str) -> int:
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
        res = subprocess.run(shlex.split(ffmpeg_cmd))
        return int(res.returncode)

    if not studio_allow_nvenc and (args.encoding_type or "CPU").upper() == "GPU":
        args.force_cpu = "true"

    full_gpu = _build_full_gpu_pipeline(
        pres_url_local, pers_url_local, pres_h, pers_h, presenter_layout, args
    )
    if full_gpu is not None:
        ia, sc, vc = full_gpu
        rc = _run_attempt("FULL_GPU", ia, sc, vc)
        if rc == 0:
            return 0
        print("FULL_GPU failed; retrying with CPU decode + NVENC encode")

    gpu_enc = _build_gpu_encode_only_pipeline(
        pres_url_local, pers_url_local, pres_h, pers_h, presenter_layout, args
    )
    if gpu_enc is not None:
        ia, sc, vc = gpu_enc
        rc = _run_attempt("GPU_ENC_ONLY", ia, sc, vc)
        if rc == 0:
            return 0
        print("GPU_ENC_ONLY failed; retrying full CPU pipeline")

    ia, sc, vc = _build_cpu_pipeline(
        pres_url_local, pers_url_local, pres_h, pers_h, presenter_layout, args
    )
    return _run_attempt("CPU", ia, sc, vc)


def _main_impl(args: argparse.Namespace) -> int:
    base_dir = args.base_dir
    work_dir = os.path.join(base_dir, args.work_dir)
    os.makedirs(work_dir, exist_ok=True)

    pres_url, pers_url, presenter_layout, smil_url = _load_mediapackage_and_layout(args)
    clip_begin, clip_end = _load_clip_times(smil_url)

    pres_url_local = _materialize_source(pres_url, work_dir, "presentation")
    pers_url_local = _materialize_source(pers_url, work_dir, "presenter")

    # Kept for compatibility: if false, GPU is forced off.
    studio_allow_nvenc = (args.studio_allow_nvenc or "").lower() in ("true", "1", "yes")

    try:
        _, pres_h, pers_h = build_input_args(pres_url_local, pers_url_local, args)
    except ValueError:
        print("ERROR: No media tracks in mediapackage")
        return 1

    subtime = build_subtime(clip_begin, clip_end)
    output_path = os.path.join(work_dir, args.output_file)
    audio_bitrate = args.studio_audio_bitrate or "192k"
    output_opts = "-movflags +faststart -f mp4 -fps_mode cfr -r 30 -max_muxing_queue_size 4000 "
    map_opts = _build_map_opts(pres_url_local, pers_url_local)

    return _run_pipelines(
        pres_url_local=pres_url_local,
        pers_url_local=pers_url_local,
        pres_h=pres_h,
        pers_h=pers_h,
        presenter_layout=presenter_layout,
        args=args,
        studio_allow_nvenc=studio_allow_nvenc,
        subtime=subtime,
        map_opts=map_opts,
        audio_bitrate=audio_bitrate,
        output_opts=output_opts,
        output_path=output_path,
    )


def main() -> None:
    args = _build_arg_parser().parse_args()
    sys.exit(_main_impl(args))


if __name__ == "__main__":
    main()
