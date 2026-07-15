"""Refresh-token flow — the fix for the 5-minute-session smoke-test pain.

WorkOS access tokens are ~5-minute JWTs by design; the session lives in the
rotated refresh token. Until the Lauzon smoke, /exchange and /sign-in DISCARDED
the refresh_token WorkOS returned, so the app's whole session was one access
token. These tests pin: the pair passes through, /auth/refresh rotates it, and
a dead refresh token yields a clean 401 (client → full sign-in).

WorkOS is respx-mocked; no network.
"""

from __future__ import annotations

from typing import Any

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.main import create_app

pytestmark = pytest.mark.integration

_WORKOS_AUTH = "https://api.workos.com/user_management/authenticate"


@pytest.fixture
async def client(monkeypatch: Any) -> Any:
    s = get_settings()
    monkeypatch.setattr(s, "workos_client_id", "client_test_refresh")
    monkeypatch.setattr(s, "workos_secret_key", "sk_test_refresh")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@respx.mock
async def test_exchange_passes_refresh_token_through(client: AsyncClient) -> None:
    route = respx.post(_WORKOS_AUTH).mock(
        return_value=Response(200, json={"access_token": "at_1", "refresh_token": "rt_1"})
    )
    r = await client.post(
        "/api/v1/auth/exchange",
        json={"code": "code_x", "redirect_uri": "http://localhost:8081/auth/callback"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == "at_1"
    assert body["refresh_token"] == "rt_1"  # no longer discarded
    assert route.called


@respx.mock
async def test_refresh_rotates_the_pair(client: AsyncClient) -> None:
    route = respx.post(_WORKOS_AUTH).mock(
        return_value=Response(200, json={"access_token": "at_2", "refresh_token": "rt_2"})
    )
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "rt_1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"] == "at_2"
    assert body["refresh_token"] == "rt_2"  # rotated pair, both returned

    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    assert sent["grant_type"] == "refresh_token"
    assert sent["refresh_token"] == "rt_1"
    assert sent["client_secret"] == "sk_test_refresh"  # secret stays server-side


@respx.mock
async def test_dead_refresh_token_is_clean_401(client: AsyncClient) -> None:
    respx.post(_WORKOS_AUTH).mock(return_value=Response(400, json={"error": "invalid_grant"}))
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "rt_dead"})
    assert r.status_code == 401
