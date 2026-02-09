# manager/app/core/config.py
"""
Configuration module for runner management system.
Handles environment variables, security settings, and application configuration.
"""

import os
from typing import Dict, Optional

from passlib.context import CryptContext

# Module-level global state - these persist across imports
_CONFIG_ENV_LOADED: bool = False
_CONFIG_INSTANCE: Optional["Config"] = None


# Keys/prefixes managed by this config; cleared on reload to reflect deletions in .env
_CONFIG_ENV_PREFIXES = ["AUTHORIZED_TOKENS__", "ADMIN_USERS__"]
_CONFIG_ENV_KEYS = [
    "MANAGER_PROTOCOL",
    "MANAGER_HOST",
    "MANAGER_PORT",
    "ENVIRONMENT",
    "UVICORN_WORKERS",
    "CLEANUP_TASK_FILES_DAYS",
    "LOG_DIRECTORY",
    "LOG_LEVEL",
    "API_DOCS_VISIBILITY",
    "RUNNERS_STORAGE_ENABLED",
    "RUNNERS_STORAGE_PATH",
    "PRIORITIES_ENABLED",
    "PRIORITY_DOMAIN",
    "MAX_OTHER_DOMAIN_TASK_PERCENT",
    "COMPLETION_NOTIFY_MAX_RETRIES",
    "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS",
    "COMPLETION_NOTIFY_BACKOFF_FACTOR",
    "CORS_ALLOW_ORIGINS",
    "CORS_ALLOW_CREDENTIALS",
    "CORS_ALLOW_METHODS",
    "CORS_ALLOW_HEADERS",
    "NOTIFY_URL_ALLOWED_HOSTS",
    "NOTIFY_URL_ALLOW_PRIVATE_NETWORKS",
    "OPENAPI_ALLOW_QUERY_TOKEN",
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


class Config:
    """
    Configuration class that reads from environment variables.
    Assumes .env file has already been loaded.
    """

    def __init__(self):
        """Initialize configuration values."""
        print("Initializing configuration from environment variables...")

        # Manager configuration
        self.MANAGER_PROTOCOL: str = os.getenv("MANAGER_PROTOCOL", "http")
        self.MANAGER_HOST: str = os.getenv("MANAGER_HOST", "0.0.0.0")
        self.MANAGER_PORT: int = int(os.getenv("MANAGER_PORT", 8000))
        # Generate Manager URL
        self.MANAGER_URL = f"{self.MANAGER_PROTOCOL}://{self.MANAGER_HOST}:{self.MANAGER_PORT}"

        # API token authentication: authorized tokens for clients and runners
        self.AUTHORIZED_TOKENS: Dict[str, str] = self._load_authorized_tokens()

        # Production settings (development/production)
        self.ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
        # Number of Uvicorn workers (for Gunicorn, production mode)
        self.UVICORN_WORKERS: int = int(os.getenv("UVICORN_WORKERS", 4))

        # Remove task files older than specified number of days
        self.CLEANUP_TASK_FILES_DAYS: int = int(os.getenv("CLEANUP_TASK_FILES_DAYS", 30))

        # Directory to store log files
        self.LOG_DIRECTORY: str = os.getenv("LOG_DIRECTORY", "/var/log/flow_runner")
        # Add slash at end if missing
        if not self.LOG_DIRECTORY.endswith("/"):
            self.LOG_DIRECTORY += "/"

        # Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

        # Runner shared storage (optional)
        self.RUNNERS_STORAGE_ENABLED: bool = _parse_bool(
            os.getenv("RUNNERS_STORAGE_ENABLED"), default=False
        )
        self.RUNNERS_STORAGE_PATH: str = os.getenv("RUNNERS_STORAGE_PATH", "/tmp/esup-runner")

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
        # Optional allowlist of notify_url hosts (comma-separated). Empty means "allow any public host".
        self.NOTIFY_URL_ALLOWED_HOSTS = [
            h.strip().lower()
            for h in (os.getenv("NOTIFY_URL_ALLOWED_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        # If true, allow notify_url resolving to private/loopback networks.
        self.NOTIFY_URL_ALLOW_PRIVATE_NETWORKS: bool = _parse_bool(
            os.getenv("NOTIFY_URL_ALLOW_PRIVATE_NETWORKS"), default=False
        )

        # OpenAPI token handling
        # Allow providing OpenAPI token in query string (?token=...). Default False to reduce leakage.
        self.OPENAPI_ALLOW_QUERY_TOKEN: bool = _parse_bool(
            os.getenv("OPENAPI_ALLOW_QUERY_TOKEN"), default=False
        )

        # Admin users configuration
        self.ADMIN_USERS: Dict[str, str] = self._load_admin_users()

        # Initialize password hashing context
        self.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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

        if self.RUNNERS_STORAGE_ENABLED and not self.RUNNERS_STORAGE_PATH:
            raise ValueError("RUNNERS_STORAGE_PATH must be set when RUNNERS_STORAGE_ENABLED=true")

        if self.PRIORITIES_ENABLED and not self.PRIORITY_DOMAIN:
            print(
                "WARNING: PRIORITIES_ENABLED=true but PRIORITY_DOMAIN is empty - priorities disabled"
            )
            self.PRIORITIES_ENABLED = False


# Create global config instance using the factory function
config: "Config" = get_config()

# Auto-validate configuration on module load
config.validate_configuration()
