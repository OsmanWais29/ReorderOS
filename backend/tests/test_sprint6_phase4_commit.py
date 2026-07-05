"""Sprint 6 S4 — commit upgrade (D-606-16) + integrity trigger (D-606-05).

The spec's "POS-identity regression" is N/A here — POS depletes via the engine and
never creates receipts, so there is no source='pos' receipt to commit through
commit_receipt. The inertness intent is reframed to the MANUAL path: an existing
same-unit receipt commits with movement quantities + idempotency keys bit-identical
to the pre-upgrade 1:1 behavior (convert() is the identity when purchase==storage).

Service-level tests use the bound-session harness; the trigger's structural branches
(pos bypass, integrity floor) are probed with admin_conn (the trigger fires for the
superuser too). The trigger's unknown-source RAISE is unreachable by normal INSERT
(the source column CHECK blocks any non-enum value first) — defense-in-depth only.
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
from app.modules.inventory.services import (
    ReceiptNothingToCommit,
    ReceiptReviewRequired,
    commit_receipt,
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


async def _seed(db: Any, *, source: str = "manual") -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, slug, name) VALUES (:id, :s, 'S4')"),
        {"id": tid, "s": f"s4-{tid.hex[:8]}"},
    )
    uom = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type) "
                "VALUES (:t, 'g', 'g', 'weight') RETURNING id"
            ),
            {"t": tid},
        )
    ).scalar_one()
    item = (
        await db.execute(
            text(
                "INSERT INTO inventory_items (tenant_id, name, inventory_mode, storage_unit_id, "
                "recipe_unit_id) VALUES (:t, 'Flour', 'recipe_deducted', :u, :u) RETURNING id"
            ),
            {"t": tid, "u": uom},
        )
    ).scalar_one()
    rid = (
        await db.execute(
            text(
                "INSERT INTO receipts (tenant_id, commit_state, source) "
                "VALUES (:t, 'draft', :src) RETURNING id"
            ),
            {"t": tid, "src": source},
        )
    ).scalar_one()
    return tid, item, rid


async def _add_line(
    db: Any,
    tid: uuid.UUID,
    rid: uuid.UUID,
    item: uuid.UUID | None,
    *,
    qty: str = "10",
    unit_cost_cents: int | None = None,
    match_status: str = "unmatched",
    manually_corrected: bool = False,
    purchase_unit_id: uuid.UUID | None = None,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    return (
        await db.execute(
            text("""
                INSERT INTO receipt_lines
                    (tenant_id, receipt_id, inventory_item_id, received_quantity,
                     purchase_unit_id, unit_cost_cents, match_status, manually_corrected,
                     idempotency_key)
                VALUES (:t, :r, :i, :q, :pu, :c, :ms, :mc, :k)
                RETURNING id
            """),
            {
                "t": tid,
                "r": rid,
                "i": item,
                "q": Decimal(qty),
                "pu": purchase_unit_id,
                "c": unit_cost_cents,
                "ms": match_status,
                "mc": manually_corrected,
                "k": idempotency_key,
            },
        )
    ).scalar_one()


# ── manual-identity keystone ──────────────────────────────────────────────────


async def test_manual_commit_is_identity_and_stamps_confirmed_at(db: Any) -> None:
    tid, item, rid = await _seed(db)
    line_id = await _add_line(db, tid, rid, item, qty="10", idempotency_key="rk-1")
    result = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
    )
    assert result["status"] == "committed"
    assert result["confirmed"] is True
    mv = (
        (
            await db.execute(
                text(
                    "SELECT delta, idempotency_key, inventory_item_id FROM inventory_movements "
                    "WHERE tenant_id=:t AND source_id=:l"
                ),
                {"t": tid, "l": line_id},
            )
        )
        .mappings()
        .one()
    )
    assert Decimal(str(mv["delta"])) == Decimal("10")  # identity — bit-identical
    assert mv["idempotency_key"] == "rk-1"  # preserved key
    rec = (
        (
            await db.execute(
                text("SELECT commit_state, confirmed_at, committed_at FROM receipts WHERE id=:r"),
                {"r": rid},
            )
        )
        .mappings()
        .one()
    )
    assert rec["commit_state"] == "committed"
    assert rec["confirmed_at"] is not None  # server-set (D-606-04)
    assert rec["committed_at"] is not None


async def test_null_key_falls_back_to_receipt_line_key(db: Any) -> None:
    tid, item, rid = await _seed(db)
    line_id = await _add_line(db, tid, rid, item, idempotency_key=None)
    await commit_receipt(db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True)
    key = (
        await db.execute(
            text(
                "SELECT idempotency_key FROM inventory_movements WHERE tenant_id=:t AND source_id=:l"
            ),
            {"t": tid, "l": line_id},
        )
    ).scalar_one()
    assert key == f"receipt_line:{line_id}"


# ── the human-review gate ─────────────────────────────────────────────────────


async def test_no_confirm_rejected(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item)
    with pytest.raises(ReceiptReviewRequired):
        await commit_receipt(db, tenant_id=tid, receipt_id=rid, confirm=False)


async def test_confirm_without_affirmation_or_correction_rejected(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, manually_corrected=False)
    with pytest.raises(ReceiptReviewRequired):
        await commit_receipt(
            db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=False
        )


async def test_manually_corrected_line_satisfies_gate(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, manually_corrected=True)
    result = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=False
    )
    assert result["status"] == "committed"  # a manual correction is itself the affirmation


async def test_all_skipped_nothing_to_commit(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, match_status="skipped")
    with pytest.raises(ReceiptNothingToCommit):
        await commit_receipt(
            db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
        )


async def test_skipped_line_writes_no_movement(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, qty="5", match_status="matched", idempotency_key="k-keep")
    await _add_line(db, tid, rid, item, qty="9", match_status="skipped", idempotency_key="k-skip")
    await commit_receipt(db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True)
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id=:t"), {"t": tid}
        )
    ).scalar_one()
    assert n == 1  # only the matched line moved


async def test_cost_snapshot_written(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, unit_cost_cents=250)
    await commit_receipt(db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True)
    n = (
        await db.execute(
            text("SELECT count(*) FROM ingredient_cost_snapshots WHERE tenant_id=:t"), {"t": tid}
        )
    ).scalar_one()
    assert n == 1


async def test_idempotent_replay_returns_already_committed(db: Any) -> None:
    tid, item, rid = await _seed(db)
    await _add_line(db, tid, rid, item, idempotency_key="rk-idem")
    r1 = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
    )
    r2 = await commit_receipt(
        db, tenant_id=tid, receipt_id=rid, confirm=True, reviewed_affirmation=True
    )
    assert r1["status"] == "committed"
    assert r2["status"] == "already_committed"
    n = (
        await db.execute(
            text("SELECT count(*) FROM inventory_movements WHERE tenant_id=:t"), {"t": tid}
        )
    ).scalar_one()
    assert n == 1  # no duplicate movement on replay


# ── trigger structural branches (admin_conn — fires for superuser too) ────────


async def test_trigger_pos_bypasses_human_guards(admin_conn: Any) -> None:
    tid = uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'P')", tid, f"p-{tid.hex[:8]}"
    )
    uom = await admin_conn.fetchval(
        "INSERT INTO units_of_measure (tenant_id,name,abbreviation,unit_type) "
        "VALUES ($1,'g','g','weight') RETURNING id",
        tid,
    )
    item = await admin_conn.fetchval(
        "INSERT INTO inventory_items (tenant_id,name,inventory_mode,storage_unit_id,recipe_unit_id) "
        "VALUES ($1,'Flour','recipe_deducted',$2,$2) RETURNING id",
        tid,
        uom,
    )
    rid = await admin_conn.fetchval(
        "INSERT INTO receipts (tenant_id, commit_state, source) VALUES ($1,'draft','pos') RETURNING id",
        tid,
    )
    await admin_conn.execute(
        "INSERT INTO receipt_lines (tenant_id,receipt_id,inventory_item_id,received_quantity,match_status) "
        "VALUES ($1,$2,$3,5,'matched')",
        tid,
        rid,
        item,
    )
    try:
        # pos commit WITHOUT confirmed_at must succeed (bypass)
        await admin_conn.execute("UPDATE receipts SET commit_state='committed' WHERE id=$1", rid)
        assert (
            await admin_conn.fetchval("SELECT commit_state FROM receipts WHERE id=$1", rid)
            == "committed"
        )
    finally:
        await admin_conn.execute("DELETE FROM receipt_lines WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM inventory_items WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM units_of_measure WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tid)


async def test_trigger_integrity_floor_rejects_no_line(admin_conn: Any) -> None:
    import asyncpg

    tid = uuid.uuid4()
    await admin_conn.execute(
        "INSERT INTO tenants (id, slug, name) VALUES ($1, $2, 'F')", tid, f"f-{tid.hex[:8]}"
    )
    rid = await admin_conn.fetchval(
        "INSERT INTO receipts (tenant_id, commit_state, source) VALUES ($1,'draft','pos') RETURNING id",
        tid,
    )
    try:
        # pos receipt with NO inventory line → integrity floor (all sources) rejects
        with pytest.raises(asyncpg.CheckViolationError):
            await admin_conn.execute(
                "UPDATE receipts SET commit_state='committed' WHERE id=$1", rid
            )
    finally:
        await admin_conn.execute("DELETE FROM receipts WHERE tenant_id=$1", tid)
        await admin_conn.execute("DELETE FROM tenants WHERE id=$1", tid)
