"""Adjustment disposition (0033) — no discount/credit is ever silently ignored.

Gate-1 live failure: an extracted product discount was never linked, nothing
forced a decision, and commit proceeded at gross. Under test here:

- extracted discount/credit rows begin 'pending'; fees/deposits carry NULL;
- 'pending' (or legacy NULL on a linkable row) BLOCKS commit with a structured
  RECEIPT_ADJUSTMENTS_UNREVIEWED payload and ZERO writes;
- link → 'linked' (+reviewed_at/by); unlink → back to 'pending' (reviewed
  cleared); exclude → 'excluded' with a reason; exclude on a linked row clears
  the link atomically; every path is an explicit lone action;
- committed math: linked commits at net, excluded commits at gross;
- the DB CHECKs make linked ⇔ adjusts_line_id set a fact, not a convention.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.database import engine, make_bound_session
from app.modules.inventory.services import ReceiptAdjustmentsUnreviewed, commit_receipt
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


async def _seed(db: Any, *, disposition: str | None = "pending") -> dict[str, Any]:
    """One item line (4 ea @ gross 10000) + one discount (−1200) + one fee (+700).

    The discount's initial disposition is parameterized; None means a legacy row
    from before 0033 (must still block)."""
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'DISP')"),
        {"id": tid, "s": f"dsp-{tid.hex[:8]}"},
    )
    ea = (
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
                "VALUES (:t, 'OLIVE OIL', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "u": ea},
        )
    ).scalar_one()
    user = (
        await db.execute(
            text("INSERT INTO users (workos_id, email) VALUES (:w, :e) RETURNING id"),
            {"w": f"wos-{tid.hex[:12]}", "e": f"dsp-{tid.hex[:8]}@test.local"},
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
    oil = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     extracted_unit, extracted_name, match_status, unit_cost_cents,
                     line_total_cents)
                VALUES (:t, :r, :i, 4, 'ea', 'OLIVE OIL 4X3L', 'matched', 2500, 10000)
                RETURNING id
            """),
            {"t": tid, "r": rid, "i": item},
        )
    ).scalar_one()
    disc = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, line_type, match_status, extracted_name,
                     line_total_cents, adjustment_disposition)
                VALUES (:t, :r, 'discount', 'skipped', 'VOLUME DISCOUNT', -1200, :d)
                RETURNING id
            """),
            {"t": tid, "r": rid, "d": disposition},
        )
    ).scalar_one()
    fee = (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, line_type, match_status, extracted_name,
                     line_total_cents)
                VALUES (:t, :r, 'fee_or_deposit', 'skipped', 'FUEL SURCHARGE', 700)
                RETURNING id
            """),
            {"t": tid, "r": rid},
        )
    ).scalar_one()
    return {
        "tid": tid,
        "rid": rid,
        "item": item,
        "oil": oil,
        "disc": disc,
        "fee": fee,
        "user": user,
    }


async def _patch(db: Any, s: dict[str, Any], line: uuid.UUID, patch: LineUpdate) -> dict[str, Any]:
    return await update_line(
        db,
        tenant_id=s["tid"],
        receipt_id=s["rid"],
        line_id=line,
        patch=patch,
        confirmed_by=s["user"],
    )


async def _commit(db: Any, s: dict[str, Any]) -> dict[str, Any]:
    return await commit_receipt(
        db, tenant_id=s["tid"], receipt_id=s["rid"], confirm=True, reviewed_affirmation=True
    )


async def _disc_row(db: Any, s: dict[str, Any]) -> Any:
    return (
        (
            await db.execute(
                text(
                    "SELECT adjustment_disposition, adjusts_line_id, disposition_reason, "
                    "disposition_reviewed_at, disposition_reviewed_by "
                    "FROM receipt_lines WHERE id = :id"
                ),
                {"id": s["disc"]},
            )
        )
        .mappings()
        .one()
    )


# ── the gate ─────────────────────────────────────────────────────────────────


async def test_pending_adjustment_blocks_commit_with_structured_errors_and_zero_writes(
    db: Any,
) -> None:
    s = await _seed(db)
    with pytest.raises(ReceiptAdjustmentsUnreviewed) as exc_info:
        await _commit(db, s)
    # Message is user-safe (counts only); structure carries the machine facts.
    assert "1 adjustment(s)" in str(exc_info.value)
    assert str(s["disc"]) not in str(exc_info.value)
    (err,) = exc_info.value.errors
    assert err["adjustment_line_id"] == str(s["disc"])
    assert err["line_type"] == "discount"
    assert err["invoice_name"] == "VOLUME DISCOUNT"
    assert err["amount_cents"] == -1200
    # Exactly one receivable item line → unambiguous suggestion.
    assert err["suggested_target_line_id"] == str(s["oil"])
    assert err["suggested_target_name"] == "OLIVE OIL"
    # Zero writes, receipt stays draft.
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


async def test_legacy_null_disposition_on_discount_also_blocks(db: Any) -> None:
    s = await _seed(db, disposition=None)
    with pytest.raises(ReceiptAdjustmentsUnreviewed):
        await _commit(db, s)


async def test_no_suggested_target_when_multiple_item_lines(db: Any) -> None:
    s = await _seed(db)
    await db.execute(
        text("""
            INSERT INTO receipt_lines
                (tenant_id, receipt_id, inventory_item_id, received_quantity,
                 extracted_unit, extracted_name, match_status, line_total_cents)
            VALUES (:t, :r, :i, 2, 'ea', 'OLIVE OIL SECOND', 'matched', 5000)
        """),
        {"t": s["tid"], "r": s["rid"], "i": s["item"]},
    )
    with pytest.raises(ReceiptAdjustmentsUnreviewed) as exc_info:
        await _commit(db, s)
    (err,) = exc_info.value.errors
    assert err["suggested_target_line_id"] is None
    assert err["suggested_target_name"] is None


async def test_fee_rows_never_block_commit(db: Any) -> None:
    """NULL disposition on a fee row is not a decision gap — after the discount
    is excluded, the untouched fee must not stop the commit."""
    s = await _seed(db)
    await _patch(db, s, s["disc"], LineUpdate(adjustment_disposition="excluded"))
    result = await _commit(db, s)
    assert result["status"] == "committed"


# ── decisions move state (persisted, atomic, reversible) ─────────────────────


async def test_link_sets_linked_and_reviewed_then_commits_at_net(db: Any) -> None:
    s = await _seed(db)
    out = await _patch(db, s, s["disc"], LineUpdate(adjusts_line_id=s["oil"]))
    # The PUT response itself reports the persisted decision (truthful-UI food).
    assert out["adjustment_disposition"] == "linked"
    assert str(out["adjusts_line_id"]) == str(s["oil"])
    row = await _disc_row(db, s)
    assert row["adjustment_disposition"] == "linked"
    assert row["disposition_reviewed_at"] is not None
    assert row["disposition_reviewed_by"] == s["user"]
    assert row["disposition_reason"] is None

    result = await _commit(db, s)
    assert result["status"] == "committed"
    exact = (
        await db.execute(
            text(
                "SELECT unit_cost_cents_exact FROM ingredient_cost_snapshots "
                "WHERE tenant_id = :t AND source_receipt_line_id = :l"
            ),
            {"t": s["tid"], "l": s["oil"]},
        )
    ).scalar_one()
    # net (10000 − 1200) / 4 ea = 2200.0000 ¢/ea
    assert Decimal(str(exact)) == Decimal("2200.0000")


async def test_unlink_returns_to_pending_and_blocks_again(db: Any) -> None:
    s = await _seed(db)
    await _patch(db, s, s["disc"], LineUpdate(adjusts_line_id=s["oil"]))
    await _patch(db, s, s["disc"], LineUpdate(adjusts_line_id=None))
    row = await _disc_row(db, s)
    assert row["adjustment_disposition"] == "pending"
    assert row["adjusts_line_id"] is None
    assert row["disposition_reviewed_at"] is None
    assert row["disposition_reviewed_by"] is None
    with pytest.raises(ReceiptAdjustmentsUnreviewed):
        await _commit(db, s)


async def test_exclude_commits_at_gross_with_reason_and_reviewer(db: Any) -> None:
    s = await _seed(db)
    await _patch(
        db,
        s,
        s["disc"],
        LineUpdate(adjustment_disposition="excluded", exclusion_reason="applies to a return"),
    )
    row = await _disc_row(db, s)
    assert row["adjustment_disposition"] == "excluded"
    assert row["disposition_reason"] == "applies to a return"
    assert row["disposition_reviewed_at"] is not None
    assert row["disposition_reviewed_by"] == s["user"]

    result = await _commit(db, s)
    assert result["status"] == "committed"
    exact = (
        await db.execute(
            text(
                "SELECT unit_cost_cents_exact FROM ingredient_cost_snapshots "
                "WHERE tenant_id = :t AND source_receipt_line_id = :l"
            ),
            {"t": s["tid"], "l": s["oil"]},
        )
    ).scalar_one()
    # excluded → gross 10000 / 4 = 2500.0000 ¢/ea (deliberate, recorded choice)
    assert Decimal(str(exact)) == Decimal("2500.0000")


async def test_exclude_on_linked_row_clears_link_atomically(db: Any) -> None:
    s = await _seed(db)
    await _patch(db, s, s["disc"], LineUpdate(adjusts_line_id=s["oil"]))
    await _patch(db, s, s["disc"], LineUpdate(adjustment_disposition="excluded"))
    row = await _disc_row(db, s)
    assert row["adjustment_disposition"] == "excluded"
    assert row["adjusts_line_id"] is None
    assert row["disposition_reason"] == "operator_choice"  # default reason code


async def test_reopen_from_excluded_returns_to_pending(db: Any) -> None:
    s = await _seed(db)
    await _patch(db, s, s["disc"], LineUpdate(adjustment_disposition="excluded"))
    await _patch(db, s, s["disc"], LineUpdate(adjustment_disposition="pending"))
    row = await _disc_row(db, s)
    assert row["adjustment_disposition"] == "pending"
    assert row["disposition_reason"] is None
    assert row["disposition_reviewed_at"] is None
    with pytest.raises(ReceiptAdjustmentsUnreviewed):
        await _commit(db, s)


async def test_disposition_change_clears_review_affirmation(db: Any) -> None:
    """A decision is a line mutation — the D-606-22 affirmation must be re-given
    against the post-decision state."""
    s = await _seed(db)
    await db.execute(
        text("UPDATE receipts SET reviewed_affirmation = true WHERE id = :r"),
        {"r": s["rid"]},
    )
    await _patch(db, s, s["disc"], LineUpdate(adjustment_disposition="excluded"))
    affirmed = (
        await db.execute(
            text("SELECT reviewed_affirmation FROM receipts WHERE id = :r"), {"r": s["rid"]}
        )
    ).scalar_one()
    assert affirmed is False


# ── guardrails ───────────────────────────────────────────────────────────────


async def test_disposition_on_fee_row_rejected(db: Any) -> None:
    s = await _seed(db)
    with pytest.raises(AdjustmentLinkInvalid):
        await _patch(db, s, s["fee"], LineUpdate(adjustment_disposition="excluded"))


def test_schema_disposition_is_a_lone_action() -> None:
    with pytest.raises(ValidationError):
        LineUpdate(adjustment_disposition="excluded", unit_cost_cents=100)
    with pytest.raises(ValidationError):
        LineUpdate(exclusion_reason="orphan reason")
    with pytest.raises(ValidationError):
        LineUpdate(adjustment_disposition="pending", exclusion_reason="not excluded")
    # 'linked' is server-derived from the link action, never client-asserted.
    with pytest.raises(ValidationError):
        LineUpdate(adjustment_disposition="linked")
    # Valid shapes still pass.
    LineUpdate(adjustment_disposition="excluded", exclusion_reason="supplier will re-credit")
    LineUpdate(adjustment_disposition="pending")


async def test_db_checks_enforce_disposition_consistency(db: Any) -> None:
    """The invariants are DB facts: no disposition on an item row; 'linked'
    exactly ⇔ adjusts_line_id set."""
    s = await _seed(db)
    for bad in (
        # disposition on an item row
        {"lt": "item", "ms": "matched", "d": "pending", "adj": None},
        # linked without a target
        {"lt": "discount", "ms": "skipped", "d": "linked", "adj": None},
        # pending WITH a target
        {"lt": "discount", "ms": "skipped", "d": "pending", "adj": s["oil"]},
    ):
        sp = await db.begin_nested()
        with pytest.raises(Exception, match="ck_disposition"):
            await db.execute(
                text("""
                    INSERT INTO receipt_lines
                        (tenant_id, receipt_id, line_type, match_status, extracted_name,
                         received_quantity, extracted_unit,
                         line_total_cents, adjustment_disposition, adjusts_line_id)
                    VALUES (:t, :r, :lt, :ms, 'BAD ROW',
                            CASE WHEN :lt = 'item' THEN 1 END,
                            CASE WHEN :lt = 'item' THEN 'ea' END,
                            -100, :d, :adj)
                """),
                {"t": s["tid"], "r": s["rid"], **bad},
            )
        await sp.rollback()
