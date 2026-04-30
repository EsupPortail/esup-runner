# manager/app/core/config.py
"""
Configuration module for runner management system.
Handles environment variables, security settings, and application configuration.
"""

import ipaddress
import os
from pathlib import Path
from typing import Dict, Optional

from app.core.passwords import BcryptPasswordContext

# Module-level global state - these persist across imports
_CONFIG_ENV_LOADED: bool = False
_CONFIG_INSTANCE: Optional["Config"] = None
_CONFIG_RELOAD_MARKER_MTIME_NS: int = 0
_MANAGER_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_RELOAD_MARKER_PATH = _MANAGER_ROOT / "data" / ".config_reload"


# Keys/prefixes managed by this config; cleared on reload to reflect deletions in .env
_CONFIG_ENV_PREFIXES = ["AUTHORIZED_TOKENS__", "ADMIN_USERS__"]
_CONFIG_ENV_KEYS = [
    "MANAGER_PROTOCOL",
    "MANAGER_HOST",
    "MANAGER_BIND_HOST",
    "MANAGER_PORT",
    "ENVIRONMENT",
    "UVICORN_WORKERS",
    "CLEANUP_TASK_FILES_DAYS",
    "LOG_DIR",
    "LOG_DIRECTORY",
    "LOG_LEVEL",
    "API_DOCS_VISIBILITY",
    "RUNNERS_STORAGE_ENABLED",
    "RUNNERS_STORAGE_DIR",
    "RUNNERS_STORAGE_PATH",
    "CACHE_DIR",
    "UV_CACHE_DIR",
    "PRIORITIES_ENABLED",
    "PRIORITY_DOMAIN",
    "MAX_OTHER_DOMAIN_TASK_PERCENT",
    "COMPLETION_NOTIFY_MAX_RETRIES",
    "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS",
    "COMPLETION_NOTIFY_BACKOFF_FACTOR",
    "SMTP_SERVER",
    "SMTP_PORT",
    "SMTP_USE_TLS",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_SENDER",
    "MANAGER_EMAIL",
    "CORS_ALLOW_ORIGINS",
    "CORS_ALLOW_CREDENTIALS",
    "CORS_ALLOW_METHODS",
    "CORS_ALLOW_HEADERS",
    "NOTIFY_URL_ALLOWED_HOSTS",
    "NOTIFY_URL_ALLOW_PRIVATE_NETWORKS",
    "RUNNER_URL_ALLOWED_HOSTS",
    "RUNNER_URL_ALLOW_PRIVATE_NETWORKS",
    "OPENAPI_ALLOW_QUERY_TOKEN",
    "OPENAPI_COOKIE_MAX_AGE_SECONDS",
    "OPENAPI_COOKIE_ROTATE_EACH_REQUEST",
    "OPENAPI_COOKIE_SECRET",
]


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    """Parse a boolean from a string with a fallback default."""
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


def _parse_int(
    value: Optional[str],
    default: int,
    *,
    min_value: Optional[int] = None,
    max_value: Optional[int] = None,
) -> int:
    """Parse an integer from a string with optional bounds and fallback default."""
    if value is None:
        return default
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError):
        return default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _parse_float(
    value: Optional[str],
    default: float,
    *,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> float:
    """Parse a float from a string with optional bounds and fallback default."""
    if value is None:
        return default
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        return default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _first_env_value(*keys: str, default: Optional[str] = None) -> str:
    """Return the first environment value found among keys."""
    for key in keys:
        value = os.getenv(key)
        if value is not None:
            return value
    return "" if default is None else default


def _is_ip_literal(value: str) -> bool:
    """Return True when value is a valid IPv4/IPv6 literal."""
    candidate = (value or "").strip()
    if not candidate:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _default_manager_bind_host(manager_host: str) -> str:
    """Compute default socket bind host from MANAGER_HOST.

    MANAGER_HOST is also used to build MANAGER_URL, so a DNS name is valid there.
    But binding directly on a DNS hostname may fail or bind unexpectedly depending
    on DNS resolution order. In that case, default to 0.0.0.0 for reliability.
    """
    host = (manager_host or "").strip()
    if not host:
        return "0.0.0.0"
    if host in {"0.0.0.0", "::", "*", "localhost"}:
        return host
    if _is_ip_literal(host):
        return host
    return "0.0.0.0"


def reload_config_env():
    """Reload configuration from .env, updating the shared config object in place."""
    global _CONFIG_ENV_LOADED, _CONFIG_INSTANCE, config

    old_config = config if "config" in globals() else None

    _CONFIG_ENV_LOADED = False
    _CONFIG_INSTANCE = None
    _clear_config_env_vars()
    _load_environment_variables()
    new_config = Config()
    _CONFIG_INSTANCE = new_config

    if old_config is not None and old_config is not new_config:
        old_config.__dict__.clear()
        old_config.__dict__.update(new_config.__dict__)
        config = old_config
    else:
        config = new_config

    return config


def get_config():
    """
    Get or create the configuration instance.
    Ensures .env file is loaded only once.

    Returns:
        Config: Configuration instance
    """
    global _CONFIG_ENV_LOADED, _CONFIG_INSTANCE

    if _CONFIG_INSTANCE is None:
        # Load .env file only if not already loaded
        if not _CONFIG_ENV_LOADED:
            _load_environment_variables()
            _CONFIG_ENV_LOADED = True

        # Create configuration instance
        _CONFIG_INSTANCE = Config()

    return _CONFIG_INSTANCE


def _load_environment_variables() -> None:
    """
    Load environment variables from .env file if it exists.
    This function is called only once.
    """
    env_override = os.getenv("CONFIG_ENV_PATH") or os.getenv("ENV_FILE")

    if env_override:
        env_path = env_override
        print(f"Loading environment variables from override path: {env_path}")
    else:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(current_dir)
        grandparent_dir = os.path.dirname(parent_dir)
        env_path = os.path.join(grandparent_dir, ".env")
        print(f"Loading environment variables from default path: {env_path}")

    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv

            # override=True to refresh already-loaded vars when reloading config
            load_dotenv(env_path, override=True)
            print(f"Loaded environment variables from: {env_path}")
        except ImportError:
            print("Warning: python-dotenv not installed, .env file will not be loaded")
    else:
        print(f"Warning: no .env file found in: {env_path}, default configuration used")


def _clear_config_env_vars() -> None:
    """Remove managed config keys from os.environ to allow deletions in .env to take effect."""
    for key in list(os.environ.keys()):
        if key in _CONFIG_ENV_KEYS:
            os.environ.pop(key, None)
        else:
            if any(key.startswith(prefix) for prefix in _CONFIG_ENV_PREFIXES):
                os.environ.pop(key, None)


def _read_config_reload_marker_mtime_ns() -> int:
    """Return reload marker mtime in nanoseconds, or 0 if unavailable."""
    try:
        return _CONFIG_RELOAD_MARKER_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return 0
    except OSError:
        return 0


def publish_config_reload_event() -> int:
    """Publish a reload signal for sibling workers using a shared marker file."""
    global _CONFIG_RELOAD_MARKER_MTIME_NS

    try:
        _CONFIG_RELOAD_MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_RELOAD_MARKER_PATH.touch(exist_ok=True)
        _CONFIG_RELOAD_MARKER_MTIME_NS = _read_config_reload_marker_mtime_ns()
    except OSError as exc:
        print(f"Warning: failed to publish config reload marker: {exc}")

    return _CONFIG_RELOAD_MARKER_MTIME_NS


def reload_config_if_signaled() -> bool:
    """Reload this worker config if another worker published a newer reload signal."""
    global _CONFIG_RELOAD_MARKER_MTIME_NS

    marker_mtime_ns = _read_config_reload_marker_mtime_ns()
    if marker_mtime_ns <= _CONFIG_RELOAD_MARKER_MTIME_NS:
        return False

    reload_config_env()
    _CONFIG_RELOAD_MARKER_MTIME_NS = marker_mtime_ns
    return True


class Config:
    """
    Configuration class that reads from environment variables.
    Assumes .env file has already been loaded.
    """

    def __init__(self):
        """Initialize configuration values."""
        print("Initializing configuration from environment variables…")

        # Manager configuration
        self.MANAGER_PROTOCOL: str = os.getenv("MANAGER_PROTOCOL", "http")
        self.MANAGER_HOST: str = (os.getenv("MANAGER_HOST", "0.0.0.0") or "").strip() or "0.0.0.0"
        manager_bind_host = _first_env_value(
            "MANAGER_BIND_HOST", default=_default_manager_bind_host(self.MANAGER_HOST)
        ).strip()
        self.MANAGER_BIND_HOST: str = manager_bind_host or _default_manager_bind_host(
            self.MANAGER_HOST
        )
        self.MANAGER_PORT: int = int(os.getenv("MANAGER_PORT", 8081))
        # Generate Manager URL
        self.MANAGER_URL = f"{self.MANAGER_PROTOCOL}://{self.MANAGER_HOST}:{self.MANAGER_PORT}"

        # API token authentication: authorized tokens for clients and runners
        self.AUTHORIZED_TOKENS: Dict[str, str] = self._load_authorized_tokens()

        # Production settings (development/production)
        self.ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
        # Number of Uvicorn workers (for Gunicorn, production mode)
        self.UVICORN_WORKERS: int = int(os.getenv("UVICORN_WORKERS", 4))

        # Remove task files older than specified number of days
        self.CLEANUP_TASK_FILES_DAYS: int = int(os.getenv("CLEANUP_TASK_FILES_DAYS", 60))

        # Directory to store log files.
        # Prefer LOG_DIR, keep LOG_DIRECTORY for backward compatibility.
        log_dir = _first_env_value("LOG_DIR", "LOG_DIRECTORY", default="/var/log/esup-runner")
        # Add slash at end if missing
        if not log_dir.endswith("/"):
            log_dir += "/"
        self.LOG_DIR: str = log_dir
        self.LOG_DIRECTORY: str = log_dir

        # Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

        # Runner shared storage (optional)
        self.RUNNERS_STORAGE_ENABLED: bool = _parse_bool(
            os.getenv("RUNNERS_STORAGE_ENABLED"), default=False
        )
        runners_storage_dir = _first_env_value(
            "RUNNERS_STORAGE_DIR",
            "RUNNERS_STORAGE_PATH",
            default="/tmp/esup-runner",
        )
        self.RUNNERS_STORAGE_DIR: str = runners_storage_dir
        # Backward-compatible alias used across existing code/tests.
        self.RUNNERS_STORAGE_PATH: str = runners_storage_dir

        # Shared cache root used for local cacheable artifacts (including uv cache).
        self.CACHE_DIR: str = os.getenv("CACHE_DIR", "/home/esup-runner/.cache/esup-runner")
        self.UV_CACHE_DIR: str = os.getenv("UV_CACHE_DIR", os.path.join(self.CACHE_DIR, "uv"))

        # Visibility of the API documentation (options: public, private -> requires token authentication)
        self.API_DOCS_VISIBILITY: str = os.getenv("API_DOCS_VISIBILITY", "public").lower()
        print(f"API documentation visibility set to: {self.API_DOCS_VISIBILITY}")

        # Domain-based priorities (optional)
        # If enabled, the manager can reserve runner capacity for a priority domain.
        self.PRIORITIES_ENABLED: bool = _parse_bool(os.getenv("PRIORITIES_ENABLED"), default=False)
        # Priority domain (suffix match)
        self.PRIORITY_DOMAIN: str = os.getenv("PRIORITY_DOMAIN", "").strip().lower()
        # Maximum percentage of non-priority tasks allowed concurrently
        self.MAX_OTHER_DOMAIN_TASK_PERCENT: int = _parse_int(
            os.getenv("MAX_OTHER_DOMAIN_TASK_PERCENT"),
            100,
            min_value=0,
            max_value=100,
        )

        # Completion notify retry settings
        self.COMPLETION_NOTIFY_MAX_RETRIES: int = _parse_int(
            os.getenv("COMPLETION_NOTIFY_MAX_RETRIES"),
            5,
            min_value=0,
        )
        # Delay between notify callback retries in seconds
        self.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS: int = _parse_int(
            os.getenv("COMPLETION_NOTIFY_RETRY_DELAY_SECONDS"),
            60,
            min_value=0,
        )
        # Backoff factor for notify callback retries
        self.COMPLETION_NOTIFY_BACKOFF_FACTOR: float = _parse_float(
            os.getenv("COMPLETION_NOTIFY_BACKOFF_FACTOR"),
            1.5,
            min_value=1.0,
        )

        # SMTP configuration for warning emails (optional)
        self.SMTP_SERVER: str = os.getenv("SMTP_SERVER", "")
        self.SMTP_PORT: int = _parse_int(os.getenv("SMTP_PORT"), 25, min_value=1, max_value=65535)
        self.SMTP_USE_TLS: bool = _parse_bool(os.getenv("SMTP_USE_TLS"), default=False)
        self.SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
        self.SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
        self.SMTP_SENDER: str = os.getenv("SMTP_SENDER", "")
        self.MANAGER_EMAIL: str = os.getenv("MANAGER_EMAIL", "")

        # CORS configuration
        # Comma-separated list of allowed origins; use "*" only when allow_credentials is False.
        cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
        self.CORS_ALLOW_ORIGINS = [
            o.strip() for o in (cors_origins_raw or "").split(",") if o.strip()
        ] or ["*"]
        self.CORS_ALLOW_CREDENTIALS: bool = _parse_bool(
            os.getenv("CORS_ALLOW_CREDENTIALS"), default=False
        )
        self.CORS_ALLOW_METHODS = [
            m.strip() for m in (os.getenv("CORS_ALLOW_METHODS", "*") or "").split(",") if m.strip()
        ] or ["*"]
        self.CORS_ALLOW_HEADERS = [
            h.strip() for h in (os.getenv("CORS_ALLOW_HEADERS", "*") or "").split(",") if h.strip()
        ] or ["*"]

        # Outbound notify_url callback hardening
        # Optional allowlist of notify_url hostnames (comma-separated).
        # Empty means allow any host (subject to private-network policy below).
        self.NOTIFY_URL_ALLOWED_HOSTS = [
            h.strip().lower()
            for h in (os.getenv("NOTIFY_URL_ALLOWED_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        # Allow notify_url resolving to private/loopback networks.
        # Default False; set True only if you control internal callbacks.
        self.NOTIFY_URL_ALLOW_PRIVATE_NETWORKS: bool = _parse_bool(
            os.getenv("NOTIFY_URL_ALLOW_PRIVATE_NETWORKS"), default=False
        )

        # Runner registration hardening
        # Optional allowlist of runner URL hostnames (comma-separated).
        # Empty means allow any host (subject to private-network policy below).
        self.RUNNER_URL_ALLOWED_HOSTS = [
            h.strip().lower()
            for h in (os.getenv("RUNNER_URL_ALLOWED_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        # Allow runner URLs resolving to private/loopback networks.
        # Default True, as runners are often on the same network.
        # Set False to require runner URLs resolving to public IPs.
        self.RUNNER_URL_ALLOW_PRIVATE_NETWORKS: bool = _parse_bool(
            os.getenv("RUNNER_URL_ALLOW_PRIVATE_NETWORKS"), default=True
        )

        # OpenAPI token handling
        # Allow providing OpenAPI token in query string (?token=…). Default False to reduce leakage.
        self.OPENAPI_ALLOW_QUERY_TOKEN: bool = _parse_bool(
            os.getenv("OPENAPI_ALLOW_QUERY_TOKEN"), default=False
        )
        # OpenAPI auth cookie TTL (seconds). Used by /admin/docs -> /docs|/redoc|/openapi.json flow.
        self.OPENAPI_COOKIE_MAX_AGE_SECONDS: int = _parse_int(
            os.getenv("OPENAPI_COOKIE_MAX_AGE_SECONDS"),
            900,
            min_value=60,
            max_value=86400,
        )
        # Rotate OpenAPI auth cookie on each protected docs request.
        self.OPENAPI_COOKIE_ROTATE_EACH_REQUEST: bool = _parse_bool(
            os.getenv("OPENAPI_COOKIE_ROTATE_EACH_REQUEST"),
            default=True,
        )
        # Optional explicit signing secret for OpenAPI auth cookie.
        # If empty, a deterministic fallback derived from configured secrets is used.
        self.OPENAPI_COOKIE_SECRET: str = (os.getenv("OPENAPI_COOKIE_SECRET", "") or "").strip()

        # Admin users configuration
        self.ADMIN_USERS: Dict[str, str] = self._load_admin_users()

        # Initialize password hashing context
        self.pwd_context = BcryptPasswordContext()

    def _load_authorized_tokens(self) -> Dict[str, str]:
        """
        Load authorized tokens from environment variables.
        Expected format: AUTHORIZED_TOKENS__TOKEN_NAME=token_value
        """
        authorized_tokens = {}
        for key, value in os.environ.items():
            if key.startswith("AUTHORIZED_TOKENS__"):
                token_name = key.split("__")[-1]
                authorized_tokens[token_name] = value
        return authorized_tokens

    def _load_admin_users(self) -> Dict[str, str]:
        """
        Load admin users from environment variables.
        Expected format: ADMIN_USERS__USERNAME=hashed_password
        """
        admin_users = {}
        for key, value in os.environ.items():
            if key.startswith("ADMIN_USERS__"):
                username = key.split("__")[-1]
                admin_users[username] = value
        return admin_users

    def validate_configuration(self) -> None:
        """
        Validate critical configuration settings.

        Raises:
            ValueError: If essential configuration is missing or invalid
        """
        if not self.AUTHORIZED_TOKENS:
            print("WARNING: No AUTHORIZED_TOKENS configured - API will be inaccessible")

        # Validate admin users
        if not self.ADMIN_USERS:
            print("WARNING: No admin users configured - admin interface will be inaccessible")

        # CORS sanity: disallow wildcard origins with credentials.
        if self.CORS_ALLOW_CREDENTIALS and ("*" in self.CORS_ALLOW_ORIGINS):
            raise ValueError(
                "Invalid CORS configuration: CORS_ALLOW_CREDENTIALS=true is not compatible with CORS_ALLOW_ORIGINS=*"
            )

        if self.RUNNERS_STORAGE_ENABLED and not self.RUNNERS_STORAGE_DIR:
            raise ValueError(
                "RUNNERS_STORAGE_DIR (legacy: RUNNERS_STORAGE_PATH) must be set when RUNNERS_STORAGE_ENABLED=true"
            )

        if self.PRIORITIES_ENABLED and not self.PRIORITY_DOMAIN:
            print(
                "WARNING: PRIORITIES_ENABLED=true but PRIORITY_DOMAIN is empty - priorities disabled"
            )
            self.PRIORITIES_ENABLED = False


_CONFIG_RELOAD_MARKER_MTIME_NS = _read_config_reload_marker_mtime_ns()

# Create global config instance using the factory function
config: "Config" = get_config()

# Auto-validate configuration on module load
config.validate_configuration()
