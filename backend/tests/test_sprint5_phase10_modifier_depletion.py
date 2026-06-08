"""Sprint 5 Phase 10 — modifier depletion (walk_modifiers + handler integration).

Handler-level tests against seeded data (bound-transaction harness). The conditions are
chosen to be SENSITIVE to the distinguishing behavior, the throughline of the sprint:
  - multiplier ≠ 1 ("Extra shot ×2") so the multiplier is actually exercised, not
    coincidentally-correct at ×1;
  - a FORCED shared inventory item (base AND modifier deplete the same item) so a
    key-collision bug would actually show — two distinct-key rows, both applied;
  - a modifier conversion failure, to prove base+modifier are ONE atomic line (whole
    line fails, base movements NOT written);
  - modifier movement crossing a count boundary fires late-signal (per-movement, source-
    agnostic).
The real-worker extraction (pointer-based confirmed-additive) is covered e2e.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from tests.helpers.sprint5 import seed_recipe_version_session


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


async def _uom(db: AsyncSession, tid: str, name: str) -> str:
    return str(
        (
            await db.execute(
                text(
                    "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
                    " VALUES (:t,:n,:n,'weight')"
                    " ON CONFLICT (tenant_id, name) DO UPDATE SET name=EXCLUDED.name RETURNING id"
                ),
                {"t": tid, "n": name},
            )
        ).scalar_one()
    )


async def _item(db: AsyncSession, tid: str, uom_id: str) -> str:
    return str(
        (
            await db.execute(
                text("""
                    INSERT INTO inventory_items
                        (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                         storage_to_recipe_factor)
                    VALUES (:t, :n, 'recipe_deducted', :su, :su, 1.0) RETURNING id
                """),
                {"t": tid, "n": f"it-{uuid.uuid4().hex[:6]}", "su": uom_id},
            )
        ).scalar_one()
    )


async def _seed_base(db: AsyncSession, *, recipe_qty: float = 2, line_qty: float = 3) -> dict[str, Any]:
    tid = str(uuid7())
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:t,'T',:s)"),
        {"t": tid, "s": f"t-{uuid.uuid4().hex[:8]}"},
    )
    uom = await _uom(db, tid, "g")
    base_item = await _item(db, tid, uom)
    seeded = await seed_recipe_version_session(
        db, tid, ingredients=[(base_item, recipe_qty, "g")], yield_quantity=1.0, status="confirmed"
    )
    inbox_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO pos_event_inbox
            (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
             vendor_event_type, vendor_ts, raw_payload, signature_verified, source)
            VALUES (:i,:t,'clover','O:p10','O','UPDATE',0,'{}',false,'webhook')
        """),
        {"i": inbox_id, "t": tid},
    )
    order_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id,
                total_amount_cents, state, payment_state, processed_at)
            VALUES (:o,:t,:i,'p10',0,'locked','PAID',now())
        """),
        {"o": order_id, "t": tid, "i": inbox_id},
    )
    sli_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO sale_line_items
            (id, tenant_id, order_id, clover_line_item_id, menu_item_id, name_at_sale,
             quantity, price_cents_at_sale, net_revenue_cents, recipe_version_id, depletion_status)
            VALUES (:id,:t,:o,:cli,:mid,'Item',:q,0,0,:rv,'pending')
        """),
        {"id": sli_id, "t": tid, "o": order_id, "cli": f"cli_{uuid.uuid4().hex[:8]}",
         "mid": seeded.menu_item_id, "q": line_qty, "rv": seeded.recipe_version_id},
    )
    await db.flush()
    return {"tid": tid, "sli_id": sli_id, "base_item": base_item, "uom": uom,
            "menu_item_id": seeded.menu_item_id}


async def _add_confirmed_modifier(
    db: AsyncSession, tid: str, menu_item_id: str, sli_id: str,
    *, item_id: str, mod_qty: float, multiplier: float, unit: str = "g",
) -> str:
    """Confirmed additive modifier + version + ingredient + a frozen slim row. Returns the
    modifier's inventory item id. Mirrors what the worker's _snapshot_modifiers produces."""
    mod_id = str(
        (
            await db.execute(
                text("""
                    INSERT INTO modifiers (tenant_id, menu_item_id, name, modifier_type, status)
                    VALUES (:t,:m,:n,'additive','confirmed') RETURNING id
                """),
                {"t": tid, "m": menu_item_id, "n": f"mod-{uuid.uuid4().hex[:5]}"},
            )
        ).scalar_one()
    )
    mv_id = str(
        (
            await db.execute(
                text("""
                    INSERT INTO modifier_versions (tenant_id, modifier_id, version_number, yield_quantity)
                    VALUES (:t,:mod,1,1.0) RETURNING id
                """),
                {"t": tid, "mod": mod_id},
            )
        ).scalar_one()
    )
    await db.execute(
        text("UPDATE modifiers SET current_version_id=:mv WHERE id=:mod"),
        {"mv": mv_id, "mod": mod_id},
    )
    await db.execute(
        text("""
            INSERT INTO modifier_ingredients (tenant_id, modifier_version_id, inventory_item_id, quantity, unit)
            VALUES (:t,:mv,:iid,:q,:u)
        """),
        {"t": tid, "mv": mv_id, "iid": item_id, "q": mod_qty, "u": unit},
    )
    await db.execute(
        text("""
            INSERT INTO sale_line_item_modifiers
                (id, tenant_id, sale_line_item_id, modifier_id, modifier_version_id, quantity, pos_modifier_id)
            VALUES (gen_random_uuid(), :t, :sli, :mod, :mv, :mult, :pid)
        """),
        {"t": tid, "sli": sli_id, "mod": mod_id, "mv": mv_id, "mult": multiplier,
         "pid": f"pos_{uuid.uuid4().hex[:6]}"},
    )
    await db.flush()
    return item_id


async def _mv(db: AsyncSession, tid: str, item_id: str) -> list[Any]:
    return (
        await db.execute(
            text(
                "SELECT delta, idempotency_key, source_type FROM inventory_movements"
                " WHERE tenant_id=:t AND inventory_item_id=:i ORDER BY idempotency_key"
            ),
            {"t": tid, "i": item_id},
        )
    ).mappings().all()


# ── multiplier ≠ 1 ───────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_modifier_multiplier_applied(db) -> None:
    """'Extra shot ×2': modifier delta = -(line_qty * multiplier * mod_qty / yield)."""
    s = await _seed_base(db, recipe_qty=2, line_qty=3)
    mod_item = await _item(db, s["tid"], s["uom"])  # separate item from base
    await _add_confirmed_modifier(
        db, s["tid"], s["menu_item_id"], s["sli_id"], item_id=mod_item, mod_qty=5, multiplier=2,
    )
    status, _ = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert status == "depleted"
    mvs = await _mv(db, s["tid"], mod_item)
    assert len(mvs) == 1
    assert mvs[0]["source_type"] == "sale_line_item_modifier"
    assert Decimal(str(mvs[0]["delta"])) == Decimal("-30")  # -(3 * 2 * 5 / 1)
    assert ":modifier:" in mvs[0]["idempotency_key"]


# ── forced shared item: base + modifier, distinct keys, both apply ───────────


@pytest.mark.integration
async def test_shared_item_base_and_modifier_distinct_rows_both_apply(db) -> None:
    """Base AND modifier deplete the SAME item → two DISTINCT-key rows (base vs modifier),
    no collision, both applied (a different-item test couldn't detect a key collision)."""
    s = await _seed_base(db, recipe_qty=2, line_qty=3)  # base delta = -(3*2/1) = -6
    await _add_confirmed_modifier(
        db, s["tid"], s["menu_item_id"], s["sli_id"],
        item_id=s["base_item"], mod_qty=1, multiplier=1,  # modifier delta = -(3*1*1/1) = -3
    )
    status, _ = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert status == "depleted"
    mvs = await _mv(db, s["tid"], s["base_item"])
    assert len(mvs) == 2  # one base row, one modifier row — NOT one collided row
    keys = {m["idempotency_key"] for m in mvs}
    assert any(":base:" in k for k in keys) and any(":modifier:" in k for k in keys)
    total = sum(Decimal(str(m["delta"])) for m in mvs)
    assert total == Decimal("-9")  # both apply: -6 (base) + -3 (modifier)


# ── modifier conversion failure fails the WHOLE line (atomic base+modifier) ──


@pytest.mark.integration
async def test_modifier_conversion_failure_fails_whole_line(db) -> None:
    """Base walks fine, but a modifier ingredient needs an absent conversion → the WHOLE
    line is failed/missing_conversion with ZERO movements (base included). Proves base+
    modifier are one atomic unit — never base-depleted-while-modifier-failed."""
    s = await _seed_base(db, recipe_qty=2, line_qty=3)
    mod_item = await _item(db, s["tid"], s["uom"])  # storage 'g'
    await _add_confirmed_modifier(
        db, s["tid"], s["menu_item_id"], s["sli_id"],
        item_id=mod_item, mod_qty=1, multiplier=1, unit="ml",  # ml→g cross-dimension, no density
    )
    status, reason = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, reason) == ("failed", "missing_conversion")
    assert await _mv(db, s["tid"], s["base_item"]) == []  # base NOT written
    assert await _mv(db, s["tid"], mod_item) == []


# ── modifier late-signal + replay ────────────────────────────────────────────


@pytest.mark.integration
async def test_modifier_movement_fires_late_signal(db) -> None:
    s = await _seed_base(db, recipe_qty=2, line_qty=3)
    mod_item = await _item(db, s["tid"], s["uom"])
    await _add_confirmed_modifier(
        db, s["tid"], s["menu_item_id"], s["sli_id"], item_id=mod_item, mod_qty=1, multiplier=1,
    )
    # count boundary at ≈ now on the modifier's item
    await db.execute(
        text("INSERT INTO inventory_count_events (tenant_id, inventory_item_id, counted_quantity)"
             " VALUES (:t,:i,1)"),
        {"t": s["tid"], "i": mod_item},
    )
    late = datetime.now(UTC) - timedelta(hours=2)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]), recorded_at=late)
    alert = (
        await db.execute(
            text("SELECT severity FROM monitoring_alerts WHERE tenant_id=:t"
                 " AND monitor_name='late_signal_reconciliation' AND resolved_at IS NULL"),
            {"t": s["tid"]},
        )
    ).scalar()
    assert alert == "warn"  # a modifier depletion crossing a boundary is alert-worthy too


@pytest.mark.integration
async def test_modifier_replay_no_duplicate(db) -> None:
    s = await _seed_base(db, recipe_qty=2, line_qty=3)
    mod_item = await _item(db, s["tid"], s["uom"])
    await _add_confirmed_modifier(
        db, s["tid"], s["menu_item_id"], s["sli_id"], item_id=mod_item, mod_qty=1, multiplier=2,
    )
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))  # replay
    assert len(await _mv(db, s["tid"], mod_item)) == 1  # distinct key + ON CONFLICT
