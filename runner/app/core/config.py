# runner/app/core/config.py
"""
Configuration module for runner.
Handles environment variables, security settings, and application configuration.
"""

import os
import re
import sys
import warnings
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import urlparse

SUPPORTED_TASK_TYPES = frozenset({"encoding", "studio", "transcription"})
SUPPORTED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
SUPPORTED_STUDIO_PRESETS = frozenset(
    {
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
    }
)


class ConfigValidationError(ValueError):
    """Raised with all configuration errors detected during validation."""

    def __init__(self, errors: List[str]):
        unique_errors = list(dict.fromkeys(errors))
        self.errors = tuple(unique_errors)
        details = "\n".join(f"- {error}" for error in unique_errors)
        super().__init__(f"Invalid runner configuration:\n{details}")


def _raise_validation_errors(errors: List[str]) -> None:
    """Raise one error containing every collected configuration issue."""
    if errors:
        raise ConfigValidationError(errors)


# Module-level global state - these persist across imports
_CONFIG_ENV_LOADED = False
_CONFIG_INSTANCE = None


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


def _normalize_base_url(value: Optional[str], default: str) -> str:
    """Normalize a base URL from env by trimming spaces and trailing slashes."""
    raw = (value or "").strip()
    if not raw:
        raw = default
    normalized = raw.rstrip("/")
    return normalized or raw


def _first_env_value(*keys: str, default: Optional[str] = None) -> str:
    """Return the first environment value found among keys."""
    for key in keys:
        value = os.getenv(key)
        if value is not None:
            return value
    return "" if default is None else default


def get_config():
    """
    Get or create the configuration instance.
    Ensures .env file is loaded only once.

    Returns:
        Config: Configuration instance
    """
    global _CONFIG_ENV_LOADED, _CONFIG_INSTANCE

    if _CONFIG_INSTANCE is None:
        if not _CONFIG_ENV_LOADED:
            _load_environment_variables()
            _CONFIG_ENV_LOADED = True

        _CONFIG_INSTANCE = Config()

    return _CONFIG_INSTANCE


def reload_config_from_env():
    """Refresh the cached config instance from current environment variables.

    This updates the existing instance in-place so modules that already imported
    ``config`` keep seeing fresh values.
    """
    global _CONFIG_ENV_LOADED, _CONFIG_INSTANCE, config

    if not _CONFIG_ENV_LOADED:
        _load_environment_variables()
        _CONFIG_ENV_LOADED = True

    refreshed = Config()
    validator = getattr(refreshed, "validate_configuration", None)
    if callable(validator):
        validator()

    if _CONFIG_INSTANCE is None:
        _CONFIG_INSTANCE = refreshed
    else:
        _CONFIG_INSTANCE.__dict__.clear()
        _CONFIG_INSTANCE.__dict__.update(refreshed.__dict__)

    config = _CONFIG_INSTANCE
    return _CONFIG_INSTANCE


def _load_environment_variables() -> None:
    """
    Load environment variables from .env file if it exists.
    This function is called only once.
    """
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    grandparent_dir = os.path.dirname(parent_dir)
    env_path = os.path.join(grandparent_dir, ".env")

    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
            print(f"Loaded environment variables from: {env_path}")
        except ImportError:
            print("Warning: python-dotenv not installed, .env file will not be loaded")
    else:
        print(f"Warning: no .env file found in: {env_path}, default configuration used")


class Config:
    """
    Configuration class that reads from environment variables.
    Assumes .env file has already been loaded.
    """

    def _record_configuration_error(self, message: str) -> None:
        """Store one error so validation can report all invalid values together."""
        self._configuration_errors.append(message)

    def _read_bool(self, name: str, default: bool) -> bool:
        """Read a boolean variable, recording an error before using its default."""
        value = os.getenv(name)
        if value is None:
            return default

        normalized = value.strip().lower()
        if normalized in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "f", "no", "n", "off"}:
            return False

        self._record_configuration_error(f"{name} must be a boolean (true/false), got {value!r}")
        return default

    def _read_int(
        self,
        name: str,
        default: int,
        *,
        min_value: Optional[int] = None,
        max_value: Optional[int] = None,
    ) -> int:
        """Read a bounded integer, recording invalid explicit values."""
        value = os.getenv(name)
        if value is None:
            return default

        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            self._record_configuration_error(f"{name} must be an integer, got {value!r}")
            return default

        if min_value is not None and parsed < min_value:
            self._record_configuration_error(f"{name} must be at least {min_value}, got {parsed}")
            return default
        if max_value is not None and parsed > max_value:
            self._record_configuration_error(f"{name} must be at most {max_value}, got {parsed}")
            return default
        return parsed

    def _read_float(
        self,
        name: str,
        default: float,
        *,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        """Read a bounded float, recording invalid explicit values."""
        value = os.getenv(name)
        if value is None:
            return default

        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            self._record_configuration_error(f"{name} must be a number, got {value!r}")
            return default

        if min_value is not None and parsed < min_value:
            self._record_configuration_error(f"{name} must be at least {min_value}, got {parsed}")
            return default
        if max_value is not None and parsed > max_value:
            self._record_configuration_error(f"{name} must be at most {max_value}, got {parsed}")
            return default
        return parsed

    def _configure_runner_task_types(
        self, runner_instances_env: Optional[str], runner_task_types_spec: str
    ) -> None:
        """Configure RUNNER_TASK_TYPES* attributes for grouped or legacy syntax."""
        try:
            grouped = _parse_grouped_task_types_spec(runner_task_types_spec)
        except ValueError as exc:
            self._record_configuration_error(str(exc))
            self.RUNNER_INSTANCES = 1
            self.RUNNER_TASK_TYPES = set(SUPPORTED_TASK_TYPES)
            self.RUNNER_TASK_TYPES_BY_INSTANCE = [set(SUPPORTED_TASK_TYPES)]
            return

        if grouped is None:
            self._configure_legacy_task_types(runner_task_types_spec)
            return

        self._configure_grouped_task_types(grouped, runner_instances_env)

    def _configure_grouped_task_types(
        self, grouped: List[Set[str]], runner_instances_env: Optional[str]
    ) -> None:
        """Configure per-instance task types from grouped RUNNER_TASK_TYPES syntax."""
        self.RUNNER_TASK_TYPES_BY_INSTANCE = grouped
        computed_instances = len(self.RUNNER_TASK_TYPES_BY_INSTANCE)
        self._warn_grouped_instances_override(runner_instances_env, computed_instances)
        self.RUNNER_INSTANCES = computed_instances
        self.RUNNER_TASK_TYPES = self._resolve_grouped_instance_task_types(computed_instances)

    def _configure_legacy_task_types(self, runner_task_types_spec: str) -> None:
        """Configure task types when using simple CSV RUNNER_TASK_TYPES syntax."""
        self.RUNNER_INSTANCES = self._read_int("RUNNER_INSTANCES", 1, min_value=1)
        task_types = _parse_task_types_csv(runner_task_types_spec)
        self.RUNNER_TASK_TYPES = task_types
        self.RUNNER_TASK_TYPES_BY_INSTANCE = [set(task_types) for _ in range(self.RUNNER_INSTANCES)]

    def _warn_grouped_instances_override(
        self, runner_instances_env: Optional[str], computed_instances: int
    ) -> None:
        """Warn when RUNNER_INSTANCES conflicts with grouped task types."""
        if runner_instances_env is None:
            return

        try:
            configured_instances = int(runner_instances_env)
        except ValueError:
            warnings.warn(
                "Invalid RUNNER_INSTANCES value ignored because RUNNER_TASK_TYPES uses grouped syntax."
            )
            return

        if configured_instances != computed_instances:
            warnings.warn(
                "RUNNER_INSTANCES is ignored because RUNNER_TASK_TYPES uses grouped syntax "
                f"(computed instances={computed_instances}, configured RUNNER_INSTANCES={configured_instances})."
            )

    def _resolve_grouped_instance_task_types(self, computed_instances: int) -> Set[str]:
        """Resolve current instance task types under grouped syntax."""
        instance_id_env = os.getenv("RUNNER_INSTANCE_ID")
        if instance_id_env is None:
            # In the launcher/main process (no instance id), expose the union.
            union: Set[str] = set()
            for types in self.RUNNER_TASK_TYPES_BY_INSTANCE:
                union |= set(types)
            return union

        instance_id = self._read_int(
            "RUNNER_INSTANCE_ID",
            0,
            min_value=0,
            max_value=computed_instances - 1,
        )

        return set(self.RUNNER_TASK_TYPES_BY_INSTANCE[instance_id])

    def __init__(self):
        """Initialize configuration values."""

        self._configuration_errors: List[str] = []

        self._load_network_configuration()
        self._load_security_configuration()
        self._load_storage_configuration()
        self._load_notification_configuration()
        self._load_business_configuration()

    def _load_network_configuration(self) -> None:
        """Load runner and manager network addressing settings."""

        # Runner/Multi-instance configuration
        self.RUNNER_PROTOCOL: str = os.getenv("RUNNER_PROTOCOL", "http")
        self.RUNNER_HOST: str = os.getenv("RUNNER_HOST", "localhost")
        self.RUNNER_BASE_NAME: str = os.getenv("RUNNER_BASE_NAME", "default-runner")
        self.RUNNER_BASE_PORT: int = self._read_int(
            "RUNNER_BASE_PORT", 8082, min_value=80, max_value=65535
        )

        # Manager URL configuration
        manager_url_env = os.getenv("MANAGER_URL")
        if manager_url_env is not None and not manager_url_env.strip():
            self._record_configuration_error("MANAGER_URL must not be empty")
        self.MANAGER_URL: str = _normalize_base_url(manager_url_env, "http://localhost:8081")

    def _load_security_configuration(self) -> None:
        """Load authentication and request security settings."""

        # API token authentication: access token for this runner (must match an authorised token in the manager)
        self.RUNNER_TOKEN: str = os.getenv("RUNNER_TOKEN", "default-runner-token")

        # CORS configuration
        cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "*")
        self.CORS_ALLOW_ORIGINS = [
            o.strip() for o in (cors_origins_raw or "").split(",") if o.strip()
        ] or ["*"]
        self.CORS_ALLOW_CREDENTIALS: bool = self._read_bool("CORS_ALLOW_CREDENTIALS", False)
        self.CORS_ALLOW_METHODS = [
            m.strip() for m in (os.getenv("CORS_ALLOW_METHODS", "*") or "").split(",") if m.strip()
        ] or ["*"]
        self.CORS_ALLOW_HEADERS = [
            h.strip() for h in (os.getenv("CORS_ALLOW_HEADERS", "*") or "").split(",") if h.strip()
        ] or ["*"]

        # Outbound downloads (assets, sources)
        # Default keeps compatibility with internal Opencast deployments.
        self.DOWNLOAD_ALLOWED_HOSTS = [
            h.strip().lower()
            for h in (os.getenv("DOWNLOAD_ALLOWED_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        self.DOWNLOAD_ALLOW_PRIVATE_NETWORKS: bool = self._read_bool(
            "DOWNLOAD_ALLOW_PRIVATE_NETWORKS", True
        )

    def _load_storage_configuration(self) -> None:
        """Load log, workspace, status, and cache paths."""

        # Log directory.
        # Prefer LOG_DIR, keep LOG_DIRECTORY for backward compatibility.
        log_dir = _first_env_value("LOG_DIR", "LOG_DIRECTORY", default="/var/log/esup-runner")
        if not log_dir.strip():
            self._record_configuration_error("LOG_DIR must not be empty")
            log_dir = "/var/log/esup-runner"
        if not log_dir.endswith("/"):
            log_dir += "/"
        self.LOG_DIR: str = log_dir
        self.LOG_DIRECTORY: str = log_dir

        # Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

        # Workspace and storage configuration
        self.STORAGE_DIR: str = os.getenv("STORAGE_DIR", "/tmp/esup-runner")

        # Persistent runner-side task status file (used for restart recovery)
        configured_task_status_file = (os.getenv("RUNNER_TASK_STATUS_FILE") or "").strip()
        if configured_task_status_file:
            self.RUNNER_TASK_STATUS_FILE: str = str(Path(configured_task_status_file))
        else:
            self.RUNNER_TASK_STATUS_FILE = str(Path(self.STORAGE_DIR) / "runner_task_statuses.json")

        # Maximum age of files in storage in days (0 for unlimited)
        self.MAX_FILE_AGE_DAYS: int = self._read_int("MAX_FILE_AGE_DAYS", 0, min_value=0)

        # Interval for periodic cleanup in hours
        self.CLEANUP_INTERVAL_HOURS: int = self._read_int("CLEANUP_INTERVAL_HOURS", 24, min_value=1)

        # Shared cache root for transcription models and uv cache.
        cache_dir_raw = os.getenv("CACHE_DIR", "/home/esup-runner/.cache/esup-runner")
        if not cache_dir_raw.strip():
            self._record_configuration_error("CACHE_DIR must not be empty")
            cache_dir_raw = "/home/esup-runner/.cache/esup-runner"
        cache_dir = Path(cache_dir_raw)
        self.CACHE_DIR: str = str(cache_dir)
        # Directory where whisper models (.gguf/.bin) are stored
        self.WHISPER_MODELS_DIR: str = os.getenv(
            "WHISPER_MODELS_DIR", str(cache_dir / "whisper-models")
        )
        # Directory where Hugging Face translation models are cached
        self.HUGGINGFACE_MODELS_DIR: str = os.getenv(
            "HUGGINGFACE_MODELS_DIR", str(cache_dir / "huggingface")
        )
        # Directory where uv stores package cache artifacts.
        self.UV_CACHE_DIR: str = os.getenv("UV_CACHE_DIR", str(cache_dir / "uv"))

    def _load_notification_configuration(self) -> None:
        """Load email and task-completion notification settings."""

        # SMTP configuration for failure notifications
        self.SMTP_SERVER: str = os.getenv("SMTP_SERVER", "")
        self.SMTP_PORT: int = self._read_int("SMTP_PORT", 25, min_value=1, max_value=65535)
        self.SMTP_USE_TLS: bool = self._read_bool("SMTP_USE_TLS", False)
        self.SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
        self.SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
        self.SMTP_SENDER: str = os.getenv("SMTP_SENDER", "")
        self.MANAGER_EMAIL: str = os.getenv("MANAGER_EMAIL", "")

        # Task completion notification retry settings
        # Maximum number of retries for notifying task completion
        self.COMPLETION_NOTIFY_MAX_RETRIES: int = self._read_int(
            "COMPLETION_NOTIFY_MAX_RETRIES",
            5,
            min_value=0,
        )

        # Delay between retries in seconds
        self.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS: int = self._read_int(
            "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS",
            60,
            min_value=0,
        )
        # Backoff factor for retry delays
        self.COMPLETION_NOTIFY_BACKOFF_FACTOR: float = self._read_float(
            "COMPLETION_NOTIFY_BACKOFF_FACTOR",
            1.5,
            min_value=1.0,
        )

    def _load_business_configuration(self) -> None:
        """Load runner execution and media-processing settings."""

        self.DEBUG: bool = self._read_bool("DEBUG", False)

        runner_instances_env = os.getenv("RUNNER_INSTANCES")
        runner_task_types_spec = os.getenv("RUNNER_TASK_TYPES", "encoding,studio")

        # Task types managed by this runner.
        #
        # Supported syntaxes:
        # - Legacy:   RUNNER_INSTANCES=2 and RUNNER_TASK_TYPES=encoding,studio,transcription
        #            => all instances handle the same set.
        # - Grouped:  RUNNER_TASK_TYPES=[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]
        #            => total instances is derived from the sum of the multipliers.
        self._configure_runner_task_types(runner_instances_env, runner_task_types_spec)

        self.RUNNER_MONITORING: bool = self._read_bool("RUNNER_MONITORING", False)

        # Maximum video size in GB for processing (0 for unlimited)
        self.MAX_VIDEO_SIZE_GB: int = self._read_int("MAX_VIDEO_SIZE_GB", 0, min_value=0)

        # Application-level denylist for media codecs rejected before FFmpeg/Whisper.
        media_codec_denylist_raw = os.getenv("MEDIA_CODEC_DENYLIST", "magicyuv")
        self.MEDIA_CODEC_DENYLIST = [
            item.strip().lower() for item in media_codec_denylist_raw.split(",") if item.strip()
        ]

        # Maximum duration (seconds) allowed for external task scripts
        # (encoding/studio/transcription handlers)
        self.EXTERNAL_SCRIPT_TIMEOUT_SECONDS: int = self._read_int(
            "EXTERNAL_SCRIPT_TIMEOUT_SECONDS",
            18000,
            min_value=1,
        )

        # Encoding type (CPU or GPU)
        self.ENCODING_TYPE: str = os.getenv("ENCODING_TYPE", "CPU").upper()

        # # # Specifics settings for GPU encoding # # #
        # HWACCEL_DEVICE parameter for GPU encoding (Ex: 0)
        self.GPU_HWACCEL_DEVICE: int = self._read_int("GPU_HWACCEL_DEVICE", 0, min_value=0)
        # CUDA_VISIBLE_DEVICES parameter for GPU encoding (Ex: 0,1)
        self.GPU_CUDA_VISIBLE_DEVICES: str = os.getenv("GPU_CUDA_VISIBLE_DEVICES", "0,1")
        # CUDA_DEVICE_ORDER parameter for GPU encoding (Ex: PCI_BUS_ID)
        self.GPU_CUDA_DEVICE_ORDER: str = os.getenv("GPU_CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        # CUDA_PATH parameter for GPU encoding (Ex: /usr/local/cuda-13.2)
        self.GPU_CUDA_PATH: str = os.getenv("GPU_CUDA_PATH", "/usr/local/cuda-13.2")

        # # # Default encoding settings for STUDIO tasks # # #
        # Compression rate factor (CRF) for video encoding
        self.STUDIO_DEFAULT_CRF: str = os.getenv("STUDIO_DEFAULT_CRF", "23")
        # Preset for video encoding (ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow)
        self.STUDIO_DEFAULT_PRESET: str = os.getenv("STUDIO_DEFAULT_PRESET", "medium")
        # Audio bitrate for audio encoding
        self.STUDIO_DEFAULT_AUDIO_BITRATE: str = os.getenv("STUDIO_DEFAULT_AUDIO_BITRATE", "128k")

        # # # Transcription (Whisper) settings # # #
        # Logical whisper model (small|medium|large|turbo)
        self.WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "small").lower()
        # Default language (e.g., 'auto', 'fr', 'en')
        self.WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "auto")

    def whisper_use_gpu(self) -> bool:
        """Return whether whisper should use GPU based on encoding type."""
        return self.ENCODING_TYPE == "GPU"

    def validate_configuration(self) -> None:
        """
        Validate critical configuration settings.

        Raises:
            ConfigValidationError: If configuration values are missing or invalid
        """
        errors = list(getattr(self, "_configuration_errors", []))
        validators = (
            self._validate_instances,
            self._validate_task_types,
            self._validate_ports,
            self._validate_tokens,
            self._validate_cors,
            self._validate_names_and_urls,
            self._validate_enums,
            self._validate_numeric_limits,
            self._validate_studio_defaults,
            self._validate_paths,
            self._validate_gpu,
        )

        for validator in validators:
            try:
                validator()
            except ConfigValidationError as exc:
                errors.extend(exc.errors)
            except ValueError as exc:
                errors.append(str(exc))

        _raise_validation_errors(errors)

    def _validate_instances(self) -> None:
        """Validate that the runner starts at least one instance."""
        if self.RUNNER_INSTANCES < 1:
            raise ValueError("RUNNER_INSTANCES must be at least 1")

    def _validate_task_types(self) -> None:
        """Validate that each instance references at least one known handler."""
        errors: List[str] = []
        if not hasattr(self, "RUNNER_TASK_TYPES_BY_INSTANCE"):
            raise ValueError("RUNNER_TASK_TYPES must define at least one instance")
        if len(self.RUNNER_TASK_TYPES_BY_INSTANCE) < 1:
            raise ValueError("RUNNER_TASK_TYPES must define at least one instance")

        for idx, task_types in enumerate(self.RUNNER_TASK_TYPES_BY_INSTANCE):
            if not task_types:
                errors.append(f"RUNNER_TASK_TYPES: instance {idx} has no task types")
                continue

            unsupported = sorted(set(task_types) - SUPPORTED_TASK_TYPES)
            if unsupported:
                errors.append(
                    f"RUNNER_TASK_TYPES: instance {idx} contains unsupported task types: "
                    f"{', '.join(unsupported)}"
                )

        _raise_validation_errors(errors)

    def _validate_ports(self) -> None:
        """Validate the complete TCP port range required by all instances."""
        errors: List[str] = []
        if not 80 <= self.RUNNER_BASE_PORT <= 65535:
            errors.append("RUNNER_BASE_PORT must be between 80 and 65535")
        elif self.RUNNER_BASE_PORT + self.RUNNER_INSTANCES - 1 > 65535:
            errors.append("RUNNER_BASE_PORT and RUNNER_INSTANCES require ports above 65535")
        _raise_validation_errors(errors)

    def _validate_tokens(self) -> None:
        """Reject empty and documented placeholder authentication tokens."""
        if not self.RUNNER_TOKEN or self.RUNNER_TOKEN in {
            "default-runner-token",
            "CHANGE_ME_RUNNERS_TOKEN",
        }:
            raise ValueError("RUNNER_TOKEN must be set to a secure value")

    def _validate_cors(self) -> None:
        """Reject the wildcard-origin and credential combination forbidden by CORS."""
        if self.CORS_ALLOW_CREDENTIALS and ("*" in self.CORS_ALLOW_ORIGINS):
            raise ValueError(
                "Invalid CORS configuration: CORS_ALLOW_CREDENTIALS=true is not compatible with CORS_ALLOW_ORIGINS=*"
            )

    def _validate_names_and_urls(self) -> None:
        """Validate required runner identifiers and the manager base URL."""
        errors: List[str] = []
        for name, value in (
            ("RUNNER_HOST", self.RUNNER_HOST),
            ("RUNNER_BASE_NAME", self.RUNNER_BASE_NAME),
        ):
            if not value.strip():
                errors.append(f"{name} must not be empty")

        if self.RUNNER_HOST.strip() and (
            any(character.isspace() for character in self.RUNNER_HOST) or "/" in self.RUNNER_HOST
        ):
            errors.append("RUNNER_HOST must contain a hostname or IP address only")

        try:
            manager_url = urlparse(self.MANAGER_URL)
            manager_hostname = manager_url.hostname
            manager_url.port
        except ValueError:
            manager_url = None
            manager_hostname = None

        if (
            manager_url is None
            or manager_url.scheme not in {"http", "https"}
            or not manager_hostname
            or any(character.isspace() for character in manager_hostname)
        ):
            errors.append("MANAGER_URL must be an absolute HTTP(S) URL")

        _raise_validation_errors(errors)

    def _validate_enums(self) -> None:
        """Validate configuration values drawn from a finite supported set."""
        errors: List[str] = []
        if self.RUNNER_PROTOCOL not in {"http", "https"}:
            errors.append("RUNNER_PROTOCOL must be either 'http' or 'https'")
        if self.LOG_LEVEL not in SUPPORTED_LOG_LEVELS:
            errors.append("LOG_LEVEL must be one of: " + ", ".join(sorted(SUPPORTED_LOG_LEVELS)))
        if self.ENCODING_TYPE not in {"CPU", "GPU"}:
            errors.append("ENCODING_TYPE must be either 'CPU' or 'GPU'")
        _raise_validation_errors(errors)

    def _validate_numeric_limits(self) -> None:
        """Validate numeric limits even when attributes were changed after loading."""
        errors: List[str] = []
        checks = (
            ("SMTP_PORT", self.SMTP_PORT, 1, 65535),
            ("COMPLETION_NOTIFY_MAX_RETRIES", self.COMPLETION_NOTIFY_MAX_RETRIES, 0, None),
            (
                "COMPLETION_NOTIFY_RETRY_DELAY_SECONDS",
                self.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS,
                0,
                None,
            ),
            ("COMPLETION_NOTIFY_BACKOFF_FACTOR", self.COMPLETION_NOTIFY_BACKOFF_FACTOR, 1, None),
            ("MAX_VIDEO_SIZE_GB", self.MAX_VIDEO_SIZE_GB, 0, None),
            ("MAX_FILE_AGE_DAYS", self.MAX_FILE_AGE_DAYS, 0, None),
            ("CLEANUP_INTERVAL_HOURS", self.CLEANUP_INTERVAL_HOURS, 1, None),
            ("EXTERNAL_SCRIPT_TIMEOUT_SECONDS", self.EXTERNAL_SCRIPT_TIMEOUT_SECONDS, 1, None),
            ("GPU_HWACCEL_DEVICE", self.GPU_HWACCEL_DEVICE, 0, None),
        )
        for name, value, minimum, maximum in checks:
            if value < minimum:
                errors.append(f"{name} must be at least {minimum}")
            if maximum is not None and value > maximum:
                errors.append(f"{name} must be at most {maximum}")
        _raise_validation_errors(errors)

    def _validate_studio_defaults(self) -> None:
        """Validate FFmpeg-compatible defaults used by Studio tasks."""
        errors: List[str] = []
        try:
            crf = int(self.STUDIO_DEFAULT_CRF)
        except (TypeError, ValueError):
            crf = None
        if crf is None or not 0 <= crf <= 51:
            errors.append("STUDIO_DEFAULT_CRF must be an integer between 0 and 51")

        if self.STUDIO_DEFAULT_PRESET not in SUPPORTED_STUDIO_PRESETS:
            errors.append(
                "STUDIO_DEFAULT_PRESET must be one of: "
                + ", ".join(sorted(SUPPORTED_STUDIO_PRESETS))
            )

        if not re.fullmatch(r"[1-9]\d*(?:\.\d+)?[kKmM]?", self.STUDIO_DEFAULT_AUDIO_BITRATE):
            errors.append("STUDIO_DEFAULT_AUDIO_BITRATE must be a positive bitrate such as '128k'")
        _raise_validation_errors(errors)

    def _validate_paths(self) -> None:
        """Ensure required storage and cache path settings are not empty."""
        errors = [
            f"{name} must not be empty"
            for name, value in (
                ("LOG_DIR", self.LOG_DIR),
                ("STORAGE_DIR", self.STORAGE_DIR),
                ("RUNNER_TASK_STATUS_FILE", self.RUNNER_TASK_STATUS_FILE),
                ("CACHE_DIR", self.CACHE_DIR),
                ("WHISPER_MODELS_DIR", self.WHISPER_MODELS_DIR),
                ("HUGGINGFACE_MODELS_DIR", self.HUGGINGFACE_MODELS_DIR),
                ("UV_CACHE_DIR", self.UV_CACHE_DIR),
            )
            if not value.strip()
        ]
        _raise_validation_errors(errors)

    def _validate_gpu(self) -> None:
        """Ensure the configured CUDA directory exists when GPU mode is enabled."""
        if self.ENCODING_TYPE != "GPU":
            return
        if not os.path.exists(self.GPU_CUDA_PATH):
            raise ValueError(f"WARNING: CUDA directory not found at: {self.GPU_CUDA_PATH}")


def _parse_task_types_csv(value: str) -> Set[str]:
    """Parse a simple comma-separated list of task types."""
    parts = [p.strip() for p in (value or "").split(",")]
    return set(filter(None, parts))


def _normalize_grouped_task_types_spec(spec: Optional[str]) -> Optional[str]:
    if spec is None:
        return None

    raw = spec.strip()
    if not raw:
        return None

    # Heuristic: grouped syntax contains something like "2x(" or "2X(".
    if not re.search(r"\d+\s*[xX]\s*\(", raw):
        return None

    # Optional surrounding brackets.
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()

    raw = raw.rstrip().rstrip(",")
    if not raw:
        raise ValueError("RUNNER_TASK_TYPES grouped syntax is empty")

    return raw


def _expand_grouped_task_types(raw: str) -> List[Set[str]]:
    pattern = re.compile(r"\s*(\d+)\s*[xX]\s*\(([^)]*)\)\s*(?:,|$)")
    pos = 0
    expanded: List[Set[str]] = []

    while pos < len(raw):
        m = pattern.match(raw, pos)
        if not m:
            snippet = raw[pos : min(len(raw), pos + 50)]
            raise ValueError(f"Invalid RUNNER_TASK_TYPES grouped syntax near: {snippet!r}")

        count = int(m.group(1))
        if count < 1:
            raise ValueError("RUNNER_TASK_TYPES grouped syntax: multiplier must be >= 1")

        types = _parse_task_types_csv(m.group(2))
        if not types:
            raise ValueError(
                "RUNNER_TASK_TYPES grouped syntax: each group must contain at least one task type"
            )

        for _ in range(count):
            expanded.append(set(types))

        pos = m.end()

    if not expanded:
        raise ValueError("RUNNER_TASK_TYPES grouped syntax produced no instances")

    return expanded


def _parse_grouped_task_types_spec(spec: str) -> Optional[List[Set[str]]]:
    """Parse a grouped task type specification.

    Example:
        [2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]

    Returns:
        A list of per-instance task type sets (expanded), or None if spec is not grouped.
    """
    raw = _normalize_grouped_task_types_spec(spec)
    if raw is None:
        return None

    return _expand_grouped_task_types(raw)


config = get_config()


def _is_pytest_run() -> bool:
    # `PYTEST_CURRENT_TEST` is only set while executing a test; during collection it
    # may be absent. We also check loaded modules/argv to reliably detect pytest.
    return (
        os.getenv("PYTEST_CURRENT_TEST") is not None
        or "pytest" in sys.modules
        or any(os.path.basename(arg).startswith("pytest") for arg in sys.argv)
    )


# Auto-validate configuration on module load (skip under pytest to avoid failing imports)
if not _is_pytest_run():
    config.validate_configuration()
