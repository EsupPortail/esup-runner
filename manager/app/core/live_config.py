"""MediaMTX discovery settings and optional broadcaster source declarations."""

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

MEDIAMTX_VERSION = "1.21.1"

# Only operator choices are configurable. Runtime limits remain internal defaults.
ENVIRONMENT_FIELDS = (
    "enabled",
    "auto_start",
    "auto_etab_name",
    "sources",
    "mediamtx_api_url",
    "mediamtx_api_user",
    "mediamtx_api_password",
    "mediamtx_log_unit",
    "mediamtx_log_file",
)


class LiveSettings(BaseModel):
    """Define optional broadcaster sources, media control access and polling limits."""

    model_config = ConfigDict(allow_inf_nan=False)
    enabled: bool = False
    auto_start: bool = True
    auto_etab_name: str = Field(default="unknown", min_length=1, max_length=200)
    sources: dict[str, str] = Field(default_factory=dict)
    mediamtx_api_url: str = "http://127.0.0.1:9997"
    mediamtx_api_user: str = "control"
    mediamtx_api_password: str = ""
    mediamtx_log_unit: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.service)?$"
    )
    mediamtx_log_file: str = ""
    mediamtx_timeout: float = Field(default=5, gt=0)
    poll_interval: float = Field(default=5, gt=0)
    runner_timeout: float = Field(default=15, gt=0)

    @classmethod
    def from_environment(cls) -> "LiveSettings":
        """Load operator settings, leaving technical limits at their internal defaults."""
        values = {
            name: os.environ[f"LIVE_{name.upper()}"]
            for name in ENVIRONMENT_FIELDS
            if f"LIVE_{name.upper()}" in os.environ
        }
        if "sources" in values:
            values["sources"] = json.loads(values["sources"])
        return cls.model_validate(values)

    def validate_enabled(self) -> None:
        """Validate the control URL and source identifiers when Live is enabled.

        Raise ValueError for invalid addresses or paths; disabled Live settings
        require no media configuration.
        """
        if not self.enabled:
            return
        self.validate_log_source()
        if self.auto_start and len(set(self.sources.values())) != len(self.sources):
            raise ValueError("LIVE_SOURCES paths must be unique with LIVE_AUTO_START")
        self.validate_control_url()
        for source_id, path in self.sources.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", source_id):
                raise ValueError("LIVE_SOURCES contains an invalid source identifier")
            if not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", path):
                raise ValueError("LIVE_SOURCES contains an invalid MediaMTX path")
            if source_id != path.rsplit("/", 1)[-1]:
                raise ValueError("LIVE_SOURCES identifiers must match broadcaster names")

    def validate_log_source(self) -> None:
        """Require at most one operator-owned MediaMTX diagnostic source."""
        if self.mediamtx_log_unit and self.mediamtx_log_file:
            raise ValueError("Choose LIVE_MEDIAMTX_LOG_UNIT or LIVE_MEDIAMTX_LOG_FILE, not both")
        if self.mediamtx_log_file and not Path(self.mediamtx_log_file).is_absolute():
            raise ValueError("LIVE_MEDIAMTX_LOG_FILE must be an absolute path")

    def validate_control_url(self) -> None:
        """Reject malformed MediaMTX control URLs and embedded credentials."""
        parsed = urlsplit(self.mediamtx_api_url)
        try:
            parsed.port
        except ValueError:
            raise ValueError("LIVE_MEDIAMTX_API_URL has an invalid port") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LIVE_MEDIAMTX_API_URL must be an HTTP(S) URL without credentials")
