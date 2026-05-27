"""Runtime helpers for FFmpeg command execution and capability checks.

Wraps subprocess interactions for encoder discovery and preflight diagnostics.
Keeps command launch/reporting behavior consistent across CPU and GPU flows.
"""

from __future__ import annotations

import shlex
import subprocess
from functools import lru_cache
from timeit import default_timer as timer


@lru_cache(maxsize=4)
def has_encoder(encoder: str, *, subprocess_module=subprocess) -> bool:
    """Return whether ffmpeg reports a specific encoder."""
    try:
        result = subprocess_module.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.DEVNULL,
            text=True,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == encoder:
                return True
    except Exception:
        return False
    return False


def choose_h264_encoder(*, has_encoder_fn=has_encoder) -> tuple[str, str]:
    """Choose libx264 when available; otherwise fallback to builtin h264."""
    if has_encoder_fn("libx264"):
        return "libx264", ""
    return "h264", "libx264 missing; forcing h264\n"


@lru_cache(maxsize=1)
def nvenc_preflight(*, subprocess_module=subprocess) -> tuple[bool, str]:
    """Return ``(ok, details)`` for NVENC availability."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
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
        out = subprocess_module.run(
            cmd,
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.STDOUT,
            text=True,
        )
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


def launch_cmd(
    ffmpeg_cmd: str,
    type: str,
    format: str,
    *,
    subprocess_module=subprocess,
) -> tuple[bool, str]:
    """Execute FFmpeg command and collect a readable log message."""
    msg = ""
    encode_start = timer()
    return_value = False

    try:
        output = subprocess_module.run(
            shlex.split(ffmpeg_cmd),
            stdout=subprocess_module.PIPE,
            stderr=subprocess_module.STDOUT,
            text=False,
        )

        encode_end = timer() - encode_start
        msg += ffmpeg_cmd + "\n"
        msg += "Encode file in {:.3}s.\n".format(encode_end)
        try:
            msg += output.stdout.decode("utf-8")
        except UnicodeDecodeError:
            pass
        msg += "\n"

        if output.returncode != 0:
            msg += "ERROR RETURN CODE for type=%s and format=%s : %s" % (
                type,
                format,
                output.returncode,
            )
        else:
            return_value = True
    except subprocess_module.CalledProcessError as e:
        msg += 20 * "////" + "\n"
        msg += "Runtime Error: {0}\n".format(e)
    except OSError as err:
        msg += 20 * "////" + "\n"
        msg += "OS error: {0}\n".format(err)
    except Exception as exc:
        msg += 20 * "////" + "\n"
        msg += "Unexpected error: {0}\n".format(exc)

    return return_value, msg


def run_and_collect_text(cmd: list[str], *, subprocess_module=subprocess) -> tuple[int, str]:
    """Run a command and return its exit code and merged text output."""
    out = subprocess_module.run(
        cmd,
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.STDOUT,
        text=True,
    )
    return int(out.returncode), out.stdout or ""


def run_shell_bytes(cmd: str, *, subprocess_module=subprocess) -> tuple[int, bytes]:
    """Run a shell-like command string and return bytes output."""
    out = subprocess_module.run(
        shlex.split(cmd),
        stdout=subprocess_module.PIPE,
        stderr=subprocess_module.STDOUT,
        text=False,
    )
    return int(out.returncode), out.stdout or b""
