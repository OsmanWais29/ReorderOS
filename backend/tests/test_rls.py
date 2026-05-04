"""RLS isolation tests — require a live Postgres with FORCE RLS enabled.

All queries through ``app_conn`` run as ``app_user`` (non-superuser),
which is subject to FORCE ROW LEVEL SECURITY on tenants, user_tenants,
and invitations.

Asyncpg runs in autocommit mode by default.  We use explicit transaction
blocks so that ``SET LOCAL`` session variables persist across the statements
within a single test.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import seed_membership, seed_tenant, seed_user


@pytest.mark.integration
async def test_force_rls_tables_exist(admin_conn: Any) -> None:
    """Confirm the three tables have FORCE RLS enabled."""
    rows = await admin_conn.fetch(
        "SELECT relname FROM pg_class WHERE relforcerowsecurity = true ORDER BY relname"
    )
    names = {r["relname"] for r in rows}
    assert "tenants" in names
    assert "user_tenants" in names
    assert "invitations" in names


@pytest.mark.integration
async def test_force_rls_blocks_unrestricted_tenant_query(admin_conn: Any, app_conn: Any) -> None:
    """No session vars → app_user sees zero tenant rows."""
    tenant = await seed_tenant(admin_conn)

    async with app_conn.transaction():
        await app_conn.execute("SET LOCAL app.tenant_id = ''")
        await app_conn.execute("SET LOCAL app.user_id = ''")
        await app_conn.execute("SET LOCAL app.rls_mode = ''")
        rows = await app_conn.fetch("SELECT id FROM tenants WHERE id = $1", tenant["id"])

    assert len(rows) == 0

    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])


@pytest.mark.integration
async def test_user_tenants_bootstrap_lists_only_own_memberships(
    admin_conn: Any, app_conn: Any
) -> None:
    """app.user_id set → user sees only their own memberships, not others'."""
    user_a = await seed_user(admin_conn)
    user_b = await seed_user(admin_conn)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user_a["id"]), str(tenant["id"]))
    await seed_membership(admin_conn, str(user_b["id"]), str(tenant["id"]), role="staff")

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.user_id = '{user_a['id']}'")
        await app_conn.execute("SET LOCAL app.tenant_id = ''")
        await app_conn.execute("SET LOCAL app.rls_mode = ''")
        rows = await app_conn.fetch("SELECT user_id FROM user_tenants")

    user_ids = {str(r["user_id"]) for r in rows}
    assert str(user_a["id"]) in user_ids
    assert str(user_b["id"]) not in user_ids

    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id IN ($1, $2)", user_a["id"], user_b["id"])


@pytest.mark.integration
async def test_active_tenant_rls_hides_other_tenant(admin_conn: Any, app_conn: Any) -> None:
    """app.tenant_id = A → tenant B is invisible."""
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a['id']}'")
        await app_conn.execute("SET LOCAL app.rls_mode = ''")
        rows_b = await app_conn.fetch("SELECT id FROM tenants WHERE id = $1", tenant_b["id"])
        rows_a = await app_conn.fetch("SELECT id FROM tenants WHERE id = $1", tenant_a["id"])

    assert len(rows_b) == 0
    assert len(rows_a) == 1

    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
    )


@pytest.mark.integration
async def test_cross_tenant_update_rowcount_zero(admin_conn: Any, app_conn: Any) -> None:
    """UPDATE on a tenant row outside context returns rowcount 0."""
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a['id']}'")
        await app_conn.execute("SET LOCAL app.rls_mode = ''")
        result = await app_conn.execute(
            "UPDATE tenants SET name = 'hacked' WHERE id = $1", tenant_b["id"]
        )

    assert result == "UPDATE 0"

    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
    )


@pytest.mark.integration
async def test_register_tenant_rls_with_check_passes(admin_conn: Any, app_conn: Any) -> None:
    """rls_mode=register allows inserting a new tenant."""
    new_id = uuid.uuid4()
    slug = f"rls-reg-{new_id.hex[:8]}"

    async with app_conn.transaction():
        await app_conn.execute("SET LOCAL app.tenant_id = ''")
        await app_conn.execute("SET LOCAL app.rls_mode = 'register'")
        await app_conn.execute(
            "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, $3)",
            new_id,
            slug,
            "RLS Reg Test",
        )

    row = await admin_conn.fetchrow("SELECT id FROM tenants WHERE id = $1", new_id)
    assert row is not None

    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", new_id)


@pytest.mark.integration
async def test_invite_accept_bootstrap_policy_finds_only_matching_invite(
    admin_conn: Any, app_conn: Any
) -> None:
    """accept_invite mode + invite_email → only that invite is visible."""
    import secrets

    user = await seed_user(admin_conn)
    tenant = await seed_tenant(admin_conn)
    await seed_membership(admin_conn, str(user["id"]), str(tenant["id"]))

    token_a = secrets.token_hex(32)
    token_b = secrets.token_hex(32)
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'alice@example.com', 'staff', $2)",
        tenant["id"],
        token_a,
    )
    await admin_conn.execute(
        "INSERT INTO invitations (tenant_id, email, role, token)"
        " VALUES ($1, 'bob@example.com', 'staff', $2)",
        tenant["id"],
        token_b,
    )

    async with app_conn.transaction():
        await app_conn.execute("SET LOCAL app.tenant_id = ''")
        await app_conn.execute("SET LOCAL app.rls_mode = 'accept_invite'")
        await app_conn.execute("SET LOCAL app.invite_email = 'alice@example.com'")
        rows = await app_conn.fetch("SELECT email FROM invitations")

    emails = [r["email"] for r in rows]
    assert "alice@example.com" in emails
    assert "bob@example.com" not in emails

    await admin_conn.execute("DELETE FROM invitations WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM user_tenants WHERE tenant_id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM tenants WHERE id = $1", tenant["id"])
    await admin_conn.execute("DELETE FROM users WHERE id = $1", user["id"])
