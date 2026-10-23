"""Validated per-instance Live configuration; no client-supplied FFmpeg options."""

import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

# Keep network, storage, hardware and rendition choices configurable, not runtime tuning.
ENVIRONMENT_FIELDS = (
    "mediamtx_api_url",
    "mediamtx_api_user",
    "mediamtx_api_password",
    "mediamtx_health_check_interval_hours",
    "pod_url",
    "pod_username",
    "pod_password",
    "pod_establishment_name",
    "upstream_rtmp_url",
    "local_rtmp_url",
    "public_hls_base_url",
    "hls_directory",
    "transcoder",
    "cpu_fallback",
    "nvdec",
    "variants",
)


class LiveVariant(BaseModel):
    """Define one adaptive rendition with a safe name, even dimensions and bitrates."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,20}$")
    width: int = Field(ge=2, le=7680, multiple_of=2)
    height: int = Field(ge=2, le=4320, multiple_of=2)
    video_bitrate: int = Field(ge=10000, le=100000000)
    audio_bitrate: int = Field(ge=16000, le=320000)


class LiveSettings(BaseModel):
    """Share media settings and resolve HLS output locations for each Runner instance."""

    model_config = ConfigDict(allow_inf_nan=False)
    mediamtx_api_url: str = "http://127.0.0.1:10997"
    mediamtx_api_user: str = "control"
    mediamtx_api_password: str = ""
    mediamtx_health_check_interval_hours: int = Field(default=12, ge=0)
    pod_url: str = ""
    pod_username: str = ""
    pod_password: str = ""
    pod_establishment_name: str = Field(default="", max_length=200)
    mediamtx_timeout: float = Field(default=5, gt=0)
    upstream_rtmp_url: str = ""
    local_rtmp_url: str = "rtmp://127.0.0.1:2935"
    public_hls_base_url: str = ""
    hls_directory: str = "/dev/shm/hls"
    transcoder: Literal["cpu", "nvenc"] = "cpu"
    cpu_fallback: bool = True
    nvdec: bool = False
    video_codec: Literal["h264"] = "h264"
    audio_codec: Literal["aac"] = "aac"
    nvenc_preset: Literal["p1", "p2", "p3", "p4", "p5", "p6", "p7"] = "p4"
    cpu_preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"
    ] = "veryfast"
    fps: int = Field(default=25, ge=1, le=120)
    gop: int = Field(default=50, ge=1, le=1000)
    segment_seconds: float = Field(default=2, ge=1, le=30)
    playlist_size: int = Field(default=6, ge=3, le=100)
    source_timeout: float = Field(default=120, gt=0)
    startup_timeout: float = Field(default=60, gt=0)
    stall_timeout: float = Field(default=20, gt=0)
    stop_timeout: float = Field(default=10, gt=0)
    poll_interval: float = Field(default=2, gt=0)
    restart_attempts: int = Field(default=3, ge=0, le=100)
    variants: list[LiveVariant] = Field(
        default_factory=lambda: [
            LiveVariant(
                name="high", width=1280, height=720, video_bitrate=1100000, audio_bitrate=128000
            ),
            LiveVariant(
                name="mid", width=720, height=406, video_bitrate=560000, audio_bitrate=96000
            ),
            LiveVariant(
                name="low", width=480, height=270, video_bitrate=280000, audio_bitrate=64000
            ),
        ],
        min_length=2,
        max_length=16,
    )

    @classmethod
    def from_environment(cls) -> "LiveSettings":
        """Load operator settings, leaving technical limits at their internal defaults."""
        values = {
            name: os.environ[f"LIVE_{name.upper()}"]
            for name in ENVIRONMENT_FIELDS
            if f"LIVE_{name.upper()}" in os.environ
        }
        if "variants" in values:
            values["variants"] = json.loads(values["variants"])
        return cls.model_validate(values)

    def for_instance(self, instance_id: int, port: int) -> "LiveSettings":
        """Resolve HLS templates without changing shared credentials or encoding settings.

        The public URL accepts {instance_id} and {port}; the output directory
        accepts only {instance_id}, keeping recovery paths stable when a port
        changes. Reject unknown or malformed placeholders without echoing values.
        Static directories may be shared: each session owns a UUID subdirectory.
        """
        values = {}
        for name, replacements in (
            ("public_hls_base_url", {"instance_id": str(instance_id), "port": str(port)}),
            ("hls_directory", {"instance_id": str(instance_id)}),
        ):
            value = getattr(self, name)
            for key, replacement in replacements.items():
                value = value.replace("{" + key + "}", replacement)
            if re.search(r"[{}]", value):
                allowed = ", ".join("{" + key + "}" for key in replacements)
                raise ValueError(f"LIVE_{name.upper()} only supports placeholders {allowed}")
            values[name] = value
        return self.model_copy(update=values)

    def validate_enabled(self) -> None:
        """Validate the URLs, output directory and adaptive profile required for Live.

        Raise ValueError for invalid URLs, embedded HTTP credentials, a non-absolute
        output path, duplicate rendition names or an incompatible GOP duration.
        """
        for name, schemes in (
            ("mediamtx_api_url", {"http", "https"}),
            ("upstream_rtmp_url", {"rtmp", "rtmps"}),
            ("local_rtmp_url", {"rtmp", "rtmps"}),
            ("public_hls_base_url", {"http", "https"}),
        ):
            parsed = urlsplit(getattr(self, name))
            try:
                parsed.port
            except ValueError:
                raise ValueError(f"LIVE_{name.upper()} has an invalid port") from None
            if (
                parsed.scheme not in schemes
                or not parsed.hostname
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"LIVE_{name.upper()} is invalid")
            if name in {"mediamtx_api_url", "public_hls_base_url"} and (
                parsed.username is not None or parsed.password is not None
            ):
                raise ValueError(f"LIVE_{name.upper()} must not contain credentials")
        self.validate_pod_settings()
        if not self.hls_directory or not Path(self.hls_directory).is_absolute():
            raise ValueError("LIVE_HLS_DIRECTORY must be an absolute per-instance directory")
        if len({v.name for v in self.variants}) != len(self.variants):
            raise ValueError("LIVE_VARIANTS names must be unique")
        if self.gop > self.fps * self.segment_seconds:
            raise ValueError("LIVE_GOP must not exceed LIVE_FPS * LIVE_SEGMENT_SECONDS")

    def validate_pod_settings(self) -> None:
        """Validate the optional Pod URL and its complete Basic Auth group."""
        pod_values = (self.pod_url, self.pod_username, self.pod_password)
        if not any(pod_values):
            return
        if not all(pod_values):
            raise ValueError(
                "LIVE_POD_URL, LIVE_POD_USERNAME and LIVE_POD_PASSWORD must be configured together"
            )
        parsed = urlsplit(self.pod_url)
        try:
            parsed.port
        except ValueError:
            raise ValueError("LIVE_POD_URL has an invalid port") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("LIVE_POD_URL must be an HTTP(S) URL without credentials")
