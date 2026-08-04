"""Health endpoint smoke tests.

`/health/live` must never touch the DB; `/health/ready` reports degraded when
the DB is unreachable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient


async def test_live_returns_ok(client: AsyncClient) -> None:
    r = await client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_version(client: AsyncClient) -> None:
    r = await client.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert "version" in body
    assert isinstance(body["version"], str)
    # commit is always present; "unknown" when SOURCE_COMMIT is unset.
    assert "commit" in body


async def test_version_reports_source_commit(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed SHA must be observable per-component for the cutover verification —
    DigitalOcean exposes no per-component commit, so /version reports SOURCE_COMMIT."""
    monkeypatch.setenv("SOURCE_COMMIT", "abc123def456")
    r = await client.get("/version")
    assert r.status_code == 200
    assert r.json()["commit"] == "abc123def456"


async def test_health_and_version_routes_are_at_root_not_api_v1() -> None:
    """ROUTE CONTRACT (Finding 4A): readiness/version live at ROOT, NOT under /api/v1 — the
    DO health_check.http_path and the runbook curl both target /health/ready. Fails if the
    prefix drifts."""
    from app.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert "/health/ready" in paths and "/health/live" in paths and "/version" in paths
    assert "/api/v1/health/ready" not in paths


async def test_ready_degraded_when_db_down(client: AsyncClient) -> None:
    async def boom() -> dict[str, object]:
        raise RuntimeError("simulated DB outage")

    with patch("app.modules.observability.router.ping_database", side_effect=boom):
        r = await client.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] == "unreachable"


@pytest.mark.integration
async def test_ready_ok_with_real_db(client: AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
