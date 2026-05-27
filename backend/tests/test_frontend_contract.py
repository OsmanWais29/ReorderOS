"""Frontend API contract tests.

Verifies that every endpoint the frontend calls:
  1. Exists and returns the expected shape.
  2. Accepts the exact headers the frontend sends.
  3. Returns meaningful errors (not 500) on bad input.

These tests mirror the fetch() calls in:
  - app/(app)/more.tsx          → GET /pos/clover/status, GET /pos/clover/connect-url
  - app/onboarding/found-summary.tsx → GET /pos/clover/status
  - app/onboarding/connecting.tsx    → GET /pos/clover/connect-url
  - app/(app)/home.tsx          → POST /auth/register-tenant

All IDs are generated dynamically — no hardcoded UUIDs, slugs, or merchant IDs.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient

from app.core.security import Identity, Principal, get_identity, get_principal
from app.main import app

pytestmark = pytest.mark.integration


# ── helpers ───────────────────────────────────────────────────────────────────

def _unique_slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _identity(workos_id: str | None = None, email: str | None = None) -> Identity:
    return Identity(
        workos_id=workos_id or f"user_{uuid.uuid4().hex[:8]}",
        email=email or f"{uuid.uuid4().hex[:8]}@test.com",
        email_verified=True,
        first_name="Test",
        last_name="User",
    )


def _principal(tenant_id: str, user_id: str, role: str = "owner") -> Principal:
    return Principal(
        tenant_id=tenant_id,
        user_id=user_id,
        workos_id=f"user_{uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@test.com",
        role=role,
    )


async def _register_tenant(client: AsyncClient, slug: str, name: str) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": name},
    )
    assert resp.status_code == 201, f"register-tenant failed: {resp.text}"
    return resp.json()


# ── Fixture: authenticated client with a fresh tenant ─────────────────────────

@pytest.fixture
async def authed(client: AsyncClient):
    """Client with owner identity + a freshly registered tenant."""
    identity = _identity()
    slug = _unique_slug()

    app.dependency_overrides[get_identity] = lambda: identity

    data = await _register_tenant(client, slug, f"Test Restaurant {slug}")
    tenant_id = str(data["tenant"]["id"])
    user_id = str(data["user"]["id"])

    principal = _principal(tenant_id, user_id, role="owner")
    app.dependency_overrides[get_principal] = lambda: principal

    yield {"client": client, "tenant_id": tenant_id, "user_id": user_id, "slug": slug}

    app.dependency_overrides.pop(get_identity, None)
    app.dependency_overrides.pop(get_principal, None)


# ══════════════════════════════════════════════════════════════════════════════
# Contract: GET /api/v1/pos/clover/status
#
# Called by more.tsx and found-summary.tsx with:
#   Authorization: Bearer <token>
#   X-Tenant-Id: <tenant_id>
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_clover_status_shape_when_not_connected(authed):
    """Returns {connected: false} with the expected keys when no connection exists."""
    client: AsyncClient = authed["client"]
    resp = await client.get("/api/v1/pos/clover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "connected" in body
    assert body["connected"] is False


@pytest.mark.asyncio
async def test_clover_status_requires_auth(client: AsyncClient):
    """Returns 4xx (not 500) with no auth — frontend must send the header."""
    resp = await client.get("/api/v1/pos/clover/status")
    assert 400 <= resp.status_code < 500


@pytest.mark.asyncio
async def test_clover_status_after_connection(authed, admin_conn):
    """Returns {connected: true, merchant_id: <id>} once a connection is seeded."""
    from app.core.encryption import TokenEncryption
    from uuid6 import uuid7

    tid = authed["tenant_id"]
    mid = f"merch_{uuid.uuid4().hex[:8]}"
    enc = TokenEncryption()

    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1 hour',
                  $5, now()+interval'30 days', 'active')
        """,
        str(uuid7()), tid, mid,
        enc.encrypt("test_access"), enc.encrypt("test_refresh"),
    )

    client: AsyncClient = authed["client"]
    resp = await client.get("/api/v1/pos/clover/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["merchant_id"] == mid


# ══════════════════════════════════════════════════════════════════════════════
# Contract: GET /api/v1/pos/clover/connect-url
#
# Called by more.tsx and connecting.tsx (owner only).
# Returns {"url": "<clover_oauth_url>"}
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_connect_url_shape(authed):
    """Returns a dict with a 'url' key pointing at Clover OAuth."""
    client: AsyncClient = authed["client"]
    resp = await client.get("/api/v1/pos/clover/connect-url")
    assert resp.status_code == 200
    body = resp.json()
    assert "url" in body
    assert "clover.com" in body["url"] or "clover" in body["url"].lower()
    assert "client_id" in body["url"]
    assert "redirect_uri" in body["url"]
    assert "state" in body["url"]


@pytest.mark.asyncio
async def test_connect_url_url_is_unique_each_call(authed):
    """state= token is different on every call — no reuse."""
    client: AsyncClient = authed["client"]
    r1 = await client.get("/api/v1/pos/clover/connect-url")
    r2 = await client.get("/api/v1/pos/clover/connect-url")
    assert r1.json()["url"] != r2.json()["url"]


@pytest.mark.asyncio
async def test_connect_url_non_owner_forbidden(authed, client: AsyncClient):
    """Manager/staff cannot get a connect URL — frontend must not show the button."""
    identity = _identity()
    tenant_id = authed["tenant_id"]
    user_id = str(uuid.uuid4())
    app.dependency_overrides[get_identity] = lambda: identity
    app.dependency_overrides[get_principal] = lambda: _principal(tenant_id, user_id, role="manager")
    resp = await client.get("/api/v1/pos/clover/connect-url")
    assert resp.status_code == 403
    app.dependency_overrides.pop(get_identity, None)
    app.dependency_overrides.pop(get_principal, None)


@pytest.mark.asyncio
async def test_connect_url_requires_auth(client: AsyncClient):
    """Returns 4xx with no auth."""
    resp = await client.get("/api/v1/pos/clover/connect-url")
    assert 400 <= resp.status_code < 500


# ══════════════════════════════════════════════════════════════════════════════
# Contract: POST /api/v1/auth/register-tenant
#
# Called by home.tsx when me.tenants.length === 0.
# Body: {slug, name}  Returns: {tenant, user, membership}
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_register_tenant_shape(client: AsyncClient):
    """Returns {tenant, user, membership} with correct sub-keys."""
    identity = _identity()
    app.dependency_overrides[get_identity] = lambda: identity
    slug = _unique_slug()
    resp = await client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": "My Restaurant"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "tenant" in body and "id" in body["tenant"]
    assert "user" in body and "id" in body["user"]
    assert "membership" in body and body["membership"]["role"] == "owner"
    app.dependency_overrides.pop(get_identity, None)


@pytest.mark.asyncio
async def test_register_tenant_slug_conflict(client: AsyncClient):
    """Duplicate slug returns 409 — frontend must show an error, not crash."""
    identity = _identity()
    app.dependency_overrides[get_identity] = lambda: identity
    slug = _unique_slug()
    await client.post("/api/v1/auth/register-tenant", json={"slug": slug, "name": "A"})
    resp2 = await client.post("/api/v1/auth/register-tenant", json={"slug": slug, "name": "B"})
    assert resp2.status_code == 409
    app.dependency_overrides.pop(get_identity, None)


@pytest.mark.asyncio
async def test_register_tenant_slug_validation(client: AsyncClient):
    """Invalid slug (spaces, uppercase) returns 422 — caught before hitting DB."""
    identity = _identity()
    app.dependency_overrides[get_identity] = lambda: identity
    resp = await client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": "My Restaurant Slug!", "name": "test"},
    )
    assert resp.status_code == 422
    app.dependency_overrides.pop(get_identity, None)


# ══════════════════════════════════════════════════════════════════════════════
# Contract: GET /api/v1/auth/me
#
# Called by AuthContext.fetchMe on every app load.
# Must return {user, tenants, active_tenant_id}.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_me_shape_new_user(client: AsyncClient):
    """New user gets {user: {...}, tenants: [], active_tenant_id: null}."""
    identity = _identity()
    app.dependency_overrides[get_identity] = lambda: identity
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert "user" in body and "email" in body["user"]
    assert "tenants" in body
    assert isinstance(body["tenants"], list)
    assert body["active_tenant_id"] is None
    app.dependency_overrides.pop(get_identity, None)


@pytest.mark.asyncio
async def test_me_shows_tenant_after_registration(client: AsyncClient):
    """After register-tenant, /me returns the new tenant in tenants[]."""
    identity = _identity()
    app.dependency_overrides[get_identity] = lambda: identity
    slug = _unique_slug()
    await client.post("/api/v1/auth/register-tenant", json={"slug": slug, "name": "Test Biz"})
    resp = await client.get("/api/v1/auth/me")
    body = resp.json()
    slugs = [t["slug"] for t in body["tenants"]]
    assert slug in slugs
    app.dependency_overrides.pop(get_identity, None)


# ══════════════════════════════════════════════════════════════════════════════
# Contract: POST /api/v1/webhooks/pos/clover
#
# Called by Clover (not the frontend), but verified here because the
# frontend test plan expects orders to flow through.
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_webhook_verification_ping(client: AsyncClient):
    """Verification ping echoes the code as text/plain — Clover checks this."""
    code = f"verify_{uuid.uuid4().hex}"
    resp = await client.post(
        "/api/v1/webhooks/pos/clover",
        json={"verificationCode": code},
    )
    assert resp.status_code == 200
    assert resp.text == code
    assert "text/plain" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_webhook_bad_auth_returns_401_not_500(client: AsyncClient):
    """Bad webhook auth is a clean 401, never a 500."""
    resp = await client.post(
        "/api/v1/webhooks/pos/clover",
        json={"merchants": {"mid": [{"objectId": "O:123", "type": "UPDATE", "ts": 0}]}},
        headers={"X-Clover-Auth": "definitely_wrong"},
    )
    assert resp.status_code == 401
