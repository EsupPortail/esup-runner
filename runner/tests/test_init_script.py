import importlib.util
from pathlib import Path


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
    init_script = _load_init_script_module()

    for key in init_script.ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LOG_DIRECTORY", raising=False)

    directories = list(init_script.collect_directories({}))

    cache_dir = Path(init_script.DEFAULT_DIRECTORY_VALUES["CACHE_DIR"])
    assert cache_dir in directories
    assert cache_dir / "huggingface" in directories
    assert cache_dir / "whisper-models" in directories
    assert cache_dir / "uv" in directories
    assert Path(init_script.DEFAULT_DIRECTORY_VALUES["STORAGE_DIR"]) in directories
    assert Path(init_script.DEFAULT_DIRECTORY_VALUES["LOG_DIR"]) in directories


def test_collect_directories_prefers_process_environment_over_defaults(monkeypatch, tmp_path):
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


def test_collect_directories_supports_legacy_log_directory_alias(monkeypatch, tmp_path):
    init_script = _load_init_script_module()

    legacy_log_dir = tmp_path / "legacy-logs"
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.setenv("LOG_DIRECTORY", str(legacy_log_dir))

    directories = list(init_script.collect_directories({}))

    assert legacy_log_dir in directories
