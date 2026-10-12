"""FFmpeg ABR construction and filesystem readiness checks."""

import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from app.core.live_config import LiveSettings


def rtmp_url(base: str, path: str) -> str:
    """MediaMTX RTMP credentials use query parameters, including with FFmpeg."""
    parsed = urlsplit(base)
    credentials = {}
    if parsed.username:
        credentials = {"user": unquote(parsed.username), "pass": unquote(parsed.password or "")}
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.rsplit("@", 1)[-1],
            f"{parsed.path.rstrip('/')}/{path}",
            urlencode(credentials),
            "",
        )
    )


def build_command(
    settings: LiveSettings, input_url: str, directory: Path, *, gpu: bool, gpu_device: int = 0
) -> list[str]:
    """Build an FFmpeg argument list for adaptive H.264/AAC HLS transcoding.

    Use NVENC when gpu is true, optionally with CUDA decoding, or libx264
    otherwise. Each configured rendition receives aligned keyframes and a
    sliding playlist under directory; this function does not launch a process.
    """
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-filter_threads",
        "1",
    ]
    if gpu and settings.nvdec:
        command += ["-hwaccel", "cuda", "-hwaccel_device", str(gpu_device)]
    command += [
        "-rw_timeout",
        str(int(settings.stall_timeout * 1000000)),
        "-protocol_whitelist",
        "rtmp,rtmps,tcp,tls",
        "-f",
        "flv",
        "-i",
        input_url,
    ]
    encoder = "h264_nvenc" if gpu else "libx264"
    for index, variant in enumerate(settings.variants):
        command += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            f"-c:v:{index}",
            encoder,
            f"-preset:v:{index}",
            settings.nvenc_preset if gpu else settings.cpu_preset,
        ]
        if gpu:
            command += [f"-gpu:v:{index}", str(gpu_device), f"-forced-idr:v:{index}", "1"]
        command += [
            f"-filter:v:{index}",
            f"fps={settings.fps},scale={variant.width}:{variant.height}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
            f"-pix_fmt:v:{index}",
            "yuv420p",
            f"-b:v:{index}",
            str(variant.video_bitrate),
            f"-maxrate:v:{index}",
            str(variant.video_bitrate),
            f"-bufsize:v:{index}",
            str(variant.video_bitrate * 2),
            f"-g:v:{index}",
            str(settings.gop),
            f"-sc_threshold:v:{index}",
            "0",
            f"-force_key_frames:v:{index}",
            f"expr:gte(t,n_forced*{settings.segment_seconds})",
            f"-c:a:{index}",
            settings.audio_codec,
            f"-b:a:{index}",
            str(variant.audio_bitrate),
            f"-ac:a:{index}",
            "2",
            f"-ar:a:{index}",
            "48000",
        ]
    stream_map = " ".join(
        f"v:{index},a:{index},name:{variant.name}"
        for index, variant in enumerate(settings.variants)
    )
    command += [
        "-f",
        "hls",
        "-hls_time",
        str(settings.segment_seconds),
        "-hls_list_size",
        str(settings.playlist_size),
        "-hls_flags",
        "delete_segments+independent_segments+temp_file+discont_start",
        "-hls_start_number_source",
        "epoch_us",
        "-hls_segment_filename",
        str(directory / "%v" / "segment_%d.ts"),
        "-master_pl_name",
        "master.m3u8",
        "-var_stream_map",
        stream_map,
        str(directory / "%v" / "index.m3u8"),
    ]
    return command


def hls_ready(directory: Path, settings: LiveSettings) -> bool:
    """Require every advertised variant and recent, non-empty media segments."""
    try:
        master = (directory / "master.m3u8").read_text()
        if "#EXTM3U" not in master:
            return False
        return all(
            _variant_ready(directory, variant.name, master, settings.stall_timeout)
            for variant in settings.variants
        )
    except (OSError, UnicodeError):
        return False


def _variant_ready(directory: Path, name: str, master: str, timeout: float) -> bool:
    """Check that an advertised rendition has a fresh playlist and non-empty segments.

    Require at least two segment entries and validate the last two files.
    Filesystem or decoding failures propagate to hls_ready.
    """
    if f"{name}/index.m3u8" not in master:
        return False
    playlist = directory / name / "index.m3u8"
    if time.time() - playlist.stat().st_mtime > timeout:
        return False
    content = playlist.read_text()
    segments = [line for line in content.splitlines() if line and not line.startswith("#")]
    if len(segments) < 2 or "#EXTINF:" not in content:
        return False
    return all(
        Path(segment).name == segment and (playlist.parent / segment).stat().st_size > 0
        for segment in segments[-2:]
    )
