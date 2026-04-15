import builtins
import io

from scripts import check_runner_resources as crr


def _task_sets(*, encoding: int, transcription: int):
    task_sets = [set(["encoding", "studio"]) for _ in range(encoding)]
    task_sets.extend(set(["transcription"]) for _ in range(transcription))
    return task_sets


def test_read_meminfo_gb_from_linux_proc(monkeypatch):
    mem_kb = 32 * 1024 * 1024  # 32 GiB in kB
    fake_meminfo = f"MemTotal:       {mem_kb} kB\nMemFree:        1234 kB\n"

    def fake_open(path, mode="r", encoding=None):
        assert path == "/proc/meminfo"
        return io.StringIO(fake_meminfo)

    monkeypatch.setattr(builtins, "open", fake_open)
    monkeypatch.setattr(crr, "_run", lambda *_args, **_kwargs: (127, ""))

    assert crr._read_meminfo_gb() == 32.0


def test_read_meminfo_gb_uses_macos_sysctl_fallback(monkeypatch):
    def failing_open(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(builtins, "open", failing_open)
    monkeypatch.setattr(crr, "_run", lambda *_args, **_kwargs: (0, "17179869184\n"))  # 16 GiB

    assert crr._read_meminfo_gb() == 16.0


def test_gpu_known_models_ignore_vram_formula_and_use_model_caps():
    resources = crr.ResourceInfo(
        vcpu=64,
        ram_gb=188.6,
        gpus=[
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
        ],
    )
    task_sets = _task_sets(encoding=14, transcription=4)
    counts = crr._get_task_counts(task_sets)
    recommended = crr._recommend_for_gpus(resources.gpus)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=recommended,
        )
    )

    assert recommended.encoding_instances == 14
    assert recommended.transcription_instances == 4
    assert required_vram == 56.0
    assert gpu_ok is True
    assert config_ok is True


def test_gpu_known_models_reject_over_model_cap_even_if_vram_is_enough():
    resources = crr.ResourceInfo(
        vcpu=64,
        ram_gb=188.6,
        gpus=[
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
            crr.GPUInfo(name="NVIDIA L4", vram_gb=22.5),
        ],
    )
    task_sets = _task_sets(encoding=15, transcription=0)
    counts = crr._get_task_counts(task_sets)
    recommended = crr._recommend_for_gpus(resources.gpus)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=recommended,
        )
    )

    assert required_vram == 30.0  # <= 45.0 total VRAM, so VRAM alone would pass.
    assert recommended.encoding_instances == 14
    assert gpu_ok is False
    assert config_ok is False


def test_gpu_unknown_models_keep_vram_based_validation():
    resources = crr.ResourceInfo(
        vcpu=32,
        ram_gb=64.0,
        gpus=[crr.GPUInfo(name="Unknown GPU", vram_gb=10.0)],
    )
    task_sets = _task_sets(encoding=6, transcription=0)
    counts = crr._get_task_counts(task_sets)

    _cpu_ok, gpu_ok, _ratio_ok, _required_vcpu, _required_ram, required_vram, _below, config_ok = (
        crr._evaluate_configuration(
            resources=resources,
            counts=counts,
            task_sets=task_sets,
            gpu_mode=True,
            recommended=crr.Recommendation(
                encoding_instances=999,
                transcription_instances=999,
                total_instances=1998,
                runner_task_types="",
                rationale="",
            ),
        )
    )

    assert required_vram == 12.0
    assert gpu_ok is False
    assert config_ok is False
