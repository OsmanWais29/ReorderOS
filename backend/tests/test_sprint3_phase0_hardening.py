"""Sprint 3 — Phase 0 accounting-hardening verification gate.

Covers the invariants introduced by migration 0010 and the corresponding
service-layer changes in app/modules/inventory/services.py:

  H0.1  yield_factor_applied is stored on every sale movement
  H0.2  yield mutation does not affect on_hand() via watermark path (snapshot wins)
  H0.3  count event stores a non-NULL reconciliation_cutoff_created_at
  H0.4  Mode A on_hand uses the full ledger under a watermark call (not post-cutoff only)
  H0.5  record_sale_reversal() zeroes a Mode A sale_depletion
  H0.6  record_sale_reversal() zeroes a Mode B sale_signal
  H0.7  record_sale_reversal() is idempotent
  H0.8  record_sale_reversal() rejects non-reversible movement types
  H0.9  late_signal_reconciliation alert fires when ingestion crosses a count boundary
  H0.10 no alert fires when the >30-min lag does not cross any count boundary
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.depletion.writer import (
    record_sale_inventory_effect,
    record_sale_reversal,
)
from app.modules.inventory.services import (
    on_hand,
    record_count_event,
    record_opening_balance,
)
from tests.conftest import seed_tenant
from tests.helpers.sprint5 import seed_recipe_version

pytestmark = pytest.mark.integration


# ── session fixture ────────────────────────────────────────────────────────────


@pytest.fixture
async def db_session() -> AsyncSession:
    from app.core.database import get_sessionmaker

    sm = get_sessionmaker()
    async with sm() as session:
        yield session
        await session.rollback()


# ── shared helpers (mirrors test_sprint3_phase4 helpers) ──────────────────────


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
) -> str:
    cadence = 7 if mode == "count_anchored" else None
    grace = 7 if mode == "count_anchored" else None
    row = await conn.fetchrow(
        """
        INSERT INTO inventory_items
            (tenant_id, name, inventory_mode, storage_unit_id,
             storage_to_recipe_factor, count_cadence_days, count_grace_days)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        RETURNING id
        """,
        uuid.UUID(tenant_id),
        f"item-{uuid.uuid4().hex[:6]}",
        mode,
        uuid.UUID(uom_id),
        factor,
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
    import time as _time

    inbox_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO pos_event_inbox"
        " (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,"
        "  vendor_event_type, vendor_ts, raw_payload, signature_verified, source)"
        " VALUES ($1,$2,'clover','O:h0stub','O','UPDATE',$3,'{}',false,'webhook')",
        inbox_id,
        uuid.UUID(tenant_id),
        int(_time.time() * 1000),
    )
    order_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO orders"
        " (id, tenant_id, pos_event_inbox_id, clover_order_id,"
        "  total_amount_cents, state, payment_state, processed_at)"
        " VALUES ($1,$2,$3,'h0_order',0,'locked','OPEN',now())",
        order_id,
        uuid.UUID(tenant_id),
        inbox_id,
    )
    row = await conn.fetchrow(
        "INSERT INTO sale_line_items"
        " (id, tenant_id, order_id, clover_line_item_id, name_at_sale,"
        "  quantity, price_cents_at_sale, net_revenue_cents, recipe_version_id)"
        " VALUES (gen_random_uuid(),$1,$2,$3,'H0 Item',$4,0,0,$5)"
        " RETURNING id",
        uuid.UUID(tenant_id),
        order_id,
        f"cli_{uuid.uuid4().hex[:8]}",
        qty,
        uuid.UUID(recipe_version_id),
    )
    return str(row["id"])


# ═════════════════════════════════════════════════════════════════════════════
# H0.1 — yield_factor_applied is stored on every sale movement
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_1_yield_factor_applied_stored(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored", factor=1.0)

    await admin_conn.execute(
        "INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, yield_factor)"
        " VALUES ($1, $2, 0.85)",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=100)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    row = await admin_conn.fetchrow(
        "SELECT yield_factor_applied FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2"
        " AND movement_type = 'sale_signal'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert row is not None
    assert row["yield_factor_applied"] == Decimal("0.85"), (
        f"expected yield_factor_applied=0.85, got {row['yield_factor_applied']}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# H0.2 — yield mutation does not affect watermark on_hand (snapshot wins)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_2_yield_mutation_no_effect_on_watermark(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    """on_hand() in the watermark path must use yield_factor_applied from each
    movement row, not the live inventory_yield_factors table.  Mutating the
    live table after a sale must not change the watermark result.
    """
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored", factor=1.0)

    await admin_conn.execute(
        "INSERT INTO inventory_yield_factors (tenant_id, inventory_item_id, yield_factor)"
        " VALUES ($1, $2, 0.80)",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("1000"),
    )
    await db_session.commit()

    # Anchor count — captures cutoff T1
    count_result = await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("1000"),
    )
    await db_session.commit()

    cutoff_row = await admin_conn.fetchrow(
        "SELECT reconciliation_cutoff_created_at FROM inventory_count_events WHERE id = $1",
        count_result["id"],
    )
    cutoff = cutoff_row["reconciliation_cutoff_created_at"]
    assert cutoff is not None

    # Post-count signal — yield_factor_applied = 0.80 stored on the row
    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=100)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    oh_before = await on_hand(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        reconciliation_cutoff=cutoff,
    )

    # Mutate live yield — watermark on_hand must not change
    await admin_conn.execute(
        "UPDATE inventory_yield_factors SET yield_factor = 9.99"
        " WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(item),
    )

    oh_after = await on_hand(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        reconciliation_cutoff=cutoff,
    )

    assert oh_before == oh_after, (
        f"yield mutation changed watermark on_hand: {oh_before} → {oh_after}; "
        "yield_factor_applied snapshot is not being used"
    )


# ═════════════════════════════════════════════════════════════════════════════
# H0.3 — count event stores reconciliation_cutoff_created_at
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_3_count_event_stores_watermark(
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
        quantity=Decimal("100"),
    )
    await db_session.commit()

    before = datetime.now(UTC)
    result = await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("100"),
    )
    after = datetime.now(UTC)
    await db_session.commit()

    row = await admin_conn.fetchrow(
        "SELECT reconciliation_cutoff_created_at FROM inventory_count_events WHERE id = $1",
        result["id"],
    )
    cutoff = row["reconciliation_cutoff_created_at"]
    assert cutoff is not None, "reconciliation_cutoff_created_at must not be NULL"
    assert before <= cutoff <= after, (
        f"cutoff {cutoff} outside capture window [{before}, {after}]"
    )


# ═════════════════════════════════════════════════════════════════════════════
# H0.4 — Mode A on_hand uses the full ledger under a watermark call
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_4_mode_a_full_ledger_under_watermark(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    """record_count_event() calls on_hand(reconciliation_cutoff=cutoff) for
    Mode A items.  All pre-count movements (opening_balance + sale_depletion)
    must be included in ledger_sum regardless of the cutoff timestamp.
    If ledger_sum were filtered by created_at > cutoff, predicted_on_hand_at_count
    would be 0 instead of 400.
    """
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=100)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    # All movements above are pre-cutoff; cutoff is captured inside record_count_event
    result = await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("400"),
    )
    await db_session.commit()

    assert result["predicted_on_hand_at_count"] == Decimal("400"), (
        f"Mode A predicted={result['predicted_on_hand_at_count']!r}, expected 400; "
        "ledger_sum is being incorrectly filtered by the watermark cutoff"
    )


# ═════════════════════════════════════════════════════════════════════════════
# H0.5 — record_sale_reversal() zeroes a Mode A sale_depletion
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_5_reversal_mode_a_zeros_depletion(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=200)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    mv_id = await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    oh_after_sale = await on_hand(
        db_session, tenant_id=uuid.UUID(tid), inventory_item_id=uuid.UUID(item)
    )
    assert oh_after_sale == Decimal("300")

    rev_id = await record_sale_reversal(
        db_session,
        tenant_id=uuid.UUID(tid),
        original_movement_id=mv_id,
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    assert rev_id is not None

    oh_after_reversal = await on_hand(
        db_session, tenant_id=uuid.UUID(tid), inventory_item_id=uuid.UUID(item)
    )
    assert oh_after_reversal == Decimal("500"), (
        f"expected 500 after reversal, got {oh_after_reversal}"
    )

    rev_row = await admin_conn.fetchrow(
        "SELECT movement_type, delta, source_id FROM inventory_movements WHERE id = $1",
        rev_id,
    )
    assert rev_row["movement_type"] == "sale_depletion_reversal"
    assert rev_row["delta"] == Decimal("200")
    assert str(rev_row["source_id"]) == str(mv_id)


# ═════════════════════════════════════════════════════════════════════════════
# H0.6 — record_sale_reversal() zeroes a Mode B sale_signal
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_6_reversal_mode_b_zeros_signal(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("400"),
    )
    await db_session.commit()

    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=100)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    mv_id = await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    oh_after_sale = await on_hand(
        db_session, tenant_id=uuid.UUID(tid), inventory_item_id=uuid.UUID(item)
    )
    assert oh_after_sale == Decimal("300")  # 400 - 100 * yield(1.0)

    rev_id = await record_sale_reversal(
        db_session,
        tenant_id=uuid.UUID(tid),
        original_movement_id=mv_id,
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    oh_after_reversal = await on_hand(
        db_session, tenant_id=uuid.UUID(tid), inventory_item_id=uuid.UUID(item)
    )
    assert oh_after_reversal == Decimal("400"), (
        f"expected 400 after reversal, got {oh_after_reversal}"
    )

    rev_row = await admin_conn.fetchrow(
        "SELECT movement_type FROM inventory_movements WHERE id = $1", rev_id
    )
    assert rev_row["movement_type"] == "sale_signal_reversal"


# ═════════════════════════════════════════════════════════════════════════════
# H0.7 — record_sale_reversal() is idempotent
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_7_reversal_idempotent(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=50)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    mv_id = await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    first = await record_sale_reversal(
        db_session,
        tenant_id=uuid.UUID(tid),
        original_movement_id=mv_id,
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    second = await record_sale_reversal(
        db_session,
        tenant_id=uuid.UUID(tid),
        original_movement_id=mv_id,
        inventory_item_id=uuid.UUID(item),
    )
    await db_session.commit()

    assert first is not None
    assert second is None, "second reversal call must return None (idempotent replay)"

    count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2"
        " AND movement_type = 'sale_depletion_reversal'",
        uuid.UUID(tid),
        uuid.UUID(item),
    )
    assert count == 1, f"expected exactly 1 reversal row, got {count}"


# ═════════════════════════════════════════════════════════════════════════════
# H0.8 — record_sale_reversal() rejects non-reversible movement types
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_8_reversal_rejects_wrong_type(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="recipe_deducted", factor=1.0)

    mv_id = await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("100"),
    )
    await db_session.commit()

    with pytest.raises(ValueError, match="only sale_depletion and sale_signal"):
        await record_sale_reversal(
            db_session,
            tenant_id=uuid.UUID(tid),
            original_movement_id=mv_id,
            inventory_item_id=uuid.UUID(item),
        )


# ═════════════════════════════════════════════════════════════════════════════
# H0.9 — late_signal_reconciliation alert fires when boundary is crossed
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_9_late_signal_alert_fires(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    """A sale signal whose recorded_at is >30 min before ingestion, and which
    crosses a count reconciliation boundary, must create a
    late_signal_reconciliation alert at severity warn.
    """
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    # Count establishes reconciliation boundary ≈ NOW()
    await record_count_event(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        counted_quantity=Decimal("500"),
    )
    await db_session.commit()

    # Late signal: recorded_at 2 hours before ingestion, crossing the boundary
    late_recorded_at = datetime.now(UTC) - timedelta(hours=2)
    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=50)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
        recorded_at=late_recorded_at,
    )
    await db_session.commit()

    alert_row = await admin_conn.fetchrow(
        "SELECT severity FROM monitoring_alerts"
        " WHERE tenant_id = $1 AND monitor_name = 'late_signal_reconciliation'"
        " AND resolved_at IS NULL",
        uuid.UUID(tid),
    )
    assert alert_row is not None, "late_signal_reconciliation alert must be created"
    assert alert_row["severity"] == "warn"


# ═════════════════════════════════════════════════════════════════════════════
# H0.10 — no alert fires when no count boundary exists
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_h0_10_no_late_alert_without_boundary(
    admin_conn: asyncpg.Connection,
    db_session: AsyncSession,
) -> None:
    """A sale signal with recorded_at >30 min before ingestion must NOT fire
    the late_signal_reconciliation alert when no count boundary exists for
    the item in that time window.
    """
    t = await seed_tenant(admin_conn)
    tid = str(t["id"])
    uom = await _mk_uom(admin_conn, tid)
    item = await _mk_item(admin_conn, tid, uom, mode="count_anchored", factor=1.0)

    await record_opening_balance(
        db_session,
        tenant_id=uuid.UUID(tid),
        inventory_item_id=uuid.UUID(item),
        quantity=Decimal("500"),
    )
    await db_session.commit()

    # No count event — no reconciliation boundary exists
    late_recorded_at = datetime.now(UTC) - timedelta(hours=2)
    rv_id, _ = await _mk_recipe(admin_conn, tid, item, recipe_qty=50)
    sl_id = await _mk_sale_line(admin_conn, tid, rv_id, qty=1)
    await record_sale_inventory_effect(
        db_session,
        tenant_id=uuid.UUID(tid),
        sale_line_item_id=uuid.UUID(sl_id),
        inventory_item_id=uuid.UUID(item),
        recorded_at=late_recorded_at,
    )
    await db_session.commit()

    alert_row = await admin_conn.fetchrow(
        "SELECT 1 FROM monitoring_alerts"
        " WHERE tenant_id = $1 AND monitor_name = 'late_signal_reconciliation'"
        " AND resolved_at IS NULL",
        uuid.UUID(tid),
    )
    assert alert_row is None, (
        "late_signal_reconciliation alert must NOT fire when no count boundary exists"
    )
