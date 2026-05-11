"""OpenAPI surface contract tests."""

from __future__ import annotations

from httpx import AsyncClient


async def test_openapi_served(client: AsyncClient) -> None:
    r = await client.get("/api/v1/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "ReorderOS API"
    paths = schema["paths"]

    # Sprint 1 surface
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/version" in paths

    # Sprint 2 surface
    assert "/api/v1/auth/register-tenant" in paths
    assert "/api/v1/auth/me" in paths
    assert "/api/v1/auth/active-tenant" in paths
    assert "/api/v1/auth/exchange" in paths
    assert "/api/v1/tenants" in paths
    assert "/api/v1/invitations" in paths
    assert "/api/v1/invitations/accept" in paths
