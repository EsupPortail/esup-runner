"""Validates configuration parsing, environment loading, and dotenv file handling."""

import builtins
import importlib.util
import os
import sys
from types import ModuleType

import pytest


@pytest.fixture(autouse=True)
def isolate_manager_config_environment(monkeypatch):
    """Keep configuration tests independent from the developer's manager .env."""
    from app.core import config as config_module

    for key in list(os.environ):
        if key in config_module._CONFIG_ENV_KEYS or any(
            key.startswith(prefix) for prefix in config_module._CONFIG_ENV_PREFIXES
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAPI_COOKIE_SECRET", "unit-test-secret")


def test_parse_helpers_cover_edge_cases():
    """Validate Parse helpers cover edge cases."""
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

    assert cfg._is_ip_literal("127.0.0.1") is True
    assert cfg._is_ip_literal("::1") is True
    assert cfg._is_ip_literal("[::1]") is True
    assert cfg._is_ip_literal("  ") is False
    assert cfg._is_ip_literal("example.org") is False
    assert cfg._default_manager_bind_host("") == "0.0.0.0"
    assert cfg._default_manager_bind_host("localhost") == "localhost"
    assert cfg._default_manager_bind_host("127.0.0.1") == "127.0.0.1"
    assert cfg._default_manager_bind_host("::1") == "::1"
    assert cfg._default_manager_bind_host("example.org") == "0.0.0.0"
    assert cfg._default_manager_bind_host("ns31777550.ip-1-2-3.eu") == "0.0.0.0"


def test_clear_config_env_vars_removes_only_managed(monkeypatch):
    """Validate Clear config env vars removes only managed."""
    from app.core import config as cfg

    monkeypatch.setenv("MANAGER_HOST", "example")
    monkeypatch.setenv("MANAGER_BIND_HOST", "127.0.0.1")
    monkeypatch.setenv("LOG_DIR", "/tmp/logs")
    monkeypatch.setenv("RUNNERS_STORAGE_DIR", "/tmp/storage")
    monkeypatch.setenv("AUTHORIZED_TOKENS__A", "token-a")
    monkeypatch.setenv("ADMIN_USERS__bob", "hash")
    monkeypatch.setenv("SOME_OTHER", "keep")

    cfg._clear_config_env_vars()

    assert "SOME_OTHER" in cfg.os.environ
    assert "MANAGER_HOST" not in cfg.os.environ
    assert "MANAGER_BIND_HOST" not in cfg.os.environ
    assert "LOG_DIR" not in cfg.os.environ
    assert "RUNNERS_STORAGE_DIR" not in cfg.os.environ
    assert "AUTHORIZED_TOKENS__A" not in cfg.os.environ
    assert "ADMIN_USERS__bob" not in cfg.os.environ


def test_load_environment_variables_override_and_default_paths(monkeypatch, capsys):
    """Validate Load environment variables override and default paths."""
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
    """Validate Load environment variables load dotenv success."""
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
    """Validate Load environment variables importerror branch."""
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
    """Validate Get config only loads env once."""
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
    """Validate Reload config env updates shared object."""
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


def test_config_reload_marker_publish_and_consume(monkeypatch, tmp_path):
    """Validate Config reload marker publish and consume."""
    from app.core import config as cfg

    marker_path = tmp_path / ".config_reload"
    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_PATH", marker_path)
    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_MTIME_NS", 0)

    reload_calls = {"count": 0}

    def fake_reload():
        reload_calls["count"] += 1
        return cfg.config

    monkeypatch.setattr(cfg, "reload_config_env", fake_reload)

    published = cfg.publish_config_reload_event()
    assert marker_path.exists()
    assert published > 0

    # Simulate another worker that has not seen this marker yet.
    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_MTIME_NS", 0)
    assert cfg.reload_config_if_signaled() is True
    assert reload_calls["count"] == 1

    # Same marker should not trigger repeated reloads.
    assert cfg.reload_config_if_signaled() is False
    assert reload_calls["count"] == 1


def test_read_config_reload_marker_mtime_ns_handles_oserror(monkeypatch):
    """Validate Read config reload marker mtime ns handles oserror."""
    from app.core import config as cfg

    class _PathWithStatError:
        def stat(self):
            raise OSError("stat-failed")

    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_PATH", _PathWithStatError())

    assert cfg._read_config_reload_marker_mtime_ns() == 0


def test_read_config_reload_marker_mtime_ns_handles_filenotfound(monkeypatch):
    """Validate Read config reload marker mtime ns handles filenotfound."""
    from app.core import config as cfg

    class _PathMissing:
        def stat(self):
            raise FileNotFoundError("missing")

    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_PATH", _PathMissing())

    assert cfg._read_config_reload_marker_mtime_ns() == 0


def test_publish_config_reload_event_handles_oserror(monkeypatch, capsys):
    """Validate Publish config reload event handles oserror."""
    from app.core import config as cfg

    class _BadParent:
        def mkdir(self, *_args, **_kwargs):
            raise OSError("mkdir-failed")

    class _BadPath:
        parent = _BadParent()

        def touch(self, **_kwargs):
            raise AssertionError("touch should not be called when mkdir fails")

    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_PATH", _BadPath())
    monkeypatch.setattr(cfg, "_CONFIG_RELOAD_MARKER_MTIME_NS", 123)

    assert cfg.publish_config_reload_event() == 123
    out = capsys.readouterr().out
    assert "failed to publish config reload marker" in out
    assert "mkdir-failed" in out


def test_validate_configuration_warns_when_missing_tokens_and_admin(capsys):
    """Validate Validate configuration warns when missing tokens and admin."""
    from app.core.config import Config

    cfg = Config()
    cfg.AUTHORIZED_TOKENS = {}
    cfg.ADMIN_USERS = {}

    cfg.validate_configuration()
    out = capsys.readouterr().out
    assert "No AUTHORIZED_TOKENS" in out
    assert "No admin users" in out


def test_config_constructor_delegates_to_coherent_loaders(monkeypatch):
    """Validate the constructor delegates each configuration area in order."""
    from app.core.config import Config

    loader_names = (
        "_load_network_configuration",
        "_load_security_configuration",
        "_load_storage_configuration",
        "_load_notification_configuration",
        "_load_business_configuration",
    )
    calls = []

    for loader_name in loader_names:
        monkeypatch.setattr(
            Config,
            loader_name,
            lambda _self, name=loader_name: calls.append(name),
        )

    config = Config()

    assert calls == list(loader_names)
    assert config._configuration_errors == []
    assert config._configuration_validated is False


def test_config_initialization_and_validation_branches(monkeypatch):
    """Validate Config initialization and validation branches."""
    from app.core.config import Config

    monkeypatch.setenv("MANAGER_PROTOCOL", "https")
    monkeypatch.setenv("MANAGER_HOST", "example.org")
    monkeypatch.delenv("MANAGER_BIND_HOST", raising=False)
    monkeypatch.setenv("MANAGER_PORT", "1234")
    monkeypatch.setenv("LOG_DIR", "/tmp/esup-logs")
    monkeypatch.delenv("LOG_DIRECTORY", raising=False)
    monkeypatch.setenv("CACHE_DIR", "/tmp/esup-cache")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setenv("OPENAPI_COOKIE_MAX_AGE_SECONDS", "1200")
    monkeypatch.setenv("OPENAPI_COOKIE_ROTATE_EACH_REQUEST", "false")
    monkeypatch.setenv("OPENAPI_COOKIE_SECRET", "cookie-secret")

    # Token/admin discovery via prefixes
    monkeypatch.setenv("AUTHORIZED_TOKENS__client", "tok")
    valid_bcrypt_hash = "$2b$12$" + ("a" * 53)
    monkeypatch.setenv("ADMIN_USERS__admin", valid_bcrypt_hash)

    cfg = Config()
    assert cfg.MANAGER_URL == "https://example.org:1234"
    assert cfg.MANAGER_BIND_HOST == "0.0.0.0"
    assert cfg.LOG_DIR.endswith("/")
    assert cfg.LOG_DIRECTORY.endswith("/")
    assert cfg.CACHE_DIR == "/tmp/esup-cache"
    assert cfg.UV_CACHE_DIR == "/tmp/esup-cache/uv"
    assert cfg.AUTHORIZED_TOKENS == {"client": "tok"}
    assert cfg.ADMIN_USERS == {"admin": valid_bcrypt_hash}
    assert cfg.OPENAPI_COOKIE_MAX_AGE_SECONDS == 1200
    assert cfg.OPENAPI_COOKIE_ROTATE_EACH_REQUEST is False
    assert cfg.OPENAPI_COOKIE_SECRET == "cookie-secret"

    # RUNNERS_STORAGE_ENABLED with missing path should raise
    monkeypatch.setenv("RUNNERS_STORAGE_ENABLED", "true")
    monkeypatch.setenv("RUNNERS_STORAGE_DIR", "")
    monkeypatch.delenv("RUNNERS_STORAGE_PATH", raising=False)
    cfg2 = Config()
    with pytest.raises(ValueError, match="RUNNERS_STORAGE_DIR"):
        cfg2.validate_configuration()

    # PRIORITIES_ENABLED requires a domain instead of silently disabling itself.
    monkeypatch.setenv("RUNNERS_STORAGE_ENABLED", "false")
    monkeypatch.setenv("PRIORITIES_ENABLED", "true")
    monkeypatch.setenv("PRIORITY_DOMAIN", "")
    cfg3 = Config()
    with pytest.raises(ValueError, match="PRIORITY_DOMAIN"):
        cfg3.validate_configuration()
    assert cfg3.PRIORITIES_ENABLED is True

    # Explicit UV cache override
    monkeypatch.setenv("UV_CACHE_DIR", "/tmp/custom-uv-cache")
    cfg4 = Config()
    assert cfg4.UV_CACHE_DIR == "/tmp/custom-uv-cache"

    # Legacy aliases still work.
    monkeypatch.delenv("LOG_DIR", raising=False)
    monkeypatch.setenv("LOG_DIRECTORY", "/tmp/esup-legacy-logs")
    monkeypatch.delenv("RUNNERS_STORAGE_DIR", raising=False)
    monkeypatch.setenv("RUNNERS_STORAGE_PATH", "/tmp/esup-legacy-storage")
    cfg5 = Config()
    assert cfg5.LOG_DIR == "/tmp/esup-legacy-logs/"
    assert cfg5.LOG_DIRECTORY == "/tmp/esup-legacy-logs/"
    assert cfg5.RUNNERS_STORAGE_DIR == "/tmp/esup-legacy-storage"
    assert cfg5.RUNNERS_STORAGE_PATH == "/tmp/esup-legacy-storage"

    # Explicit bind host override for DNS MANAGER_HOST.
    monkeypatch.setenv("MANAGER_BIND_HOST", "127.0.0.1")
    cfg6 = Config()
    assert cfg6.MANAGER_BIND_HOST == "127.0.0.1"

    # Blank/whitespace override falls back to computed default.
    monkeypatch.setenv("MANAGER_BIND_HOST", "   ")
    cfg7 = Config()
    assert cfg7.MANAGER_BIND_HOST == "0.0.0.0"


def test_validate_configuration_rejects_wildcard_origins_with_credentials(monkeypatch):
    """Validate Validate configuration rejects wildcard origins with credentials."""
    from app.core.config import Config

    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    monkeypatch.setenv("CORS_ALLOW_CREDENTIALS", "true")

    cfg = Config()
    with pytest.raises(ValueError, match="Invalid CORS configuration"):
        cfg.validate_configuration()


def test_config_strict_readers_collect_invalid_explicit_values(monkeypatch):
    """Validate invalid scalar values are reported together instead of hidden."""
    from app.core.config import Config, ConfigValidationError

    monkeypatch.setenv("RUNNERS_STORAGE_ENABLED", "sometimes")
    monkeypatch.setenv("MANAGER_PORT", "not-a-port")
    monkeypatch.setenv("UVICORN_WORKERS", "0")
    monkeypatch.setenv("MAX_OTHER_DOMAIN_TASK_PERCENT", "101")
    monkeypatch.setenv("COMPLETION_NOTIFY_BACKOFF_FACTOR", "0.5")

    config = Config()

    assert config.RUNNERS_STORAGE_ENABLED is False
    assert config.MANAGER_PORT == 8081
    assert config.UVICORN_WORKERS == 4
    assert config.MAX_OTHER_DOMAIN_TASK_PERCENT == 100
    assert config.COMPLETION_NOTIFY_BACKOFF_FACTOR == 1.5

    with pytest.raises(ConfigValidationError) as raised:
        config.validate_configuration()

    errors = raised.value.errors
    assert any("RUNNERS_STORAGE_ENABLED must be a boolean" in error for error in errors)
    assert any("MANAGER_PORT must be an integer" in error for error in errors)
    assert any("UVICORN_WORKERS must be at least 1" in error for error in errors)
    assert any("MAX_OTHER_DOMAIN_TASK_PERCENT must be at most 100" in error for error in errors)
    assert any("COMPLETION_NOTIFY_BACKOFF_FACTOR must be at least 1.0" in error for error in errors)


def test_config_strict_readers_cover_defaults_formats_and_upper_bounds(monkeypatch):
    """Validate strict scalar helpers cover default, format, bound, and valid paths."""
    from app.core.config import Config

    config = object.__new__(Config)
    config._configuration_errors = []

    monkeypatch.delenv("TEST_BOOL", raising=False)
    assert config._read_bool("TEST_BOOL", True) is True
    monkeypatch.setenv("TEST_BOOL", "off")
    assert config._read_bool("TEST_BOOL", True) is False
    monkeypatch.setenv("TEST_BOOL", "on")
    assert config._read_bool("TEST_BOOL", False) is True

    monkeypatch.delenv("TEST_INT", raising=False)
    assert config._read_int("TEST_INT", 3) == 3
    monkeypatch.setenv("TEST_INT", "11")
    assert config._read_int("TEST_INT", 3, max_value=10) == 3
    monkeypatch.setenv("TEST_INT", "7")
    assert config._read_int("TEST_INT", 3, min_value=1, max_value=10) == 7

    monkeypatch.delenv("TEST_FLOAT", raising=False)
    assert config._read_float("TEST_FLOAT", 2.0) == 2.0
    monkeypatch.setenv("TEST_FLOAT", "invalid")
    assert config._read_float("TEST_FLOAT", 2.0) == 2.0
    monkeypatch.setenv("TEST_FLOAT", "4.0")
    assert config._read_float("TEST_FLOAT", 2.0, max_value=3.0) == 2.0
    monkeypatch.setenv("TEST_FLOAT", "2.5")
    assert config._read_float("TEST_FLOAT", 2.0, min_value=1.0, max_value=3.0) == 2.5

    assert len(config._configuration_errors) == 3


def test_config_validation_aggregates_schema_errors(monkeypatch):
    """Validate independent schema violations are returned in one exception."""
    from app.core.config import Config, ConfigValidationError

    monkeypatch.setenv("MANAGER_PROTOCOL", "ftp")
    monkeypatch.setenv("MANAGER_HOST", "bad host/path")
    monkeypatch.setenv("MANAGER_BIND_HOST", "bad bind/path")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    monkeypatch.setenv("API_DOCS_VISIBILITY", "hidden")
    monkeypatch.setenv("PRIORITY_DOMAIN", "https://priority.example.org")

    config = Config()

    with pytest.raises(ConfigValidationError) as raised:
        config.validate_configuration()

    message = str(raised.value)
    assert "MANAGER_HOST must contain" in message
    assert "MANAGER_BIND_HOST must contain" in message
    assert "MANAGER_PROTOCOL" in message
    assert "ENVIRONMENT" in message
    assert "LOG_LEVEL" in message
    assert "API_DOCS_VISIBILITY" in message
    assert "PRIORITY_DOMAIN must contain a hostname only" in message


def test_config_validation_rejects_placeholders_empty_credentials_and_paths(monkeypatch, capsys):
    """Validate credential placeholders fail while the cookie placeholder warns."""
    from app.core.config import Config, ConfigValidationError

    monkeypatch.setenv("MANAGER_HOST", "")
    monkeypatch.setenv("LOG_DIR", "")
    monkeypatch.setenv("CACHE_DIR", "")
    monkeypatch.setenv("UV_CACHE_DIR", "")
    monkeypatch.setenv("OPENAPI_COOKIE_SECRET", "change-me-with-a-long-random-secret")
    monkeypatch.setenv("AUTHORIZED_TOKENS__", "")
    monkeypatch.setenv("AUTHORIZED_TOKENS__client", "CHANGE_ME_APP_TOKEN")
    monkeypatch.setenv("ADMIN_USERS__", "")
    monkeypatch.setenv("ADMIN_USERS__admin", "CHANGE_ME_BCRYPT_HASH")
    monkeypatch.setenv("ADMIN_USERS__broken", "not-a-bcrypt-hash")

    config = Config()

    assert config.MANAGER_HOST == "0.0.0.0"
    assert config.LOG_DIR == "/var/log/esup-runner/"
    assert config.CACHE_DIR == "/home/esup-runner/.cache/esup-runner"
    assert config.UV_CACHE_DIR == "/home/esup-runner/.cache/esup-runner/uv"

    with pytest.raises(ConfigValidationError) as raised:
        config.validate_configuration()

    message = str(raised.value)
    assert "MANAGER_HOST must not be empty" in message
    assert "LOG_DIR must not be empty" in message
    assert "CACHE_DIR must not be empty" in message
    assert "UV_CACHE_DIR must not be empty" in message
    assert "AUTHORIZED_TOKENS entries must have a non-empty name" in message
    assert "AUTHORIZED_TOKENS__client must be replaced" in message
    assert "ADMIN_USERS entries must have a non-empty username" in message
    assert "ADMIN_USERS__admin must be replaced" in message
    assert "ADMIN_USERS__broken must contain a valid bcrypt hash" in message
    assert "OPENAPI_COOKIE_SECRET" not in message
    assert "OPENAPI_COOKIE_SECRET uses a documented placeholder" in capsys.readouterr().out


def test_openapi_cookie_secret_placeholder_is_warning_only(monkeypatch, capsys):
    """Validate the optional example cookie secret does not block startup."""
    from app.core import _check_output
    from app.core.config import Config

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("OPENAPI_COOKIE_SECRET", "change-me-with-a-long-random-secret")

    config = Config()
    config.validate_configuration()

    output = capsys.readouterr().out
    assert f"{_check_output._COLORS['warning']}⚠ WARNING: OPENAPI_COOKIE_SECRET" in output
    assert _check_output._RESET in output
    assert "change-me-with-a-long-random-secret" not in output


def test_semantic_validators_cover_mutated_numeric_paths_and_ipv6(monkeypatch):
    """Validate semantic checks protect post-load mutations and IPv6 URL formatting."""
    from app.core.config import Config, ConfigValidationError

    monkeypatch.setenv("MANAGER_HOST", "::1")
    config = Config()
    assert config.MANAGER_URL == "http://[::1]:8081"

    config.MANAGER_BIND_HOST = ""
    with pytest.raises(ConfigValidationError, match="MANAGER_BIND_HOST must not be empty"):
        config._validate_network_identity()
    config.MANAGER_BIND_HOST = "::1"

    config.MANAGER_PORT = 70000
    config.UVICORN_WORKERS = 0
    config.CLEANUP_TASK_FILES_DAYS = -1
    config.SMTP_PORT = 0
    config.OPENAPI_COOKIE_MAX_AGE_SECONDS = 59
    with pytest.raises(ConfigValidationError) as numeric_error:
        config._validate_numeric_limits()
    assert "MANAGER_PORT must be at most 65535" in str(numeric_error.value)
    assert "UVICORN_WORKERS must be at least 1" in str(numeric_error.value)
    assert "CLEANUP_TASK_FILES_DAYS must be at least 0" in str(numeric_error.value)
    assert "SMTP_PORT must be at least 1" in str(numeric_error.value)
    assert "OPENAPI_COOKIE_MAX_AGE_SECONDS must be at least 60" in str(numeric_error.value)

    config.LOG_DIR = ""
    config.RUNNERS_STORAGE_ENABLED = True
    config.RUNNERS_STORAGE_DIR = ""
    with pytest.raises(ConfigValidationError) as path_error:
        config._validate_paths()
    assert "LOG_DIR must not be empty" in str(path_error.value)
    assert "RUNNERS_STORAGE_DIR" in str(path_error.value)


def test_reload_config_env_rejects_invalid_config_without_mutating_shared_state(monkeypatch):
    """Validate failed hot reload restores environment and keeps the live object."""
    from app.core import config as config_module

    stable_config = ModuleType("stable_config")
    stable_config.stable_value = "kept"
    monkeypatch.setattr(config_module, "config", stable_config)
    monkeypatch.setattr(config_module, "_CONFIG_INSTANCE", stable_config)
    monkeypatch.setattr(config_module, "_CONFIG_ENV_LOADED", True)
    monkeypatch.setenv("MANAGER_HOST", "stable.example.org")

    def load_invalid_environment():
        """Simulate a newly loaded .env containing an invalid port."""
        config_module.os.environ["MANAGER_HOST"] = "invalid.example.org"
        config_module.os.environ["MANAGER_PORT"] = "invalid"

    monkeypatch.setattr(config_module, "_load_environment_variables", load_invalid_environment)

    with pytest.raises(config_module.ConfigValidationError, match="MANAGER_PORT"):
        config_module.reload_config_env()

    assert config_module.config is stable_config
    assert config_module._CONFIG_INSTANCE is stable_config
    assert stable_config.stable_value == "kept"
    assert os.environ["MANAGER_HOST"] == "stable.example.org"
    assert "MANAGER_PORT" not in os.environ


def test_config_module_auto_validates_outside_pytest(monkeypatch, tmp_path):
    """Validate a normal process runs central validation during module import."""
    from app.core import config as config_module

    env_path = tmp_path / ".env"
    env_path.write_text(
        "MANAGER_PROTOCOL=http\n"
        "MANAGER_HOST=localhost\n"
        "MANAGER_PORT=8081\n"
        "OPENAPI_COOKIE_SECRET=unit-test-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_ENV_PATH", str(env_path))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(sys, "argv", ["python"])

    pytest_module = sys.modules.pop("pytest", None)
    try:
        spec = importlib.util.spec_from_file_location(
            "manager_config_auto_validation", config_module.__file__
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if pytest_module is not None:
            sys.modules["pytest"] = pytest_module

    assert module.config.MANAGER_HOST == "localhost"
    module.config.validate_configuration()
