"""End-to-end: Clover webhook → pos_event_inbox → order → sale_line_items → inventory_movements.

These tests verify the full pipeline that was assembled across Sprint 3/4:

  E2E.1  Locked order with a catalogued, recipe-linked menu item writes
         a sale_depletion movement with correct delta and yield snapshot.

  E2E.2  delta = -(sale_qty * recipe_qty / storage_to_recipe_factor).

  E2E.3  Mode B item: movement_type is sale_signal (not sale_depletion).

  E2E.4  recipe_version_id is frozen on sale_line_items at insert time;
         changing menu_items.recipe_version_id later has no effect on
         movements from prior sales.

  E2E.5  Replay (duplicate inbox event for same order) produces exactly
         one inventory_movement row — idempotency holds end-to-end.

  E2E.6  Refunded line item: no inventory_movement written.

  E2E.7  Voided (exchanged) line item: no inventory_movement written.

  E2E.8  Line item with no recipe link (menu_item not catalogued):
         no inventory_movement written, inbox processed cleanly.

  E2E.9  Multi-ingredient recipe: one movement per ingredient.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import asyncpg  # noqa: F401
import httpx
import pytest
import respx
from uuid6 import uuid7

from app.modules.pos.worker import InboxWorker
from tests.helpers.phase7 import make_clover_order, seed_worker_prereqs
from tests.helpers.sprint5 import seed_recipe_version

pytestmark = pytest.mark.integration


# ── seed helpers ───────────────────────────────────────────────────────────────


async def _seed_uom(conn: asyncpg.Connection, tenant_id: str) -> str:
    name = f"uom-{uuid.uuid4().hex[:6]}"
    row = await conn.fetchrow(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
        " VALUES ($1, $2, $3, 'weight') RETURNING id",
        uuid.UUID(tenant_id),
        name,
        name[:3],
    )
    return str(row["id"])


async def _seed_inventory_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    uom_id: str,
    *,
    mode: str = "recipe_deducted",
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


async def _seed_recipe(
    conn: asyncpg.Connection,
    tenant_id: str,
    *ingredients: tuple[str, float],
) -> str:
    """Create a recipe_version + one recipe_ingredient per (inventory_item_id, qty) pair.
    Returns the recipe_version_id.
    """
    seeded = await seed_recipe_version(conn, tenant_id, ingredients=ingredients)
    return seeded.recipe_version_id


async def _seed_menu_item(
    conn: asyncpg.Connection,
    tenant_id: str,
    pos_item_id: str,
    *,
    recipe_version_id: str | None = None,
) -> str:
    row = await conn.fetchrow(
        "INSERT INTO menu_items (tenant_id, pos_item_id, name, recipe_version_id)"
        " VALUES ($1, $2, $3, $4) RETURNING id",
        uuid.UUID(tenant_id),
        pos_item_id,
        f"menu-{uuid.uuid4().hex[:6]}",
        uuid.UUID(recipe_version_id) if recipe_version_id else None,
    )
    return str(row["id"])


def _make_li_with_item(pos_item_id: str, *, name: str = "Test Item", price: int = 1000, qty: int = 1) -> dict:
    return {
        "id": f"li_{uuid.uuid4().hex[:8]}",
        "item": {"id": pos_item_id},
        "name": name,
        "price": price,
        "unitQty": qty,
        "discountAmount": 0,
    }


# ── autouse: neutralise pre-existing inbox rows ────────────────────────────────


@pytest.fixture(autouse=True)
async def clean_pending_inbox(admin_conn):
    await admin_conn.execute("""
        UPDATE pos_event_inbox
        SET state = 'duplicate_ignored'
        WHERE state IN ('pending', 'failed', 'processing')
    """)
    yield


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.1 — Mode A sale writes sale_depletion movement
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_1_mode_a_sale_writes_depletion(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 2.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=3)
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="PAID",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    assert len(claimed) == 1
    await worker.process_event(claimed[0])

    inbox_state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", seed["inbox_id"]
    )
    assert inbox_state == "processed"

    mv = await admin_conn.fetchrow(
        "SELECT movement_type, delta, yield_factor_applied"
        " FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert mv is not None, "Expected a sale_depletion movement"
    assert mv["movement_type"] == "sale_depletion"
    # delta = -(sale_qty * recipe_qty / factor) = -(3 * 2.0 / 1.0) = -6
    assert Decimal(str(mv["delta"])) == Decimal("-6")
    assert mv["yield_factor_applied"] is not None


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.2 — delta formula: sale_qty * recipe_qty / storage_to_recipe_factor
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_2_delta_formula(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    # factor = 4.0 → stored in storage units, recipe in recipe units
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted", factor=4.0)
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 3.0))  # recipe_qty = 3
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=2)  # sale_qty = 2
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    delta = await admin_conn.fetchval(
        "SELECT delta FROM inventory_movements WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    # expected = -(2 * 3.0 / 4.0) = -1.5
    assert Decimal(str(delta)) == Decimal("-1.5")


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.3 — Mode B item writes sale_signal (not sale_depletion)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_3_mode_b_writes_signal(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="count_anchored")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 1.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=1)
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    mv_type = await admin_conn.fetchval(
        "SELECT movement_type FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert mv_type == "sale_signal"


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.4 — recipe_version_id frozen at insert; later FK change has no effect
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_4_recipe_version_frozen(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 5.0))

    # Also create a second recipe version with a different qty
    inv_id2 = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id2 = await _seed_recipe(admin_conn, tid, (inv_id2, 99.0))

    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    menu_item_id = await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=1)
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    # Verify recipe_version_id was snapshotted on sale_line_items
    sli_rv = await admin_conn.fetchval(
        "SELECT recipe_version_id FROM sale_line_items WHERE tenant_id = $1",
        uuid.UUID(tid),
    )
    assert str(sli_rv) == rv_id, "recipe_version_id should be frozen at original rv_id"

    # Now change the FK on menu_items to point to rv_id2 (different recipe)
    await admin_conn.execute(
        "UPDATE menu_items SET recipe_version_id = $1 WHERE id = $2",
        uuid.UUID(rv_id2),
        uuid.UUID(menu_item_id),
    )

    # The movement that was already written is still based on the original recipe
    delta = await admin_conn.fetchval(
        "SELECT delta FROM inventory_movements WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert Decimal(str(delta)) == Decimal("-5"), "Historical movement must not be affected by recipe FK change"

    # And inv_id2 (the new recipe's item) has no movement
    mv2_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id2),
    )
    assert mv2_count == 0


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.5 — Replay: duplicate inbox event writes exactly one movement
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_5_replay_idempotent(admin_conn):
    from tests.helpers.phase7 import seed_inbox_event
    import time

    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    order_id = seed["vendor_event_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 1.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=2)
    clover_order = make_clover_order(
        order_id=order_id,
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{order_id}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()

    # First processing
    claimed1 = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed1[0])

    # Seed a second inbox event for the same order (replay)
    await seed_inbox_event(
        admin_conn,
        seed,
        vendor_event_id=order_id,
        vendor_ts=int(time.time() * 1000) + 5000,
    )

    # Second processing
    claimed2 = await worker.claim_batch(batch_size=1)
    if claimed2:
        await worker.process_event(claimed2[0])

    # Exactly one inventory movement
    mv_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements"
        " WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert mv_count == 1, f"Expected 1 movement after replay, got {mv_count}"


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.6 — Refunded line item: no inventory_movement written
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_6_refunded_line_no_movement(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 1.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = {**_make_li_with_item(pos_item_id), "refunded": True}
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    mv_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert mv_count == 0, "Refunded line item must not write a depletion"


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.7 — Voided (exchanged) line item: no inventory_movement written
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_7_voided_line_no_movement(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 1.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = {**_make_li_with_item(pos_item_id), "exchanged": True}
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    mv_count = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id = $1 AND inventory_item_id = $2",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert mv_count == 0, "Voided line item must not write a depletion"


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.8 — No recipe link: inbox processed cleanly, no movements
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_8_no_recipe_no_movement(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    # menu_item exists but has no recipe_version_id
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=None)

    li = _make_li_with_item(pos_item_id)
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    before = await admin_conn.fetchval("SELECT COUNT(*) FROM inventory_movements")

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    after = await admin_conn.fetchval("SELECT COUNT(*) FROM inventory_movements")
    assert after == before

    inbox_state = await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", seed["inbox_id"]
    )
    assert inbox_state == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# E2E.9 — Multi-ingredient recipe: one movement per ingredient
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_9_multi_ingredient(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]

    uom_id = await _seed_uom(admin_conn, tid)
    inv_a = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    inv_b = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    # One recipe with two ingredients
    rv_id = await _seed_recipe(admin_conn, tid, (inv_a, 1.0), (inv_b, 2.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = _make_li_with_item(pos_item_id, qty=1)
    clover_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=clover_order)
    )

    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    await worker.process_event(claimed[0])

    rows = await admin_conn.fetch(
        "SELECT inventory_item_id, delta FROM inventory_movements"
        " WHERE tenant_id = $1 ORDER BY delta",
        uuid.UUID(tid),
    )
    assert len(rows) == 2, f"Expected 2 movements for 2-ingredient recipe, got {len(rows)}"

    deltas = {str(r["inventory_item_id"]): Decimal(str(r["delta"])) for r in rows}
    assert deltas[inv_a] == Decimal("-1")
    assert deltas[inv_b] == Decimal("-2")
