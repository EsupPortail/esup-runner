# runner/app/core/config.py
"""
Configuration module for runner.
Handles environment variables, security settings, and application configuration.
"""

import os
import re
import sys
import warnings
from typing import List, Optional, Set

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

    def __init__(self):
        """Initialize configuration values."""

        # DEBUG mode
        debug_env = os.getenv("DEBUG", "False")
        self.DEBUG: bool = (
            debug_env.lower() in ("true", "1", "yes")
            if isinstance(debug_env, str)
            else bool(debug_env)
        )

        # Runner/Multi-instance configuration
        self.RUNNER_PROTOCOL: str = os.getenv("RUNNER_PROTOCOL", "http")
        self.RUNNER_HOST: str = os.getenv("RUNNER_HOST", "localhost")
        self.RUNNER_BASE_NAME: str = os.getenv("RUNNER_BASE_NAME", "default-runner")
        self.RUNNER_BASE_PORT: int = int(os.getenv("RUNNER_BASE_PORT", 8081))

        runner_instances_env = os.getenv("RUNNER_INSTANCES")
        runner_task_types_spec = os.getenv("RUNNER_TASK_TYPES", "encoding,studio")

        # Task types managed by this runner.
        #
        # Supported syntaxes:
        # - Legacy:   RUNNER_INSTANCES=2 and RUNNER_TASK_TYPES=encoding,studio,transcription
        #            => all instances handle the same set.
        # - Grouped:  RUNNER_TASK_TYPES=[2x(encoding,studio,transcription),1x(encoding,studio),1x(transcription)]
        #            => total instances is derived from the sum of the multipliers.
        grouped = _parse_grouped_task_types_spec(runner_task_types_spec)
        if grouped is not None:
            self.RUNNER_TASK_TYPES_BY_INSTANCE: List[Set[str]] = grouped
            computed_instances = len(self.RUNNER_TASK_TYPES_BY_INSTANCE)
            if runner_instances_env is not None:
                try:
                    configured_instances = int(runner_instances_env)
                    if configured_instances != computed_instances:
                        warnings.warn(
                            "RUNNER_INSTANCES is ignored because RUNNER_TASK_TYPES uses grouped syntax "
                            f"(computed instances={computed_instances}, configured RUNNER_INSTANCES={configured_instances})."
                        )
                except ValueError:
                    warnings.warn(
                        "Invalid RUNNER_INSTANCES value ignored because RUNNER_TASK_TYPES uses grouped syntax."
                    )

            self.RUNNER_INSTANCES = computed_instances

            instance_id_env = os.getenv("RUNNER_INSTANCE_ID")
            if instance_id_env is None:
                # In the launcher/main process (no instance id), expose the union.
                union: Set[str] = set()
                for types in self.RUNNER_TASK_TYPES_BY_INSTANCE:
                    union |= set(types)
                self.RUNNER_TASK_TYPES = union
            else:
                instance_id = int(instance_id_env)
                if not (0 <= instance_id < computed_instances):
                    raise ValueError(
                        f"RUNNER_INSTANCE_ID={instance_id} out of range for grouped RUNNER_TASK_TYPES (instances={computed_instances})"
                    )
                self.RUNNER_TASK_TYPES = set(self.RUNNER_TASK_TYPES_BY_INSTANCE[instance_id])

        else:
            # Legacy behavior: same task types for all instances.
            self.RUNNER_INSTANCES = int(os.getenv("RUNNER_INSTANCES", 1))
            task_types = _parse_task_types_csv(runner_task_types_spec)
            self.RUNNER_TASK_TYPES = task_types
            self.RUNNER_TASK_TYPES_BY_INSTANCE = [
                set(task_types) for _ in range(self.RUNNER_INSTANCES)
            ]

        # Monitor instances and automatically restart failed ones
        self.RUNNER_MONITORING: bool = os.getenv("RUNNER_MONITORING", "False").lower() in (
            "true",
            "1",
            "yes",
        )

        # API token authentication: access token for this runner (must match an authorised token in the manager)
        self.RUNNER_TOKEN: str = os.getenv("RUNNER_TOKEN", "default-runner-token")

        # Manager URL configuration
        self.MANAGER_URL: str = os.getenv("MANAGER_URL", "http://localhost:8000")

        # SMTP configuration for failure notifications
        self.SMTP_SERVER: str = os.getenv("SMTP_SERVER", "")
        self.SMTP_PORT: int = _parse_int(os.getenv("SMTP_PORT"), 25, min_value=1, max_value=65535)
        self.SMTP_USE_TLS: bool = _parse_bool(os.getenv("SMTP_USE_TLS"), default=False)
        self.SMTP_USERNAME: str = os.getenv("SMTP_USERNAME", "")
        self.SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD", "")
        self.SMTP_SENDER: str = os.getenv("SMTP_SENDER", "")
        self.MANAGER_EMAIL: str = os.getenv("MANAGER_EMAIL", "")

        # Task completion notification retry settings
        # Maximum number of retries for notifying task completion
        self.COMPLETION_NOTIFY_MAX_RETRIES: int = _parse_int(
            os.getenv("COMPLETION_NOTIFY_MAX_RETRIES"),
            5,
            min_value=0,
        )

        # Delay between retries in seconds
        self.COMPLETION_NOTIFY_RETRY_DELAY_SECONDS: int = _parse_int(
            os.getenv("COMPLETION_NOTIFY_RETRY_DELAY_SECONDS"),
            60,
            min_value=0,
        )
        # Backoff factor for retry delays
        self.COMPLETION_NOTIFY_BACKOFF_FACTOR: float = _parse_float(
            os.getenv("COMPLETION_NOTIFY_BACKOFF_FACTOR"),
            1.5,
            min_value=1.0,
        )

        # CORS configuration
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

        # Outbound downloads (assets, sources)
        # Default keeps compatibility with internal Opencast deployments.
        self.DOWNLOAD_ALLOWED_HOSTS = [
            h.strip().lower()
            for h in (os.getenv("DOWNLOAD_ALLOWED_HOSTS", "") or "").split(",")
            if h.strip()
        ]
        self.DOWNLOAD_ALLOW_PRIVATE_NETWORKS: bool = _parse_bool(
            os.getenv("DOWNLOAD_ALLOW_PRIVATE_NETWORKS"), default=True
        )

        # Log directory
        self.LOG_DIRECTORY: str = os.getenv("LOG_DIRECTORY", "/var/log/esup-runner")
        # Add slash at end if missing
        if not self.LOG_DIRECTORY.endswith("/"):
            self.LOG_DIRECTORY += "/"

        # Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

        # Workspace and storage configuration
        self.STORAGE_DIR: str = os.getenv("STORAGE_DIR", "/tmp/esup-runner/storage")

        # Maximum video size in GB for processing (0 for unlimited)
        self.MAX_VIDEO_SIZE_GB: int = int(os.getenv("MAX_VIDEO_SIZE_GB", 0))

        # Maximum age of files in storage in days (0 for unlimited)
        self.MAX_FILE_AGE_DAYS: int = int(os.getenv("MAX_FILE_AGE_DAYS", 0))

        # Interval for periodic cleanup in hours
        self.CLEANUP_INTERVAL_HOURS: int = int(os.getenv("CLEANUP_INTERVAL_HOURS", 24))

        # Maximum duration (seconds) allowed for external task scripts
        # (encoding/studio/transcription handlers)
        external_script_timeout = _parse_int(
            os.getenv("EXTERNAL_SCRIPT_TIMEOUT_SECONDS"),
            18000,
        )
        self.EXTERNAL_SCRIPT_TIMEOUT_SECONDS: int = (
            external_script_timeout if external_script_timeout > 0 else 18000
        )

        # Encoding type (CPU or GPU)
        self.ENCODING_TYPE: str = os.getenv("ENCODING_TYPE", "CPU").upper()

        # # # Specifics settings for GPU encoding # # #
        # HWACCEL_DEVICE parameter for GPU encoding (Ex: 0)
        self.GPU_HWACCEL_DEVICE: int = int(os.getenv("GPU_HWACCEL_DEVICE", 0))
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
        # Directory where whisper models (.gguf/.bin) are stored
        self.WHISPER_MODELS_DIR: str = os.getenv(
            "WHISPER_MODELS_DIR", "/tmp/esup-runner/whisper-models"
        )
        # Default language (e.g., 'auto', 'fr', 'en')
        self.WHISPER_LANGUAGE: str = os.getenv("WHISPER_LANGUAGE", "auto")

    def whisper_use_gpu(self) -> bool:
        """Return whether whisper should use GPU based on encoding type."""
        return self.ENCODING_TYPE == "GPU"

    def validate_configuration(self) -> None:
        """
        Validate critical configuration settings.

        Raises:
            ValueError: If essential configuration is missing or invalid
        """
        self._validate_instances()
        self._validate_task_types()
        self._validate_ports()
        self._validate_tokens()
        self._validate_cors()
        self._validate_gpu()

    def _validate_instances(self) -> None:
        if self.RUNNER_INSTANCES < 1:
            raise ValueError("RUNNER_INSTANCES must be at least 1")

    def _validate_task_types(self) -> None:
        if not hasattr(self, "RUNNER_TASK_TYPES_BY_INSTANCE"):
            raise ValueError("RUNNER_TASK_TYPES must define at least one instance")
        if len(self.RUNNER_TASK_TYPES_BY_INSTANCE) < 1:
            raise ValueError("RUNNER_TASK_TYPES must define at least one instance")

        for idx, task_types in enumerate(self.RUNNER_TASK_TYPES_BY_INSTANCE):
            if not task_types:
                raise ValueError(f"RUNNER_TASK_TYPES: instance {idx} has no task types")

    def _validate_ports(self) -> None:
        if not (80 <= self.RUNNER_BASE_PORT <= 65535):
            raise ValueError("RUNNER_BASE_PORT must be between 80 and 65535")

    def _validate_tokens(self) -> None:
        if not self.RUNNER_TOKEN or self.RUNNER_TOKEN == "default-runner-token":
            raise ValueError("RUNNER_TOKEN must be set to a secure value")

    def _validate_cors(self) -> None:
        if self.CORS_ALLOW_CREDENTIALS and ("*" in self.CORS_ALLOW_ORIGINS):
            raise ValueError(
                "Invalid CORS configuration: CORS_ALLOW_CREDENTIALS=true is not compatible with CORS_ALLOW_ORIGINS=*"
            )

    def _validate_gpu(self) -> None:
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


# Create global config instance using the factory function
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
