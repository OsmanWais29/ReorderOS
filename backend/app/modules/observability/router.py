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


@router.get("/health/startup", summary="Startup probe")
async def startup() -> dict[str, str]:
    """Process has finished initializing. Mirrors liveness for App Platform."""
    return {"status": "ok"}


@router.get("/health/storage", summary="Object-storage readiness (Sprint 6 receipts)")
async def storage_ready() -> dict[str, Any]:
    """Which Spaces settings the RUNNING process actually sees — booleans only, NEVER
    values. Receipts uploads need all five present; this makes a config/injection gap
    visible without a token or a log dive (added during the Sprint 6 staging smoke,
    where an env-injection gap read as an opaque 503)."""
    from app.core.config import get_settings

    s = get_settings()
    fields = {
        "endpoint": bool(s.spaces_endpoint),
        "region": bool(s.spaces_region),
        "bucket": bool(s.spaces_bucket),
        "key": bool(s.spaces_key),
        "secret": bool(s.spaces_secret),
    }
    return {"configured": all(fields.values()), "fields": fields}


@router.get("/version", summary="Build info")
async def version() -> dict[str, str]:
    return {"version": __version__}
