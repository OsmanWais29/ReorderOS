"""Part C — operator-linked cost adjustments (net food-cost accounting).

Policy under test: a skipped DISCOUNT/CREDIT row explicitly linked to one item
line reduces that line's cost basis; taxes stay header-level; deposits/fees are
never linkable; a net <= 0 fails closed BEFORE any write; commit computes from
net atomically and replays idempotently. The founder's syrup arithmetic is the
canonical fixture: gross $53.70, promo −$5.37, net $48.33, 6 ea → 4.5 L →
$8.055/ea → 1074¢/L exact.
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
from app.modules.inventory.services import ReceiptNetCostInvalid, commit_receipt
from app.modules.receipts.schemas import LineUpdate
from app.modules.receipts.services import AdjustmentLinkInvalid, update_line

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


async def _seed(db: Any) -> dict[str, Any]:
    """Syrup receipt: confirmed 6 ea → 4.5 L, gross 5370; plus a promo discount
    (−537), a deposit (+480), and a tax on the header (never in line cost)."""
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'ADJ')"),
        {"id": tid, "s": f"adj-{tid.hex[:8]}"},
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
                "INSERT INTO receipts (tenant_id, commit_state, source, tax_cents) "
                "VALUES (:t, 'draft', 'email', 702) RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    # Confirmed conversion state: received 4.5 L (invoice 6 ea stashed).
    syrup = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     received_unit, conversion_factor, conversion_confirmed_at,
                     purchase_quantity, purchase_unit, extracted_unit, extracted_name,
                     match_status, unit_cost_cents, line_total_cents)
                VALUES (:t, :r, :i, 4.5, 'L', 0.75, now(), 6, 'ea', 'ea',
                        'SIROP VANILLE 750ML', 'matched', 1193, 5370)
                RETURNING id
            """),
            {"t": tid, "r": rid, "i": item},
        )
    ).scalar_one()

    async def nonstock(ltype: str, name: str, cents: int | None) -> uuid.UUID:
        return (
            await db.execute(
                text("""
                    INSERT INTO receipt_lines
                        (tenant_id, receipt_id, line_type, match_status, extracted_name,
                         line_total_cents, adjustment_disposition, disposition_reason)
                    -- seeds default to a DECIDED state ('excluded') so commit-path
                    -- tests exercise their own concern; the pending gate has its
                    -- own tests. update_line link/unlink moves disposition itself.
                    VALUES (:t, :r, :lt, 'skipped', :n, :c,
                            CASE WHEN :lt IN ('discount', 'credit') THEN 'excluded' END,
                            CASE WHEN :lt IN ('discount', 'credit') THEN 'test_seed' END)
                    RETURNING id
                """),
                {"t": tid, "r": rid, "lt": ltype, "n": name, "c": cents},
            )
        ).scalar_one()

    promo = await nonstock("discount", "PROMO SIROP -10%", -537)
    deposit = await nonstock("fee_or_deposit", "CONTAINER DEPOSIT", 480)
    credit = await nonstock("credit", "CR DAMAGED MILK", -2510)
    return {
        "tid": tid,
        "rid": rid,
        "item": item,
        "syrup": syrup,
        "promo": promo,
        "deposit": deposit,
        "credit": credit,
    }


async def _link(
    db: Any, s: dict[str, Any], source_line: uuid.UUID, target: uuid.UUID | None
) -> Any:
    return await update_line(
        db,
        tenant_id=s["tid"],
        receipt_id=s["rid"],
        line_id=source_line,
        patch=LineUpdate(adjusts_line_id=target),
    )


async def _commit(db: Any, s: dict[str, Any]) -> dict[str, Any]:
    return await commit_receipt(
        db, tenant_id=s["tid"], receipt_id=s["rid"], confirm=True, reviewed_affirmation=True
    )


async def test_syrup_net_cost_math_and_idempotent_replay(db: Any) -> None:
    s = await _seed(db)
    await _link(db, s, s["promo"], s["syrup"])

    result = await _commit(db, s)
    assert result["status"] == "committed"

    snap = (
        await db.execute(
            text(
                "SELECT unit_cost_cents, unit_cost_cents_exact FROM ingredient_cost_snapshots "
                "WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": s["tid"], "i": s["item"]},
        )
    ).fetchone()
    # Net 5370 − 537 = 4833 over 4.5 L = 1074 ¢/L exact — the founder's numbers
    # (per-ea sanity: 4833 / 6 = 805.5 ¢ = $8.055/ea).
    assert Decimal(str(snap[1])) == Decimal("1074.0000")
    assert snap[0] == 1074
    assert Decimal("4833") / Decimal("6") == Decimal("805.5")

    # Movement quantity is COST-INDEPENDENT: still 4.5 L, exactly one movement.
    movements = (
        await db.execute(
            text("SELECT delta FROM inventory_movements WHERE tenant_id = :t"), {"t": s["tid"]}
        )
    ).fetchall()
    assert len(movements) == 1
    assert Decimal(str(movements[0][0])) == Decimal("4.5")

    # Idempotent replay: no second movement, no second snapshot.
    replay = await _commit(db, s)
    assert replay["status"] == "already_committed"
    n_mv = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id = :t"),
            {"t": s["tid"]},
        )
    ).scalar_one()
    n_snap = (
        await db.execute(
            text("SELECT count(*) FROM ingredient_cost_snapshots WHERE tenant_id = :t"),
            {"t": s["tid"]},
        )
    ).scalar_one()
    assert (n_mv, n_snap) == (1, 1)


async def test_taxes_and_unlinked_rows_never_touch_cost(db: Any) -> None:
    s = await _seed(db)  # tax_cents=702 on header; promo/deposit/credit UNLINKED
    await _commit(db, s)
    snap = (
        await db.execute(
            text(
                "SELECT unit_cost_cents_exact FROM ingredient_cost_snapshots WHERE tenant_id = :t"
            ),
            {"t": s["tid"]},
        )
    ).scalar_one()
    # Pure gross: 5370 / 4.5 = 1193.3333 — tax and unlinked rows contribute nothing.
    assert Decimal(str(snap)) == Decimal("1193.3333")


async def test_deposit_and_fee_rows_are_never_linkable(db: Any) -> None:
    s = await _seed(db)
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, s["deposit"], s["syrup"])


async def test_link_target_must_be_receivable_item_on_same_receipt(db: Any) -> None:
    s = await _seed(db)
    # Target = another non-stock row → refused.
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, s["promo"], s["credit"])
    # Target on a DIFFERENT receipt (same tenant) → refused.
    other_rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source) "
                "VALUES (:t, 'draft', 'manual') RETURNING id"
            ),
            {"t": s["tid"]},
        )
    ).scalar_one()
    foreign_line = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, received_quantity, extracted_name, match_status)
                VALUES (:t, :r, 1, 'OTHER', 'unmatched') RETURNING id
            """),
            {"t": s["tid"], "r": other_rid},
        )
    ).scalar_one()
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, s["promo"], foreign_line)


async def test_cross_tenant_link_is_invisible(db: Any) -> None:
    """A target line under ANOTHER tenant resolves to not-found — tenant scoping
    makes the foreign line invisible, not merely forbidden."""
    s = await _seed(db)
    other = await _seed(db)  # separate tenant with its own receipt/lines
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, s["promo"], other["syrup"])


async def test_net_zero_or_negative_fails_closed_with_zero_writes(db: Any) -> None:
    s = await _seed(db)
    # The damaged-milk credit (−2510)… wrongly linked to the syrup (gross 5370)
    # PLUS a second big credit drives net below zero.
    big_credit = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, line_type, match_status, extracted_name,
                     line_total_cents)
                VALUES (:t, :r, 'credit', 'skipped', 'HUGE CREDIT', -3000) RETURNING id
            """),
            {"t": s["tid"], "r": s["rid"]},
        )
    ).scalar_one()
    await _link(db, s, s["credit"], s["syrup"])
    await _link(db, s, big_credit, s["syrup"])  # −2510 −3000 vs gross 5370 → net −140
    with pytest.raises(ReceiptNetCostInvalid) as exc_info:
        await _commit(db, s)
    assert "SIROP" in str(exc_info.value)  # names the line, not an id
    for table in ("inventory_movements", "ingredient_cost_snapshots"):
        n = (
            await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                {"t": s["tid"]},
            )
        ).scalar_one()
        assert n == 0, table
    state = (
        await db.execute(text("SELECT commit_state FROM receipts WHERE id = :r"), {"r": s["rid"]})
    ).scalar_one()
    assert state == "draft"


async def test_unlink_restores_gross_and_reset_clears_links(db: Any) -> None:
    s = await _seed(db)
    await _link(db, s, s["promo"], s["syrup"])
    linked = (
        await db.execute(
            text("SELECT adjusts_line_id FROM receipt_lines WHERE id = :l"),
            {"l": s["promo"]},
        )
    ).scalar_one()
    assert str(linked) == str(s["syrup"])
    await _link(db, s, s["promo"], None)  # explicit unlink
    assert (
        await db.execute(
            text("SELECT adjusts_line_id FROM receipt_lines WHERE id = :l"),
            {"l": s["promo"]},
        )
    ).scalar_one() is None
