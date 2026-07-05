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
    """Confirm the original Sprint-2 tables have FORCE RLS enabled."""
    rows = await admin_conn.fetch(
        "SELECT relname FROM pg_class WHERE relforcerowsecurity = true ORDER BY relname"
    )
    names = {r["relname"] for r in rows}
    assert "tenants" in names
    assert "user_tenants" in names
    assert "invitations" in names


# Tables that intentionally run ENABLE-only (no FORCE): the webhook/inbox worker
# must see ALL tenants' rows before resolving merchant→tenant, so these carry
# USING(true) cross-tenant policies by design. Everything else with a tenant_id
# and RLS enabled MUST be FORCE'd, or the table-owning request-path role bypasses
# its tenant-isolation policy (see 0022 and test_rls.py module docstring).
_ENABLE_ONLY_BY_DESIGN = {"pos_event_inbox", "tenant_pos_connections"}


@pytest.mark.integration
async def test_every_tenant_table_is_force_rls(admin_conn: Any) -> None:
    """Lint guard (RLS Option-3): every RLS-enabled tenant_id table must have FORCE,
    except the documented USING(true) inbox tables. A new table that enables RLS but
    forgets FORCE fails here — its owner-role queries would silently bypass RLS."""
    rows = await admin_conn.fetch(
        """
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind = 'r'
          AND c.relrowsecurity            -- RLS enabled
          AND NOT c.relforcerowsecurity   -- but NOT forced
          AND EXISTS (
              SELECT 1 FROM information_schema.columns col
              WHERE col.table_schema = 'public'
                AND col.table_name = c.relname
                AND col.column_name = 'tenant_id'
          )
        ORDER BY c.relname
        """
    )
    unforced = {r["relname"] for r in rows} - _ENABLE_ONLY_BY_DESIGN
    assert not unforced, (
        f"tenant tables with RLS enabled but FORCE missing: {sorted(unforced)} — "
        "add FORCE ROW LEVEL SECURITY (see migration 0022) or, if cross-tenant by "
        "design, add to _ENABLE_ONLY_BY_DESIGN with justification."
    )


@pytest.mark.integration
async def test_menu_items_cross_tenant_isolation(admin_conn: Any, app_conn: Any) -> None:
    """Prove the menu_items tenant policy actually isolates under the RLS-subject role
    (the full suite runs as superuser ``reorderos``, which bypasses RLS, so the policy
    is otherwise never exercised). app_conn runs as ``app_user`` — a non-owner,
    non-superuser role, hence subject to RLS via ENABLE alone.

    NOTE on what this does and does NOT prove: app_user is a non-owner, so this passes
    with or without FORCE — it confirms the POLICY is correct and not over-blocking, not
    that FORCE is load-bearing. FORCE only changes behavior for a query running AS the
    table owner; whether that occurs depends on the production connection role (see
    migration 0022 and STATUS.md). This is the policy-correctness half of the guarantee;
    the FORCE-presence half is test_every_tenant_table_is_force_rls."""
    tenant_a = await seed_tenant(admin_conn)
    tenant_b = await seed_tenant(admin_conn)
    # Seed a menu item for tenant B as superuser (bypasses RLS to set up the attack).
    item_b = await admin_conn.fetchval(
        "INSERT INTO menu_items (tenant_id, name) VALUES ($1, $2) RETURNING id",
        tenant_b["id"],
        "Tenant B Burger",
    )
    item_a = await admin_conn.fetchval(
        "INSERT INTO menu_items (tenant_id, name) VALUES ($1, $2) RETURNING id",
        tenant_a["id"],
        "Tenant A Latte",
    )

    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{tenant_a['id']}'")
        await app_conn.execute("SET LOCAL app.rls_mode = ''")
        # Tenant A context must NOT see tenant B's item, MUST see its own.
        sees_b = await app_conn.fetch("SELECT id FROM menu_items WHERE id = $1", item_b)
        sees_a = await app_conn.fetch("SELECT id FROM menu_items WHERE id = $1", item_a)
        # A cross-tenant write must affect zero rows.
        upd = await app_conn.execute("UPDATE menu_items SET name = 'hacked' WHERE id = $1", item_b)

    assert len(sees_b) == 0, "FORCE/policy failed: app_user read another tenant's menu_items"
    assert len(sees_a) == 1, "policy over-blocked: app_user cannot see its own menu_items"
    assert upd == "UPDATE 0", "FORCE/policy failed: app_user updated another tenant's row"

    await admin_conn.execute("DELETE FROM menu_items WHERE id IN ($1, $2)", item_a, item_b)
    await admin_conn.execute(
        "DELETE FROM tenants WHERE id IN ($1, $2)", tenant_a["id"], tenant_b["id"]
    )


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
