"""Sprint 3 — Phase 1 verification gate.

Tests the schema, opening-balance guard, append-only grants, and
monitoring_alerts table per the Sprint 3 spec.

All tests use admin_conn (superuser) for setup and app_conn (app_user,
subject to FORCE RLS) for permission checks.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from tests.conftest import seed_tenant

pytestmark = pytest.mark.integration


# ── helpers ───────────────────────────────────────────────────────────────────


async def _mk_uom(conn: asyncpg.Connection, tenant_id: str, *, name: str = "kg") -> str:
    row = await conn.fetchrow(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
        " VALUES ($1, $2, $3, $4) RETURNING id",
        uuid.UUID(tenant_id),
        name,
        name[:3],
        "weight",
    )
    return str(row["id"])


async def _mk_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    uom_id: str,
    *,
    mode: str = "recipe_deducted",
    count_cadence_days: int | None = None,
    count_grace_days: int | None = None,
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"item-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        count_cadence_days,
        count_grace_days,
    )
    return str(row["id"])


async def _insert_movement(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    *,
    movement_type: str,
    delta: float,
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_movements
            (tenant_id, inventory_item_id, movement_type, delta)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
        delta,
    )
    return str(row["id"])


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.1 — app_user cannot UPDATE inventory_movements
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_1_app_user_cannot_update_movements(
    admin_conn: asyncpg.Connection,
    app_conn: asyncpg.Connection,
) -> None:
    tenant = await seed_tenant(admin_conn)
    tid = tenant["id"]
    uom_id = await _mk_uom(admin_conn, str(tid))
    item_id = await _mk_item(admin_conn, str(tid), uom_id)

    # Insert via superuser so we have a row to try updating.
    await _insert_movement(
        admin_conn, str(tid), item_id, movement_type="opening_balance", delta=100
    )

    # Attempt UPDATE as app_user.
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE inventory_movements SET delta = 999")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.2 — app_user cannot DELETE inventory_movements
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_2_app_user_cannot_delete_movements(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM inventory_movements")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.3 — app_user cannot UPDATE inventory_count_events
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_3_app_user_cannot_update_count_events(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("UPDATE inventory_count_events SET counted_quantity = 0")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.4 — app_user cannot DELETE inventory_count_events
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_4_app_user_cannot_delete_count_events(
    app_conn: asyncpg.Connection,
) -> None:
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        await app_conn.execute("DELETE FROM inventory_count_events")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.5 — second opening_balance → unique violation (23505)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_5_duplicate_opening_balance_raises_23505(
    admin_conn: asyncpg.Connection,
) -> None:
    tenant = await seed_tenant(admin_conn)
    tid = str(tenant["id"])
    uom_id = await _mk_uom(admin_conn, tid, name=f"kg-{uuid.uuid4().hex[:4]}")
    item_id = await _mk_item(admin_conn, tid, uom_id)

    # First opening_balance succeeds.
    await _insert_movement(admin_conn, tid, item_id, movement_type="opening_balance", delta=100)

    # Second opening_balance must violate the partial unique index (23505).
    with pytest.raises(asyncpg.UniqueViolationError):
        await _insert_movement(admin_conn, tid, item_id, movement_type="opening_balance", delta=50)


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.6 — opening_balance after another movement → trigger 23514
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_6_opening_balance_after_other_movement_raises_23514(
    admin_conn: asyncpg.Connection,
) -> None:
    tenant = await seed_tenant(admin_conn)
    tid = str(tenant["id"])
    uom_id = await _mk_uom(admin_conn, tid, name=f"kg-{uuid.uuid4().hex[:4]}")
    item_id = await _mk_item(admin_conn, tid, uom_id)

    # Insert a non-OB movement first.
    await _insert_movement(admin_conn, tid, item_id, movement_type="receive", delta=10)

    # Now attempt opening_balance — trigger must fire with ERRCODE 23514.
    with pytest.raises(asyncpg.CheckViolationError) as exc_info:
        await _insert_movement(admin_conn, tid, item_id, movement_type="opening_balance", delta=100)
    assert exc_info.value.sqlstate == "23514"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.7 — monitoring_alerts accepts NULL tenant_id (global alert)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_7_monitoring_alerts_accepts_null_tenant(
    admin_conn: asyncpg.Connection,
) -> None:
    row = await admin_conn.fetchrow(
        """
        INSERT INTO monitoring_alerts (tenant_id, monitor_name, severity, trigger_payload)
        VALUES (NULL, 'test_global', 'info', '{}')
        RETURNING id, tenant_id
        """
    )
    assert row is not None
    assert row["tenant_id"] is None


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.8 — monitoring_alerts dedup index blocks duplicate active alert
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_8_monitoring_alerts_dedup_index(
    admin_conn: asyncpg.Connection,
) -> None:
    tenant = await seed_tenant(admin_conn)
    tid = tenant["id"]

    await admin_conn.execute(
        """
        INSERT INTO monitoring_alerts (tenant_id, monitor_name, severity, trigger_payload)
        VALUES ($1, 'integrity_drift_high', 'warn', '{}')
        """,
        tid,
    )

    with pytest.raises(asyncpg.UniqueViolationError):
        await admin_conn.execute(
            """
            INSERT INTO monitoring_alerts (tenant_id, monitor_name, severity, trigger_payload)
            VALUES ($1, 'integrity_drift_high', 'warn', '{}')
            """,
            tid,
        )


# ═════════════════════════════════════════════════════════════════════════════
# TEST 1.9 — Mode A item with NULL cadence/grace passes cadence_coherence CHECK
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_9_mode_a_null_cadence_grace_passes(
    admin_conn: asyncpg.Connection,
) -> None:
    tenant = await seed_tenant(admin_conn)
    tid = str(tenant["id"])
    uom_id = await _mk_uom(admin_conn, tid, name=f"unit-{uuid.uuid4().hex[:4]}")

    row = await admin_conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, 'recipe_deducted', $3, NULL, NULL)
        RETURNING id, count_cadence_days, count_grace_days
        """,
        uuid.UUID(tid),
        f"mode-a-{uuid.uuid4().hex[:6]}",
        uuid.UUID(uom_id),
    )
    assert row["count_cadence_days"] is None
    assert row["count_grace_days"] is None
