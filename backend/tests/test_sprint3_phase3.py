"""Sprint 3 — Phase 3 verification gate: count-event trigger."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import pytest

from tests.conftest import seed_tenant

pytestmark = pytest.mark.integration

UTC = UTC


# ── shared helpers ────────────────────────────────────────────────────────────


async def _mk_uom(conn: asyncpg.Connection, tenant_id: str) -> str:
    name = f"uom-{uuid.uuid4().hex[:6]}"
    row = await conn.fetchrow(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
        " VALUES ($1, $2, $3, 'weight') RETURNING id",
        uuid.UUID(tenant_id),
        name,
        name[:3],
    )
    return str(row["id"])


async def _mk_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    uom_id: str,
    *,
    mode: str,
    last_count_at: datetime | None = None,
    last_count_qty: float | None = None,
) -> str:
    cadence = 7 if mode == "count_anchored" else None
    grace = 7 if mode == "count_anchored" else None  # grace >= cadence per v5
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             last_count_at, last_count_quantity,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"item-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        last_count_at,
        last_count_qty,
        cadence,
        grace,
    )
    return str(row["id"])


async def _mv(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    movement_type: str,
    delta: float,
    recorded_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO inventory_movements
            (tenant_id, inventory_item_id, movement_type, delta, recorded_at)
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
        delta,
        recorded_at,
    )


async def _on_hand(conn: asyncpg.Connection, tenant_id: str, item_id: str) -> Decimal | None:
    row = await conn.fetchrow(
        """
        WITH item AS (
            SELECT inventory_mode, last_count_at, last_count_quantity
              FROM inventory_items WHERE tenant_id=$1 AND id=$2
        ),
        ledger_sum AS (
            SELECT COALESCE(SUM(delta),0) AS qty FROM inventory_movements
             WHERE tenant_id=$1 AND inventory_item_id=$2
               AND movement_type NOT IN ('sale_signal','sale_signal_reversal')
        ),
        receipts_since AS (
            SELECT COALESCE(SUM(m.delta),0) AS qty
              FROM inventory_movements m, item
             WHERE m.tenant_id=$1 AND m.inventory_item_id=$2
               AND m.recorded_at > item.last_count_at
               AND m.movement_type IN ('receive','transfer_in','count_adjust','opening_balance')
        ),
        signals_since AS (
            SELECT COALESCE(SUM(ABS(m.delta)),0) AS qty
              FROM inventory_movements m, item
             WHERE m.tenant_id=$1 AND m.inventory_item_id=$2
               AND m.recorded_at > item.last_count_at
               AND m.movement_type = 'sale_signal'
        )
        SELECT CASE
            WHEN item.inventory_mode = 'recipe_deducted' THEN ledger_sum.qty
            WHEN item.inventory_mode = 'count_anchored'
                 AND item.last_count_quantity IS NOT NULL
                THEN item.last_count_quantity + receipts_since.qty
                     - (signals_since.qty * COALESCE(
                           (SELECT yield_factor FROM inventory_yield_factors
                             WHERE tenant_id=$1 AND inventory_item_id=$2), 1.0))
            ELSE NULL
        END AS qty
        FROM item, ledger_sum, receipts_since, signals_since
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
    )
    return row["qty"]


async def _insert_count_event(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    *,
    counted_quantity: float,
    predicted_on_hand: float | None,
    counted_at: datetime | None = None,
) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_count_events
            (tenant_id, inventory_item_id, counted_quantity,
             predicted_on_hand_at_count, counted_at)
        VALUES ($1, $2, $3, $4, COALESCE($5, NOW()))
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        counted_quantity,
        predicted_on_hand,
        counted_at,
    )
    return str(row["id"])


async def _count_movements_of_type(
    conn: asyncpg.Connection,
    tenant_id: str,
    item_id: str,
    movement_type: str,
) -> int:
    row = await conn.fetchrow(
        "SELECT COUNT(*) AS n FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2 AND movement_type = $3",
        uuid.UUID(tenant_id),
        uuid.UUID(item_id),
        movement_type,
    )
    return row["n"]


# ═════════════════════════════════════════════════════════════════════════════
# TEST 3.1 — Mode A count with drift emits count_adjust
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_1_mode_a_count_with_drift_emits_count_adjust(
    admin_conn: asyncpg.Connection,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)

    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=95,
        predicted_on_hand=100,
    )

    # Trigger must have emitted a count_adjust row.
    n_adj = await _count_movements_of_type(admin_conn, tid, item, "count_adjust")
    assert n_adj == 1, f"expected 1 count_adjust, got {n_adj}"

    # Verify delta = -5 (counted - predicted = 95 - 100).
    row = await admin_conn.fetchrow(
        "SELECT delta, source_type FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2 AND movement_type = 'count_adjust'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert row["delta"] == Decimal("-5"), f"expected delta=-5, got {row['delta']}"
    assert row["source_type"] == "count_event"

    # on_hand must now equal counted_quantity.
    assert await _on_hand(admin_conn, tid, item) == Decimal("95")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 3.2 — Mode A zero-drift count emits nothing
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_2_mode_a_zero_drift_emits_nothing(
    admin_conn: asyncpg.Connection,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await _mv(admin_conn, tid, item, "opening_balance", +100)

    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=100,
        predicted_on_hand=100,
    )

    n_adj = await _count_movements_of_type(admin_conn, tid, item, "count_adjust")
    assert n_adj == 0, f"expected 0 count_adjust rows, got {n_adj}"
    assert await _on_hand(admin_conn, tid, item) == Decimal("100")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 3.3 — Mode B count does NOT emit count_adjust; updates anchor
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_3_mode_b_count_does_not_emit_count_adjust(
    admin_conn: asyncpg.Connection,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=100,
    )

    counted_at = datetime(2026, 3, 2, 0, 0, 0, tzinfo=UTC)
    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=80,
        predicted_on_hand=None,
        counted_at=counted_at,
    )

    n_adj = await _count_movements_of_type(admin_conn, tid, item, "count_adjust")
    assert n_adj == 0, f"expected 0 count_adjust rows, got {n_adj}"

    row = await admin_conn.fetchrow(
        "SELECT last_count_quantity, last_count_at FROM inventory_items WHERE id = $1",
        uuid.UUID(item),
    )
    assert row["last_count_quantity"] == Decimal("80")
    assert row["last_count_at"] == counted_at


# ═════════════════════════════════════════════════════════════════════════════
# TEST 3.4 — Mode B count re-anchors on_hand
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_4_mode_b_count_reanchors_on_hand(
    admin_conn: asyncpg.Connection,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)

    t0 = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 4, 1, 6, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 4, 2, 0, 0, 0, tzinfo=UTC)  # count time

    item = await _mk_item(
        admin_conn,
        tid,
        uom,
        mode="count_anchored",
        last_count_at=t0,
        last_count_qty=200,
    )

    await _mv(admin_conn, tid, item, "receive", +50, t1)
    await _mv(admin_conn, tid, item, "sale_signal", +30, t1)
    # on_hand before count: 200 + 50 - 30*1.0 = 220

    pre_count = await _on_hand(admin_conn, tid, item)
    assert pre_count == Decimal("220"), f"pre-count on_hand wrong: {pre_count}"

    # Insert count event — trigger updates anchor to 210 at t2.
    await _insert_count_event(
        admin_conn,
        tid,
        item,
        counted_quantity=210,
        predicted_on_hand=None,
        counted_at=t2,
    )

    row = await admin_conn.fetchrow(
        "SELECT last_count_quantity FROM inventory_items WHERE id = $1",
        uuid.UUID(item),
    )
    assert row["last_count_quantity"] == Decimal("210")

    # No movements after t2, so on_hand = 210.
    post_count = await _on_hand(admin_conn, tid, item)
    assert post_count == Decimal("210"), f"post-count on_hand wrong: {post_count}"
