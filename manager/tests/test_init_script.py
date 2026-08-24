"""Validates manager directory initialization and service-account ownership."""

import importlib.util
from pathlib import Path


def _load_init_script_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "init.py"
    spec = importlib.util.spec_from_file_location("manager_init_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_directories_defaults_follow_service_user(monkeypatch):
    """Validate default manager directories derive from SERVICE_USER."""
    init_script = _load_init_script_module()

    for key in (*init_script.ENV_KEYS, "SERVICE_USER", "LOG_DIRECTORY", "RUNNERS_STORAGE_PATH"):
        monkeypatch.delenv(key, raising=False)

    directories = list(init_script.collect_directories({"SERVICE_USER": "media-manager"}))

    cache_dir = Path("/home/media-manager/.cache/esup-runner")
    assert Path("/var/log/esup-runner") in directories
    assert Path("/tmp/esup-runner") in directories
    assert cache_dir in directories
    assert cache_dir / "uv" in directories


def test_resolve_target_uid_gid_uses_service_account(monkeypatch):
    """Validate directory ownership resolves the configured account."""
    init_script = _load_init_script_module()

    account = type("Account", (), {"pw_uid": 2345, "pw_gid": 6789})()
    monkeypatch.setattr(
        init_script.pwd,
        "getpwnam",
        lambda user: account if user == "media-manager" else None,
    )

    assert init_script.resolve_target_uid_gid("media-manager") == (2345, 6789)
