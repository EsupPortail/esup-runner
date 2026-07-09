"""Runtime df-like disk usage diagnostics for runner status."""

from types import SimpleNamespace

from app.core import disk_usage


def test_humanize_bytes_returns_df_like_values():
    """Validate byte formatting uses compact df-like units."""
    assert disk_usage._humanize_bytes(0) == "0B"
    assert disk_usage._humanize_bytes(512) == "512B"
    assert disk_usage._humanize_bytes(1536) == "1.5K"
    assert disk_usage._humanize_bytes(2 * 1024**3) == "2.0G"


def test_status_for_used_percent_thresholds():
    """Validate runtime disk usage status thresholds."""
    assert disk_usage._status_for_used_percent(None) == "unknown"
    assert disk_usage._status_for_used_percent(74.9) == "green"
    assert disk_usage._status_for_used_percent(75.0) == "orange"
    assert disk_usage._status_for_used_percent(89.9) == "orange"
    assert disk_usage._status_for_used_percent(90.0) == "red"


def test_worst_status_returns_most_severe_status():
    """Validate runtime disk usage aggregate status severity."""
    assert disk_usage._worst_status(["green", "green"]) == "green"
    assert disk_usage._worst_status(["green", "unknown"]) == "unknown"
    assert disk_usage._worst_status(["green", "orange"]) == "orange"
    assert disk_usage._worst_status(["orange", "red"]) == "red"


def test_find_existing_parent_returns_none_when_no_parent_exists(monkeypatch):
    """Validate runtime disk usage parent lookup handles missing roots."""
    monkeypatch.setattr(disk_usage.Path, "exists", lambda _self: False)

    assert disk_usage._find_existing_parent(disk_usage.Path("/missing")) is None


def test_usage_for_path_returns_filesystem_values(monkeypatch, tmp_path):
    """Validate runtime disk usage reads filesystem totals for the target path."""

    class Usage:
        total = 100 * 1024**3
        used = 76 * 1024**3
        free = 24 * 1024**3

    monkeypatch.setattr(disk_usage.shutil, "disk_usage", lambda _path: Usage())

    payload = disk_usage._usage_for_path(str(tmp_path), "Runner storage")

    assert payload["path"] == str(tmp_path)
    assert payload["target_path"] == str(tmp_path)
    assert payload["description"] == "Runner storage"
    assert payload["exists"] is True
    assert payload["total_human"] == "100.0G"
    assert payload["used_human"] == "76.0G"
    assert payload["free_human"] == "24.0G"
    assert payload["used_percent"] == 76.0
    assert payload["used_percent_display"] == "76.0%"
    assert payload["status"] == "orange"
    assert payload["error"] == ""


def test_usage_for_path_uses_existing_parent_for_missing_directory(monkeypatch, tmp_path):
    """Validate runtime disk usage falls back to an existing parent path."""

    class Usage:
        total = 10 * 1024**3
        used = 1 * 1024**3
        free = 9 * 1024**3

    monkeypatch.setattr(disk_usage.shutil, "disk_usage", lambda _path: Usage())

    payload = disk_usage._usage_for_path(str(tmp_path / "missing" / "leaf"), "Missing")

    assert payload["exists"] is False
    assert payload["target_path"] == str(tmp_path)
    assert payload["status"] == "green"


def test_usage_for_path_handles_missing_parent(monkeypatch, tmp_path):
    """Validate runtime disk usage handles paths without existing parents."""
    monkeypatch.setattr(disk_usage, "_find_existing_parent", lambda _path: None)

    payload = disk_usage._usage_for_path(str(tmp_path / "missing"), "Missing")

    assert payload["target_path"] == ""
    assert payload["total_human"] == "0B"
    assert payload["used_percent"] is None
    assert payload["used_percent_display"] == "n/a"
    assert payload["status"] == "unknown"
    assert payload["error"] == "No existing parent path found."


def test_usage_for_path_handles_disk_usage_error(monkeypatch, tmp_path):
    """Validate runtime disk usage handles OS errors."""

    def _raise_disk_usage(_path):
        raise OSError("df failed")

    monkeypatch.setattr(disk_usage.shutil, "disk_usage", _raise_disk_usage)

    payload = disk_usage._usage_for_path(str(tmp_path), "Storage")

    assert payload["target_path"] == str(tmp_path)
    assert payload["exists"] is True
    assert payload["status"] == "unknown"
    assert payload["error"] == "df failed"


def test_collect_disk_usage_returns_runtime_directories(monkeypatch):
    """Validate runtime disk usage payload contains runner storage and caches."""
    statuses = {
        "STORAGE_DIR": "green",
        "CACHE_DIR": "orange",
        "WHISPER_MODELS_DIR": "green",
        "HUGGINGFACE_MODELS_DIR": "green",
        "UV_CACHE_DIR": "green",
        "LOG_DIR": "green",
    }

    def _fake_usage(path, description):
        key = path.rsplit("/", 1)[-1].upper().replace("-", "_")
        status = statuses.get(key, "green")
        return {
            "path": path,
            "target_path": path,
            "description": description,
            "exists": True,
            "total_bytes": 100,
            "used_bytes": 50,
            "free_bytes": 50,
            "total_human": "100B",
            "used_human": "50B",
            "free_human": "50B",
            "used_percent": 50.0,
            "used_percent_display": "50.0%",
            "status": status,
            "error": "",
        }

    cfg = SimpleNamespace(
        STORAGE_DIR="/tmp/storage-dir",
        CACHE_DIR="/tmp/cache-dir",
        WHISPER_MODELS_DIR="/tmp/whisper-models",
        HUGGINGFACE_MODELS_DIR="/tmp/huggingface-models",
        UV_CACHE_DIR="/tmp/uv-cache",
        LOG_DIR="/tmp/log-dir",
    )
    monkeypatch.setattr(disk_usage, "_usage_for_path", _fake_usage)

    payload = disk_usage.collect_disk_usage(cfg)

    assert payload["status"] == "orange"
    assert payload["ok"] is True
    assert payload["output_dir_pattern"] == "/tmp/storage-dir/<task_id>/output"
    assert set(payload["directories"]) == {
        "STORAGE_DIR",
        "CACHE_DIR",
        "WHISPER_MODELS_DIR",
        "HUGGINGFACE_MODELS_DIR",
        "UV_CACHE_DIR",
        "LOG_DIR",
    }
    assert payload["thresholds"]["orange_from_used_percent"] == 75.0
    assert payload["thresholds"]["red_from_used_percent"] == 90.0
