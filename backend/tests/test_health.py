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
