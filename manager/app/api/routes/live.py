"""Authenticated Live API used by Esup-Pod and operational clients."""

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.core.auth import verify_token
from app.models.live import LiveResponse, LiveStartRequest
from app.services import live_service
from app.services.live_sources import known_sources

router = APIRouter(prefix="/live", tags=["Live"], dependencies=[Depends(verify_token)])


@router.post("/start", response_model=LiveResponse, status_code=202)
async def start_live(request: LiveStartRequest, token: str = Depends(verify_token)) -> LiveResponse:
    """Start or retrieve an idempotent Live session for the authenticated client."""
    return await live_service.start(request, token)


@router.get("/metrics", response_class=PlainTextResponse)
async def live_metrics() -> str:
    """Return persisted Live session and Runner gauges in Prometheus format."""
    return live_service.metrics()


@router.get("/sources")
async def list_sources() -> dict[str, list[str]]:
    """List configured/discovered identifiers without exposing private RTMP paths."""
    return {"sources": sorted(known_sources())}


@router.get("/sources/{source_id}")
async def get_source(source_id: str) -> dict:
    """Return known source metrics and its latest automatic session without RTMP secrets."""
    return await live_service.source_status(source_id)


@router.get("/{live_id}", response_model=LiveResponse)
async def get_live(live_id: str, token: str = Depends(verify_token)) -> LiveResponse:
    """Authorize access and refresh an owned or automatic session's state and HLS URL."""
    live_service.load(live_id, token)
    return live_service.response(await live_service.synchronize(live_id))


@router.post("/{live_id}/stop", response_model=LiveResponse, status_code=202)
async def stop_live(live_id: str, token: str = Depends(verify_token)) -> LiveResponse:
    """Persist an idempotent stop for an owned or shared automatic session."""
    return await live_service.stop(live_id, token)


@router.post("/{live_id}/restart", response_model=LiveResponse, status_code=202)
async def restart_live(live_id: str, token: str = Depends(verify_token)) -> LiveResponse:
    """Restart a terminal owned/automatic session with one idempotent replacement."""
    return await live_service.restart(live_id, token)
