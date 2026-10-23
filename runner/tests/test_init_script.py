"""Validates directory collection from environment variables and configuration file handling."""

import importlib.util
from pathlib import Path

import pytest


def _load_init_script_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "init.py"
    spec = importlib.util.spec_from_file_location("init_script", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_directories_uses_defaults_when_env_file_key_is_missing(monkeypatch):
    """Validate Collect directories uses defaults when env file key is missing."""
    init_script = _load_init_script_module()

    for key in init_script.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LOG_DIRECTORY", raising=False)

    directories = list(init_script.collect_directories({}))

    cache_dir = Path(f"/home/{init_script.DEFAULT_SERVICE_USER}/.cache/esup-runner")
    assert cache_dir.parent in directories
    assert cache_dir in directories
    assert cache_dir / "huggingface" in directories
    assert cache_dir / "whisper-models" in directories
    assert cache_dir / "uv" in directories
    assert Path(init_script.DEFAULT_DIRECTORY_VALUES["STORAGE_DIR"]) in directories
    assert Path(init_script.DEFAULT_DIRECTORY_VALUES["LOG_DIR"]) in directories


def test_collect_directories_prefers_process_environment_over_defaults(monkeypatch, tmp_path):
    """Validate Collect directories prefers process environment over defaults."""
    init_script = _load_init_script_module()

    custom_dir = tmp_path / "cache-root"
    monkeypatch.delenv("WHISPER_MODELS_DIR", raising=False)
    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setenv("CACHE_DIR", str(custom_dir))

    directories = list(init_script.collect_directories({}))

    assert custom_dir in directories
    assert custom_dir / "huggingface" in directories
    assert custom_dir / "whisper-models" in directories
    assert custom_dir / "uv" in directories


def test_collect_directories_uses_env_file_value_before_default(monkeypatch, tmp_path):
    """Validate Collect directories uses env file value before default."""
    init_script = _load_init_script_module()

    custom_dir = tmp_path / "cache-root-from-env-file"
    monkeypatch.delenv("WHISPER_MODELS_DIR", raising=False)
    monkeypatch.delenv("HUGGINGFACE_MODELS_DIR", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("CACHE_DIR", raising=False)

    directories = list(init_script.collect_directories({"CACHE_DIR": str(custom_dir)}))

    assert custom_dir in directories
    assert custom_dir / "huggingface" in directories
    assert custom_dir / "whisper-models" in directories
    assert custom_dir / "uv" in directories


def test_collect_directories_default_cache_follows_service_user(monkeypatch):
    """Validate the default cache root follows SERVICE_USER from .env."""
    init_script = _load_init_script_module()

    for key in init_script.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SERVICE_USER", raising=False)

    directories = list(init_script.collect_directories({"SERVICE_USER": "media-runner"}))

    cache_dir = Path("/home/media-runner/.cache/esup-runner")
    assert cache_dir.parent in directories
    assert cache_dir in directories
    assert cache_dir / "huggingface" in directories
    assert cache_dir / "whisper-models" in directories
    assert cache_dir / "uv" in directories


def test_resolve_target_uid_gid_uses_service_account(monkeypatch):
    """Validate directory ownership resolves the configured account."""
    init_script = _load_init_script_module()

    account = type("Account", (), {"pw_uid": 1234, "pw_gid": 5678})()
    monkeypatch.setattr(
        init_script.pwd, "getpwnam", lambda user: account if user == "media" else None
    )

    assert init_script.resolve_target_uid_gid("media") == (1234, 5678)


def test_collect_directories_supports_legacy_log_directory_alias(monkeypatch, tmp_path):
    """Validate Collect directories supports legacy log directory alias."""
    init_script = _load_init_script_module()

    legacy_log_dir = tmp_path / "legacy-logs"
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.setenv("LOG_DIRECTORY", str(legacy_log_dir))

    directories = list(init_script.collect_directories({}))

    assert legacy_log_dir in directories


def test_collect_directories_cache_root_follows_service_user_environment():
    """Use the process service account even when .env configures another one."""
    init_script = _load_init_script_module()

    directories = list(
        init_script.collect_directories(
            {"SERVICE_USER": "dotenv-runner"}, {"SERVICE_USER": "process-runner"}
        )
    )

    assert Path("/home/process-runner/.cache") in directories
    assert Path("/home/dotenv-runner/.cache") not in directories


@pytest.mark.parametrize("already_exists", [False, True])
def test_ensure_directory_assigns_cache_root_to_service_account(
    monkeypatch, tmp_path, already_exists
):
    """Create or repair .cache without changing its existing parent or contents."""
    init_script = _load_init_script_module()
    cache_root = tmp_path / ".cache"
    if already_exists:
        (cache_root / "uv").mkdir(parents=True)
        (cache_root / "uv" / "cached-file").write_text("keep", encoding="utf-8")
    ownership_changes = []
    monkeypatch.setattr(
        init_script.os,
        "chown",
        lambda path, uid, gid, **kwargs: ownership_changes.append((path, uid, gid, kwargs)),
    )

    init_script.ensure_directory(cache_root, 1234, 5678)

    assert cache_root.is_dir()
    assert ownership_changes == [(cache_root, 1234, 5678, {"follow_symlinks": False})]
    if already_exists:
        assert (cache_root / "uv" / "cached-file").read_text(encoding="utf-8") == "keep"


def test_ensure_directory_assigns_new_parents_to_service_account(monkeypatch, tmp_path):
    """Keep new intermediate directories writable without chowning existing ancestors."""
    init_script = _load_init_script_module()
    cache_root = tmp_path / ".cache"
    target = cache_root / "esup-runner" / "uv"
    ownership_changes = []
    monkeypatch.setattr(
        init_script.os,
        "chown",
        lambda path, uid, gid, **kwargs: ownership_changes.append((path, uid, gid, kwargs)),
    )

    init_script.ensure_directory(target, 1234, 5678)

    assert target.is_dir()
    assert ownership_changes == [
        (cache_root, 1234, 5678, {"follow_symlinks": False}),
        (cache_root / "esup-runner", 1234, 5678, {"follow_symlinks": False}),
        (target, 1234, 5678, {"follow_symlinks": False}),
    ]


def test_ensure_directory_does_not_reassign_symlink_target(monkeypatch, tmp_path):
    """Do not transfer a symlink's destination to the service account."""
    init_script = _load_init_script_module()
    shared_cache = tmp_path / "shared-cache"
    shared_cache.mkdir()
    cache_root = tmp_path / ".cache"
    cache_root.symlink_to(shared_cache, target_is_directory=True)
    ownership_changes = []
    monkeypatch.setattr(
        init_script.os,
        "chown",
        lambda path, uid, gid, **kwargs: ownership_changes.append((path, uid, gid, kwargs)),
    )

    init_script.ensure_directory(cache_root, 1234, 5678)

    assert cache_root.is_symlink()
    assert ownership_changes == [(cache_root, 1234, 5678, {"follow_symlinks": False})]
