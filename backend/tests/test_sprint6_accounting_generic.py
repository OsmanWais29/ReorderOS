"""Genericity proof for product-adjustment accounting (no Lauzon-specific behavior).

Every DB test here drives the PRODUCTION services (update_line, commit_receipt)
with vendors/products/units/amounts unrelated to any live invoice. The formula
tests target the ONE production function (compute_line_cost_basis) — no test
re-implements the arithmetic beyond stating expected outputs as literals.

Zero-net policy (case J, decided + documented here): a net cost of exactly $0
is BLOCKED for manual review, same as negative — genuinely free promotional
stock should be entered with a $0 price on the line itself, not synthesized by
adjustment links, because a link that happens to zero the line is far more
often a mis-link than a real giveaway.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import AsyncIterator
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.services import (
    ReceiptNetCostInvalid,
    commit_receipt,
    compute_line_cost_basis,
)
from app.modules.receipts.schemas import LineUpdate
from app.modules.receipts.services import (
    AdjustmentLinkInvalid,
    ReceiptImmutable,
    reset_extraction,
    update_line,
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


# ── generic seed: arbitrary vendor/product/unit, one confirmed item line ──────


async def _tenant(db: Any) -> uuid.UUID:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'GEN')"),
        {"id": tid, "s": f"gen-{tid.hex[:8]}"},
    )
    return tid


async def _receipt(
    db: Any,
    tid: uuid.UUID,
    *,
    item_name: str,
    storage_unit: str,
    unit_type: str,
    invoice_qty: str,
    invoice_unit: str,
    received_qty: str,
    gross_cents: int,
    supplier: str = "Vendor",
) -> dict[str, Any]:
    uom = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, :n, :n, :ut) RETURNING id"
            ),
            {"t": tid, "n": storage_unit, "ut": unit_type},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, "
                "storage_unit_id, recipe_unit_id) "
                "VALUES (:t, :n, 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "n": item_name, "u": uom},
        )
    ).scalar_one()
    rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source, supplier_name) "
                "VALUES (:t, 'draft', 'email', :s) RETURNING id"
            ),
            {"t": tid, "s": supplier},
        )
    ).scalar_one()
    # Identity when invoice unit == storage unit; otherwise a confirmed conversion.
    identity = invoice_unit == storage_unit
    line = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     received_unit, conversion_factor, conversion_confirmed_at,
                     purchase_quantity, purchase_unit, extracted_unit, extracted_name,
                     match_status, line_total_cents)
                VALUES (:t, :r, :i, :rq,
                        CASE WHEN :ident THEN NULL ELSE :su END,
                        CASE WHEN :ident THEN NULL
                             ELSE CAST(:rq AS numeric) / CAST(:iq AS numeric) END,
                        CASE WHEN :ident THEN NULL ELSE now() END,
                        CASE WHEN :ident THEN NULL ELSE CAST(:iq AS numeric) END,
                        CASE WHEN :ident THEN NULL ELSE :iu END,
                        :iu, :n, 'matched', :g)
                RETURNING id
            """),
            {
                "t": tid,
                "r": rid,
                "i": item,
                "rq": Decimal(received_qty),
                "iq": Decimal(invoice_qty),
                "su": storage_unit,
                "iu": invoice_unit,
                "ident": identity,
                "n": f"{item_name} INVOICE ROW",
                "g": gross_cents,
            },
        )
    ).scalar_one()
    return {"tid": tid, "rid": rid, "item": item, "line": line}


async def _nonstock(
    db: Any, s: dict[str, Any], ltype: str, cents: int | None, name: str = "ADJ ROW"
) -> uuid.UUID:
    return (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, line_type, match_status, extracted_name,
                     line_total_cents)
                VALUES (:t, :r, :lt, 'skipped', :n, :c) RETURNING id
            """),
            {"t": s["tid"], "r": s["rid"], "lt": ltype, "n": name, "c": cents},
        )
    ).scalar_one()


async def _link(db: Any, s: dict[str, Any], src: uuid.UUID, target: uuid.UUID | None) -> None:
    await update_line(
        db,
        tenant_id=s["tid"],
        receipt_id=s["rid"],
        line_id=src,
        patch=LineUpdate(adjusts_line_id=target),
    )


async def _commit(db: Any, s: dict[str, Any]) -> dict[str, Any]:
    return await commit_receipt(
        db, tenant_id=s["tid"], receipt_id=s["rid"], confirm=True, reviewed_affirmation=True
    )


async def _snapshot(db: Any, s: dict[str, Any]) -> tuple[int, Decimal] | None:
    row = (
        await db.execute(
            text(
                "SELECT unit_cost_cents, unit_cost_cents_exact FROM ingredient_cost_snapshots "
                "WHERE tenant_id = :t AND inventory_item_id = :i"
            ),
            {"t": s["tid"], "i": s["item"]},
        )
    ).fetchone()
    return (row[0], Decimal(str(row[1]))) if row else None


# ── 3. parameterized table — unrelated vendors/products/units/amounts ─────────

_CASES = [
    # (name, storage_unit/type, invoice qty+unit, received qty, gross,
    #  linked adjustments, expected exact ¢/storage-unit)
    ("A widgets", "ea", "count", "10", "ea", "10", 10_000, [-1_000], "900.0000"),
    ("B kombucha", "L", "volume", "3", "CS", "48", 8_244, [-244], "166.6667"),
    ("C brisket", "kg", "weight", "12", "kg", "12", 24_600, [-600, -1_200], "1900.0000"),
]


@pytest.mark.parametrize(
    ("label", "su", "ut", "iq", "iu", "rq", "gross", "adjs", "expected"),
    [(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8]) for c in _CASES],
)
async def test_table_driven_net_costs(
    db: Any,
    label: str,
    su: str,
    ut: str,
    iq: str,
    iu: str,
    rq: str,
    gross: int,
    adjs: list[int],
    expected: str,
) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name=f"ITEM {label}",
        storage_unit=su,
        unit_type=ut,
        invoice_qty=iq,
        invoice_unit=iu,
        received_qty=rq,
        gross_cents=gross,
        supplier="Acme Provisions",
    )
    for cents in adjs:
        ltype = "discount" if cents < 0 else "credit"
        row = await _nonstock(db, s, ltype, cents)
        await _link(db, s, row, s["line"])
    await _commit(db, s)
    snap = await _snapshot(db, s)
    assert snap is not None
    assert snap[1] == Decimal(expected)


async def test_case_d_unlinked_discount_never_allocates(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="NAPKINS",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="24",
        invoice_unit="ea",
        received_qty="24",
        gross_cents=12_000,
    )
    await _nonstock(db, s, "discount", -2_000)  # present but NEVER linked
    await _commit(db, s)
    snap = await _snapshot(db, s)
    assert snap is not None and snap[1] == Decimal("500.0000")  # $5/ea, gross only


async def test_case_e_deposit_link_rejected(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="SODA",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="6",
        invoice_unit="ea",
        received_qty="6",
        gross_cents=6_000,
    )
    deposit = await _nonstock(db, s, "fee_or_deposit", 500)
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, deposit, s["line"])


async def test_case_f_cross_tenant_target_invisible(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="FLOUR",
        storage_unit="kg",
        unit_type="weight",
        invoice_qty="5",
        invoice_unit="kg",
        received_qty="5",
        gross_cents=2_000,
    )
    other = await _receipt(
        db,
        await _tenant(db),
        item_name="OTHER TENANT SECRET",
        storage_unit="kg",
        unit_type="weight",
        invoice_qty="5",
        invoice_unit="kg",
        received_qty="5",
        gross_cents=2_000,
    )
    disc = await _nonstock(db, s, "discount", -100)
    with pytest.raises(AdjustmentLinkInvalid) as exc_info:
        await _link(db, s, disc, other["line"])
    assert "OTHER TENANT SECRET" not in str(exc_info.value)


async def test_case_g_cross_receipt_same_tenant_rejected(db: Any) -> None:
    tid = await _tenant(db)
    s1 = await _receipt(
        db,
        tid,
        item_name="RICE",
        storage_unit="kg",
        unit_type="weight",
        invoice_qty="10",
        invoice_unit="kg",
        received_qty="10",
        gross_cents=3_000,
    )
    other_rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source) "
                "VALUES (:t, 'draft', 'manual') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    foreign_line = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, received_quantity, extracted_name, match_status)
                VALUES (:t, :r, 1, 'ELSEWHERE', 'matched') RETURNING id
            """),
            {"t": tid, "r": other_rid},
        )
    ).scalar_one()
    disc = await _nonstock(db, s1, "discount", -100)
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s1, disc, foreign_line)


async def test_case_h_bad_targets_rejected_cleanly(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="BEANS",
        storage_unit="kg",
        unit_type="weight",
        invoice_qty="4",
        invoice_unit="kg",
        received_qty="4",
        gross_cents=1_600,
    )
    disc = await _nonstock(db, s, "discount", -100)
    other_adj = await _nonstock(db, s, "credit", -50)
    # → another adjustment
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, disc, other_adj)
    # → itself
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, disc, disc)
    # → an operator-SKIPPED item line
    skipped_item = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, received_quantity, extracted_name,
                     match_status, line_type)
                VALUES (:t, :r, 2, 'SKIPPED ITEM', 'skipped', 'item') RETURNING id
            """),
            {"t": s["tid"], "r": s["rid"]},
        )
    ).scalar_one()
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, disc, skipped_item)
    # → a deleted (nonexistent) line
    with pytest.raises(AdjustmentLinkInvalid):
        await _link(db, s, disc, uuid.uuid4())


async def test_case_i_negative_net_atomic_zero_writes(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="SAFFRON",
        storage_unit="g",
        unit_type="weight",
        invoice_qty="2",
        invoice_unit="g",
        received_qty="2",
        gross_cents=900,
    )
    credit = await _nonstock(db, s, "credit", -1_000)
    await _link(db, s, credit, s["line"])
    with pytest.raises(ReceiptNetCostInvalid):
        await _commit(db, s)
    for table in ("inventory_movements", "ingredient_cost_snapshots"):
        n = (
            await db.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                {"t": s["tid"]},
            )
        ).scalar_one()
        assert n == 0, table


async def test_case_j_exact_zero_net_blocked_for_manual_review(db: Any) -> None:
    """POLICY (decided): net == 0 is blocked, like negative. Free promotional
    stock is entered as a $0-priced line, never synthesized by adjustment links."""
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="SAMPLES",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="5",
        invoice_unit="ea",
        received_qty="5",
        gross_cents=1_000,
    )
    disc = await _nonstock(db, s, "discount", -1_000)  # exactly zero net
    await _link(db, s, disc, s["line"])
    with pytest.raises(ReceiptNetCostInvalid):
        await _commit(db, s)


# ── 4. property-based: 200 generated examples against THE production formula ──


def test_property_formula_200_examples() -> None:
    rng = random.Random(0x5EED)
    units_qty = [
        lambda r: Decimal(r.randint(1, 500)),  # counts
        lambda r: Decimal(r.randint(1, 500_000)) / 1000,  # weights/volumes, 3dp
        lambda r: Decimal(r.randint(1, 9_999)) / 100,  # 2dp quantities
    ]
    checked = 0
    for _ in range(200):
        gross = rng.randint(1, 5_000_000)  # positive gross cents
        adjs = [
            rng.randint(-100_000, 100_000)
            for _ in range(rng.randint(1, 5))  # 1..5 signed adjustments
        ]
        qty = rng.choice(units_qty)(rng)
        assert qty > 0
        net = gross + sum(adjs)
        result = compute_line_cost_basis(
            gross_total_cents=gross,
            adjustment_cents=sum(adjs),
            storage_qty=qty,
            fallback_unit_cost_cents=None,
        )
        assert result is not None
        cost_int, cost_exact = result
        expected_exact = (Decimal(net) / qty).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        assert cost_exact == expected_exact
        assert cost_int == int((Decimal(net) / qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        # Decimal end to end — never a binary float.
        assert isinstance(cost_exact, Decimal)
        checked += 1
    assert checked == 200


async def test_property_invalid_net_rejected_with_zero_writes(db: Any) -> None:
    """DB-level property sample: any generated net <= 0 must reject atomically."""
    rng = random.Random(0xBAD)
    for _ in range(5):
        gross = rng.randint(100, 5_000)
        overshoot = gross + rng.randint(0, 500)  # adjustment >= gross → net <= 0
        s = await _receipt(
            db,
            await _tenant(db),
            item_name=f"P{rng.randint(0, 999)}",
            storage_unit="ea",
            unit_type="count",
            invoice_qty="3",
            invoice_unit="ea",
            received_qty="3",
            gross_cents=gross,
        )
        credit = await _nonstock(db, s, "credit", -overshoot)
        await _link(db, s, credit, s["line"])
        with pytest.raises(ReceiptNetCostInvalid):
            await _commit(db, s)
        n = (
            await db.execute(
                text("SELECT count(*) FROM inventory_movements WHERE tenant_id = :t"),
                {"t": s["tid"]},
            )
        ).scalar_one()
        assert n == 0


# ── 5. metamorphic relations ──────────────────────────────────────────────────


async def _committed_exact(db: Any, s: dict[str, Any]) -> Decimal:
    await _commit(db, s)
    snap = await _snapshot(db, s)
    assert snap is not None
    return snap[1]


async def test_meta_split_discount_equals_single(db: Any) -> None:
    base = await _receipt(
        db,
        await _tenant(db),
        item_name="TEA",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="10",
        invoice_unit="ea",
        received_qty="10",
        gross_cents=10_000,
    )
    one = await _nonstock(db, base, "discount", -1_000)
    await _link(db, base, one, base["line"])
    single = await _committed_exact(db, base)

    split = await _receipt(
        db,
        await _tenant(db),
        item_name="TEA2",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="10",
        invoice_unit="ea",
        received_qty="10",
        gross_cents=10_000,
    )
    for cents in (-400, -600):
        row = await _nonstock(db, split, "discount", cents)
        await _link(db, split, row, split["line"])
    assert await _committed_exact(db, split) == single == Decimal("900.0000")


async def test_meta_row_order_irrelevant(db: Any) -> None:
    """Adjustment rows inserted BEFORE the item line (reversed physical order)
    produce the identical snapshot."""
    tid = await _tenant(db)
    uom = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'ea', 'ea', 'count') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, "
                "storage_unit_id, recipe_unit_id) "
                "VALUES (:t, 'CUPS', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "u": uom},
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
    s = {"tid": tid, "rid": rid, "item": item}
    disc = await _nonstock(db, s, "discount", -1_000)  # adjustment FIRST
    line = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     extracted_unit, extracted_name, match_status, line_total_cents)
                VALUES (:t, :r, :i, 10, 'ea', 'CUPS ROW', 'matched', 10000) RETURNING id
            """),
            {"t": tid, "r": rid, "i": item},
        )
    ).scalar_one()
    s["line"] = line
    await _link(db, s, disc, line)
    assert await _committed_exact(db, s) == Decimal("900.0000")


async def test_meta_link_delta_is_adjustment_over_quantity(db: Any) -> None:
    """Unlinked → no effect; linking changes cost by exactly adj/qty; doubling
    everything preserves unit cost."""
    plain = await _receipt(
        db,
        await _tenant(db),
        item_name="OIL",
        storage_unit="L",
        unit_type="volume",
        invoice_qty="8",
        invoice_unit="L",
        received_qty="8",
        gross_cents=6_400,
    )
    await _nonstock(db, plain, "discount", -800)  # unlinked
    unlinked_cost = await _committed_exact(db, plain)
    assert unlinked_cost == Decimal("800.0000")  # untouched gross $8/L

    linked = await _receipt(
        db,
        await _tenant(db),
        item_name="OIL2",
        storage_unit="L",
        unit_type="volume",
        invoice_qty="8",
        invoice_unit="L",
        received_qty="8",
        gross_cents=6_400,
    )
    row = await _nonstock(db, linked, "discount", -800)
    await _link(db, linked, row, linked["line"])
    linked_cost = await _committed_exact(db, linked)
    assert unlinked_cost - linked_cost == Decimal("-800") / Decimal("8") * -1  # 100 ¢/L

    doubled = await _receipt(
        db,
        await _tenant(db),
        item_name="OIL3",
        storage_unit="L",
        unit_type="volume",
        invoice_qty="16",
        invoice_unit="L",
        received_qty="16",
        gross_cents=12_800,
    )
    row2 = await _nonstock(db, doubled, "discount", -1_600)
    await _link(db, doubled, row2, doubled["line"])
    assert await _committed_exact(db, doubled) == linked_cost  # scale-invariant


async def test_meta_reset_cannot_retain_stale_link(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="JAM",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="4",
        invoice_unit="ea",
        received_qty="4",
        gross_cents=2_000,
    )
    disc = await _nonstock(db, s, "discount", -200)
    await _link(db, s, disc, s["line"])
    await reset_extraction(db, tenant_id=s["tid"], receipt_id=s["rid"], discard_edits=True)
    remaining = (
        await db.execute(
            text(
                "SELECT count(*) FROM receipt_lines "
                "WHERE receipt_id = :r AND adjusts_line_id IS NOT NULL"
            ),
            {"r": s["rid"]},
        )
    ).scalar_one()
    assert remaining == 0  # all lines (and therefore all links) are gone


# ── 7. lifecycle: committed receipts cannot be relinked ───────────────────────


async def test_committed_receipt_cannot_be_relinked(db: Any) -> None:
    s = await _receipt(
        db,
        await _tenant(db),
        item_name="HONEY",
        storage_unit="ea",
        unit_type="count",
        invoice_qty="2",
        invoice_unit="ea",
        received_qty="2",
        gross_cents=1_800,
    )
    disc = await _nonstock(db, s, "discount", -200)
    await _commit(db, s)
    with pytest.raises(ReceiptImmutable):
        await _link(db, s, disc, s["line"])
