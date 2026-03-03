import builtins
import io

from scripts import check_runner_resources as crr


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
