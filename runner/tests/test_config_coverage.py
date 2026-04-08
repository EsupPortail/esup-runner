import builtins
import importlib.util
import sys
import types
import warnings
from pathlib import Path

import pytest

import app.core.config as config_module
import app.core.state as state_module


def test_parse_helpers_cover_defaults_invalid_values_and_bounds():
    assert config_module._parse_bool("true") is True
    assert config_module._parse_bool("off") is False
    assert config_module._parse_bool("maybe", default=True) is True

    assert config_module._parse_int("4", 1, min_value=2, max_value=5) == 4
    assert config_module._parse_int("nope", 7) == 7
    assert config_module._parse_int("-3", 0, min_value=0) == 0
    assert config_module._parse_int("99", 0, max_value=10) == 10

    assert config_module._parse_float("1.25", 0.0, min_value=1.0, max_value=2.0) == 1.25
    assert config_module._parse_float("bad", 2.5) == 2.5
    assert config_module._parse_float("-1.0", 0.0, min_value=0.5) == 0.5
    assert config_module._parse_float("9.0", 0.0, max_value=3.0) == 3.0


def test_get_config_loads_environment_only_once(monkeypatch):
    calls = {"load": 0, "init": 0}

    class FakeConfig:
        def __init__(self):
            calls["init"] += 1
            self.marker = calls["init"]

    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", None)
    monkeypatch.setattr(config_module, "_CONFIG_ENV_LOADED", False)
    monkeypatch.setattr(
        config_module,
        "_load_environment_variables",
        lambda: calls.__setitem__("load", calls["load"] + 1),
    )
    monkeypatch.setattr(config_module, "Config", FakeConfig)

    first = config_module.get_config()
    second = config_module.get_config()

    assert first is second
    assert calls == {"load": 1, "init": 1}


def test_reload_config_from_env_initializes_when_no_cached_instance(monkeypatch):
    calls = {"load": 0}

    class FakeConfig:
        def __init__(self):
            self.value = "fresh"

    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", None)
    monkeypatch.setattr(config_module, "_CONFIG_ENV_LOADED", False)
    monkeypatch.setattr(
        config_module,
        "_load_environment_variables",
        lambda: calls.__setitem__("load", calls["load"] + 1),
    )
    monkeypatch.setattr(config_module, "Config", FakeConfig)

    refreshed = config_module.reload_config_from_env()

    assert refreshed.value == "fresh"
    assert config_module.config is refreshed
    assert calls["load"] == 1


def test_reload_config_from_env_updates_existing_instance_in_place(monkeypatch):
    existing = types.SimpleNamespace(old_value="stale")

    class FakeConfig:
        def __init__(self):
            self.new_value = "fresh"

    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", existing)
    monkeypatch.setattr(config_module, "_CONFIG_ENV_LOADED", True)
    monkeypatch.setattr(config_module, "Config", FakeConfig)

    refreshed = config_module.reload_config_from_env()

    assert refreshed is existing
    assert refreshed.new_value == "fresh"
    assert not hasattr(refreshed, "old_value")


def test_load_environment_variables_warns_when_dotenv_is_missing(monkeypatch, capsys):
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "dotenv":
            raise ImportError("missing dotenv")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(config_module.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(builtins, "__import__", fake_import)

    config_module._load_environment_variables()

    out = capsys.readouterr().out
    assert "python-dotenv not installed" in out


def test_load_environment_variables_warns_when_env_file_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(config_module.os.path, "exists", lambda _path: False)

    config_module._load_environment_variables()

    out = capsys.readouterr().out
    assert "no .env file found" in out


def test_config_warns_when_grouped_task_types_override_runner_instances(monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_TYPES", "[2x(encoding),1x(studio)]")
    monkeypatch.setenv("RUNNER_INSTANCES", "1")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = config_module.Config()

    assert cfg.RUNNER_INSTANCES == 3
    assert any("RUNNER_INSTANCES is ignored" in str(w.message) for w in caught)


def test_config_warns_when_grouped_task_types_ignore_invalid_runner_instances(monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_TYPES", "[1x(encoding)]")
    monkeypatch.setenv("RUNNER_INSTANCES", "invalid")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = config_module.Config()

    assert cfg.RUNNER_INSTANCES == 1
    assert any("Invalid RUNNER_INSTANCES value ignored" in str(w.message) for w in caught)


def test_config_raises_for_grouped_instance_out_of_range(monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_TYPES", "[1x(encoding)]")
    monkeypatch.setenv("RUNNER_INSTANCES", "1")
    monkeypatch.setenv("RUNNER_INSTANCE_ID", "4")

    with pytest.raises(ValueError, match="out of range"):
        config_module.Config()


def test_config_uses_legacy_task_type_distribution(monkeypatch):
    monkeypatch.setenv("RUNNER_TASK_TYPES", "encoding,studio")
    monkeypatch.setenv("RUNNER_INSTANCES", "2")
    monkeypatch.delenv("RUNNER_INSTANCE_ID", raising=False)

    cfg = config_module.Config()

    assert cfg.RUNNER_INSTANCES == 2
    assert cfg.RUNNER_TASK_TYPES == {"encoding", "studio"}
    assert cfg.RUNNER_TASK_TYPES_BY_INSTANCE == [{"encoding", "studio"}, {"encoding", "studio"}]


def test_config_validate_configuration_success(monkeypatch):
    monkeypatch.setenv("RUNNER_TOKEN", "secure-token")
    monkeypatch.setenv("RUNNER_TASK_TYPES", "encoding")
    monkeypatch.setenv("RUNNER_INSTANCES", "1")
    monkeypatch.setenv("RUNNER_BASE_PORT", "8082")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://example.org")
    monkeypatch.delenv("CORS_ALLOW_CREDENTIALS", raising=False)
    monkeypatch.delenv("ENCODING_TYPE", raising=False)

    cfg = config_module.Config()
    cfg.validate_configuration()


def test_config_validate_instances_requires_at_least_one():
    cfg = object.__new__(config_module.Config)
    cfg.RUNNER_INSTANCES = 0

    with pytest.raises(ValueError, match="at least 1"):
        cfg._validate_instances()


def test_config_validate_task_types_requires_non_empty_instances():
    cfg = object.__new__(config_module.Config)

    with pytest.raises(ValueError, match="define at least one instance"):
        cfg._validate_task_types()

    cfg.RUNNER_TASK_TYPES_BY_INSTANCE = []
    with pytest.raises(ValueError, match="define at least one instance"):
        cfg._validate_task_types()

    cfg.RUNNER_TASK_TYPES_BY_INSTANCE = [set()]
    with pytest.raises(ValueError, match="instance 0 has no task types"):
        cfg._validate_task_types()


def test_config_validate_ports_tokens_cors_and_gpu(monkeypatch):
    cfg = object.__new__(config_module.Config)
    cfg.RUNNER_BASE_PORT = 79
    with pytest.raises(ValueError, match="between 80 and 65535"):
        cfg._validate_ports()

    cfg.RUNNER_TOKEN = "default-runner-token"
    with pytest.raises(ValueError, match="secure value"):
        cfg._validate_tokens()

    cfg.CORS_ALLOW_CREDENTIALS = True
    cfg.CORS_ALLOW_ORIGINS = ["*"]
    with pytest.raises(ValueError, match="Invalid CORS configuration"):
        cfg._validate_cors()

    cfg.ENCODING_TYPE = "CPU"
    cfg._validate_gpu()

    cfg.ENCODING_TYPE = "GPU"
    cfg.GPU_CUDA_PATH = "/missing/cuda"
    monkeypatch.setattr(config_module.os.path, "exists", lambda _path: False)
    with pytest.raises(ValueError, match="CUDA directory not found"):
        cfg._validate_gpu()


def test_grouped_task_type_parsing_helpers_cover_error_paths(monkeypatch):
    assert config_module._normalize_grouped_task_types_spec(None) is None
    assert config_module._normalize_grouped_task_types_spec("  ") is None
    assert config_module._normalize_grouped_task_types_spec("encoding,studio") is None
    assert config_module._normalize_grouped_task_types_spec("[2x(encoding),1x(studio)]") == (
        "2x(encoding),1x(studio)"
    )

    monkeypatch.setattr(config_module.re, "search", lambda *_args, **_kwargs: object())
    with pytest.raises(ValueError, match="grouped syntax is empty"):
        config_module._normalize_grouped_task_types_spec("[]")

    with pytest.raises(ValueError, match="near"):
        config_module._expand_grouped_task_types("oops")

    with pytest.raises(ValueError, match="multiplier must be >= 1"):
        config_module._expand_grouped_task_types("0x(encoding)")

    with pytest.raises(ValueError, match="at least one task type"):
        config_module._expand_grouped_task_types("1x()")

    with pytest.raises(ValueError, match="produced no instances"):
        config_module._expand_grouped_task_types("")


def test_config_module_auto_validates_outside_pytest(monkeypatch):
    config_path = Path(config_module.__file__)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("RUNNER_TOKEN", "secure-token")
    monkeypatch.setenv("RUNNER_TASK_TYPES", "encoding")
    monkeypatch.setenv("RUNNER_INSTANCES", "1")
    monkeypatch.setenv("RUNNER_BASE_PORT", "8082")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://example.org")
    monkeypatch.setattr(sys, "argv", ["python"])
    monkeypatch.delitem(sys.modules, "pytest", raising=False)

    spec = importlib.util.spec_from_file_location("runner_config_no_pytest", config_path)
    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.config.RUNNER_TOKEN == "secure-token"
    assert module._is_pytest_run() is False


def test_state_helpers_cover_attempts_urls_heartbeat_and_uptime():
    snapshot = state_module._RUNNER_STATE.copy()
    try:
        state_module._RUNNER_STATE["registration_attempts"] = 0
        assert state_module.increment_registration_attempts() == 1
        assert state_module.get_registration_attempts() == 1

        state_module.set_manager_url("http://manager.example.org")
        assert state_module.get_manager_url() == "http://manager.example.org"

        state_module.update_heartbeat()
        assert isinstance(state_module.get_last_heartbeat(), float)

        state_module.set_startup_time()
        startup = state_module.get_startup_time()
        assert isinstance(startup, float)
        assert state_module.get_uptime() >= 0

        state_module._RUNNER_STATE["startup_time"] = None
        assert state_module.get_uptime() is None
    finally:
        state_module._RUNNER_STATE.clear()
        state_module._RUNNER_STATE.update(snapshot)
