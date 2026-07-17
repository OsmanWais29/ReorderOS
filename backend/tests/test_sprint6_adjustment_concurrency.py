"""REAL concurrency proof for adjustment links vs commit (two live sessions).

Not lock-order inspection: two independent AsyncSessions contend on one draft
receipt through the production services. Both interleavings are exercised:

  link-first:   A holds the receipt lock mid-link → B's commit WAITS → after A
                commits, B re-reads the fresh link and snapshots the ADJUSTED
                cost.
  commit-first: A holds the lock through commit → B's link WAITS → after A
                commits, B's update fails ReceiptImmutable — no stale link, no
                stale cost.

Committed seed data (admin_conn) because both sessions must see it; explicit
cleanup. Generous wait budgets double as the no-deadlock assertion.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine
from app.modules.inventory.services import commit_receipt
from app.modules.receipts.schemas import LineUpdate
from app.modules.receipts.services import ReceiptImmutable, update_line

pytestmark = pytest.mark.integration

_WAIT_PROOF_S = 0.6  # long enough to prove blocking, short enough to stay fast
_COMPLETE_BUDGET_S = 15  # exceeding this = deadlock → test fails, as it should


async def _seed(admin_conn: Any) -> dict[str, Any]:
    tid, rid = uuid.uuid4(), uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'CONC')", tid, f"cc-{tid.hex[:8]}"
    )
    uom = await admin_conn.fetchval(
        "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
        "VALUES ($1, 'ea', 'ea', 'count') RETURNING id",
        tid,
    )
    item = await admin_conn.fetchval(
        "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
        "recipe_unit_id) VALUES ($1, 'WIDGETS', 'recipe_deducted', $2, $2) RETURNING id",
        tid,
        uom,
    )
    await admin_conn.execute(
        "INSERT INTO receipts (id, tenant_id, commit_state, source) "
        "VALUES ($1, $2, 'draft', 'email')",
        rid,
        tid,
    )
    line = await admin_conn.fetchval(
        "INSERT INTO receipt_lines (tenant_id, receipt_id, inventory_item_id, "
        "received_quantity, extracted_unit, extracted_name, match_status, line_total_cents) "
        "VALUES ($1, $2, $3, 10, 'ea', 'WIDGETS ROW', 'matched', 10000) RETURNING id",
        tid,
        rid,
        item,
    )
    disc = await admin_conn.fetchval(
        "INSERT INTO receipt_lines (tenant_id, receipt_id, line_type, match_status, "
        "extracted_name, line_total_cents, adjustment_disposition, disposition_reason) "
        # DECIDED seed ('excluded'): the commit-first interleaving must reach the
        # lock contention under test, not the adjustment-decision gate. The
        # link-first interleaving flips it to 'linked' via update_line anyway.
        "VALUES ($1, $2, 'discount', 'skipped', 'PROMO', -1000, 'excluded', 'test_seed') "
        "RETURNING id",
        tid,
        rid,
    )
    return {"tid": tid, "rid": rid, "item": item, "line": line, "disc": disc}


async def _cleanup(admin_conn: Any, tid: uuid.UUID) -> None:
    # FK order: snapshots → lines (emits_movement_id FK) → movements → the rest.
    for table in (
        "ingredient_cost_snapshots",
        "receipt_lines",
        "inventory_movements",
        "receipts",
        "inventory_items",
        "units_of_measure",
        "tenants",
    ):
        col = "id" if table == "tenants" else "tenant_id"
        await admin_conn.execute(f"DELETE FROM {table} WHERE {col} = $1", tid)


async def _counts(admin_conn: Any, tid: uuid.UUID) -> tuple[int, int]:
    mv = await admin_conn.fetchval(
        "SELECT count(*) FROM inventory_movements WHERE tenant_id = $1", tid
    )
    snap = await admin_conn.fetchval(
        "SELECT count(*) FROM ingredient_cost_snapshots WHERE tenant_id = $1", tid
    )
    return mv, snap


async def test_link_holds_lock_commit_waits_then_uses_fresh_link(admin_conn: Any) -> None:
    s = await _seed(admin_conn)
    session_a = AsyncSession(engine)
    session_b = AsyncSession(engine)
    try:
        # A: link the discount and HOLD the transaction (receipt FOR UPDATE held).
        await update_line(
            session_a,
            tenant_id=s["tid"],
            receipt_id=s["rid"],
            line_id=s["disc"],
            patch=LineUpdate(adjusts_line_id=s["line"]),
        )

        async def commit_b() -> dict[str, Any]:
            result = await commit_receipt(
                session_b,
                tenant_id=s["tid"],
                receipt_id=s["rid"],
                confirm=True,
                reviewed_affirmation=True,
            )
            await session_b.commit()
            return result

        task = asyncio.create_task(commit_b())
        # PROOF OF BLOCKING: B cannot finish while A holds the lock.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=_WAIT_PROOF_S)
        assert not task.done()

        # A releases → B proceeds, RE-READS the fresh link, commits adjusted cost.
        await session_a.commit()
        result = await asyncio.wait_for(task, timeout=_COMPLETE_BUDGET_S)
        assert result["status"] == "committed"

        snap = await admin_conn.fetchrow(
            "SELECT unit_cost_cents, unit_cost_cents_exact FROM ingredient_cost_snapshots "
            "WHERE tenant_id = $1",
            s["tid"],
        )
        # Net (10000 − 1000) / 10 — B saw A's link, never the stale gross.
        assert Decimal(str(snap["unit_cost_cents_exact"])) == Decimal("900.0000")
        assert await _counts(admin_conn, s["tid"]) == (1, 1)
    finally:
        await session_a.close()
        await session_b.close()
        await _cleanup(admin_conn, s["tid"])


async def test_commit_holds_lock_link_waits_then_receipt_immutable(admin_conn: Any) -> None:
    s = await _seed(admin_conn)
    session_a = AsyncSession(engine)
    session_b = AsyncSession(engine)
    try:
        # A: commit the receipt and HOLD the transaction open (lock held).
        result = await commit_receipt(
            session_a,
            tenant_id=s["tid"],
            receipt_id=s["rid"],
            confirm=True,
            reviewed_affirmation=True,
        )
        assert result["status"] == "committed"

        async def link_b() -> None:
            await update_line(
                session_b,
                tenant_id=s["tid"],
                receipt_id=s["rid"],
                line_id=s["disc"],
                patch=LineUpdate(adjusts_line_id=s["line"]),
            )
            await session_b.commit()

        task = asyncio.create_task(link_b())
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=_WAIT_PROOF_S)
        assert not task.done()

        # A releases → B acquires the lock, sees commit_state='committed' → refused.
        await session_a.commit()
        with pytest.raises(ReceiptImmutable):
            await asyncio.wait_for(task, timeout=_COMPLETE_BUDGET_S)

        # One movement, one snapshot, GROSS cost (the late link never landed).
        snap = await admin_conn.fetchrow(
            "SELECT unit_cost_cents_exact FROM ingredient_cost_snapshots WHERE tenant_id = $1",
            s["tid"],
        )
        assert Decimal(str(snap["unit_cost_cents_exact"])) == Decimal("1000.0000")
        assert await _counts(admin_conn, s["tid"]) == (1, 1)
        linked = await admin_conn.fetchval(
            "SELECT adjusts_line_id FROM receipt_lines WHERE id = $1", s["disc"]
        )
        assert linked is None  # no stale link survived
    finally:
        await session_a.close()
        await session_b.close()
        await _cleanup(admin_conn, s["tid"])
