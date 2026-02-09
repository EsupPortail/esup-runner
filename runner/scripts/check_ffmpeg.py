#!/usr/bin/env python3
"""FFmpeg smoke checks for esup-runner.

Runs lightweight capability checks for the `encoding` and `studio` pipelines.
It validates that FFmpeg/ffprobe are available, that the expected CPU/GPU
codecs/filters work and PNG support is available, based on the runner configuration.

Usage examples:
  - Auto (reads .env via app.core.config):
      uv run scripts/check_ffmpeg.py

  - Force GPU checks (even if ENCODING_TYPE=CPU):
      uv run scripts/check_ffmpeg.py --mode gpu

  - Check both modes:
      uv run scripts/check_ffmpeg.py --all-modes

Exit codes:
  0: all required checks passed
  2: warnings only
  3: at least one required check failed
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

RE_NVENC_API_MISMATCH = re.compile(r"required\s*:\s*([0-9.]+).*found\s*:\s*([0-9.]+)", re.I | re.S)
RE_MIN_DRIVER = re.compile(r"minimum\s*required\s*driver\s*version\s*:\s*([0-9.]+)", re.I)


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool
    details: str = ""

    def as_json(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "details": self.details,
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _run(
    cmd: List[str], *, timeout_s: float = 15.0, env: Optional[Dict[str, str]] = None
) -> Tuple[int, str]:
    try:
        out = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            timeout=timeout_s,
        )
        return out.returncode, out.stdout or ""
    except FileNotFoundError:
        return 127, f"Command not found: {cmd[0]}\n"
    except subprocess.TimeoutExpired:
        return 124, f"Timeout after {timeout_s}s: {' '.join(cmd)}\n"
    except Exception as exc:
        return 1, f"Exception running {' '.join(cmd)}: {exc}\n"


def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _load_config() -> Any:
    """Load app config (loads .env once)."""
    _ensure_import_path()
    try:
        from app.core.config import get_config  # type: ignore

        return get_config()
    except Exception as exc:
        return exc


def _parse_encoders(encoders_text: str) -> set[str]:
    encoders: set[str] = set()
    for line in encoders_text.splitlines():
        parts = line.split()
        # Typical line: " V....D h264_nvenc           NVIDIA NVENC H.264 encoder"
        if len(parts) >= 2 and re.match(r"^[A-Z.]{6}$", parts[0]):
            encoders.add(parts[1])
    return encoders


def _parse_decoders(decoders_text: str) -> set[str]:
    decoders: set[str] = set()
    for line in decoders_text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^[A-Z.]{6}$", parts[0]):
            decoders.add(parts[1])
    return decoders


def _parse_filters(filters_text: str) -> set[str]:
    filters: set[str] = set()
    for line in filters_text.splitlines():
        parts = line.split()
        # Typical line: " ... scale_npp          V->V       NVIDIA Performance Primitives scaler"
        if len(parts) >= 2 and re.match(r"^[A-Z.]{3}$", parts[0]):
            filters.add(parts[1])
    return filters


def _diagnose_nvenc_failure(output: str) -> str:
    if not output:
        return ""

    low = output.lower()
    if "driver does not support the required nvenc api version" in low:
        # Provide actionable hints for common NVENC driver/API mismatches.
        extra = "Detection: NVENC API / NVIDIA driver mismatch.\n"
        m = RE_NVENC_API_MISMATCH.search(output)
        if m:
            extra += f"Details: required={m.group(1)} found={m.group(2)}\n"
        md = RE_MIN_DRIVER.search(output)
        if md:
            extra += f"Minimum driver required (per FFmpeg): {md.group(1)}\n"
        extra += "Action: update the NVIDIA driver (or use ENCODING_TYPE=CPU).\n"
        return extra

    if "no capable devices found" in low or "cannot load libcuda" in low:
        return (
            "Detection: CUDA unavailable in this environment.\n"
            "Action: verify GPU access (drivers, /dev/nvidia*, container, permissions) or use CPU.\n"
        )

    if "frame dimensions" in low and "minimum supported" in low:
        return (
            "Detection: resolution too small for NVENC.\n"
            "Action: use a larger test source (this script already uses 640x360).\n"
        )

    return ""


def _build_gpu_env(cuda_visible_devices: Optional[str]) -> Dict[str, str]:
    env = dict(os.environ)
    if cuda_visible_devices is not None and str(cuda_visible_devices).strip() != "":
        env["CUDA_VISIBLE_DEVICES"] = str(cuda_visible_devices).strip()
    return env


def check_binaries() -> List[CheckResult]:
    results: List[CheckResult] = []
    for bin_name in ("ffmpeg", "ffprobe"):
        path = _which(bin_name)
        ok = bool(path)
        details = path or "Not found in PATH"
        results.append(
            CheckResult(name=f"binary:{bin_name}", ok=ok, required=True, details=details)
        )
    return results


def check_versions() -> List[CheckResult]:
    results: List[CheckResult] = []
    for bin_name in ("ffmpeg", "ffprobe"):
        rc, out = _run([bin_name, "-hide_banner", "-version"], timeout_s=10)
        ok = rc == 0
        first = out.splitlines()[0] if out else ""
        results.append(CheckResult(name=f"version:{bin_name}", ok=ok, required=True, details=first))
    return results


def collect_capabilities(
    env: Optional[Dict[str, str]] = None,
) -> Tuple[set[str], set[str], set[str], str]:
    # Query ffmpeg once per capability set to keep output parsing simple.
    rc_e, out_e = _run(["ffmpeg", "-hide_banner", "-encoders"], timeout_s=20, env=env)
    rc_d, out_d = _run(["ffmpeg", "-hide_banner", "-decoders"], timeout_s=20, env=env)
    rc_f, out_f = _run(["ffmpeg", "-hide_banner", "-filters"], timeout_s=20, env=env)

    if rc_e != 0 or rc_d != 0 or rc_f != 0:
        raw = ""
        if rc_e != 0:
            raw += f"ffmpeg -encoders failed ({rc_e})\n{out_e}\n"
        if rc_d != 0:
            raw += f"ffmpeg -decoders failed ({rc_d})\n{out_d}\n"
        if rc_f != 0:
            raw += f"ffmpeg -filters failed ({rc_f})\n{out_f}\n"
        return set(), set(), set(), raw

    return _parse_encoders(out_e), _parse_decoders(out_d), _parse_filters(out_f), ""


def check_build_configuration(*, for_studio: bool) -> List[CheckResult]:
    """Check FFmpeg build flags that impact WebM/VPx robustness.

    Rationale:
      - Studio sources from Opencast are commonly WebM (VP8/VP9 + Opus).
      - Some degraded builds (e.g. built with --disable-x86asm and/or without libvpx)
        can lead to green/pink corruption even when the pipeline succeeds.
    """

    results: List[CheckResult] = []
    rc, out = _run(["ffmpeg", "-hide_banner", "-buildconf"], timeout_s=10)
    if rc != 0:
        results.append(
            CheckResult(
                name="buildconf:ffmpeg",
                ok=False,
                required=for_studio,
                details=out or f"ffmpeg -buildconf failed (rc={rc})\n",
            )
        )
        return results

    low = (out or "").lower()
    has_libvpx = "--enable-libvpx" in low
    disables_x86asm = "--disable-x86asm" in low

    results.append(
        CheckResult(
            name="buildconf:libvpx",
            ok=has_libvpx,
            required=for_studio,
            details=(
                "--enable-libvpx present"
                if has_libvpx
                else "Missing --enable-libvpx (WebM VP8/VP9 may be unreliable)"
            ),
        )
    )

    # Not a strict requirement, but strongly recommended.
    results.append(
        CheckResult(
            name="buildconf:x86asm",
            ok=not disables_x86asm,
            required=False,
            details=(
                "OK"
                if not disables_x86asm
                else "Built with --disable-x86asm (may cause corruption/perf issues). Rebuild without it."
            ),
        )
    )

    return results


def preflight_cpu_encode(prefer_libx264: bool = True) -> CheckResult:
    # Select encoder; libx264 gives best signal, but allow fallback.
    encoders, _, _, raw_err = collect_capabilities()
    if raw_err:
        return CheckResult(name="preflight:cpu", ok=False, required=True, details=raw_err)

    if prefer_libx264 and "libx264" in encoders:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
        encoder_name = "libx264"
    elif "h264" in encoders:
        # builtin h264 encoder: keep it simple
        video_args = ["-c:v", "h264", "-q:v", "28"]
        encoder_name = "h264"
    else:
        return CheckResult(
            name="preflight:cpu",
            ok=False,
            required=True,
            details="No CPU H.264 encoder found (libx264 or h264).\n",
        )

    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=640x360:r=30",
        "-t",
        "0.2",
        "-an",
        "-pix_fmt",
        "yuv420p",
        *video_args,
        "-f",
        "null",
        "-",
    ]

    rc, out = _run(cmd, timeout_s=20)
    ok = rc == 0
    details = f"encoder={encoder_name}"
    if not ok and out:
        details += "\n" + out
    return CheckResult(name="preflight:cpu", ok=ok, required=True, details=details)


def check_png_encoder() -> CheckResult:
    # PNG encoder is required for thumbnails and overview sprite generation.
    encoders, _, _, raw_err = collect_capabilities()
    if raw_err:
        return CheckResult(name="cap:png_encoder", ok=False, required=True, details=raw_err)

    ok = "png" in encoders
    details = "png" if ok else "Missing PNG encoder (ffmpeg -encoders)"
    return CheckResult(name="cap:png_encoder", ok=ok, required=True, details=details)


def preflight_nvenc_basic(env: Dict[str, str]) -> CheckResult:
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
    rc, out = _run(cmd, timeout_s=20, env=env)
    ok = rc == 0
    details = ""
    if not ok:
        details = out
        details += _diagnose_nvenc_failure(out)
    return CheckResult(name="preflight:gpu:nvenc", ok=ok, required=True, details=details)


def preflight_scale_npp_to_nvenc(env: Dict[str, str], hwdev: int) -> CheckResult:
    # Test a minimal CUDA filterchain: hwupload_cuda -> scale_npp -> NVENC.
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-init_hw_device",
        f"cuda=cuda:{hwdev}",
        "-filter_hw_device",
        "cuda",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=1280x720:r=30",
        "-t",
        "0.1",
        "-an",
        "-vf",
        "format=yuv420p,hwupload_cuda,scale_npp=-2:360:interp_algo=super",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]

    rc, out = _run(cmd, timeout_s=25, env=env)
    ok = rc == 0
    details = ""
    if not ok:
        details = out
        details += _diagnose_nvenc_failure(out)
    return CheckResult(name="preflight:gpu:scale_npp+nvenc", ok=ok, required=True, details=details)


def check_expected_caps_for_mode(
    mode: str, env: Optional[Dict[str, str]], *, for_encoding: bool, for_studio: bool
) -> List[CheckResult]:
    encoders, decoders, filters, raw_err = collect_capabilities(env=env)
    results: List[CheckResult] = []

    if raw_err:
        results.append(CheckResult(name=f"caps:{mode}", ok=False, required=True, details=raw_err))
        return results

    # CPU expected capabilities.
    if mode == "cpu":
        ok_h264 = ("libx264" in encoders) or ("h264" in encoders)
        results.append(
            CheckResult(
                name="cap:cpu:h264_encoder",
                ok=ok_h264,
                required=True,
                details=(
                    "libx264"
                    if "libx264" in encoders
                    else ("h264" if "h264" in encoders else "missing")
                ),
            )
        )
        return results

    # GPU expected capabilities.
    ok_nvenc = "h264_nvenc" in encoders
    ok_cuvid = "h264_cuvid" in decoders
    ok_scale_npp = "scale_npp" in filters
    ok_hwupload = "hwupload_cuda" in filters

    results.append(CheckResult(name="cap:gpu:h264_nvenc", ok=ok_nvenc, required=True, details=""))
    # cuvid is used by studio/encoding GPU input pipeline; if missing, GPU mode will be unreliable.
    results.append(CheckResult(name="cap:gpu:h264_cuvid", ok=ok_cuvid, required=True, details=""))

    # scale_npp is required for the HLS ladder in encoding and for studio GPU scaling.
    results.append(
        CheckResult(name="cap:gpu:scale_npp", ok=ok_scale_npp, required=True, details="")
    )
    results.append(
        CheckResult(name="cap:gpu:hwupload_cuda", ok=ok_hwupload, required=False, details="")
    )

    if for_studio:
        ok_overlay_cuda = "overlay_cuda" in filters
        # Not strictly required: studio can fall back to CPU if overlay_cuda missing.
        results.append(
            CheckResult(
                name="cap:gpu:overlay_cuda",
                ok=ok_overlay_cuda,
                required=False,
                details="(piph/pipb GPU overlay needs overlay_cuda; otherwise studio falls back to CPU)",
            )
        )

    return results


def _mode_from_config(cfg: Any) -> str:
    try:
        enc = str(getattr(cfg, "ENCODING_TYPE", "CPU")).upper()
        return "gpu" if enc == "GPU" else "cpu"
    except Exception:
        return "cpu"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke test FFmpeg for encoding/studio (CPU/GPU)")
    parser.add_argument(
        "--mode",
        choices=["auto", "cpu", "gpu"],
        default="auto",
        help="Mode to check (auto = based on ENCODING_TYPE)",
    )
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="Check both CPU and GPU (even if ENCODING_TYPE=CPU)",
    )
    parser.add_argument(
        "--check",
        default="encoding,studio",
        help="Components to check: encoding,studio (CSV)",
    )
    parser.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
    parser.add_argument("--verbose", action="store_true", help="Show more details")
    parser.add_argument(
        "--hwaccel-device",
        type=int,
        default=None,
        help="Override GPU_HWACCEL_DEVICE (default: config/env)",
    )
    parser.add_argument(
        "--cuda-visible-devices",
        default=None,
        help="Override GPU_CUDA_VISIBLE_DEVICES/CUDA_VISIBLE_DEVICES (e.g. 0)",
    )
    return parser


def _selected_components(check_csv: str) -> tuple[set[str], bool, bool]:
    components = {c.strip().lower() for c in str(check_csv).split(",") if c.strip()}
    return components, "encoding" in components, "studio" in components


def _load_cfg_defaults() -> tuple[str, int, str, str]:
    cfg = _load_config()
    if isinstance(cfg, Exception):
        cfg_mode = "cpu"
        cfg_hwdev = 0
        cfg_cuda = os.getenv("GPU_CUDA_VISIBLE_DEVICES") or os.getenv("CUDA_VISIBLE_DEVICES") or ""
        return cfg_mode, cfg_hwdev, cfg_cuda, str(cfg)

    cfg_mode = _mode_from_config(cfg)
    cfg_hwdev = int(getattr(cfg, "GPU_HWACCEL_DEVICE", 0) or 0)
    cfg_cuda = str(getattr(cfg, "GPU_CUDA_VISIBLE_DEVICES", "") or "")
    return cfg_mode, cfg_hwdev, cfg_cuda, ""


def _choose_modes(args_mode: str, cfg_mode: str, all_modes: bool) -> tuple[str, List[str]]:
    selected = cfg_mode if args_mode == "auto" else args_mode
    return selected, (["cpu", "gpu"] if all_modes else [selected])


def _collect_results(
    *,
    modes: List[str],
    hwdev: int,
    cuda_visible: str,
    for_encoding: bool,
    for_studio: bool,
    cfg_err: str,
    verbose: bool,
) -> List[CheckResult]:
    results: List[CheckResult] = []
    # Base checks common to all modes.
    results.extend(check_binaries())
    results.extend(check_versions())
    results.extend(check_build_configuration(for_studio=for_studio))
    if cfg_err and verbose:
        results.append(CheckResult(name="config:warning", ok=True, required=False, details=cfg_err))
    results.append(preflight_cpu_encode())
    results.append(check_png_encoder())

    # Mode-specific checks.
    for mode in modes:
        if mode == "cpu":
            results.extend(
                check_expected_caps_for_mode(
                    "cpu", env=None, for_encoding=for_encoding, for_studio=for_studio
                )
            )
            continue

        gpu_env = _build_gpu_env(cuda_visible)
        results.extend(
            check_expected_caps_for_mode(
                "gpu", env=gpu_env, for_encoding=for_encoding, for_studio=for_studio
            )
        )
        results.append(preflight_nvenc_basic(gpu_env))
        results.append(preflight_scale_npp_to_nvenc(gpu_env, hwdev=hwdev))
    return results


def _render_json(
    *,
    selected_mode: str,
    modes: List[str],
    components: set[str],
    hwdev: int,
    cuda_visible: str,
    all_results: List[CheckResult],
    required_failed: List[CheckResult],
    warnings: List[CheckResult],
) -> None:
    payload = {
        "mode": selected_mode,
        "modes_checked": modes,
        "components": sorted(list(components)),
        "gpu": {"hwaccel_device": hwdev, "cuda_visible_devices": cuda_visible},
        "results": [r.as_json() for r in all_results],
        "summary": {"required_failed": len(required_failed), "warnings": len(warnings)},
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _fmt_result(r: CheckResult) -> str:
    tag = "OK" if r.ok else ("FAIL" if r.required else "WARN")
    return f"[{tag}] {r.name}"


def _print_env_info(*, modes: List[str], hwdev: int, cuda_visible: str) -> None:
    if "gpu" not in modes:
        return
    print(f"- GPU_HWACCEL_DEVICE: {hwdev}")
    if cuda_visible:
        print(f"- CUDA_VISIBLE_DEVICES: {cuda_visible}")


def _print_results(*, all_results: List[CheckResult], verbose: bool) -> None:
    for r in all_results:
        print(_fmt_result(r))
        if verbose and r.details:
            print(r.details.strip())


def _print_summary(
    *,
    required_failed: List[CheckResult],
    warnings: List[CheckResult],
    verbose: bool,
) -> None:
    if required_failed:
        print("\nRequired failures:")
        for r in required_failed:
            print(f"- {r.name}")
            if r.details:
                print(r.details.strip())

    if warnings:
        print("\nWarnings (non-blocking):")
        for r in warnings:
            print(f"- {r.name}")
            if verbose and r.details:
                print(r.details.strip())


def _render_text(
    *,
    cfg_mode: str,
    selected_mode: str,
    modes: List[str],
    components: set[str],
    hwdev: int,
    cuda_visible: str,
    all_results: List[CheckResult],
    required_failed: List[CheckResult],
    warnings: List[CheckResult],
    verbose: bool,
) -> None:
    print("FFmpeg smoke checks")
    print(f"- components: {', '.join(sorted(components)) or '(none)'}")
    print(
        f"- config mode: {cfg_mode} | selected mode: {selected_mode} | checked: {', '.join(modes)}"
    )
    _print_env_info(modes=modes, hwdev=hwdev, cuda_visible=cuda_visible)
    _print_results(all_results=all_results, verbose=verbose)
    _print_summary(required_failed=required_failed, warnings=warnings, verbose=verbose)


def main() -> int:
    args = _build_parser().parse_args()
    components, for_encoding, for_studio = _selected_components(args.check)
    cfg_mode, cfg_hwdev, cfg_cuda, cfg_err = _load_cfg_defaults()
    selected_mode, modes = _choose_modes(args.mode, cfg_mode, bool(args.all_modes))
    hwdev = args.hwaccel_device if args.hwaccel_device is not None else cfg_hwdev
    cuda_visible = args.cuda_visible_devices if args.cuda_visible_devices is not None else cfg_cuda

    all_results = _collect_results(
        modes=modes,
        hwdev=hwdev,
        cuda_visible=cuda_visible,
        for_encoding=for_encoding,
        for_studio=for_studio,
        cfg_err=cfg_err,
        verbose=bool(args.verbose),
    )

    required_failed = [r for r in all_results if r.required and not r.ok]
    warnings = [r for r in all_results if (not r.required) and (not r.ok)]

    if args.json_out:
        _render_json(
            selected_mode=selected_mode,
            modes=modes,
            components=components,
            hwdev=hwdev,
            cuda_visible=cuda_visible,
            all_results=all_results,
            required_failed=required_failed,
            warnings=warnings,
        )
    else:
        _render_text(
            cfg_mode=cfg_mode,
            selected_mode=selected_mode,
            modes=modes,
            components=components,
            hwdev=hwdev,
            cuda_visible=cuda_visible,
            all_results=all_results,
            required_failed=required_failed,
            warnings=warnings,
            verbose=bool(args.verbose),
        )

    if required_failed:
        return 3
    if warnings:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
