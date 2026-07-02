"""Bootstrap route tests: register-tenant, /me, /active-tenant."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from tests.conftest import seed_membership, seed_tenant, seed_user

# ── /api/v1/auth/register-tenant ─────────────────────────────────────────────


@pytest.mark.integration
async def test_register_tenant_creates_owner(owner_client: AsyncClient, admin_conn: Any) -> None:
    """Fresh identity creates a new tenant and becomes owner."""
    import uuid

    slug = f"regtest-{uuid.uuid4().hex[:8]}"
    r = await owner_client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": "Registration Test"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["tenant"]["slug"] == slug
    assert body["membership"]["role"] == "owner"

    tenant_id = body["tenant"]["id"]
    row = await admin_conn.fetchrow("SELECT id FROM tenants WHERE id = $1", tenant_id)
    assert row is not None

    # Cleanup
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant_id)
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_test_fixed'")


@pytest.mark.integration
async def test_register_tenant_duplicate_slug_returns_409(
    owner_client: AsyncClient, admin_conn: Any
) -> None:
    """Duplicate slug returns 409, not 500."""
    import uuid

    slug = f"dup-{uuid.uuid4().hex[:8]}"

    r1 = await owner_client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": "First"},
    )
    assert r1.status_code == 201

    r2 = await owner_client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": "Second"},
    )
    assert r2.status_code == 409

    # Cleanup
    tenant_id = r1.json()["tenant"]["id"]
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant_id)
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant_id)
    await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_test_fixed'")


@pytest.mark.integration
async def test_register_tenant_invalid_slug_returns_422(
    owner_client: AsyncClient,
) -> None:
    r = await owner_client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": "UPPER_CASE!", "name": "Bad"},
    )
    assert r.status_code == 422


# ── /api/v1/auth/me ───────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_me_returns_user_and_tenants(owner_client: AsyncClient, admin_conn: Any) -> None:
    """After registration, /me returns the user with their tenant list."""
    import uuid

    slug = f"me-test-{uuid.uuid4().hex[:8]}"
    await owner_client.post(
        "/api/v1/auth/register-tenant",
        json={"slug": slug, "name": "Me Test"},
    )

    r = await owner_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user"]["email"] == "owner@example.com"
    assert any(t["slug"] == slug for t in body["tenants"])

    # Cleanup
    await admin_conn.execute(
        "DELETE FROM user_tenants WHERE user_id IN "
        "(SELECT id FROM users WHERE workos_id = 'user_test_fixed')"
    )
    await admin_conn.execute(f"DELETE FROM tenants WHERE slug = '{slug}'")
    await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_test_fixed'")


@pytest.mark.integration
async def test_me_upserts_user_on_first_login(owner_client: AsyncClient, admin_conn: Any) -> None:
    """First call to /me auto-creates the user from JWT claims and returns 200."""
    r = await owner_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["workos_id"] == "user_test_fixed"
    # Clean up the committed row so subsequent tests can seed the same workos_id.
    await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_test_fixed'")


# ── /api/v1/auth/active-tenant ────────────────────────────────────────────────


@pytest.mark.integration
async def test_active_tenant_returns_role(owner_client: AsyncClient, admin_conn: Any) -> None:
    """PATCH /active-tenant resolves membership and returns role."""
    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    r = await owner_client.patch(
        "/api/v1/auth/active-tenant",
        json={"tenant_id": str(tenant["id"])},
        headers={"X-Tenant-Id": str(tenant["id"])},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "owner"

    # Cleanup
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_active_tenant_missing_header_returns_400(
    owner_client: AsyncClient,
) -> None:
    r = await owner_client.patch(
        "/api/v1/auth/active-tenant",
        json={"tenant_id": "00000000-0000-0000-0000-000000000001"},
    )
    assert r.status_code == 400


@pytest.mark.integration
async def test_active_tenant_wrong_tenant_returns_403(
    owner_client: AsyncClient, admin_conn: Any
) -> None:
    """Membership doesn't exist → 403."""
    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    other_tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    r = await owner_client.patch(
        "/api/v1/auth/active-tenant",
        json={"tenant_id": str(other_tenant["id"])},
        headers={"X-Tenant-Id": str(other_tenant["id"])},
    )
    assert r.status_code == 403

    # Cleanup
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant["id"], other_tenant["id"]
    )
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_get_identity_works_with_no_tenant(
    owner_client: AsyncClient, admin_conn: Any
) -> None:
    """GET /auth/me (identity-only, no tenant) works without X-Tenant-Id."""
    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")

    r = await owner_client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["workos_id"] == "user_test_fixed"

    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_register_tenant_rolls_back_on_membership_failure(
    owner_client: AsyncClient, admin_conn: Any, monkeypatch: Any
) -> None:
    """F2.6 atomicity — the dangerous leg: if the owner-membership INSERT fails AFTER the
    tenant INSERT, NEITHER row may persist (no orphan tenant without an owner).

    The duplicate-slug test only fails at the FIRST write; this injects a failure BETWEEN
    the two writes by making repo.UserTenant raise IntegrityError after tenant.flush(),
    exercising the route's real `except IntegrityError -> rollback` path. A surviving tenant
    row means atomicity is broken.
    """
    import uuid

    from sqlalchemy.exc import IntegrityError

    import app.modules.tenants.repo as repo_mod

    slug = f"atomic-{uuid.uuid4().hex[:8]}"

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise IntegrityError(
            "INSERT INTO user_tenants", {}, Exception("injected membership failure")
        )

    monkeypatch.setattr(repo_mod, "UserTenant", _boom)

    try:
        r = await owner_client.post(
            "/api/v1/auth/register-tenant",
            json={"slug": slug, "name": "Atomicity Test"},
        )
        assert r.status_code == 409, (
            f"expected 409 from rollback path, got {r.status_code}: {r.text}"
        )

        orphan = await admin_conn.fetchval("SELECT count(*) FROM tenants WHERE slug = $1", slug)
        assert orphan == 0, (
            "orphan tenant persisted after membership-insert failure (F2.6 atomicity broken)"
        )
    finally:
        await admin_conn.execute(
            "DELETE FROM user_tenants WHERE tenant_id IN (SELECT id FROM tenants WHERE slug = $1)",
            slug,
        )
        await admin_conn.execute("DELETE FROM tenants WHERE slug = $1", slug)
        await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_test_fixed'")
