"""The probes: the one route that needs no identity (app.py's OPEN_PATHS)."""

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/heartbeat", response_class=PlainTextResponse, status_code=200)
async def heartbeat() -> str:
    """Meru's liveness and readiness probes (nuwa-app's heartbeat.path)."""
    return "ok"
