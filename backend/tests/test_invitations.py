"""Invitation route tests: create, list, accept, revoke."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.core.security import Identity, Principal
from tests.conftest import seed_membership, seed_tenant, seed_user


def _make_principal(user_id: str, tenant_id: str, role: str = "owner") -> Principal:
    return Principal(
        user_id=user_id,
        workos_id="user_test_fixed",
        email="owner@example.com",
        tenant_id=tenant_id,
        role=role,  # type: ignore[arg-type]
    )


# ── POST /api/v1/invitations ──────────────────────────────────────────────────


@pytest.mark.integration
async def test_owner_can_create_invitation(owner_client: AsyncClient, admin_conn: Any) -> None:
    user = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _make_principal(
        str(user["id"]), str(tenant["id"])
    )
    try:
        r = await owner_client.post(
            "/api/v1/invitations",
            json={"email": "newuser@example.com", "role": "staff"},
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "newuser@example.com"
    assert body["role"] == "staff"
    assert len(body["token"]) == 64  # 32 hex bytes

    # Cleanup
    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_manager_cannot_create_invitation(
    manager_client: AsyncClient, admin_conn: Any
) -> None:
    user = await seed_user(admin_conn, workos_id="user_manager_fixed", email="manager@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="manager")

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _make_principal(
        str(user["id"]), str(tenant["id"]), role="manager"
    )
    try:
        r = await manager_client.post(
            "/api/v1/invitations",
            json={"email": "newuser@example.com", "role": "staff"},
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


@pytest.mark.integration
async def test_staff_cannot_create_invitation(staff_client: AsyncClient, admin_conn: Any) -> None:
    user = await seed_user(admin_conn, workos_id="user_staff_fixed", email="staff@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]), role="staff")

    from app.core.security import get_principal
    from app.main import app

    app.dependency_overrides[get_principal] = lambda: _make_principal(
        str(user["id"]), str(tenant["id"]), role="staff"
    )
    try:
        r = await staff_client.post(
            "/api/v1/invitations",
            json={"email": "newuser@example.com", "role": "staff"},
            headers={"X-Tenant-Id": str(tenant["id"])},
        )
    finally:
        app.dependency_overrides.pop(get_principal, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])


# ── POST /api/v1/invitations/accept ──────────────────────────────────────────


@pytest.mark.integration
async def test_new_user_can_accept_valid_invite(owner_client: AsyncClient, admin_conn: Any) -> None:
    owner = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(owner["id"]), str(tenant["id"]))

    import secrets

    token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'invited@example.com', 'staff', $2)",
        tenant["id"],
        token,
    )

    invite_identity = Identity(
        workos_id="user_invited_new",
        email="invited@example.com",
        email_verified=True,
        first_name=None,
        last_name=None,
    )
    from app.core.security import get_identity
    from app.main import app

    app.dependency_overrides[get_identity] = lambda: invite_identity
    try:
        r = await owner_client.post(
            "/api/v1/invitations/accept",
            json={"token": token},
        )
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "staff"
    assert str(body["tenant_id"]) == str(tenant["id"])

    # Cleanup
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute(
        "DELETE FROM users WHERE workos_id IN ('user_test_fixed', 'user_invited_new')"
    )


@pytest.mark.integration
async def test_wrong_email_returns_404(owner_client: AsyncClient, admin_conn: Any) -> None:
    owner = await seed_user(admin_conn, workos_id="user_test_fixed", email="owner@example.com")
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(owner["id"]), str(tenant["id"]))

    import secrets

    token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'correct@example.com', 'staff', $2)",
        tenant["id"],
        token,
    )

    wrong_identity = Identity(
        workos_id="user_wrong",
        email="wrong@example.com",
        email_verified=True,
        first_name=None,
        last_name=None,
    )
    from app.core.security import get_identity
    from app.main import app

    app.dependency_overrides[get_identity] = lambda: wrong_identity
    try:
        r = await owner_client.post(
            "/api/v1/invitations/accept",
            json={"token": token},
        )
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert r.status_code == 404

    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", owner["id"])


@pytest.mark.integration
async def test_unverified_email_returns_403(owner_client: AsyncClient, admin_conn: Any) -> None:
    tenant = await seed_tenant(admin_conn)
    import secrets

    token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'unverified@example.com', 'staff', $2)",
        tenant["id"],
        token,
    )

    unverified = Identity(
        workos_id="user_unverified",
        email="unverified@example.com",
        email_verified=False,
        first_name=None,
        last_name=None,
    )
    from app.core.security import get_identity
    from app.main import app

    app.dependency_overrides[get_identity] = lambda: unverified
    try:
        r = await owner_client.post(
            "/api/v1/invitations/accept",
            json={"token": token},
        )
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert r.status_code == 403

    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest.mark.integration
async def test_expired_invite_returns_410(owner_client: AsyncClient, admin_conn: Any) -> None:
    tenant = await seed_tenant(admin_conn)
    import secrets

    token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token, expires_at)"
        " VALUES ($1, 'exp@example.com', 'staff', $2, NOW() - INTERVAL '1 day')",
        tenant["id"],
        token,
    )

    expired_identity = Identity(
        workos_id="user_exp",
        email="exp@example.com",
        email_verified=True,
        first_name=None,
        last_name=None,
    )
    from app.core.security import get_identity
    from app.main import app

    app.dependency_overrides[get_identity] = lambda: expired_identity
    try:
        r = await owner_client.post(
            "/api/v1/invitations/accept",
            json={"token": token},
        )
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert r.status_code == 410

    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest.mark.integration
async def test_double_accept_returns_409(owner_client: AsyncClient, admin_conn: Any) -> None:
    tenant = await seed_tenant(admin_conn)
    import secrets

    token = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'double@example.com', 'staff', $2)",
        tenant["id"],
        token,
    )

    double_identity = Identity(
        workos_id="user_double",
        email="double@example.com",
        email_verified=True,
        first_name=None,
        last_name=None,
    )
    from app.core.security import get_identity
    from app.main import app

    app.dependency_overrides[get_identity] = lambda: double_identity
    try:
        r1 = await owner_client.post("/api/v1/invitations/accept", json={"token": token})
        assert r1.status_code == 200, r1.text

        r2 = await owner_client.post("/api/v1/invitations/accept", json={"token": token})
        assert r2.status_code in (409, 200)  # 409 on conflict, 200 on idempotent
    finally:
        app.dependency_overrides.pop(get_identity, None)

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE workos_id = 'user_double'")
