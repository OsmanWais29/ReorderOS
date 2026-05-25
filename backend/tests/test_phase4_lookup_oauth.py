"""Sprint 4 — Phase 4: lookup_tenant_by_merchant + oauth_states (22 test functions).

Components under test:
  lookup_tenant_by_merchant: SECURITY DEFINER, STABLE, search_path=public,
                              returns uuid, EXECUTE only for app_user + service_worker
  oauth_states: PK on state, NOT NULL on tenant_id/user_id/expires_at,
                no RLS, SELECT/INSERT/DELETE (no UPDATE) for both roles
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from uuid6 import uuid7

from app.core.encryption import TokenEncryption
from tests.helpers.oauth import make_oauth_state_row

enc = TokenEncryption()


# ── Test 1: Function Exists and Returns uuid ──────────────────────────────────


@pytest.mark.asyncio
async def test_function_exists_and_returns_uuid(admin_conn):
    row = await admin_conn.fetchrow("""
        SELECT proname, prorettype::regtype::text AS return_type
        FROM pg_proc WHERE proname = 'lookup_tenant_by_merchant'
    """)
    assert row is not None, "Function lookup_tenant_by_merchant does not exist"
    assert row["return_type"] == "uuid"


# ── Test 2: Resolves Active Connection ────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_finds_active_merchant(admin_conn, service_conn):
    """service_worker calls the function without SET LOCAL.

    SECURITY DEFINER bypasses RLS on tenant_pos_connections.
    """
    tid = str(uuid.uuid4())
    mid = f"merch_{uuid.uuid4().hex[:8]}"
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        "LookupActive",
        f"lookup-active-{tid[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1h', $5, now()+interval'8h', 'active')
    """,
        str(uuid7()),
        tid,
        mid,
        enc.encrypt("tok"),
        enc.encrypt("ref"),
    )

    result = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert str(result) == tid


# ── Test 3: Returns NULL for Unknown Merchant ─────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_returns_null_for_unknown(service_conn):
    result = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', 'nonexistent_merchant_xyz')",
    )
    assert result is None


# ── Test 4: Ignores Revoked Connections ──────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_ignores_revoked(admin_conn, service_conn):
    tid = str(uuid.uuid4())
    mid = f"merch_rev_{uuid.uuid4().hex[:8]}"
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        "LookupRevoked",
        f"lookup-revoked-{tid[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1h', $5, now()+interval'8h', 'revoked')
    """,
        str(uuid7()),
        tid,
        mid,
        enc.encrypt("tok"),
        enc.encrypt("ref"),
    )

    result = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert result is None


# ── Test 5: Resolves Error-State Connections ──────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_finds_error_state(admin_conn, service_conn):
    """state IN ('active', 'error') — error connections must resolve.

    A connection in error has failed token refresh but the merchant still
    sends webhooks. Losing those events violates the sprint goal.
    """
    tid = str(uuid.uuid4())
    mid = f"merch_err_{uuid.uuid4().hex[:8]}"
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        "LookupError",
        f"lookup-error-{tid[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1h', $5, now()+interval'8h', 'error')
    """,
        str(uuid7()),
        tid,
        mid,
        enc.encrypt("tok"),
        enc.encrypt("ref"),
    )

    result = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert str(result) == tid


# ── Test 6: SECURITY DEFINER and STABLE ──────────────────────────────────────


@pytest.mark.asyncio
async def test_function_security_attributes(admin_conn):
    row = await admin_conn.fetchrow("""
        SELECT prosecdef, provolatile::text AS volatile
        FROM pg_proc WHERE proname = 'lookup_tenant_by_merchant'
    """)
    assert row["prosecdef"] is True, "Must be SECURITY DEFINER"
    assert row["volatile"] == "s", "Must be STABLE (not volatile)"


# ── Test 7: search_path Pinned to public ──────────────────────────────────────


@pytest.mark.asyncio
async def test_function_search_path_pinned(admin_conn):
    """Without search_path=public, a malicious schema could shadow
    tenant_pos_connections and intercept merchant lookups.
    """
    row = await admin_conn.fetchrow("""
        SELECT proconfig FROM pg_proc
        WHERE proname = 'lookup_tenant_by_merchant'
    """)
    assert row["proconfig"] is not None, "No SET clause on function"
    assert any("search_path=public" in item for item in row["proconfig"])


# ── Test 8: EXECUTE Grants — Only app_user and service_worker ─────────────────


@pytest.mark.asyncio
async def test_function_execute_grants(admin_conn):
    grants = await admin_conn.fetch("""
        SELECT grantee FROM information_schema.role_routine_grants
        WHERE routine_name = 'lookup_tenant_by_merchant'
          AND privilege_type = 'EXECUTE'
    """)
    grantees = {row["grantee"] for row in grants}
    assert "PUBLIC" not in grantees, "EXECUTE must be revoked from PUBLIC"
    assert "app_user" in grantees
    assert "service_worker" in grantees


# ── Test 9: app_user Can Call Lookup Without SET LOCAL ────────────────────────


@pytest.mark.asyncio
async def test_app_user_can_call_lookup(admin_conn, app_conn):
    """app_user calls lookup_tenant_by_merchant without SET LOCAL — must work."""
    tid = str(uuid.uuid4())
    mid = f"merch_app_{uuid.uuid4().hex[:8]}"
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        "LookupApp",
        f"lookup-app-{tid[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1h', $5, now()+interval'8h', 'active')
    """,
        str(uuid7()),
        tid,
        mid,
        enc.encrypt("tok"),
        enc.encrypt("ref"),
    )

    result = await app_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert str(result) == tid


# ── Test 10: oauth_states Table Exists ───────────────────────────────────────


@pytest.mark.asyncio
async def test_oauth_states_table_exists(admin_conn):
    exists = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'oauth_states'
        )
    """)
    assert exists is True


# ── Test 11: oauth_states Has No RLS ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_oauth_states_no_rls(admin_conn):
    """oauth_states is ephemeral — state token is the security mechanism, not RLS."""
    rls = await admin_conn.fetchval(
        "SELECT relrowsecurity FROM pg_class WHERE relname = 'oauth_states'",
    )
    assert rls is False


# ── Test 12: app_user CRUD on oauth_states ────────────────────────────────────


@pytest.mark.asyncio
async def test_app_user_oauth_crud(admin_conn, app_conn):
    """app_user can INSERT, SELECT, DELETE on oauth_states. Cannot UPDATE."""
    row = make_oauth_state_row()

    # INSERT
    await app_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        row["state"],
        row["tenant_id"],
        row["user_id"],
        row["expires_at"],
    )

    # SELECT
    found = await app_conn.fetchval(
        "SELECT tenant_id FROM oauth_states WHERE state = $1",
        row["state"],
    )
    assert str(found) == row["tenant_id"]

    # UPDATE must fail while the row still exists
    with pytest.raises(Exception) as exc:
        await app_conn.execute(
            "UPDATE oauth_states SET expires_at = now() WHERE state = $1",
            row["state"],
        )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()

    # DELETE (consume the state)
    await app_conn.execute(
        "DELETE FROM oauth_states WHERE state = $1",
        row["state"],
    )
    count = await app_conn.fetchval(
        "SELECT count(*) FROM oauth_states WHERE state = $1",
        row["state"],
    )
    assert count == 0


# ── Test 13: service_worker CRUD on oauth_states ──────────────────────────────


@pytest.mark.asyncio
async def test_service_worker_oauth_crud(admin_conn, service_conn):
    """service_worker has the same grants: INSERT, SELECT, DELETE. No UPDATE."""
    row = make_oauth_state_row()

    # INSERT
    await service_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        row["state"],
        row["tenant_id"],
        row["user_id"],
        row["expires_at"],
    )

    # SELECT
    found = await service_conn.fetchval(
        "SELECT tenant_id FROM oauth_states WHERE state = $1",
        row["state"],
    )
    assert str(found) == row["tenant_id"]

    # UPDATE denied
    with pytest.raises(Exception) as exc:
        await service_conn.execute(
            "UPDATE oauth_states SET expires_at = now() WHERE state = $1",
            row["state"],
        )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()

    # DELETE
    await service_conn.execute(
        "DELETE FROM oauth_states WHERE state = $1",
        row["state"],
    )


# ── Test 14: PK Uniqueness on oauth_states ────────────────────────────────────


@pytest.mark.asyncio
async def test_oauth_state_pk_uniqueness(admin_conn, app_conn):
    row = make_oauth_state_row()
    await app_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        row["state"],
        row["tenant_id"],
        row["user_id"],
        row["expires_at"],
    )

    with pytest.raises(Exception) as exc:
        await app_conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
            VALUES ($1, $2, $3, $4)
        """,
            row["state"],
            str(uuid.uuid4()),
            str(uuid.uuid4()),
            row["expires_at"],
        )
    assert "unique" in str(exc.value).lower() or "duplicate" in str(exc.value).lower()


# ── Test 15: NOT NULL Constraints on oauth_states ─────────────────────────────


@pytest.mark.asyncio
async def test_oauth_state_not_null_expires_at(app_conn):
    row = make_oauth_state_row()
    with pytest.raises(Exception):
        await app_conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
            VALUES ($1, $2, $3, NULL)
        """,
            row["state"],
            row["tenant_id"],
            row["user_id"],
        )


@pytest.mark.asyncio
async def test_oauth_state_not_null_tenant_id(app_conn):
    row = make_oauth_state_row()
    with pytest.raises(Exception):
        await app_conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
            VALUES ($1, NULL, $2, $3)
        """,
            row["state"],
            row["user_id"],
            row["expires_at"],
        )


@pytest.mark.asyncio
async def test_oauth_state_not_null_user_id(app_conn):
    row = make_oauth_state_row()
    with pytest.raises(Exception):
        await app_conn.execute(
            """
            INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
            VALUES ($1, $2, NULL, $3)
        """,
            row["state"],
            row["tenant_id"],
            row["expires_at"],
        )


# ── Test 16: service_worker Cannot Access inventory_movements ─────────────────


@pytest.mark.asyncio
async def test_service_worker_no_inventory_access(service_conn):
    """service_worker must not access inventory_movements until Sprint 5 adds grants.

    If this fails, someone prematurely granted access and the worker could
    accidentally deplete inventory.
    """
    with pytest.raises(Exception) as exc:
        await service_conn.execute(
            "SELECT 1 FROM inventory_movements LIMIT 1",
        )
    assert "permission denied" in str(exc.value).lower() or "insufficient" in str(exc.value).lower()


# ── Test 17: Two Connection Pools Work Simultaneously ────────────────────────


@pytest.mark.asyncio
async def test_two_pools_work(app_conn, service_conn):
    """Both connections are usable in the same test without interference."""
    count_app = await app_conn.fetchval("SELECT count(*) FROM oauth_states")
    assert count_app is not None

    count_svc = await service_conn.fetchval("SELECT count(*) FROM pos_event_inbox")
    assert count_svc is not None

    role_app = await app_conn.fetchval("SELECT current_user")
    role_svc = await service_conn.fetchval("SELECT current_user")
    assert role_app != role_svc
    assert role_app == "app_user"
    assert role_svc == "service_worker"


# ── Test 18: Revoked EXECUTE Blocks Lookup Call ───────────────────────────────


@pytest.mark.asyncio
async def test_revoked_execute_blocks_call(admin_conn, service_conn):
    """Temporarily revoke EXECUTE from service_worker, prove the call fails,
    then restore. Validates enforcement, not just the catalog row.
    """
    await admin_conn.execute(
        "REVOKE EXECUTE ON FUNCTION lookup_tenant_by_merchant(text, text) FROM service_worker"
    )
    try:
        with pytest.raises(Exception) as exc:
            await service_conn.fetchval("SELECT lookup_tenant_by_merchant('clover', 'x')")
        assert (
            "permission denied" in str(exc.value).lower()
            or "insufficient" in str(exc.value).lower()
        )
    finally:
        await admin_conn.execute(
            "GRANT EXECUTE ON FUNCTION lookup_tenant_by_merchant(text, text) TO service_worker"
        )


# ── Test 19: Full Lifecycle — OAuth → Lookup → Order ─────────────────────────


@pytest.mark.asyncio
async def test_full_phase4_lifecycle(admin_conn, app_conn, service_conn):
    """End-to-end: OAuth state creation → consumption → merchant lookup
    → inbox insert → order creation.
    """
    # 1. SEED
    tid = str(uuid.uuid4())
    mid = f"lifecycle_{uuid.uuid4().hex[:8]}"
    uid = str(uuid.uuid4())
    await admin_conn.execute(
        "INSERT INTO tenants (id, name, slug) VALUES ($1, $2, $3)",
        tid,
        "Phase4Life",
        f"phase4-life-{tid[:8]}",
    )
    await admin_conn.execute(
        """
        INSERT INTO tenant_pos_connections (
            connection_id, tenant_id, vendor, merchant_id, environment,
            access_token_enc, access_token_expires_at,
            refresh_token_enc, refresh_token_expires_at, state
        ) VALUES ($1,$2,'clover',$3,'sandbox',
                  $4, now()+interval'1h', $5, now()+interval'8h', 'active')
    """,
        str(uuid7()),
        tid,
        mid,
        enc.encrypt("real_access_token"),
        enc.encrypt("real_refresh_token"),
    )

    # 2. OAUTH INITIATION (app_user creates state)
    state_token = str(uuid.uuid4())
    await app_conn.execute(
        """
        INSERT INTO oauth_states (state, tenant_id, user_id, expires_at)
        VALUES ($1, $2, $3, $4)
    """,
        state_token,
        tid,
        uid,
        datetime.now(UTC) + timedelta(minutes=10),
    )

    # 3. OAUTH CALLBACK (service_worker consumes state)
    stored = await service_conn.fetchrow(
        "SELECT tenant_id, user_id FROM oauth_states WHERE state = $1",
        state_token,
    )
    assert str(stored["tenant_id"]) == tid
    assert str(stored["user_id"]) == uid
    await service_conn.execute(
        "DELETE FROM oauth_states WHERE state = $1",
        state_token,
    )
    count = await service_conn.fetchval(
        "SELECT count(*) FROM oauth_states WHERE state = $1",
        state_token,
    )
    assert count == 0

    # 4. WEBHOOK ARRIVES — resolve merchant → tenant
    resolved = await service_conn.fetchval(
        "SELECT lookup_tenant_by_merchant('clover', $1)",
        mid,
    )
    assert str(resolved) == tid

    # 5. INBOX EVENT (service_worker, USING(true) on inbox)
    inbox_id = str(uuid7())
    await service_conn.execute(
        """
        INSERT INTO pos_event_inbox (
            inbox_id, tenant_id, vendor, vendor_event_id,
            vendor_object_type, vendor_event_type, vendor_ts,
            raw_payload, signature_verified, source
        ) VALUES ($1,$2,'clover',$3,'O','CREATE',$4,'{}',true,'webhook')
    """,
        inbox_id,
        str(resolved),
        f"order_{uuid.uuid4().hex[:8]}",
        int(time.time() * 1000),
    )

    # 6. ORDER CREATION (service_worker with SET LOCAL)
    order_id = str(uuid7())
    async with service_conn.transaction():
        await service_conn.execute(f"SET LOCAL app.tenant_id = '{resolved}'")
        await service_conn.execute(
            """
            INSERT INTO orders (
                id, tenant_id, pos_event_inbox_id, clover_order_id,
                total_amount_cents, state, payment_state, processed_at
            ) VALUES ($1,$2,$3,$4,4200,'locked','PAID',now())
        """,
            order_id,
            str(resolved),
            inbox_id,
            f"CO-{uuid.uuid4().hex[:8]}",
        )

    # 7. VERIFY
    order = await admin_conn.fetchrow(
        "SELECT tenant_id, state, payment_state FROM orders WHERE id = $1",
        order_id,
    )
    assert str(order["tenant_id"]) == tid
    assert order["state"] == "locked"
    assert order["payment_state"] == "PAID"

    # Verify encrypted tokens survive round-trip
    stored_tok = await admin_conn.fetchval(
        "SELECT access_token_enc FROM tenant_pos_connections "
        "WHERE vendor = 'clover' AND merchant_id = $1",
        mid,
    )
    assert enc.decrypt(stored_tok) == "real_access_token"
