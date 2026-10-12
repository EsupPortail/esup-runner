"""Public Live contract, independent of runner addresses and RTMP credentials."""

from typing import Literal

from pydantic import BaseModel, Field

LiveState = Literal[
    "pending", "waiting_source", "starting", "running", "stopping", "completed", "failed"
]


class LiveStartRequest(BaseModel):
    """Identify a broadcaster source and an event idempotency key scoped to a client."""

    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
    event_id: str = Field(min_length=1, max_length=200)
    equipment: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    etab_name: str = Field(min_length=1, max_length=200)
    app_name: str = Field(default="Esup-Pod", min_length=1, max_length=200)


class LiveInfo(BaseModel):
    """Persist a session's lifecycle, playback readiness and media measurements."""

    source_id: str
    event_id: str
    equipment: str | None = None
    automatic: bool = False
    publication_id: str | None = None
    state: LiveState = "pending"
    hls_url: str | None = None
    hls_ready: bool = False
    stop_requested: bool = False
    source_ready: bool = False
    input_bytes: int = 0
    forward_bytes: int = 0
    hls_sessions: int | None = None
    input_bitrate: float = 0
    codecs: list[str] = Field(default_factory=list)
    resolutions: list[str] = Field(default_factory=list)
    transcode_errors: int = 0
    last_seen: str | None = None
    error: str | None = None


class LiveResponse(LiveInfo):
    """Expose Live status together with its stable identifier, Runner and timestamps."""

    live_id: str
    runner: str
    created_at: str
    updated_at: str
