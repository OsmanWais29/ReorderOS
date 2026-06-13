"""Sprint 3 — Phase 4 verification gate: service layer (4A-4D)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.services import (
    add_receipt_line,
    commit_receipt,
    create_receipt,
    record_count_event,
    record_opening_balance,
)
from tests.conftest import seed_tenant, seed_user
from tests.helpers.sprint5 import seed_recipe_version

pytestmark = pytest.mark.integration


# ── session fixture ────────────────────────────────────────────────────────────


@pytest.fixture
async def db_session() -> AsyncSession:
    """SQLAlchemy async session connected as the superuser (bypasses FORCE RLS)."""
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        yield session
        # let each test commit explicitly; rollback any uncommitted state
        await session.rollback()


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
    factor: float = 1.0,
    last_count_at: datetime | None = None,
    last_count_qty: float | None = None,
) -> str:
    cadence = 7 if mode == "count_anchored" else None
    grace = 7 if mode == "count_anchored" else None  # grace >= cadence per v5
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             storage_to_recipe_factor,
             last_count_at, last_count_quantity,
             count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"item-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        factor,
        last_count_at,
        last_count_qty,
        cadence,
        grace,
    )
    return str(row["id"])


async def _mk_recipe(
    conn: asyncpg.Connection,
    tenant_id: str,
    inventory_item_id: str,
    *,
    recipe_qty: float,
) -> tuple[str, str]:
    """Returns (recipe_version_id, recipe_ingredient_id)."""
    seeded = await seed_recipe_version(
        conn, tenant_id, ingredients=[(inventory_item_id, recipe_qty)]
    )
    return seeded.recipe_version_id, seeded.ingredient_ids[0]


async def _mk_sale_line(
    conn: asyncpg.Connection,
    tenant_id: str,
    recipe_version_id: str,
    *,
    qty: float,
) -> str:
    # Sprint 4: sale_line_items requires order_id (FK to orders), which in turn
    # requires pos_event_inbox_id. Create minimal prereqs as superuser.
    import time as _time

    inbox_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO pos_event_inbox"
        " (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,"
        "  vendor_event_type, vendor_ts, raw_payload, signature_verified, source)"
        " VALUES ($1,$2,'clover','O:s3stub','O','UPDATE',$3,'{}',false,'webhook')",
        inbox_id,
        uuid.UUID(tenant_id),
        int(_time.time() * 1000),
    )
    order_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO orders"
        " (id, tenant_id, pos_event_inbox_id, clover_order_id,"
        "  total_amount_cents, state, payment_state, processed_at)"
        " VALUES ($1,$2,$3,'s3_order',0,'locked','OPEN',now())",
        order_id,
        uuid.UUID(tenant_id),
        inbox_id,
    )
    row = await conn.fetchrow(
        "INSERT INTO sale_line_items"
        " (id, tenant_id, order_id, clover_line_item_id, name_at_sale,"
        "  quantity, price_cents_at_sale, net_revenue_cents, recipe_version_id)"
        " VALUES (gen_random_uuid(),$1,$2,$3,'Sprint 3 Item',$4,0,0,$5)"
        " RETURNING id",
        uuid.UUID(tenant_id),
        order_id,
        f"cli_{uuid.uuid4().hex[:8]}",
        qty,
        uuid.UUID(recipe_version_id),
    )
    return str(row["id"])


# ⚠️ NOT the canonical on_hand — DO NOT copy as a reference (V1 finding F3.1-oracle).
# This test-local approximation uses SUM(ABS(delta)) for signals, counts only 'sale_signal'
# (excludes sale_signal_reversal), and ignores yield_factor_applied. It coincides with the
# production on_hand ONLY on reversal-free, yield-1 data (all this file uses). The canonical
# formula is inventory_accounting_semantics.md §3 (SUM(delta × yield) over signal AND reversal);
# the single correct reference lands in V2 (tests/reference_models/depletion.py), which replaces this.
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


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.1 — Opening balance Mode A
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_1_opening_balance_mode_a(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    mv_id = await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    # verify movement row
    row = await admin_conn.fetchrow(
        "SELECT delta, movement_type FROM inventory_movements WHERE id = $1",
        mv_id,
    )
    assert row["movement_type"] == "opening_balance"
    assert row["delta"] == Decimal("500")

    assert await _on_hand(admin_conn, tid, item) == Decimal("500")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.2 — Opening balance Mode B sets anchor
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_2_opening_balance_mode_b_sets_anchor(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored")

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("300"),
    )
    await db_session.commit()

    item_row = await admin_conn.fetchrow(
        "SELECT last_count_quantity, last_count_at FROM inventory_items WHERE id = $1",
        uuid.UUID(item),
    )
    assert item_row["last_count_quantity"] == Decimal("300")
    assert item_row["last_count_at"] is not None

    # on_hand() = anchor (300) + 0 receipts_since = 300
    assert await _on_hand(admin_conn, tid, item) == Decimal("300")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.3 — Opening balance cannot be second
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_3_opening_balance_cannot_be_second(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    # First call succeeds.
    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("100"),
    )
    await db_session.commit()

    # Second call is idempotent (returns existing ID — no new row, no exception).
    _ = await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("999"),  # different qty — should be ignored
    )
    await db_session.commit()

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2"
        " AND movement_type = 'opening_balance'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert count == 1, f"expected exactly 1 opening_balance, got {count}"
    # on_hand still 100 (the original quantity)
    assert await _on_hand(admin_conn, tid, item) == Decimal("100")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.4 — Mode A sale depletion
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_7_mode_a_count_with_drift_creates_alert(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    result = await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("400"),
    )
    await db_session.commit()

    # predicted must have been 500 (captured before insert)
    assert result["predicted_on_hand_at_count"] == Decimal("500")

    event_row = await admin_conn.fetchrow(
        "SELECT predicted_on_hand_at_count FROM inventory_count_events WHERE id = $1",
        result["id"],
    )
    assert event_row["predicted_on_hand_at_count"] == Decimal("500")

    # trigger emits count_adjust -100
    adj_row = await admin_conn.fetchrow(
        "SELECT delta FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2 AND movement_type = 'count_adjust'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert adj_row is not None
    assert adj_row["delta"] == Decimal("-100")

    assert await _on_hand(admin_conn, tid, item) == Decimal("400")

    # drift = 500-400=100, drift_pct = 100/400 = 0.25 → critical
    alert_row = await admin_conn.fetchrow(
        "SELECT severity FROM monitoring_alerts"
        " WHERE tenant_id = $1 AND monitor_name = 'integrity_drift_high'"
        " AND resolved_at IS NULL",
        uuid.UUID(tid),
    )
    assert alert_row is not None, "monitoring_alert not created"
    assert alert_row["severity"] == "critical"


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.8 — Mode B count does NOT create count_adjust
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_8_mode_b_count_no_count_adjust(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored")

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("300"),
    )
    await db_session.commit()

    await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("280"),
    )
    await db_session.commit()

    # no count_adjust
    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2"
        " AND movement_type = 'count_adjust'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert count == 0

    # anchor updated
    row = await admin_conn.fetchrow(
        "SELECT last_count_quantity FROM inventory_items WHERE id = $1",
        uuid.UUID(item),
    )
    assert row["last_count_quantity"] == Decimal("280")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.9 — Receipt commit creates movements and cost snapshots
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_9_receipt_commit_creates_movements(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    u = await seed_user(admin_conn)
    tid = str(t["id"])
    uid = str(u["id"])
    uom = await _mk_uom(admin_conn, tid)
    item_a = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")
    item_b = await _mk_item(admin_conn, tid, uom, mode="count_anchored")

    # Give item_b an anchor so on_hand() works
    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item_b),
        quantity=Decimal("0"),
    )
    await db_session.commit()

    rid = await create_receipt(
        db_session,
        tenant_id=uuid.UUID(tid),
        created_by=uuid.UUID(uid),
    )
    line_a = await add_receipt_line(
        db_session,
        tenant_id=uuid.UUID(tid),
        receipt_id=rid,
        inventory_item_id=uuid.UUID(item_a),
        received_quantity=Decimal("10"),
        unit_cost_cents=500,  # $5/unit
    )
    line_b = await add_receipt_line(
        db_session,
        tenant_id=uuid.UUID(tid),
        receipt_id=rid,
        inventory_item_id=uuid.UUID(item_b),
        received_quantity=Decimal("5"),
        unit_cost_cents=800,  # $8/unit
    )
    await db_session.commit()

    result = await commit_receipt(
        db_session,
        tenant_id=uuid.UUID(tid),
        receipt_id=rid,
    )
    await db_session.commit()

    assert result["status"] == "committed"
    assert len(result["movement_ids"]) == 2

    # 2 receive movements
    n_mv = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND movement_type = 'receive'"
        " AND (inventory_item_id = $2 OR inventory_item_id = $3)",
        uuid.UUID(tid),
        uuid.UUID(item_a),
        uuid.UUID(item_b),
    )
    assert n_mv == 2

    # 2 cost snapshots
    n_snap = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM ingredient_cost_snapshots"
        " WHERE tenant_id = $1"
        " AND (inventory_item_id = $2 OR inventory_item_id = $3)",
        uuid.UUID(tid),
        uuid.UUID(item_a),
        uuid.UUID(item_b),
    )
    assert n_snap == 2

    # emits_movement_id set on both lines
    for lid in (line_a, line_b):
        mv_link = await admin_conn.fetchval(
            "SELECT emits_movement_id FROM receipt_lines WHERE id = $1",
            lid,
        )
        assert mv_link is not None

    # receipt marked committed
    state = await admin_conn.fetchval(
        "SELECT commit_state FROM receipts WHERE id = $1",
        rid,
    )
    assert state == "committed"

    # on_hand increased correctly
    oh_a = await _on_hand(admin_conn, tid, item_a)
    assert oh_a == Decimal("10")

    oh_b = await _on_hand(admin_conn, tid, item_b)
    assert oh_b == Decimal("5")


# ═════════════════════════════════════════════════════════════════════════════
# TEST 4.10 — Receipt commit idempotency
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_10_receipt_commit_idempotency(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted")

    rid = await create_receipt(db_session, tenant_id=uuid.UUID(tid))
    await add_receipt_line(
        db_session,
        tenant_id=uuid.UUID(tid),
        receipt_id=rid,
        inventory_item_id=uuid.UUID(item),
        received_quantity=Decimal("20"),
        unit_cost_cents=300,
    )
    await db_session.commit()

    await commit_receipt(db_session, tenant_id=uuid.UUID(tid), receipt_id=rid)
    await db_session.commit()

    # second commit
    result2 = await commit_receipt(db_session, tenant_id=uuid.UUID(tid), receipt_id=rid)
    await db_session.commit()

    assert result2["status"] == "already_committed"

    n_mv = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2"
        " AND movement_type = 'receive'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert n_mv == 1, f"expected 1 receive movement, got {n_mv}"
