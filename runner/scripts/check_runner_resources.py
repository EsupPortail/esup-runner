#!/usr/bin/env python3
"""Check runner instance counts against machine resources.

This script validates that RUNNER_INSTANCES and RUNNER_TASK_TYPES matches available CPU/RAM/GPU
resources and prints a recommended RUNNER_INSTANCES and RUNNER_TASK_TYPES values for the current
machine. It loads configuration from .env via app.core.config.

Remarks:
- The resource requirements are conservative upper bounds based on typical usage patterns and may not reflect actual usage in all cases. Adjust the constants as needed for your workload.
- The GPU max recommendations are based on user-provided estimates for common models. Add more models or adjust counts as needed.
- The script assumes NVIDIA GPUs and uses nvidia-smi for detection. It will not detect other GPU types or configurations.

Usage:
  uv run scripts/check_runner_resources.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Set, Tuple

# Conservative CPU/RAM requirements (upper bounds)
CPU_ENCODING_VCPU_MAX = 8
CPU_ENCODING_RAM_GB = 2
CPU_TRANSCRIPTION_VCPU = 4
CPU_TRANSCRIPTION_RAM_GB_MAX = 10
CPU_COMBINED_VCPU = max(CPU_ENCODING_VCPU_MAX, CPU_TRANSCRIPTION_VCPU)
CPU_COMBINED_RAM_GB = max(CPU_ENCODING_RAM_GB, CPU_TRANSCRIPTION_RAM_GB_MAX)

# Conservative GPU VRAM requirements (upper bounds)
GPU_ENCODING_VRAM_GB = 1
GPU_TRANSCRIPTION_VRAM_GB_MAX = 7
GPU_COMBINED_VRAM_GB = max(GPU_ENCODING_VRAM_GB, GPU_TRANSCRIPTION_VRAM_GB_MAX)

# Per-GPU max recommendations
#  - Tesla T4 (1× NVENC): Recommended 1–2 encodes per GPU (max speed at 1; ~half speed at 2).
#    Up to ~6–10 is possible, but queueing causes much longer runtimes.
#  - L40S (3× NVENC): Recommended 3–6 encodes per GPU (one per NVENC at 3; light time-slicing at 6).
#    Up to ~15–30 per GPU is possible, mainly limited by storage (HLS writes) and CPU (AAC/mux), not NVENC itself.
# Solution to explore: Runtime control.
# -> if nvidia-smi dmon shows enc > 90–95% continuously, do not add new jobs to this GPU.
GPU_RECOMMENDATIONS = {
    "tesla t4": (6, 1),
    "l40s": (15, 4),
}


@dataclass
class ResourceInfo:
    vcpu: int
    ram_gb: float
    gpus: List["GPUInfo"]


@dataclass
class GPUInfo:
    name: str
    vram_gb: float


@dataclass
class TaskCounts:
    total_instances: int
    encoding_instances: int
    transcription_instances: int
    encoding_or_studio_instances: int


@dataclass
class Recommendation:
    encoding_instances: int
    transcription_instances: int
    total_instances: int
    runner_task_types: str
    rationale: str


def _repo_root() -> Path:
    """Return the repository root directory."""
    return Path(__file__).resolve().parents[1]


def _ensure_import_path() -> None:
    """Ensure the repository root is on sys.path."""
    root = str(_repo_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _load_config():
    """Load config and parsing helpers from app.core.config."""
    _ensure_import_path()
    from app.core.config import (  # type: ignore
        _parse_grouped_task_types_spec,
        _parse_task_types_csv,
        get_config,
    )

    return get_config(), _parse_grouped_task_types_spec, _parse_task_types_csv


def _read_meminfo_gb() -> float:
    """Read total system memory in GB from /proc/meminfo."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    kb = float(parts[1])
                    return kb / (1024.0 * 1024.0)
    except Exception:
        pass
    return 0.0


def _run(cmd: Sequence[str], timeout_s: float = 5.0) -> Tuple[int, str]:
    """Run a command and return (returncode, output)."""
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
    except Exception:
        return 1, ""


def _detect_gpus() -> List[GPUInfo]:
    """Detect NVIDIA GPUs and return their names and VRAM size in GB."""
    rc, out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        ]
    )
    if rc != 0 or not out.strip():
        return []

    gpus: List[GPUInfo] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        name = parts[0]
        try:
            vram_gb = float(parts[1]) / 1024.0
        except ValueError:
            continue
        gpus.append(GPUInfo(name=name, vram_gb=vram_gb))
    return gpus


def _get_resources() -> ResourceInfo:
    """Collect CPU, RAM, and GPU resources from the host."""
    vcpu = os.cpu_count() or 0
    ram_gb = _read_meminfo_gb()
    gpus = _detect_gpus()
    return ResourceInfo(vcpu=vcpu, ram_gb=ram_gb, gpus=gpus)


def _get_task_sets(cfg, parse_grouped, parse_csv) -> List[Set[str]]:
    """Build per-instance task type sets from RUNNER_TASK_TYPES."""
    spec = os.getenv("RUNNER_TASK_TYPES")
    if spec:
        grouped = parse_grouped(spec)
        if grouped is not None:
            return list(grouped)

        types = parse_csv(spec)
        instances = os.getenv("RUNNER_INSTANCES")
        try:
            inst_count = int(instances) if instances is not None else int(cfg.RUNNER_INSTANCES)
        except Exception:
            inst_count = int(getattr(cfg, "RUNNER_INSTANCES", 1))
        if inst_count < 1:
            inst_count = 1
        return [set(types) for _ in range(inst_count)]

    task_sets: List[Set[str]] = list(getattr(cfg, "RUNNER_TASK_TYPES_BY_INSTANCE", []))
    if not task_sets:
        task_sets = [set(getattr(cfg, "RUNNER_TASK_TYPES", set()))]
    return task_sets


def _colorize(text: str, *, level: str) -> str:
    """Colorize output based on severity level."""
    colors = {
        "info": "\033[32m",
        "warning": "\033[33m",
        "error": "\033[31m",
    }
    reset = "\033[0m"
    color = colors.get(level, "")
    if not color:
        return text
    return f"{color}{text}{reset}"


def _get_task_counts(task_sets: List[Set[str]]) -> TaskCounts:
    """Count encoding/transcription instances across task sets."""
    if not task_sets:
        task_sets = [set()]

    total = len(task_sets)
    encoding_or_studio = 0
    transcription = 0
    for types in task_sets:
        has_encoding = "encoding" in types or "studio" in types
        has_transcription = "transcription" in types
        if has_encoding:
            encoding_or_studio += 1
        if has_transcription:
            transcription += 1

    return TaskCounts(
        total_instances=total,
        encoding_instances=encoding_or_studio,
        transcription_instances=transcription,
        encoding_or_studio_instances=encoding_or_studio,
    )


def _format_task_types(encoding_instances: int, transcription_instances: int) -> str:
    """Format a simple grouped RUNNER_TASK_TYPES string."""
    if encoding_instances <= 1 and transcription_instances <= 1:
        if encoding_instances == 1 and transcription_instances == 0:
            return "encoding,studio"
        if encoding_instances == 0 and transcription_instances == 1:
            return "transcription"
        if encoding_instances == 1 and transcription_instances == 1:
            return "[1x(encoding,studio),1x(transcription)]"
        return "encoding,studio"

    parts = []
    if encoding_instances > 0:
        parts.append(f"{encoding_instances}x(encoding,studio)")
    if transcription_instances > 0:
        parts.append(f"{transcription_instances}x(transcription)")
    return f"[{','.join(parts)}]"


def _format_task_types_from_mix(
    *,
    encoding_only: int,
    transcription_only: int,
    combined: int,
) -> str:
    """Format RUNNER_TASK_TYPES for mixed (combined + dedicated) instances."""
    if combined == 0:
        return _format_task_types(encoding_only, transcription_only)

    parts: List[str] = []
    if combined > 0:
        parts.append(f"{combined}x(encoding,studio,transcription)")
    if encoding_only > 0:
        parts.append(f"{encoding_only}x(encoding,studio)")
    if transcription_only > 0:
        parts.append(f"{transcription_only}x(transcription)")

    if not parts:
        return "encoding,studio"
    return f"[{','.join(parts)}]"


def _recommend_mix_by_cpu(vcpu: int, ram_gb: float) -> Recommendation:
    """Recommend instances for CPU mode based on vCPU/RAM bounds."""
    max_combined = min(int(vcpu // CPU_COMBINED_VCPU), int(ram_gb // CPU_COMBINED_RAM_GB))
    max_encoding = min(int(vcpu // CPU_ENCODING_VCPU_MAX), int(ram_gb // CPU_ENCODING_RAM_GB))
    max_trans = min(
        int(vcpu // CPU_TRANSCRIPTION_VCPU), int(ram_gb // CPU_TRANSCRIPTION_RAM_GB_MAX)
    )

    best = (0, 0, 0)
    best_total = -1
    best_enc = -1
    best_trans = -1

    for c in range(max_combined + 1):
        for e in range(max_encoding + 1):
            for t in range(max_trans + 1):
                used_vcpu = (
                    c * CPU_COMBINED_VCPU + e * CPU_ENCODING_VCPU_MAX + t * CPU_TRANSCRIPTION_VCPU
                )
                used_ram = (
                    c * CPU_COMBINED_RAM_GB
                    + e * CPU_ENCODING_RAM_GB
                    + t * CPU_TRANSCRIPTION_RAM_GB_MAX
                )
                if used_vcpu > vcpu or used_ram > ram_gb:
                    continue

                enc_count = e + c
                trans_count = t + c
                if trans_count > 0 and enc_count < trans_count:
                    continue

                total = c + e + t
                if (
                    total > best_total
                    or (total == best_total and enc_count > best_enc)
                    or (total == best_total and enc_count == best_enc and trans_count > best_trans)
                ):
                    best = (e, t, c)
                    best_total = total
                    best_enc = enc_count
                    best_trans = trans_count

    encoding_only, transcription_only, combined = best
    if encoding_only == 0 and transcription_only == 0 and combined == 0 and vcpu > 0 and ram_gb > 0:
        encoding_only = 1

    runner_task_types = _format_task_types_from_mix(
        encoding_only=encoding_only,
        transcription_only=transcription_only,
        combined=combined,
    )
    encoding = encoding_only + combined
    transcription = transcription_only + combined
    rationale = (
        "CPU mode based on conservative per-task bounds: "
        f"encoding≈{CPU_ENCODING_VCPU_MAX} vCPU/{CPU_ENCODING_RAM_GB} GB, "
        f"transcription≈{CPU_TRANSCRIPTION_VCPU} vCPU/{CPU_TRANSCRIPTION_RAM_GB_MAX} GB."
    )
    return Recommendation(
        encoding_instances=encoding,
        transcription_instances=transcription,
        total_instances=encoding_only + transcription_only + combined,
        runner_task_types=runner_task_types,
        rationale=rationale,
    )


def _recommend_mix_by_vram(vram_gb: float) -> Recommendation:
    """Recommend instances for GPU mode based on VRAM bounds."""
    max_combined = int(vram_gb // GPU_COMBINED_VRAM_GB)
    max_encoding = int(vram_gb // GPU_ENCODING_VRAM_GB)
    max_trans = int(vram_gb // GPU_TRANSCRIPTION_VRAM_GB_MAX)

    best = (0, 0, 0)
    best_total = -1
    best_enc = -1
    best_trans = -1
    for c in range(max_combined + 1):
        for e in range(max_encoding + 1):
            for t in range(max_trans + 1):
                used_vram = (
                    c * GPU_COMBINED_VRAM_GB
                    + e * GPU_ENCODING_VRAM_GB
                    + t * GPU_TRANSCRIPTION_VRAM_GB_MAX
                )
                if used_vram > vram_gb:
                    continue

                enc_count = e + c
                trans_count = t + c
                if trans_count > 0 and enc_count < trans_count:
                    continue

                total = c + e + t
                if (
                    total > best_total
                    or (total == best_total and enc_count > best_enc)
                    or (total == best_total and enc_count == best_enc and trans_count > best_trans)
                ):
                    best = (e, t, c)
                    best_total = total
                    best_enc = enc_count
                    best_trans = trans_count

    encoding_only, transcription_only, combined = best
    if encoding_only == 0 and transcription_only == 0 and combined == 0 and vram_gb > 0:
        encoding_only = 1

    runner_task_types = _format_task_types_from_mix(
        encoding_only=encoding_only,
        transcription_only=transcription_only,
        combined=combined,
    )
    encoding = encoding_only + combined
    transcription = transcription_only + combined
    rationale = (
        "GPU mode based on VRAM bounds: "
        f"encoding≈{GPU_ENCODING_VRAM_GB} GB VRAM, "
        f"transcription≈{GPU_TRANSCRIPTION_VRAM_GB_MAX} GB VRAM."
    )
    return Recommendation(
        encoding_instances=encoding,
        transcription_instances=transcription,
        total_instances=encoding_only + transcription_only + combined,
        runner_task_types=runner_task_types,
        rationale=rationale,
    )


def _recommend_for_gpus(gpus: List[GPUInfo]) -> Recommendation:
    """Recommend instances based on known GPU models or total VRAM."""
    if not gpus:
        return Recommendation(
            encoding_instances=0,
            transcription_instances=0,
            total_instances=0,
            runner_task_types="encoding,studio",
            rationale="No GPU detected; falling back to CPU recommendation.",
        )

    total_encoding = 0
    total_trans = 0
    known = True
    details = []
    for gpu in gpus:
        key = gpu.name.strip().lower()
        normalized = re.sub(r"\s+", " ", key)
        rec = None
        for model, counts in GPU_RECOMMENDATIONS.items():
            if model in normalized:
                rec = counts
                break
        if rec is None:
            known = False
            rec = (0, 0)
        enc, trans = rec
        total_encoding += enc
        total_trans += trans
        details.append(f"{gpu.name}: {enc} encodings, {trans} transcriptions")

    if known and (total_encoding > 0 or total_trans > 0):
        return Recommendation(
            encoding_instances=total_encoding,
            transcription_instances=total_trans,
            total_instances=total_encoding + total_trans,
            runner_task_types=_format_task_types(total_encoding, total_trans),
            rationale="Per-GPU max recommendations: " + "; ".join(details),
        )

    total_vram = sum(gpu.vram_gb for gpu in gpus)
    rec = _recommend_mix_by_vram(total_vram)
    rec.rationale = "Unknown GPU model, estimation based on total VRAM."
    return rec


def _required_resources(task_sets: List[Set[str]]) -> Tuple[int, float, float]:
    """Compute aggregate CPU/RAM/VRAM requirements for current task sets."""
    required_vcpu = 0
    required_ram = 0.0
    required_vram = 0.0

    for types in task_sets:
        has_encoding = "encoding" in types or "studio" in types
        has_transcription = "transcription" in types

        inst_vcpu = 0
        inst_ram = 0.0
        inst_vram = 0.0

        if has_encoding:
            inst_vcpu = max(inst_vcpu, CPU_ENCODING_VCPU_MAX)
            inst_ram = max(inst_ram, CPU_ENCODING_RAM_GB)
            inst_vram = max(inst_vram, GPU_ENCODING_VRAM_GB)

        if has_transcription:
            inst_vcpu = max(inst_vcpu, CPU_TRANSCRIPTION_VCPU)
            inst_ram = max(inst_ram, CPU_TRANSCRIPTION_RAM_GB_MAX)
            inst_vram = max(inst_vram, GPU_TRANSCRIPTION_VRAM_GB_MAX)

        required_vcpu += inst_vcpu
        required_ram += inst_ram
        required_vram += inst_vram

    return required_vcpu, required_ram, required_vram


def _evaluate_configuration(
    *,
    resources: ResourceInfo,
    counts: TaskCounts,
    task_sets: List[Set[str]],
    gpu_mode: bool,
    recommended: Recommendation,
) -> Tuple[bool, bool, bool, int, float, float, bool, bool]:
    """Evaluate adequacy and compute requirements and flags."""
    required_vcpu, required_ram, required_vram = _required_resources(task_sets)

    cpu_ok = required_vcpu <= resources.vcpu and required_ram <= resources.ram_gb
    gpu_ok = True
    if gpu_mode:
        total_vram = sum(g.vram_gb for g in resources.gpus)
        gpu_ok = required_vram <= total_vram if total_vram > 0 else False
        cpu_ok = True

    ratio_ok = True
    if counts.transcription_instances > 0:
        ratio_ok = counts.encoding_or_studio_instances >= counts.transcription_instances

    below_recommendation = (
        counts.encoding_or_studio_instances < recommended.encoding_instances
        and counts.transcription_instances < recommended.transcription_instances
    )

    config_ok = (cpu_ok and (gpu_ok if gpu_mode else True)) and ratio_ok

    return (
        cpu_ok,
        gpu_ok,
        ratio_ok,
        required_vcpu,
        required_ram,
        required_vram,
        below_recommendation,
        config_ok,
    )


def _print_header(
    *,
    configured_spec: str,
    configured_instances: str,
    encoding_type: str,
    resources: ResourceInfo,
) -> None:
    """Print initial configuration and detected resources."""
    print("=== Resource check for RUNNER_TASK_TYPES ===")
    print("Initial configuration:")
    print(f"  RUNNER_INSTANCES: {configured_instances}")
    print(f"  RUNNER_TASK_TYPES: {configured_spec}")
    print(f"ENCODING_TYPE: {encoding_type}")
    print(f"Detected vCPU: {resources.vcpu}")
    print(f"Detected RAM: {resources.ram_gb:.1f} GB")
    if resources.gpus:
        print("Detected GPUs:")
        for gpu in resources.gpus:
            print(f"  - {gpu.name} ({gpu.vram_gb:.1f} GB VRAM)")
    else:
        print("Detected GPUs: none")


def _print_configuration(
    *,
    counts: TaskCounts,
    gpu_mode: bool,
    required_vcpu: int,
    required_ram: float,
    required_vram: float,
    cpu_ok: bool,
    gpu_ok: bool,
    ratio_ok: bool,
) -> None:
    """Print current config, requirements, and adequacy checks."""
    print("\nCurrent configuration:")
    print(f"  Total instances: {counts.total_instances}")
    print(f"  Encoding/Studio instances: {counts.encoding_or_studio_instances}")
    print(f"  Transcription instances: {counts.transcription_instances}")

    print("\nEstimated requirements (upper bound):")
    if gpu_mode:
        print(f"  Required vCPU: {required_vcpu} (for CPU mode)")
    else:
        print(f"  Required vCPU: {required_vcpu}")
    print(f"  Required RAM: {required_ram:.1f} GB")
    if gpu_mode:
        print(f"  Required VRAM: {required_vram:.1f} GB")

    print("\nStatus:")
    print(f"  CPU/RAM adequate: {'YES' if cpu_ok else 'NO'}")
    if gpu_mode:
        print(f"  GPU/VRAM adequate: {'YES' if gpu_ok else 'NO'}")

    if not ratio_ok:
        print("  Encoding/Transcription ratio: NO (encoding should be higher)")
    else:
        print("  Encoding/Transcription ratio: YES")


def _print_recommendation(*, recommended: Recommendation, below_recommendation: bool) -> None:
    """Print recommended RUNNER_INSTANCES and RUNNER_TASK_TYPES."""
    print("\nRecommended RUNNER_INSTANCES:")
    print(f"  {recommended.total_instances}")
    print("Recommended RUNNER_TASK_TYPES:")
    print(f"  {recommended.runner_task_types}")
    print(f"  Rationale: {recommended.rationale}")
    if below_recommendation:
        print("  Note: current configuration uses fewer instances than recommended.")


def _print_conclusion(*, config_ok: bool, below_recommendation: bool) -> None:
    """Print final adequacy conclusion with severity coloring."""
    print("\nConclusion:")
    if config_ok:
        if below_recommendation:
            print(
                _colorize(
                    "⚠ WARNING: Configuration is adequate (below recommendation)", level="warning"
                )
            )
        else:
            print(_colorize("✓ INFO: Configuration is adequate", level="info"))
    else:
        print(_colorize("✗ ERROR: Configuration is NOT adequate", level="error"))


def main() -> int:
    """Entry point for the resource check script."""
    cfg, parse_grouped, parse_csv = _load_config()
    resources = _get_resources()
    task_sets = _get_task_sets(cfg, parse_grouped, parse_csv)
    counts = _get_task_counts(task_sets)

    configured_spec = os.getenv("RUNNER_TASK_TYPES", "")
    configured_instances = os.getenv("RUNNER_INSTANCES", str(getattr(cfg, "RUNNER_INSTANCES", 1)))

    encoding_type = str(getattr(cfg, "ENCODING_TYPE", "CPU")).upper()
    gpu_mode = encoding_type == "GPU"

    recommended: Recommendation
    if gpu_mode:
        recommended = _recommend_for_gpus(resources.gpus)
        if recommended.encoding_instances == 0 and recommended.transcription_instances == 0:
            recommended = _recommend_mix_by_cpu(resources.vcpu, resources.ram_gb)
            recommended.rationale = "No GPU detected, CPU recommendation applied."
    else:
        recommended = _recommend_mix_by_cpu(resources.vcpu, resources.ram_gb)
    (
        cpu_ok,
        gpu_ok,
        ratio_ok,
        required_vcpu,
        required_ram,
        required_vram,
        below_recommendation,
        config_ok,
    ) = _evaluate_configuration(
        resources=resources,
        counts=counts,
        task_sets=task_sets,
        gpu_mode=gpu_mode,
        recommended=recommended,
    )

    _print_header(
        configured_spec=configured_spec,
        configured_instances=configured_instances,
        encoding_type=encoding_type,
        resources=resources,
    )
    _print_configuration(
        counts=counts,
        gpu_mode=gpu_mode,
        required_vcpu=required_vcpu,
        required_ram=required_ram,
        required_vram=required_vram,
        cpu_ok=cpu_ok,
        gpu_ok=gpu_ok,
        ratio_ok=ratio_ok,
    )
    _print_recommendation(recommended=recommended, below_recommendation=below_recommendation)
    _print_conclusion(config_ok=config_ok, below_recommendation=below_recommendation)

    return 0 if config_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
