from scripts import check_gpu as cgp


def test_probe_torch_runtime_reports_cpu_only_build(monkeypatch):
    class _FakeTorchCpu:
        __version__ = "2.0.0+cpu"

        class version:
            cuda = None

        class cuda:
            @staticmethod
            def is_available():
                return False

            @staticmethod
            def device_count():
                return 0

    monkeypatch.setattr(cgp, "_import_torch_module", lambda: _FakeTorchCpu)
    result, info = cgp._probe_torch_runtime()

    assert result.ok is False
    assert result.required is True
    assert "CPU-only" in result.details
    assert info["torch"] == "2.0.0+cpu"
    assert info["cuda_build"] is None
    assert info["cuda_available"] is False
    assert info["gpu_count"] == 0


def test_probe_torch_runtime_reports_cuda_available(monkeypatch):
    class _FakeTorchGpu:
        __version__ = "2.1.0"

        class version:
            cuda = "13.0"

        class cuda:
            @staticmethod
            def is_available():
                return True

            @staticmethod
            def device_count():
                return 2

            @staticmethod
            def get_device_name(idx):
                return f"Fake GPU {idx}"

    monkeypatch.setattr(cgp, "_import_torch_module", lambda: _FakeTorchGpu)
    result, info = cgp._probe_torch_runtime()

    assert result.ok is True
    assert result.required is True
    assert info["torch"] == "2.1.0"
    assert info["cuda_build"] == "13.0"
    assert info["cuda_available"] is True
    assert info["gpu_count"] == 2
    assert info["gpu_names"] == ["Fake GPU 0", "Fake GPU 1"]


def test_probe_nvidia_smi_counts_detected_gpus(monkeypatch):
    monkeypatch.setattr(
        cgp,
        "_run",
        lambda *_args, **_kwargs: (
            0,
            "NVIDIA L4, 570.124.06, 23034 MiB\nNVIDIA L4, 570.124.06, 23034 MiB\n",
        ),
    )
    result = cgp._probe_nvidia_smi()
    assert result.ok is True
    assert result.required is False
    assert result.details == "2 GPU(s) detected"
