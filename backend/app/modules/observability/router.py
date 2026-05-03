"""Health endpoints. Liveness and readiness for App Platform / Kubernetes-style probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.core.database import ping_database
from app.core.logging import get_logger

router = APIRouter(tags=["health"])
log = get_logger(__name__)


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Process is up. Does not touch the database."""
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, Any]:
    """Process is up AND Postgres responds to ``SELECT 1``.

    Returns 503 with ``{"status": "degraded", ...}`` if the DB ping fails so
    a load balancer can pull this instance out of rotation.
    """
    try:
        db = await ping_database()
    except Exception as exc:
        log.warning("readiness_check_failed", error=str(exc))
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "db": "unreachable"}
    return {"status": "ok", **db}


@router.get("/version", summary="Build info")
async def version() -> dict[str, str]:
    return {"version": __version__}
