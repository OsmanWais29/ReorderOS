"""Sprint 6 S5 — post-commit adjustments (append-only compensating movements).

Proves: the adjustment writes a receipt_adjustments row + a compensating
inventory_movement linked both ways; adjustment_type maps to the right movement_type;
the compensating delta flows into on-hand; the original committed lines/movements are
untouched (append-only, inventory_accounting_semantics §5); and adjust requires a
committed receipt.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.services import commit_receipt
from app.modules.receipts.services import (
    ReceiptNotCommitted,
    ReceiptNotFound,
    create_adjustment,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    connection: AsyncConnection
    async with engine.connect() as connection:
        await connection.begin()
        session = make_bound_session(connection)
        try:
            yield session
        finally:
            await connection.rollback()


async def _committed_receipt(db: Any) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a tenant/item and a COMMITTED manual receipt with one received line."""
    tid = uuid.uuid4()
    uid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'S5')"),
        {"id": tid, "s": f"s5-{tid.hex[:8]}"},
    )
    await db.execute(
        text("INSERT INTO users (id, workos_id, email) VALUES (:id, :w, :e)"),
        {"id": uid, "w": f"w_{uid.hex[:8]}", "e": f"{uid.hex[:8]}@t.com"},
    )
    uom = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t,'g','g','weight') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id,name,inventory_mode,storage_unit_id,"
                "recipe_unit_id) VALUES (:t,'Flour','recipe_deducted',:u,:u) RETURNING id"
            ),
            {"t": tid, "u": uom},
        )
    ).scalar_one()
    rid = (
        await db.execute(
            text("INSERT INTO receipts (tenant_id,commit_state,source) VALUES (:t,'draft','manual') RETURNING id"),
            {"t": tid},
        )
    ).scalar_one()
    line_id = (
        await db.execute(
            text(
                "INSERT INTO receipt_lines (tenant_id,receipt_id,inventory_item_id,received_quantity,"
                "match_status,manually_corrected,idempotency_key) "
                "VALUES (:t,:r,:i,10,'matched',true,'rk') RETURNING id"
            ),
            {"t": tid, "r": rid, "i": item},
        )
    ).scalar_one()
    await commit_receipt(db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True)
    return tid, item, rid, line_id, uid


async def _on_hand(db: Any, tid: uuid.UUID, item: uuid.UUID) -> Decimal:
    val = (
        await db.execute(
            text(
                "SELECT COALESCE(SUM(delta),0) FROM inventory_movements "
                "WHERE tenant_id=:t AND inventory_item_id=:i "
                "AND movement_type NOT IN ('sale_signal','sale_signal_reversal')"
            ),
            {"t": tid, "i": item},
        )
    ).scalar_one()
    return Decimal(str(val))


async def test_adjustment_writes_linked_compensating_movement(db: Any) -> None:
    tid, item, rid, line_id, uid = await _committed_receipt(db)
    assert await _on_hand(db, tid, item) == Decimal("10")  # the receive

    result = await create_adjustment(
        db, tenant_id=tid, receipt_id=rid, adjustment_type="damage",
        inventory_item_id=item, delta_quantity=Decimal("-3"), delta_unit="g",
        reason="spillage", receipt_line_id=line_id, delta_cost_cents=90,
        created_by=uid,
    )
    assert result["movement_type"] == "waste"

    adj = (
        await db.execute(
            text("SELECT * FROM receipt_adjustments WHERE id=:a"),
            {"a": result["adjustment_id"]},
        )
    ).mappings().one()
    assert adj["compensating_movement_id"] == result["compensating_movement_id"]
    assert adj["receipt_line_id"] == line_id
    assert Decimal(str(adj["delta_quantity"])) == Decimal("-3")

    mv = (
        await db.execute(
            text("SELECT movement_type, delta, source_type, source_id, idempotency_key "
                 "FROM inventory_movements WHERE id=:m"),
            {"m": result["compensating_movement_id"]},
        )
    ).mappings().one()
    assert mv["movement_type"] == "waste"
    assert Decimal(str(mv["delta"])) == Decimal("-3")
    assert mv["source_type"] == "receipt_adjustment"
    assert mv["source_id"] == result["adjustment_id"]
    assert mv["idempotency_key"] == f"receipt_adjustment:{result['adjustment_id']}"

    # the compensating delta flows into on-hand
    assert await _on_hand(db, tid, item) == Decimal("7")


@pytest.mark.parametrize(
    ("atype", "expected"),
    [("correction", "adjustment"), ("count_fix", "count_adjust"),
     ("return", "waste"), ("damage", "waste")],
)
async def test_adjustment_type_maps_movement_type(db: Any, atype: str, expected: str) -> None:
    tid, item, rid, _line, uid = await _committed_receipt(db)
    result = await create_adjustment(
        db, tenant_id=tid, receipt_id=rid, adjustment_type=atype,
        inventory_item_id=item, delta_quantity=Decimal("-1"), delta_unit="g",
        reason=None, receipt_line_id=None, delta_cost_cents=None, created_by=uid,
    )
    assert result["movement_type"] == expected


async def test_append_only_original_untouched(db: Any) -> None:
    tid, item, rid, line_id, uid = await _committed_receipt(db)
    before = (
        await db.execute(
            text("SELECT delta FROM inventory_movements WHERE tenant_id=:t AND source_id=:l"),
            {"t": tid, "l": line_id},
        )
    ).scalar_one()
    await create_adjustment(
        db, tenant_id=tid, receipt_id=rid, adjustment_type="correction",
        inventory_item_id=item, delta_quantity=Decimal("2"), delta_unit="g",
        reason="recount", receipt_line_id=line_id, delta_cost_cents=None, created_by=uid,
    )
    after = (
        await db.execute(
            text("SELECT delta FROM inventory_movements WHERE tenant_id=:t AND source_id=:l"),
            {"t": tid, "l": line_id},
        )
    ).scalar_one()
    assert Decimal(str(before)) == Decimal(str(after)) == Decimal("10")  # original receive untouched
    # receipt still committed, lines intact
    assert (
        await db.execute(text("SELECT commit_state FROM receipts WHERE id=:r"), {"r": rid})
    ).scalar_one() == "committed"


async def test_adjust_requires_committed_receipt(db: Any) -> None:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'S5')"),
        {"id": tid, "s": f"s5-{tid.hex[:8]}"},
    )
    item = uuid.uuid4()
    rid = (
        await db.execute(
            text("INSERT INTO receipts (tenant_id,commit_state,source) VALUES (:t,'draft','manual') RETURNING id"),
            {"t": tid},
        )
    ).scalar_one()
    with pytest.raises(ReceiptNotCommitted):
        await create_adjustment(
            db, tenant_id=tid, receipt_id=rid, adjustment_type="correction",
            inventory_item_id=item, delta_quantity=Decimal("1"), delta_unit="g",
            reason=None, receipt_line_id=None, delta_cost_cents=None, created_by=uuid.uuid4(),
        )
    with pytest.raises(ReceiptNotFound):
        await create_adjustment(
            db, tenant_id=tid, receipt_id=uuid.uuid4(), adjustment_type="correction",
            inventory_item_id=item, delta_quantity=Decimal("1"), delta_unit="g",
            reason=None, receipt_line_id=None, delta_cost_cents=None, created_by=uuid.uuid4(),
        )
