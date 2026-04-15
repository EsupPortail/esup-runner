#!/usr/bin/env python3
"""GPU runtime smoke checks for transcription (Torch/CUDA).

This script verifies that the current runner environment can use CUDA through
PyTorch for transcription workloads.

Usage examples:
  - Use defaults from .env/config:
      uv run scripts/check_gpu.py

  - Override GPU visibility:
      uv run scripts/check_gpu.py --cuda-visible-devices 1

  - JSON output:
      uv run scripts/check_gpu.py --json

Exit codes:
  0: torch CUDA runtime is available
  1: torch CUDA runtime is unavailable (or torch import failed)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple


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


def _run(cmd: Sequence[str], timeout_s: float = 10.0) -> Tuple[int, str]:
    try:
        out = subprocess.run(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        return out.returncode, out.stdout or ""
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, f"Timeout after {timeout_s}s: {' '.join(cmd)}\n"
    except Exception as exc:
        return 1, f"Exception running {' '.join(cmd)}: {exc}\n"


def _load_config() -> Any:
    _ensure_import_path()
    try:
        from app.core.config import get_config  # type: ignore

        return get_config()
    except Exception as exc:
        return exc


def _load_cfg_defaults() -> tuple[str, int, str, str, str]:
    cfg = _load_config()
    if isinstance(cfg, Exception):
        return (
            str(os.getenv("ENCODING_TYPE", "CPU")).upper(),
            int(os.getenv("GPU_HWACCEL_DEVICE", "0") or 0),
            str(os.getenv("GPU_CUDA_VISIBLE_DEVICES", "") or ""),
            str(os.getenv("GPU_CUDA_DEVICE_ORDER", "") or ""),
            str(cfg),
        )

    return (
        str(getattr(cfg, "ENCODING_TYPE", "CPU")).upper(),
        int(getattr(cfg, "GPU_HWACCEL_DEVICE", 0) or 0),
        str(getattr(cfg, "GPU_CUDA_VISIBLE_DEVICES", "") or ""),
        str(getattr(cfg, "GPU_CUDA_DEVICE_ORDER", "") or ""),
        "",
    )


def _apply_cuda_env(cuda_visible: str, cuda_order: str) -> None:
    if str(cuda_visible).strip():
        os.environ["CUDA_VISIBLE_DEVICES"] = str(cuda_visible).strip()
    if str(cuda_order).strip():
        os.environ["CUDA_DEVICE_ORDER"] = str(cuda_order).strip()


def _probe_nvidia_smi() -> CheckResult:
    rc, out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,memory.total",
            "--format=csv,noheader",
        ],
        timeout_s=10.0,
    )

    if rc == 0 and out.strip():
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        return CheckResult(
            name="nvidia-smi",
            ok=True,
            required=False,
            details=f"{len(lines)} GPU(s) detected",
        )

    if rc == 127:
        details = "nvidia-smi not found in PATH"
    elif rc == 124:
        details = (out or "nvidia-smi probe timed out").strip()
    else:
        details = (out or f"nvidia-smi probe failed (rc={rc})").strip()
    return CheckResult(name="nvidia-smi", ok=False, required=False, details=details)


def _import_torch_module() -> Any:
    import torch  # type: ignore

    return torch


def _new_torch_info() -> Dict[str, Any]:
    return {
        "torch": "unknown",
        "cuda_build": None,
        "cuda_available": False,
        "gpu_count": 0,
        "gpu_names": [],
    }


def _probe_cuda_available(torch: Any, info: Dict[str, Any]) -> tuple[bool, CheckResult | None]:
    try:
        cuda_available = bool(torch.cuda.is_available())
    except Exception as exc:
        return (
            False,
            CheckResult(
                name="torch:cuda_available",
                ok=False,
                required=True,
                details=f"torch.cuda.is_available() failed: {exc}",
            ),
        )

    info["cuda_available"] = cuda_available
    return cuda_available, None


def _probe_gpu_count(torch: Any, cuda_available: bool) -> int:
    if not cuda_available:
        return 0
    try:
        return int(torch.cuda.device_count())
    except Exception:
        return 0


def _probe_gpu_names(torch: Any, gpu_count: int) -> list[str]:
    gpu_names: list[str] = []
    for idx in range(gpu_count):
        try:
            gpu_names.append(str(torch.cuda.get_device_name(idx)))
        except Exception:
            gpu_names.append(f"gpu:{idx}")
    return gpu_names


def _build_torch_runtime_result(info: Dict[str, Any]) -> CheckResult:
    if info["cuda_available"]:
        return CheckResult(
            name="torch:cuda_runtime",
            ok=True,
            required=True,
            details=f"{info['gpu_count']} CUDA device(s) available",
        )

    if info["cuda_build"] is None:
        details = "torch build is CPU-only (torch.version.cuda=None)"
    else:
        details = "CUDA build detected but runtime unavailable"
    return CheckResult(name="torch:cuda_runtime", ok=False, required=True, details=details)


def _probe_torch_runtime() -> tuple[CheckResult, Dict[str, Any]]:
    info: Dict[str, Any] = _new_torch_info()
    try:
        torch = _import_torch_module()
    except Exception as exc:
        return (
            CheckResult(
                name="torch:import",
                ok=False,
                required=True,
                details=f"Failed to import torch: {exc}",
            ),
            info,
        )

    info["torch"] = str(getattr(torch, "__version__", "unknown"))
    info["cuda_build"] = getattr(getattr(torch, "version", None), "cuda", None)

    cuda_available, cuda_error = _probe_cuda_available(torch, info)
    if cuda_error is not None:
        return cuda_error, info

    info["gpu_count"] = _probe_gpu_count(torch, cuda_available)
    if cuda_available and info["gpu_count"] > 0:
        info["gpu_names"] = _probe_gpu_names(torch, int(info["gpu_count"]))

    return _build_torch_runtime_result(info), info


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check torch CUDA runtime availability")
    parser.add_argument("--json", dest="json_out", action="store_true", help="JSON output")
    parser.add_argument("--verbose", action="store_true", help="Show extra details")
    parser.add_argument(
        "--cuda-visible-devices",
        default=None,
        help="Override GPU_CUDA_VISIBLE_DEVICES/CUDA_VISIBLE_DEVICES",
    )
    parser.add_argument(
        "--cuda-device-order",
        default=None,
        help="Override GPU_CUDA_DEVICE_ORDER/CUDA_DEVICE_ORDER",
    )
    parser.add_argument(
        "--hwaccel-device",
        type=int,
        default=None,
        help="Override GPU_HWACCEL_DEVICE (informational)",
    )
    return parser


def _fmt_result(result: CheckResult) -> str:
    tag = "OK" if result.ok else ("FAIL" if result.required else "WARN")
    return f"[{tag}] {result.name}"


def _render_text(
    *,
    encoding_type: str,
    hwdev: int,
    cuda_visible: str,
    cuda_order: str,
    cfg_err: str,
    nvidia_result: CheckResult,
    torch_result: CheckResult,
    torch_info: Dict[str, Any],
    verbose: bool,
) -> None:
    print("GPU runtime checks")
    print(f"- ENCODING_TYPE: {encoding_type}")
    print(f"- GPU_HWACCEL_DEVICE: {hwdev}")
    if cuda_visible:
        print(f"- CUDA_VISIBLE_DEVICES: {cuda_visible}")
    if cuda_order:
        print(f"- CUDA_DEVICE_ORDER: {cuda_order}")
    if cfg_err and verbose:
        print(f"- config warning: {cfg_err}")

    print(_fmt_result(nvidia_result))
    if verbose and nvidia_result.details:
        print(nvidia_result.details)

    print(_fmt_result(torch_result))
    if torch_result.details:
        print(torch_result.details)

    print(f"torch={torch_info['torch']}")
    print(f"cuda_build={torch_info['cuda_build']}")
    print(f"cuda_available={torch_info['cuda_available']}")
    print(f"gpu_count={torch_info['gpu_count']}")
    if torch_info["gpu_names"]:
        print("gpu_names=" + ",".join(torch_info["gpu_names"]))


def _render_json(
    *,
    encoding_type: str,
    hwdev: int,
    cuda_visible: str,
    cuda_order: str,
    cfg_err: str,
    nvidia_result: CheckResult,
    torch_result: CheckResult,
    torch_info: Dict[str, Any],
) -> None:
    payload = {
        "encoding_type": encoding_type,
        "gpu_hwaccel_device": hwdev,
        "cuda_visible_devices": cuda_visible,
        "cuda_device_order": cuda_order,
        "config_warning": cfg_err or "",
        "results": [nvidia_result.as_json(), torch_result.as_json()],
        "torch": torch_info,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def main() -> int:
    args = _build_parser().parse_args()
    encoding_type, cfg_hwdev, cfg_cuda_visible, cfg_cuda_order, cfg_err = _load_cfg_defaults()

    hwdev = args.hwaccel_device if args.hwaccel_device is not None else cfg_hwdev
    cuda_visible = (
        args.cuda_visible_devices if args.cuda_visible_devices is not None else cfg_cuda_visible
    )
    cuda_order = args.cuda_device_order if args.cuda_device_order is not None else cfg_cuda_order

    _apply_cuda_env(cuda_visible, cuda_order)
    nvidia_result = _probe_nvidia_smi()
    torch_result, torch_info = _probe_torch_runtime()

    if args.json_out:
        _render_json(
            encoding_type=encoding_type,
            hwdev=hwdev,
            cuda_visible=cuda_visible,
            cuda_order=cuda_order,
            cfg_err=cfg_err,
            nvidia_result=nvidia_result,
            torch_result=torch_result,
            torch_info=torch_info,
        )
    else:
        _render_text(
            encoding_type=encoding_type,
            hwdev=hwdev,
            cuda_visible=cuda_visible,
            cuda_order=cuda_order,
            cfg_err=cfg_err,
            nvidia_result=nvidia_result,
            torch_result=torch_result,
            torch_info=torch_info,
            verbose=bool(args.verbose),
        )

    return 0 if torch_result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
