"""Phase 1 — 13 tests: tenant_pos_connections.

All tests are integration tests that require a live database.
admin_conn: superuser, autocommit — inserts are immediately visible to other connections.
app_conn:   SET ROLE app_user, subject to T1 RLS — uses asyncpg transaction() for SET LOCAL.
service_conn: direct service_worker login, USING(true) RLS, SELECT+UPDATE only.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import asyncpg
import pytest
from uuid6 import uuid7

from app.core.encryption import TokenEncryption
from tests.helpers.pos_connection import (
    FUTURE_1H,
    FUTURE_8H,
    enc,
    insert_connection,
    make_connection_row,
    seed_tenant,
)

# ── Test 1: Migration applies cleanly ──────────────────────────��──────────────


@pytest.mark.asyncio
async def test_migration_applies_and_table_exists(admin_conn):
    """tenant_pos_connections exists after migration.

    Why: If the migration has a syntax error or a missing FK dependency
    (e.g., tenants table created after), this fails immediately — not
    in Phase 5 when the OAuth callback tries to INSERT.
    """
    exists = await admin_conn.fetchval("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'tenant_pos_connections'
        )
    """)
    assert exists is True


# ── Test 2a–d: CHECK constraints ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_vendor_rejects_invalid(admin_conn):
    """vendor must be 'clover' — 'square' is rejected.

    Why: If this CHECK is missing, a row with vendor='square' enters the table.
    The webhook handler calls lookup_tenant_by_merchant with vendor='clover'.
    The worker tries to hit a Square API that doesn't exist. All events
    dead-letter with no obvious reason.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "CheckVendor", f"cv-{t_id[:8]}")

    row = make_connection_row({"tenant_id": t_id, "vendor": "square"})
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_connection(admin_conn, row)
    assert "tenant_pos_vendor_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_environment_rejects_invalid(admin_conn):
    """environment must be sandbox, production, eu, or latam."""
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "CheckEnv", f"ce-{t_id[:8]}")

    row = make_connection_row({"tenant_id": t_id, "environment": "staging"})
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_connection(admin_conn, row)
    assert "tenant_pos_environment_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_state_rejects_invalid(admin_conn):
    """state must be pending, active, revoked, or error."""
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "CheckState", f"cs-{t_id[:8]}")

    row = make_connection_row({"tenant_id": t_id, "state": "disconnected"})
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_connection(admin_conn, row)
    assert "tenant_pos_state_valid" in str(exc.value)


@pytest.mark.asyncio
async def test_check_token_expiry_order_rejects_invalid(admin_conn):
    """refresh_token_expires_at must be >= access_token_expires_at.

    Why: If refresh expires before access, the token refresh job would try
    to use an already-expired refresh token, get a 401 from Clover, and
    increment refresh_failure_count until state = 'error'. The CHECK
    prevents this bad state at INSERT time, not hours later at runtime.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "CheckExpiry", f"chx-{t_id[:8]}")

    row = make_connection_row({
        "tenant_id": t_id,
        "access_token_expires_at": FUTURE_8H,
        "refresh_token_expires_at": FUTURE_1H,  # INVALID: refresh < access
    })
    with pytest.raises(asyncpg.CheckViolationError) as exc:
        await insert_connection(admin_conn, row)
    assert "tenant_pos_token_expiry_order" in str(exc.value)


# ── Test 3: Partial unique — blocks two active for same tenant+vendor ──────────


@pytest.mark.asyncio
async def test_blocks_two_active_same_tenant_vendor(admin_conn):
    """Two active connections for the same tenant+vendor are blocked.

    Why: Two active connections means lookup_tenant_by_merchant returns one
    arbitrarily (LIMIT 1). Token refresh might refresh the wrong one.
    The partial unique covers only state='active' — pending/revoked/error
    are allowed to coexist.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "DupActive", f"da-{t_id[:8]}")
    merchant = f"m_dup_{uuid.uuid4().hex[:8]}"

    row1 = make_connection_row({"tenant_id": t_id, "state": "active", "merchant_id": merchant})
    await insert_connection(admin_conn, row1)

    # Different merchant, same tenant — still blocked (one active per vendor per tenant)
    row2 = make_connection_row({"tenant_id": t_id, "state": "active",
                                "merchant_id": f"m_dup2_{uuid.uuid4().hex[:8]}"})
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await insert_connection(admin_conn, row2)
    assert "tenant_pos_one_active_per_tenant_vendor" in str(exc.value)


# ── Test 4: Partial unique — reconnect allowed after revoke ───────────────────


@pytest.mark.asyncio
async def test_reconnect_allowed_after_revoke(admin_conn):
    """Revoking a connection frees the slot for a new active one.

    Why: A restaurant disconnects Clover (owner revokes). The revoked row stays
    for audit. When they re-authorize, a new active row must succeed. Without
    the WHERE state='active' predicate, the index would cover the revoked row
    and reconnection would be permanently blocked.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "Reconn", f"rc-{t_id[:8]}")
    merchant = f"m_reconn_{uuid.uuid4().hex[:8]}"

    row1 = make_connection_row({"tenant_id": t_id, "state": "active", "merchant_id": merchant})
    await insert_connection(admin_conn, row1)

    # Revoke — exits the partial unique index
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'revoked', revoked_at = now() "
        "WHERE connection_id = $1", row1["connection_id"]
    )

    # New active connection must succeed
    row2 = make_connection_row({"tenant_id": t_id, "state": "active", "merchant_id": merchant})
    await insert_connection(admin_conn, row2)

    state = await admin_conn.fetchval(
        "SELECT state FROM tenant_pos_connections WHERE connection_id = $1",
        row2["connection_id"]
    )
    assert state == "active"


# ── Test 5: Cross-tenant merchant guard ───────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_tenant_merchant_guard(admin_conn):
    """Two different tenants cannot connect to the same Clover merchant.

    Why: If tenant-A and tenant-B both map to merchant_id='M123', every
    webhook for M123 is attributed to whichever lookup_tenant_by_merchant
    returns first (LIMIT 1). Half the sales would deplete the wrong
    restaurant's inventory.
    """
    t1_id = str(uuid.uuid4())
    t2_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t1_id, "CrossA", f"cxa-{t1_id[:8]}")
    await seed_tenant(admin_conn, t2_id, "CrossB", f"cxb-{t2_id[:8]}")

    merchant = f"m_shared_{uuid.uuid4().hex[:8]}"

    row1 = make_connection_row({"tenant_id": t1_id, "merchant_id": merchant, "state": "active"})
    await insert_connection(admin_conn, row1)

    row2 = make_connection_row({"tenant_id": t2_id, "merchant_id": merchant, "state": "active"})
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await insert_connection(admin_conn, row2)
    assert "tenant_pos_one_tenant_per_merchant" in str(exc.value)


# ── Test 6: Merchant guard frees after revoke ────────────────────────────��────


@pytest.mark.asyncio
async def test_merchant_guard_frees_after_revoke(admin_conn):
    """After the original tenant revokes, another tenant can claim the merchant.

    Why: A restaurant changes ownership or switches ReorderOS accounts.
    The new tenant must be able to connect the same Clover merchant.
    The guard uses WHERE state IN ('pending','active','error') — revoked
    exits the index.
    """
    t1_id = str(uuid.uuid4())
    t2_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t1_id, "OldOwner", f"old-{t1_id[:8]}")
    await seed_tenant(admin_conn, t2_id, "NewOwner", f"new-{t2_id[:8]}")

    merchant = f"m_transfer_{uuid.uuid4().hex[:8]}"

    row1 = make_connection_row({"tenant_id": t1_id, "merchant_id": merchant, "state": "active"})
    await insert_connection(admin_conn, row1)

    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'revoked' WHERE connection_id = $1",
        row1["connection_id"]
    )

    row2 = make_connection_row({"tenant_id": t2_id, "merchant_id": merchant, "state": "active"})
    await insert_connection(admin_conn, row2)

    state = await admin_conn.fetchval(
        "SELECT state FROM tenant_pos_connections WHERE connection_id = $1",
        row2["connection_id"]
    )
    assert state == "active"


# ── Test 7: Encryption round-trip through DB ─────────────────────────────���────


@pytest.mark.asyncio
async def test_encryption_roundtrip_through_db(admin_conn):
    """Encrypt → store in text column → read back → decrypt = original.

    Why: Phase 0 tests encryption in memory. This test proves the text column
    doesn't truncate or corrupt the Fernet ciphertext (base64 with +, /, =
    characters). PostgreSQL text handles arbitrary UTF-8, but an encoding
    misconfiguration would corrupt those characters silently.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "EncRT", f"ert-{t_id[:8]}")

    access_plain = "clover_live_access_token_abc123xyz"
    refresh_plain = "clover_live_refresh_token_def456uvw"

    row = make_connection_row({
        "tenant_id": t_id,
        "access_token_enc": enc.encrypt(access_plain),
        "refresh_token_enc": enc.encrypt(refresh_plain),
    })
    await insert_connection(admin_conn, row)

    stored = await admin_conn.fetchrow(
        "SELECT access_token_enc, refresh_token_enc "
        "FROM tenant_pos_connections WHERE connection_id = $1",
        row["connection_id"]
    )

    assert enc.decrypt(stored["access_token_enc"]) == access_plain
    assert enc.decrypt(stored["refresh_token_enc"]) == refresh_plain


# ── Test 8: RLS — app_user sees own tenant only ──────────────────────────��────


@pytest.mark.asyncio
async def test_rls_app_user_tenant_isolation(admin_conn, app_conn):
    """app_user sees only its own tenant's rows, and nothing when unset.

    Why: Without RLS, a bug in the API layer (missing WHERE tenant_id = ?)
    would expose every merchant's connection details — including encrypted
    tokens — to any authenticated user. NULLIF handles the empty-string case
    from clear_rls_context() without crashing.
    """
    t1_id = str(uuid.uuid4())
    t2_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t1_id, "RLS_A", f"rlsa-{t1_id[:8]}")
    await seed_tenant(admin_conn, t2_id, "RLS_B", f"rlsb-{t2_id[:8]}")

    row1 = make_connection_row({"tenant_id": t1_id})
    row2 = make_connection_row({"tenant_id": t2_id})
    await insert_connection(admin_conn, row1)
    await insert_connection(admin_conn, row2)

    # With tenant-A context: sees only tenant-A's rows
    # asyncpg transaction() makes SET LOCAL persist for the block
    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{t1_id}'")
        rows = await app_conn.fetch("SELECT connection_id FROM tenant_pos_connections")
        ids = {str(r["connection_id"]) for r in rows}
        assert row1["connection_id"] in ids
        assert row2["connection_id"] not in ids

    # Without any tenant context: fail-safe → zero rows, no crash
    async with app_conn.transaction():
        rows_empty = await app_conn.fetch(
            "SELECT connection_id FROM tenant_pos_connections"
        )
        assert len(rows_empty) == 0


# ── Test 9: RLS — service_worker sees all tenants ─────────────────────────────


@pytest.mark.asyncio
async def test_rls_service_worker_sees_all(admin_conn, service_conn):
    """service_worker reads all tenants via USING(true) without setting tenant context.

    Why: The token refresh job queries:
        SELECT * FROM tenant_pos_connections
        WHERE state = 'active' AND access_token_expires_at < now() + '10m'
    This must return connections across ALL tenants. T1 RLS would require
    an O(n) loop setting app.tenant_id for each tenant before scanning.
    """
    t1_id = str(uuid.uuid4())
    t2_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t1_id, "SW_A", f"swa-{t1_id[:8]}")
    await seed_tenant(admin_conn, t2_id, "SW_B", f"swb-{t2_id[:8]}")

    row1 = make_connection_row({"tenant_id": t1_id})
    row2 = make_connection_row({"tenant_id": t2_id})
    await insert_connection(admin_conn, row1)
    await insert_connection(admin_conn, row2)

    # service_worker needs no tenant context — USING(true) allows all rows
    rows = await service_conn.fetch(
        "SELECT connection_id FROM tenant_pos_connections "
        "WHERE connection_id = ANY($1::uuid[])",
        [row1["connection_id"], row2["connection_id"]],
    )
    assert len(rows) == 2


# ── Test 10: service_worker cannot INSERT ───────────────────────────────��─────


@pytest.mark.asyncio
async def test_service_worker_cannot_insert(admin_conn, service_conn):
    """service_worker has SELECT + UPDATE only — INSERT is denied.

    Why: Connection creation is an OAuth callback action that must run
    as app_user with the correct tenant context. If service_worker could
    INSERT, a bug in the token refresh loop could create rogue connections
    that bypass the OAuth flow entirely.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "NoIns", f"ni-{t_id[:8]}")

    row = make_connection_row({"tenant_id": t_id})
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await insert_connection(service_conn, row)


# ── Test 11: refresh_failure_count is integer, defaults to 0 ─────────────────


@pytest.mark.asyncio
async def test_refresh_failure_count_is_integer(admin_conn):
    """refresh_failure_count defaults to 0 and increments correctly as int.

    Why: The refresh job does `new_count = conn.refresh_failure_count + 1`.
    If the column were text, Python would do '0' + 1 → TypeError. If the
    default were NULL, NULL + 1 = NULL and the counter never advances past
    the first failure.
    """
    col = await admin_conn.fetchrow("""
        SELECT data_type, column_default
        FROM information_schema.columns
        WHERE table_name = 'tenant_pos_connections'
          AND column_name = 'refresh_failure_count'
    """)
    assert col["data_type"] == "integer"
    assert col["column_default"] == "0"

    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "RFCTest", f"rfc-{t_id[:8]}")

    row = make_connection_row({"tenant_id": t_id})
    await insert_connection(admin_conn, row)

    count = await admin_conn.fetchval(
        "SELECT refresh_failure_count FROM tenant_pos_connections "
        "WHERE connection_id = $1", row["connection_id"]
    )
    assert count == 0
    assert isinstance(count, int)

    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET refresh_failure_count = 3 "
        "WHERE connection_id = $1", row["connection_id"]
    )
    count = await admin_conn.fetchval(
        "SELECT refresh_failure_count FROM tenant_pos_connections "
        "WHERE connection_id = $1", row["connection_id"]
    )
    assert count == 3
    assert isinstance(count, int)


# ── Test 12: UUIDv7 primary key accepted ─────────────────────────────���────────


@pytest.mark.asyncio
async def test_uuidv7_accepted_as_pk(admin_conn):
    """PostgreSQL accepts a UUIDv7 value in the connection_id column.

    Why: If a trigger or CHECK validated the UUID version (allowing only v4),
    every INSERT using uuid7() would fail. This test confirms v7 is accepted.
    """
    t_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t_id, "UUIDv7", f"uv7-{t_id[:8]}")

    v7_id = str(uuid7())
    row = make_connection_row({"connection_id": v7_id, "tenant_id": t_id})
    await insert_connection(admin_conn, row)

    stored_id = await admin_conn.fetchval(
        "SELECT connection_id::text FROM tenant_pos_connections "
        "WHERE connection_id = $1::uuid", v7_id
    )
    assert stored_id == v7_id
    assert v7_id[14] == "7"  # version nibble in position 14


# ── Test 13: Full production lifecycle ──────────────────────────────────��─────


@pytest.mark.asyncio
async def test_full_lifecycle(admin_conn, app_conn, service_conn):
    """Full production lifecycle: pending → active → blocked → revoke → reconnect
    → cross-tenant guard → token refresh → encryption → RLS isolation.

    Why: Each guard works individually in tests 1–12. This test verifies
    they compose correctly in sequence — the real failure mode is a guard
    that works in isolation but breaks when another action precedes it.
    """
    t1_id = str(uuid.uuid4())
    t2_id = str(uuid.uuid4())
    await seed_tenant(admin_conn, t1_id, "Life_A", f"lfa-{t1_id[:8]}")
    await seed_tenant(admin_conn, t2_id, "Life_B", f"lfb-{t2_id[:8]}")

    merchant = f"m_lifecycle_{uuid.uuid4().hex[:8]}"
    access_plain = "lifecycle_access_token"
    refresh_plain = "lifecycle_refresh_token"

    # ── 1. PENDING (OAuth initiation) ────────────────────────────────────────
    conn_1 = make_connection_row({
        "tenant_id": t1_id, "merchant_id": merchant, "state": "pending",
        "access_token_enc": enc.encrypt(access_plain),
        "refresh_token_enc": enc.encrypt(refresh_plain),
    })
    await insert_connection(admin_conn, conn_1)

    # ── 2. ACTIVATE (OAuth callback completes) ──────────────────────────��────
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'active' "
        "WHERE connection_id = $1", conn_1["connection_id"]
    )
    state = await admin_conn.fetchval(
        "SELECT state FROM tenant_pos_connections WHERE connection_id = $1",
        conn_1["connection_id"]
    )
    assert state == "active"

    # ── 3. DUPLICATE BLOCKED ─────────────────────────────��───────────────────
    conn_dup = make_connection_row({"tenant_id": t1_id, "state": "active"})
    with pytest.raises(asyncpg.UniqueViolationError) as exc:
        await insert_connection(admin_conn, conn_dup)
    assert "tenant_pos_one_active_per_tenant_vendor" in str(exc.value)

    # ── 4. REVOKE ─────────────────────────────────���──────────────────────────
    await admin_conn.execute(
        "UPDATE tenant_pos_connections SET state = 'revoked', revoked_at = now() "
        "WHERE connection_id = $1", conn_1["connection_id"]
    )

    # ── 5. RECONNECT ───────────────────────────────���─────────────────────────
    conn_3 = make_connection_row({
        "tenant_id": t1_id, "merchant_id": merchant, "state": "active",
        "access_token_enc": enc.encrypt(access_plain),
        "refresh_token_enc": enc.encrypt(refresh_plain),
    })
    await insert_connection(admin_conn, conn_3)

    # ── 6. CROSS-TENANT GUARD ─────────────────────────────────────────���──────
    conn_b = make_connection_row({
        "tenant_id": t2_id, "merchant_id": merchant, "state": "active",
    })
    with pytest.raises(asyncpg.UniqueViolationError) as exc2:
        await insert_connection(admin_conn, conn_b)
    assert "tenant_pos_one_tenant_per_merchant" in str(exc2.value)

    # ── 7. SERVICE_WORKER TOKEN REFRESH ────────────────────────────���─────────
    new_access = "refreshed_access_token"
    new_refresh = "refreshed_refresh_token"
    await service_conn.execute("""
        UPDATE tenant_pos_connections
        SET access_token_enc = $1,
            access_token_expires_at = $2,
            refresh_token_enc = $3,
            refresh_token_expires_at = $4,
            prev_refresh_token_enc = refresh_token_enc,
            last_token_refresh_at = now(),
            refresh_failure_count = 0
        WHERE connection_id = $5
    """,
        enc.encrypt(new_access),
        FUTURE_1H + timedelta(hours=2),
        enc.encrypt(new_refresh),
        FUTURE_8H + timedelta(days=1),
        conn_3["connection_id"],
    )

    # ── 8. ENCRYPTION ROUND-TRIP ─────────────────────────────────────────────
    stored_access = await admin_conn.fetchval(
        "SELECT access_token_enc FROM tenant_pos_connections "
        "WHERE connection_id = $1", conn_3["connection_id"]
    )
    assert enc.decrypt(stored_access) == new_access

    prev_rt = await admin_conn.fetchval(
        "SELECT prev_refresh_token_enc FROM tenant_pos_connections "
        "WHERE connection_id = $1", conn_3["connection_id"]
    )
    assert prev_rt is not None
    assert enc.decrypt(prev_rt) == refresh_plain  # Original stored before rotation

    # ── 9. RLS: app_user sees only tenant-A ───────────────────────────��──────
    async with app_conn.transaction():
        await app_conn.execute(f"SET LOCAL app.tenant_id = '{t1_id}'")
        rows = await app_conn.fetch(
            "SELECT connection_id FROM tenant_pos_connections "
            "WHERE connection_id = ANY($1::uuid[])",
            [conn_1["connection_id"], conn_3["connection_id"]],
        )
        ids = {str(r["connection_id"]) for r in rows}
        assert conn_1["connection_id"] in ids   # revoked row visible to its tenant
        assert conn_3["connection_id"] in ids   # active row visible

    # ── 10. SERVICE_WORKER sees both tenants ─────────────────────────────────
    all_rows = await service_conn.fetch(
        "SELECT connection_id FROM tenant_pos_connections "
        "WHERE connection_id = ANY($1::uuid[])",
        [conn_1["connection_id"], conn_3["connection_id"]],
    )
    all_ids = {str(r["connection_id"]) for r in all_rows}
    assert conn_1["connection_id"] in all_ids
    assert conn_3["connection_id"] in all_ids
