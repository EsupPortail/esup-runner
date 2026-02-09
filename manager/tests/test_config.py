import builtins
import sys
from types import ModuleType

import pytest


def test_parse_helpers_cover_edge_cases():
    from app.core import config as cfg

    assert cfg._parse_bool(None, default=True) is True
    assert cfg._parse_bool(" TrUe ") is True
    assert cfg._parse_bool("0") is False
    assert cfg._parse_bool("not-a-bool", default=False) is False

    assert cfg._parse_int(None, 7) == 7
    assert cfg._parse_int("not-an-int", 7) == 7
    assert cfg._parse_int(" 5 ", 0) == 5
    assert cfg._parse_int("5", 0, min_value=10) == 10
    assert cfg._parse_int("50", 0, max_value=10) == 10

    assert cfg._parse_float(None, 1.25) == 1.25
    assert cfg._parse_float("not-a-float", 1.25) == 1.25
    assert cfg._parse_float(" 2.5 ", 0.0) == 2.5
    assert cfg._parse_float("0.5", 0.0, min_value=1.0) == 1.0
    assert cfg._parse_float("5.0", 0.0, max_value=2.0) == 2.0


def test_clear_config_env_vars_removes_only_managed(monkeypatch):
    from app.core import config as cfg

    monkeypatch.setenv("MANAGER_HOST", "example")
    monkeypatch.setenv("AUTHORIZED_TOKENS__A", "token-a")
    monkeypatch.setenv("ADMIN_USERS__bob", "hash")
    monkeypatch.setenv("SOME_OTHER", "keep")

    cfg._clear_config_env_vars()

    assert "SOME_OTHER" in cfg.os.environ
    assert "MANAGER_HOST" not in cfg.os.environ
    assert "AUTHORIZED_TOKENS__A" not in cfg.os.environ
    assert "ADMIN_USERS__bob" not in cfg.os.environ


def test_load_environment_variables_override_and_default_paths(monkeypatch, capsys):
    from app.core import config as cfg

    monkeypatch.setenv("CONFIG_ENV_PATH", "/tmp/does-not-exist.env")
    monkeypatch.setattr(cfg.os.path, "exists", lambda _: False)
    cfg._load_environment_variables()
    out = capsys.readouterr().out
    assert "override path" in out
    assert "no .env file found" in out

    monkeypatch.delenv("CONFIG_ENV_PATH", raising=False)
    monkeypatch.setattr(cfg.os.path, "exists", lambda _: False)
    cfg._load_environment_variables()
    out = capsys.readouterr().out
    assert "default path" in out
    assert "no .env file found" in out


def test_load_environment_variables_load_dotenv_success(monkeypatch, capsys):
    from app.core import config as cfg

    calls = []

    fake_dotenv = ModuleType("dotenv")

    def fake_load_dotenv(path, *, override=False):
        calls.append((path, override))

    fake_dotenv.load_dotenv = fake_load_dotenv

    monkeypatch.setenv("CONFIG_ENV_PATH", "/tmp/fake.env")
    monkeypatch.setattr(cfg.os.path, "exists", lambda _: True)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    cfg._load_environment_variables()

    assert calls == [("/tmp/fake.env", True)]
    out = capsys.readouterr().out
    assert "Loaded environment variables from" in out


def test_load_environment_variables_importerror_branch(monkeypatch, capsys):
    from app.core import config as cfg

    monkeypatch.setenv("CONFIG_ENV_PATH", "/tmp/fake.env")
    monkeypatch.setattr(cfg.os.path, "exists", lambda _: True)

    real_import = builtins.__import__

    def raising_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "dotenv":
            raise ImportError("boom")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", raising_import)

    cfg._load_environment_variables()

    out = capsys.readouterr().out
    assert "python-dotenv not installed" in out


def test_get_config_only_loads_env_once(monkeypatch):
    from app.core import config as cfg

    original_instance = cfg._CONFIG_INSTANCE
    original_loaded = cfg._CONFIG_ENV_LOADED

    load_calls = {"count": 0}

    def fake_load_env():
        load_calls["count"] += 1

    monkeypatch.setattr(cfg, "_load_environment_variables", fake_load_env)

    try:
        cfg._CONFIG_INSTANCE = None
        cfg._CONFIG_ENV_LOADED = False

        c1 = cfg.get_config()
        c2 = cfg.get_config()

        assert c1 is c2
        assert load_calls["count"] == 1

        cfg._CONFIG_INSTANCE = None
        cfg._CONFIG_ENV_LOADED = True
        c3 = cfg.get_config()
        assert load_calls["count"] == 1
        assert c3 is not None

    finally:
        cfg._CONFIG_INSTANCE = original_instance
        cfg._CONFIG_ENV_LOADED = original_loaded


def test_reload_config_env_updates_shared_object(monkeypatch):
    from app.core import config as cfg

    original_instance = cfg._CONFIG_INSTANCE
    original_loaded = cfg._CONFIG_ENV_LOADED

    # Avoid touching filesystem / real dotenv in this unit test.
    # Note: reload_config_env() clears managed env vars first, so the value must
    # be re-injected after the clear step. The real implementation does this via
    # loading the .env file.
    def fake_load_env():
        cfg.os.environ["MANAGER_HOST"] = "reload-example"

    monkeypatch.setattr(cfg, "_load_environment_variables", fake_load_env)

    original_config_obj = cfg.config

    updated = cfg.reload_config_env()

    assert updated is original_config_obj
    assert cfg.config is original_config_obj
    assert cfg.config.MANAGER_HOST == "reload-example"
    assert cfg._CONFIG_INSTANCE is not None

    # Also cover the branch where `config` is missing from globals
    old_config_obj = cfg.config

    cfg_dict = cfg.__dict__
    removed = cfg_dict.pop("config")
    try:
        returned = cfg.reload_config_env()
        assert returned is not old_config_obj

        # Restore object identity expected by the rest of the test suite
        removed.__dict__.clear()
        removed.__dict__.update(returned.__dict__)
        cfg.config = removed
        cfg._CONFIG_INSTANCE = removed
    finally:
        # Ensure `config` exists again even if assertion fails
        cfg.config = removed
        cfg._CONFIG_INSTANCE = removed

    assert cfg.config is removed

    cfg._CONFIG_INSTANCE = original_instance
    cfg._CONFIG_ENV_LOADED = original_loaded


def test_validate_configuration_warns_when_missing_tokens_and_admin(capsys):
    from app.core.config import Config

    cfg = Config()
    cfg.AUTHORIZED_TOKENS = {}
    cfg.ADMIN_USERS = {}

    cfg.validate_configuration()
    out = capsys.readouterr().out
    assert "No AUTHORIZED_TOKENS" in out
    assert "No admin users" in out


def test_config_initialization_and_validation_branches(monkeypatch):
    from app.core.config import Config

    monkeypatch.setenv("MANAGER_PROTOCOL", "https")
    monkeypatch.setenv("MANAGER_HOST", "example.org")
    monkeypatch.setenv("MANAGER_PORT", "1234")
    monkeypatch.setenv("LOG_DIRECTORY", "/tmp/esup-logs")

    # Token/admin discovery via prefixes
    monkeypatch.setenv("AUTHORIZED_TOKENS__client", "tok")
    monkeypatch.setenv("ADMIN_USERS__admin", "hash")

    cfg = Config()
    assert cfg.MANAGER_URL == "https://example.org:1234"
    assert cfg.LOG_DIRECTORY.endswith("/")
    assert cfg.AUTHORIZED_TOKENS == {"client": "tok"}
    assert cfg.ADMIN_USERS == {"admin": "hash"}

    # RUNNERS_STORAGE_ENABLED with missing path should raise
    monkeypatch.setenv("RUNNERS_STORAGE_ENABLED", "true")
    monkeypatch.setenv("RUNNERS_STORAGE_PATH", "")
    cfg2 = Config()
    with pytest.raises(ValueError, match="RUNNERS_STORAGE_PATH"):
        cfg2.validate_configuration()

    # PRIORITIES_ENABLED with empty domain disables itself
    monkeypatch.setenv("RUNNERS_STORAGE_ENABLED", "false")
    monkeypatch.setenv("PRIORITIES_ENABLED", "true")
    monkeypatch.setenv("PRIORITY_DOMAIN", "")
    cfg3 = Config()
    cfg3.validate_configuration()
    assert cfg3.PRIORITIES_ENABLED is False


def test_validate_configuration_rejects_wildcard_origins_with_credentials(monkeypatch):
    from app.core.config import Config

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")

    cfg = Config()
    with pytest.raises(ValueError, match="Invalid CORS configuration"):
        cfg.validate_configuration()
