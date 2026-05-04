"""RBAC tests: owner-only routes reject manager/staff; any-role routes allow all."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.security import Principal
from tests.conftest import seed_membership, seed_tenant, seed_user


def _principal(user_id: str, tenant_id: str, role: str) -> Principal:
    return Principal(
        user_id=user_id,
        workos_id=f"user_{role}_fixed",
        email=f"{role}@example.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


# ── Invitation RBAC (owner-only) ──────────────────────────────────────────────


@pytest.mark.integration
async def test_owner_can_list_invitations(owner_client: AsyncClient, admin_conn: Any) -> None:
    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "owner"
    )
    try:
        r = await owner_client.get(
            "/api/v1/invitations",
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 200

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_manager_cannot_list_invitations(
    manager_client: AsyncClient, admin_conn: Any
) -> None:
    user = await seed_user(admin_conn, workos_id="user_manager_fixed", email="manager@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="manager")

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "manager"
    )
    try:
        r = await manager_client.get(
            "/api/v1/invitations",
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_staff_cannot_list_invitations(staff_client: AsyncClient, admin_conn: Any) -> None:
    user = await seed_user(admin_conn, workos_id="user_staff_fixed", email="staff@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="staff")

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "staff"
    )
    try:
        r = await staff_client.get(
            "/api/v1/invitations",
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_owner_can_revoke_invitation(owner_client: AsyncClient, admin_conn: Any) -> None:
    import secrets

    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    token = secrets.token_hex(32)
    inv = await admin_conn.fetchrow(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'todel@example.com', 'staff', $2) RETURNING id",
        tenant["id"],
        token,
    )

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "owner"
    )
    try:
        r = await owner_client.delete(
            f"/api/v1/invitations/{inv['id']}",
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 204

    row = await admin_conn.fetchrow("SELECT id FROM invitations WHERE id = $1", inv["id"])
    assert row is None

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_manager_cannot_revoke_invitation(
    manager_client: AsyncClient, admin_conn: Any
) -> None:
    import secrets

    user = await seed_user(admin_conn, workos_id="user_manager_fixed", email="manager@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="manager")

    token = secrets.token_hex(32)
    inv = await admin_conn.fetchrow(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'nodelete@example.com', 'staff', $2) RETURNING id",
        tenant["id"],
        token,
    )

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "manager"
    )
    try:
        r = await manager_client.delete(
            f"/api/v1/invitations/{inv['id']}",
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


# ── Any-role routes ───────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_any_role_can_call_active_tenant(staff_client: AsyncClient, admin_conn: Any) -> None:
    user = await seed_user(admin_conn, workos_id="user_staff_fixed", email="staff@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="staff")

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _principal(
        str(user["id"]), str(tenant["id"]), "staff"
    )
    try:
        r = await staff_client.patch(
            "/api/v1/auth/active-tenant",
            json={"tenant_id": str(tenant["id"])},
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 200
    assert r.json()["role"] == "staff"

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])
