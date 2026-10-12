#!/usr/bin/env python3
"""Check runner instance counts against machine resources.

This script validates that RUNNER_INSTANCES and RUNNER_TASK_TYPES matches available CPU/RAM/GPU
resources and prints a recommended RUNNER_INSTANCES and RUNNER_TASK_TYPES values for the current
machine. It loads configuration from .env via app.core.config, including the independent Live
transcoder, CPU fallback and rendition profile when an instance declares the live task type.

Remarks:
- The resource requirements are planning estimates, not measured upper bounds. Adjust the constants as needed for your workload.
- Live estimates target the default three-rendition profile, with a proportional increase for larger profiles. Viewer delivery is budgeted separately and still needs CPU resources with NVENC.
- Live network estimates use video and audio bitrates for one rendition per viewer. They exclude HLS/HTTP/TLS overhead and do not validate network link capacity.
- The GPU max recommendations are based on user-provided estimates for common models. Add more models or adjust counts as needed.
- The script assumes NVIDIA GPUs and uses nvidia-smi for detection. It will not detect other GPU types or configurations.

Usage:
  uv run scripts/check_runner_resources.py
  uv run scripts/check_runner_resources.py --live-viewers 4000
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

RUNNER_ROOT = Path(__file__).resolve().parents[1]
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from app.core._check_output import format_check, format_status
from app.core.live_config import LiveSettings

# Conservative CPU/RAM requirements (upper bounds)
CPU_ENCODING_VCPU_MAX = 8
CPU_ENCODING_RAM_GB = 2
CPU_TRANSCRIPTION_VCPU = 4
CPU_TRANSCRIPTION_RAM_GB_MAX = 10
CPU_COMBINED_VCPU = max(CPU_ENCODING_VCPU_MAX, CPU_TRANSCRIPTION_VCPU)
CPU_COMBINED_RAM_GB = max(CPU_ENCODING_RAM_GB, CPU_TRANSCRIPTION_RAM_GB_MAX)

# Conservative GPU VRAM requirements (upper bounds)
GPU_ENCODING_VRAM_GB = 2
GPU_TRANSCRIPTION_VRAM_GB_MAX = 7
GPU_COMBINED_VRAM_GB = max(GPU_ENCODING_VRAM_GB, GPU_TRANSCRIPTION_VRAM_GB_MAX)

# Live planning budgets for the default three-rendition H.264/AAC profile.
# CPU values and viewer ratio come from historical Nginx RTMP sizing.
# NVENC values are initial estimates, not NVIDIA guarantees or measurements.
CPU_LIVE_VCPU_MAX = 2
CPU_LIVE_RAM_GB_MAX = 2
GPU_LIVE_VCPU_MAX = 1
GPU_LIVE_RAM_GB_MAX = 2
GPU_LIVE_VRAM_GB_MAX = 1
LIVE_REFERENCE_VARIANTS = 3
LIVE_VIEWERS_PER_VCPU = 2000

# Per-GPU max recommendations
#  - Tesla T4 (1× NVENC): Recommended 3–4 encodes per GPU (max speed at 1; ~half speed at 2).
#    Up to ~5–7 is possible, but queueing causes much longer runtimes.
#  - NVIDIA L4 (2× NVENC): Recommended 6-8 encodes per GPU (max speed at 1; ~half speed at 2).
#    Up to ~8–10 is possible, but queueing causes much longer runtimes.
#  - Nvidia L40S (3× NVENC): Recommended 10-15 encodes per GPU (one per NVENC at 3; light time-slicing at 8).
#    Up to ~15–20 per GPU is possible, mainly limited by storage (HLS writes) and CPU (AAC/mux), not NVENC itself.
# Solution to explore: Runtime control.
# -> if nvidia-smi dmon shows enc > 90–95% continuously, do not add new jobs to this GPU.
GPU_MAX_RECOMMENDATIONS = {
    "tesla t4": (4, 1),
    "nvidia l4": (7, 2),
    "nvidia l40s": (14, 4),
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
    live_instances: int = 0


@dataclass
class LiveBudget:
    """Describe Live sizing assumptions shared by all instances on this host."""

    transcoder: str = "cpu"
    cpu_fallback: bool = True
    variant_count: int = LIVE_REFERENCE_VARIANTS
    viewers: int = 0
    gpu_device: int = 0

    def requirements(self) -> Tuple[int, float, float]:
        """Return per-session vCPU, RAM and VRAM budgets, reserving CPU fallback."""
        factor = max(1.0, self.variant_count / LIVE_REFERENCE_VARIANTS)
        if self.transcoder == "nvenc":
            vcpu = CPU_LIVE_VCPU_MAX if self.cpu_fallback else GPU_LIVE_VCPU_MAX
            return (
                math.ceil(vcpu * factor),
                GPU_LIVE_RAM_GB_MAX * factor,
                GPU_LIVE_VRAM_GB_MAX * factor,
            )
        return math.ceil(CPU_LIVE_VCPU_MAX * factor), CPU_LIVE_RAM_GB_MAX * factor, 0.0

    def viewer_vcpu(self) -> int:
        """Return the delivery budget for the total concurrent viewers on this host."""
        return math.ceil(self.viewers / LIVE_VIEWERS_PER_VCPU)


@dataclass
class Recommendation:
    encoding_instances: int
    transcription_instances: int
    total_instances: int
    runner_task_types: str
    rationale: str
    live_instances: int = 0


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
    """Read total system memory in GB.

    Linux: read from /proc/meminfo.
    macOS: fallback to `sysctl -n hw.memsize`.
    """
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    kb = float(parts[1])
                    return kb / (1024.0 * 1024.0)
    except Exception:
        pass

    # macOS fallback (bytes)
    rc, out = _run(["sysctl", "-n", "hw.memsize"])
    if rc == 0:
        try:
            mem_bytes = float(out.strip().splitlines()[0])
            if mem_bytes > 0:
                return mem_bytes / (1024.0 * 1024.0 * 1024.0)
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


def _match_gpu_recommendation(gpu_name: str) -> Optional[Tuple[int, int]]:
    """Return per-GPU recommendation tuple when model is known."""
    normalized = re.sub(r"\s+", " ", gpu_name.strip().lower())
    for model, counts in GPU_MAX_RECOMMENDATIONS.items():
        if model in normalized:
            return counts
    return None


def _all_gpus_have_known_recommendations(gpus: List[GPUInfo]) -> bool:
    """Return True when every detected GPU model is present in recommendations."""
    if not gpus:
        return False
    return all(_match_gpu_recommendation(gpu.name) is not None for gpu in gpus)


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


def _get_task_counts(task_sets: List[Set[str]]) -> TaskCounts:
    """Count instances supporting encoding, transcription or Live task types."""
    if not task_sets:
        task_sets = [set()]

    total = len(task_sets)
    encoding_or_studio = 0
    transcription = 0
    live = 0
    for types in task_sets:
        has_encoding = "encoding" in types or "studio" in types
        has_transcription = "transcription" in types
        if has_encoding:
            encoding_or_studio += 1
        if has_transcription:
            transcription += 1
        if "live" in types:
            live += 1

    return TaskCounts(
        total_instances=total,
        encoding_instances=encoding_or_studio,
        transcription_instances=transcription,
        encoding_or_studio_instances=encoding_or_studio,
        live_instances=live,
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
        rec = _match_gpu_recommendation(gpu.name)
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


def _required_resources(
    task_sets: List[Set[str]],
    *,
    live: Optional[LiveBudget] = None,
    gpu_mode: Optional[bool] = None,
) -> Tuple[int, float, float]:
    """Sum per-instance maxima and the shared Live viewer delivery budget.

    Each instance runs one task at a time, so alternative task budgets are not
    added together. With gpu_mode unset, retain the CPU and GPU VOD estimates;
    an explicit mode selects VOD GPU requirements independently of Live.
    GPU VOD CPU overhead is not modeled, as in the existing VOD checker.
    """
    live = live or LiveBudget()
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
            if gpu_mode is not True:
                inst_vcpu = max(inst_vcpu, CPU_ENCODING_VCPU_MAX)
            inst_ram = max(inst_ram, CPU_ENCODING_RAM_GB)
            if gpu_mode is not False:
                inst_vram = max(inst_vram, GPU_ENCODING_VRAM_GB)

        if has_transcription:
            if gpu_mode is not True:
                inst_vcpu = max(inst_vcpu, CPU_TRANSCRIPTION_VCPU)
            inst_ram = max(inst_ram, CPU_TRANSCRIPTION_RAM_GB_MAX)
            if gpu_mode is not False:
                inst_vram = max(inst_vram, GPU_TRANSCRIPTION_VRAM_GB_MAX)

        if "live" in types:
            live_vcpu, live_ram, live_vram = live.requirements()
            inst_vcpu = max(inst_vcpu, live_vcpu)
            inst_ram = max(inst_ram, live_ram)
            inst_vram = max(inst_vram, live_vram)

        required_vcpu += inst_vcpu
        required_ram += inst_ram
        required_vram += inst_vram

    if any("live" in types for types in task_sets):
        required_vcpu += live.viewer_vcpu()
    return required_vcpu, required_ram, required_vram


def _live_gpu_ok(resources: ResourceInfo, live: LiveBudget, live_instances: int) -> bool:
    """Check Live VRAM and known encoding budgets on its configured GPU device.

    The runtime sends all Live sessions to GPU_HWACCEL_DEVICE. Each rendition
    uses a separate NVENC session; known VOD encoding maxima serve as conservative
    encoding budgets, not real-time Live benchmarks. Unknown throughput and
    driver session limits require operator validation.
    """
    if not 0 <= live.gpu_device < len(resources.gpus):
        return False
    gpu = resources.gpus[live.gpu_device]
    if live_instances * live.requirements()[2] > gpu.vram_gb:
        return False
    cap = _match_gpu_recommendation(gpu.name)
    return cap is None or live_instances * live.variant_count <= cap[0]


def _evaluate_configuration(
    *,
    resources: ResourceInfo,
    counts: TaskCounts,
    task_sets: List[Set[str]],
    gpu_mode: bool,
    recommended: Recommendation,
    live: Optional[LiveBudget] = None,
) -> Tuple[bool, bool, bool, int, float, float, bool, bool]:
    """Evaluate VOD and independent Live budgets, including CPU fallback and viewers."""
    live = live or LiveBudget()
    has_live = counts.live_instances > 0
    live_gpu = has_live and live.transcoder == "nvenc"
    vod_gpu = gpu_mode and (
        counts.encoding_or_studio_instances > 0 or counts.transcription_instances > 0
    )
    required_vcpu, required_ram, required_vram = _required_resources(
        task_sets, live=live, gpu_mode=gpu_mode if has_live else None
    )

    cpu_ok = required_vcpu <= resources.vcpu and required_ram <= resources.ram_gb
    gpu_ok = True
    if (gpu_mode and not has_live) or vod_gpu or live_gpu:
        # Known GPU models rely on explicit per-model maxima.
        # Unknown models keep conservative VRAM-based validation.
        if _all_gpus_have_known_recommendations(resources.gpus):
            caps = _recommend_for_gpus(resources.gpus) if has_live else recommended
            encoding_load = sum(
                max(
                    int(gpu_mode and bool(types & {"encoding", "studio"})),
                    live.variant_count if live_gpu and "live" in types else 0,
                )
                for types in task_sets
            )
            gpu_ok = (
                encoding_load <= caps.encoding_instances
                and (counts.transcription_instances if gpu_mode else 0)
                <= caps.transcription_instances
            )
        else:
            total_vram = sum(g.vram_gb for g in resources.gpus)
            gpu_ok = required_vram <= total_vram if total_vram > 0 else False
        if has_live:
            gpu_ok = gpu_ok and required_vram <= sum(g.vram_gb for g in resources.gpus)
        if live_gpu:
            gpu_ok = gpu_ok and _live_gpu_ok(resources, live, counts.live_instances)
    if gpu_mode and not has_live:
        cpu_ok = True

    ratio_ok = True
    if counts.transcription_instances > 0:
        ratio_ok = counts.encoding_or_studio_instances >= counts.transcription_instances

    below_recommendation = (
        counts.encoding_or_studio_instances < recommended.encoding_instances
        and counts.transcription_instances < recommended.transcription_instances
    )
    if has_live:
        below_recommendation = counts.total_instances < recommended.total_instances

    config_ok = cpu_ok and gpu_ok and ratio_ok

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


def _recommend_with_live(
    resources: ResourceInfo, task_sets: List[Set[str]], gpu_mode: bool, live: LiveBudget
) -> Recommendation:
    """Grow configured task groups proportionally while their estimated budgets fit.

    Keep dedicated and mixed groups when they fit. If no Live instance fits,
    try adding Live as an alternative task on one retained instance. Validate
    its complete budgets before suggesting it. This greedy sizing suggestion
    is not an optimal packing or a guarantee of real-time throughput.
    """
    groups: dict[frozenset[str], int] = {}
    for types in task_sets:
        group = frozenset(types)
        groups[group] = groups.get(group, 0) + 1
    allocated = dict.fromkeys(groups, 0)
    active = list(groups)
    suggested: List[Set[str]] = []
    caps = _recommend_for_gpus(resources.gpus)
    while active:
        eligible = [
            item
            for item in active
            if _get_task_counts(suggested + [set(item)]).encoding_instances
            >= _get_task_counts(suggested + [set(item)]).transcription_instances
        ]
        if not eligible:
            break
        group = min(eligible, key=lambda item: allocated[item] / groups[item])
        candidate = suggested + [set(group)]
        counts = _get_task_counts(candidate)
        result = _evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=candidate,
            gpu_mode=gpu_mode,
            recommended=caps,
            live=live,
        )
        # A group with no modeled resource cost must not grow indefinitely.
        requirements = _required_resources([set(group)], live=live, gpu_mode=gpu_mode)
        if not result[-1] or not any(requirements):
            active.remove(group)
            continue
        suggested = candidate
        allocated[group] += 1

    if not any("live" in types for types in suggested) and any(
        "live" in types for types in task_sets
    ):
        for index, types in enumerate(suggested):
            candidate = list(suggested)
            candidate[index] = types | {"live"}
            result = _evaluate_configuration(
                resources=resources,
                counts=_get_task_counts(candidate),
                task_sets=candidate,
                gpu_mode=gpu_mode,
                recommended=caps,
                live=live,
            )
            if result[-1]:
                allocated[frozenset(types)] -= 1
                mixed_group = frozenset(candidate[index])
                allocated[mixed_group] = allocated.get(mixed_group, 0) + 1
                suggested = candidate
                break

    ordered_types = ("encoding", "studio", "transcription", "live")
    parts = [
        f"{count}x({','.join(name for name in ordered_types if name in group)})"
        for group, count in allocated.items()
        if count
    ]
    counts = _get_task_counts(suggested) if suggested else TaskCounts(0, 0, 0, 0)
    return Recommendation(
        encoding_instances=counts.encoding_instances,
        transcription_instances=counts.transcription_instances,
        total_instances=counts.total_instances,
        runner_task_types=f"[{','.join(parts)}]" if parts else "",
        rationale="",
        live_instances=counts.live_instances,
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
    """Print current config and requirements with shared colored adequacy checks."""
    print("\nCurrent configuration:")
    print(f"  Total instances: {counts.total_instances}")
    print(f"  Encoding/Studio instances: {counts.encoding_or_studio_instances}")
    print(f"  Transcription instances: {counts.transcription_instances}")
    if counts.live_instances:
        print(f"  Live-capable instances: {counts.live_instances}")

    print("\nEstimated requirements (planning budgets):")
    if gpu_mode and not counts.live_instances:
        print(f"  Required vCPU: {required_vcpu} (for CPU mode)")
    else:
        print(f"  Required vCPU: {required_vcpu}")
    print(f"  Required RAM: {required_ram:.1f} GB")
    if gpu_mode:
        print(f"  Required VRAM: {required_vram:.1f} GB")

    print("\nStatus:")
    print(format_check(f"CPU/RAM adequate: {'YES' if cpu_ok else 'NO'}", ok=cpu_ok, required=True))
    if gpu_mode:
        print(
            format_check(
                f"GPU/VRAM adequate: {'YES' if gpu_ok else 'NO'}", ok=gpu_ok, required=True
            )
        )

    if not ratio_ok:
        ratio_message = "Encoding/Transcription ratio: NO (encoding should be higher)"
    else:
        ratio_message = "Encoding/Transcription ratio: YES"
    print(format_check(ratio_message, ok=ratio_ok, required=True))


def _print_recommendation(*, recommended: Recommendation, below_recommendation: bool) -> None:
    """Print recommended instance settings and shared colored sizing notices."""
    if not recommended.total_instances:
        print()
        print(
            format_status(
                "No feasible configuration fits the modeled resource budgets.", level="error"
            )
        )
        return
    print("\nRecommended RUNNER_INSTANCES:")
    print(f"  {recommended.total_instances}")
    print("Recommended RUNNER_TASK_TYPES:")
    print(f"  {recommended.runner_task_types}")
    if recommended.rationale:
        print(f"  Rationale: {recommended.rationale}")
    if below_recommendation:
        print(
            format_status(
                "Note: current configuration uses fewer instances than max recommended.",
                level="warning",
            )
        )


def _print_conclusion(*, config_ok: bool, below_recommendation: bool) -> None:
    """Print final adequacy conclusion with severity coloring."""
    print("\nConclusion:")
    if config_ok:
        if below_recommendation:
            print(
                format_status("Configuration is adequate (below max recommended)", level="warning")
            )
        else:
            print(format_status("Configuration is adequate", level="info"))
    else:
        print(format_status("Configuration is NOT adequate", level="error"))


def _nonnegative_int(value: str) -> int:
    """Parse a nonnegative CLI integer, rejecting invalid viewer counts."""
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a nonnegative integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def _parse_args() -> argparse.Namespace:
    """Parse the optional total Live viewer budget for this Runner host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-viewers",
        type=_nonnegative_int,
        default=0,
        metavar="COUNT",
        help="total concurrent viewers served by this host across all Live sessions "
        "(default: 0; viewer delivery excluded)",
    )
    return parser.parse_args()


def _print_live_budget(live: LiveBudget) -> None:
    """Print non-sensitive Live assumptions with shared colored sizing warnings."""
    vcpu, ram, vram = live.requirements()
    print("\nLive planning assumptions:")
    print(f"  LIVE_TRANSCODER: {live.transcoder} (independent of ENCODING_TYPE)")
    print(f"  Renditions per session: {live.variant_count}")
    print(f"  Per session: {vcpu} vCPU / {ram:.1f} GB RAM / {vram:.1f} GB VRAM")
    if live.transcoder == "nvenc":
        print(f"  GPU_HWACCEL_DEVICE: {live.gpu_device}")
        print(f"  NVENC sessions per Live: {live.variant_count}")
        print(f"  CPU fallback reserved: {'YES' if live.cpu_fallback else 'NO'}")
    print(f"  Total concurrent viewers on this host: {live.viewers}")
    print(f"  Viewer delivery: +{live.viewer_vcpu()} vCPU (1 per {LIVE_VIEWERS_PER_VCPU} viewers)")
    if not live.viewers:
        print(
            format_status(
                "Viewer delivery is excluded; set --live-viewers to size it.", level="warning"
            )
        )
    print(
        format_status(
            "Historical RTMP / initial NVENC estimates; benchmark the actual HLS workload.",
            level="warning",
        )
    )
    print(
        format_status(
            "Larger resolutions, RAM-backed HLS storage, network/TLS and NVENC driver limits "
            "need separate validation.",
            level="warning",
        )
    )


def _print_live_network_impact(settings: LiveSettings, viewers: int) -> None:
    """Print nominal per-viewer and aggregate outbound Live bandwidth in Mbps.

    Each viewer receives one rendition's video and audio. Use the configured
    minimum/maximum rendition bitrates and the host-wide viewer count, without
    multiplying by the session count. Exclude transport overhead and peaks;
    this report assumes direct HLS delivery and does not inspect network links.
    """
    bitrates = [variant.video_bitrate + variant.audio_bitrate for variant in settings.variants]
    low_mbps = min(bitrates) / 1_000_000
    high_mbps = max(bitrates) / 1_000_000
    print("\nLive network impact:")
    print(
        format_status(
            "Each concurrent viewer consumes outbound bandwidth on the server serving HLS, "
            "with either CPU or NVENC transcoding.",
            level="info",
        )
    )
    print(f"  Per viewer (one rendition, video + audio): {low_mbps:.3f} - {high_mbps:.3f} Mbps")
    if viewers:
        print(
            f"  Outbound payload estimate for {viewers} viewers: "
            f"{viewers * low_mbps:.1f} - {viewers * high_mbps:.1f} Mbps"
        )
    else:
        print("  Total outbound bandwidth: not estimated; set --live-viewers.")
    print(
        format_status(
            "Plan egress for the highest rendition plus HLS/HTTP/TLS overhead and traffic peaks; "
            "network link capacity is not measured or validated by this script.",
            level="warning",
        )
    )
    print("  Estimates assume direct HLS delivery; a cache/CDN can reduce Runner traffic.")


def main() -> int:
    """Entry point for the resource check script."""
    args = _parse_args()
    cfg, parse_grouped, parse_csv = _load_config()
    resources = _get_resources()
    task_sets = _get_task_sets(cfg, parse_grouped, parse_csv)
    counts = _get_task_counts(task_sets)

    configured_spec = os.getenv("RUNNER_TASK_TYPES", "")
    configured_instances = os.getenv("RUNNER_INSTANCES", str(getattr(cfg, "RUNNER_INSTANCES", 1)))

    encoding_type = str(getattr(cfg, "ENCODING_TYPE", "CPU")).upper()
    gpu_mode = encoding_type == "GPU"
    live = LiveBudget()
    # Task declarations enable Live; saved MediaMTX credentials do not.
    if counts.live_instances:
        live = LiveBudget(
            transcoder=cfg.LIVE.transcoder,
            cpu_fallback=cfg.LIVE.cpu_fallback,
            variant_count=len(cfg.LIVE.variants),
            viewers=args.live_viewers,
            gpu_device=cfg.GPU_HWACCEL_DEVICE,
        )

    recommended: Recommendation
    if counts.live_instances:
        recommended = _recommend_with_live(resources, task_sets, gpu_mode, live)
    elif gpu_mode:
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
        live=live,
    )

    _print_header(
        configured_spec=configured_spec,
        configured_instances=configured_instances,
        encoding_type=encoding_type,
        resources=resources,
    )
    if counts.live_instances:
        _print_live_budget(live)
        _print_live_network_impact(cfg.LIVE, live.viewers)
    uses_gpu = (
        gpu_mode
        if not counts.live_instances
        else (
            gpu_mode
            and (counts.encoding_or_studio_instances > 0 or counts.transcription_instances > 0)
        )
        or (counts.live_instances > 0 and live.transcoder == "nvenc")
    )
    _print_configuration(
        counts=counts,
        gpu_mode=uses_gpu,
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
