"""OpenAPI surface contract tests."""

from __future__ import annotations

from httpx import AsyncClient


async def test_openapi_served(client: AsyncClient) -> None:
    r = await client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "ReorderOS API"
    # Sprint 1 surface only.
    paths = schema["paths"]
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/version" in paths
