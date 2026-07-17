"""Structured conversion-error contract (live-cert fix: raw 'no conversion path:
ea -> L (inventory_item_id=..., tenant_id=...)' reached the UI).

Proves: canonical-cross-dimension lines (ea → L) are caught by the PRE-write
gate with ALL blockers in one structured payload; tenant-safe item names; no
UUIDs/internal terms in any user-facing message; zero movements/snapshots on
failure; the accept-suggestion fix path (1 ea = 0.75 L → 6 ea = 4.5 L) unblocks
commit; replay stays idempotent.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.services import ReceiptConversionRequired, commit_receipt
from app.modules.receipts.schemas import LineUpdate
from app.modules.receipts.services import update_line

pytestmark = pytest.mark.integration

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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


async def _seed_sirop(db: Any) -> dict[str, Any]:
    """The live-cert shape: SIROP VANILLE tracked in L, invoice line 6 ea with a
    750 ml package hint."""
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'CV')"),
        {"id": tid, "s": f"cv-{tid.hex[:8]}"},
    )
    litre = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'L', 'L', 'volume') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, "
                "storage_unit_id, recipe_unit_id) "
                "VALUES (:t, 'SIROP VANILLE', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "u": litre},
        )
    ).scalar_one()
    rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source) "
                "VALUES (:t, 'draft', 'email') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    line = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     extracted_unit, extracted_name, match_status, unit_cost_cents,
                     line_total_cents, pack_size_qty, pack_size_unit)
                VALUES (:t, :r, :i, 6, 'ea', 'SIROP VANILLE 750ML', 'matched', 895,
                        5370, 750, 'ml')
                RETURNING id
            """),
            {"t": tid, "r": rid, "i": item},
        )
    ).scalar_one()
    return {"tid": tid, "rid": rid, "item": item, "line": line, "litre": litre}


async def _commit(db: Any, s: dict[str, Any]) -> dict[str, Any]:
    return await commit_receipt(
        db, tenant_id=s["tid"], receipt_id=s["rid"], confirm=True, reviewed_affirmation=True
    )


async def test_ea_to_litre_returns_structured_error(db: Any) -> None:
    s = await _seed_sirop(db)
    with pytest.raises(ReceiptConversionRequired) as exc_info:
        await _commit(db, s)
    exc = exc_info.value
    # Message: user-safe — no UUIDs, no internal vocabulary.
    assert not _UUID_RE.search(str(exc))
    assert "conversion path" not in str(exc)
    assert "tenant" not in str(exc).lower()
    # Structured payload identifies the exact line + tenant-owned item name.
    assert len(exc.errors) == 1
    e = exc.errors[0]
    assert e["receipt_line_id"] == str(s["line"])
    assert e["inventory_item_id"] == str(s["item"])
    assert e["inventory_item_name"] == "SIROP VANILLE"
    assert e["invoice_name"] == "SIROP VANILLE 750ML"
    assert e["purchase_quantity"] == "6"
    assert e["purchase_unit"] == "ea"
    assert e["storage_unit"] == "L"
    assert e["package_hint"] == "750 ml"
    assert Decimal(e["suggested_factor"]) == Decimal("0.75")
    assert Decimal(e["suggested_received_quantity"]) == Decimal("4.5")


async def test_multiple_blockers_returned_together_and_nothing_written(db: Any) -> None:
    s = await _seed_sirop(db)
    # Second failing line: classic non-canonical CS with no confirmed conversion.
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (tenant_id, receipt_id, inventory_item_id, received_quantity,
                 extracted_unit, extracted_name, match_status)
            VALUES (:t, :r, :i, 3, 'CS', 'LAIT 4X4L', 'matched')
        """),
        {"t": s["tid"], "r": s["rid"], "i": s["item"]},
    )
    with pytest.raises(ReceiptConversionRequired) as exc_info:
        await _commit(db, s)
    assert len(exc_info.value.errors) == 2  # ALL blockers in one response

    # Zero writes of any kind: no movements, no cost snapshots, receipt stays draft.
    for table in ("inventory_movements", "ingredient_cost_snapshots"):
        n = (
            await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                {"t": s["tid"]},
            )
        ).scalar_one()
        assert n == 0, table
    state = (
        await db.execute(
            text("SELECT commit_state, committed_at FROM receipts WHERE id = :r"),
            {"r": s["rid"]},
        )
    ).fetchone()
    assert state[0] == "draft" and state[1] is None


async def test_cross_tenant_item_name_never_leaks(db: Any) -> None:
    s = await _seed_sirop(db)
    other_tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'OT')"),
        {"id": other_tid, "s": f"ot-{other_tid.hex[:8]}"},
    )
    other_unit = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'kg', 'kg', 'weight') RETURNING id"
            ),
            {"t": other_tid},
        )
    ).scalar_one()
    foreign_item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, "
                "storage_unit_id, recipe_unit_id) "
                "VALUES (:t, 'FOREIGN SECRET ITEM', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": other_tid, "u": other_unit},
        )
    ).scalar_one()
    # Force the pathological linkage directly (the API's tenant-scoped item
    # validation forbids this path — the gate must STILL not leak).
    await db.execute(
        text("UPDATE receipt_lines SET inventory_item_id = :i WHERE id = :l"),
        {"i": foreign_item, "l": s["line"]},
    )
    with pytest.raises(ReceiptConversionRequired) as exc_info:
        await _commit(db, s)
    [e] = exc_info.value.errors
    assert e["inventory_item_name"] is None  # tenant-safe join: name never crosses
    assert "FOREIGN SECRET ITEM" not in str(exc_info.value)
    assert "FOREIGN SECRET ITEM" not in str(exc_info.value.errors)


async def test_accept_suggestion_unblocks_and_commit_is_idempotent(db: Any) -> None:
    s = await _seed_sirop(db)
    # The FE "Accept suggestion" PUT: 6 ea received as 4.5 L (1 ea = 0.75 L),
    # cost recomputed from the printed line total (5370 / 4.5 = 1193 ¢/L).
    await update_line(
        db,
        tenant_id=s["tid"],
        receipt_id=s["rid"],
        line_id=s["line"],
        patch=LineUpdate(
            received_quantity=4.5,
            received_unit="L",
            conversion_factor=0.75,
            unit_cost_cents=1193,
            remember_conversion=True,
        ),
    )
    row = (
        await db.execute(
            text(
                "SELECT received_quantity, received_unit, conversion_factor, "
                "purchase_quantity, purchase_unit FROM receipt_lines WHERE id = :l"
            ),
            {"l": s["line"]},
        )
    ).fetchone()
    assert Decimal(str(row[0])) == Decimal("4.5")
    assert row[1] == "L"
    assert Decimal(str(row[2])) == Decimal("0.75")
    assert Decimal(str(row[3])) == Decimal("6")  # invoice originals stashed
    assert row[4] == "ea"
    # Remembered factor persisted for next time (0028).
    remembered = (
        await db.execute(
            text(
                "SELECT factor FROM tenant_item_purchase_conversions "
                "WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": s["tid"], "i": s["item"]},
        )
    ).scalar_one()
    assert Decimal(str(remembered)) == Decimal("0.75")

    # The edit cleared the affirmation server-side — re-affirm, then receive.
    result = await _commit(db, s)
    assert result["status"] == "committed"
    movements = (
        await db.execute(
            text(
                "SELECT delta FROM inventory_movements "
                "WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": s["tid"], "i": s["item"]},
        )
    ).fetchall()
    assert len(movements) == 1
    assert Decimal(str(movements[0][0])) == Decimal("4.5")  # 6 ea became 4.5 L

    # Replay: idempotent no-op, movements unchanged.
    replay = await _commit(db, s)
    assert replay["status"] == "already_committed"
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id = :t"),
            {"t": s["tid"]},
        )
    ).scalar_one()
    assert n == 1
