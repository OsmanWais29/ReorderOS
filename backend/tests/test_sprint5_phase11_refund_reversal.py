"""Sprint 5 Phase 11 — refund/reversal wiring + PARTIALLY_REFUNDED activation.

Design: decisions/adr/0001-sprint5-phase11-refund-reversal.md. Two layers:

  UNIT (bound-session harness, superuser) — handler.reverse_line as a PURE LEDGER OPERATION:
    reverses base + modifier + legacy keys, idempotent replay, no-op when no forward movement
    (D4), Mode-A/Mode-B type-specific reversals (D7), and — proving the D2 ownership split —
    reverse_line does NOT set is_refunded (the worker owns that). Plus the process_line
    eligibility flip (PARTIALLY_REFUNDED depletes non-refunded / fails refunded — gate 20).

  E2E (real service_worker role, via InboxWorker) — the keystone: a superuser unit test
    STRUCTURALLY can't catch a missing column GRANT. The worker's refund-reconcile step
    UPDATEs sale_line_items.is_refunded, which 0017 excluded and 0021 re-adds. Only the real
    role proves the grant. Covers both refund-arrival paths (edit 8), modifier-key reversal
    (D3), replayed-refund idempotency (D1), reversal immutability (gate 25/41), the
    PARTIALLY_REFUNDED mixed-line case (gate 20), and the late-void out-of-scope decision (D8).

Gates: 20, 21. Fail-gate 15. ADR 0001 decisions D1–D8.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from app.modules.pos.worker import InboxWorker
from tests.helpers.phase7 import make_clover_order, seed_inbox_event, seed_worker_prereqs
from tests.test_e2e_pos_inventory import (
    _make_li_with_item,
    _seed_confirmed_modifier,
    _seed_inventory_item,
    _seed_menu_item,
    _seed_recipe,
    _seed_uom,
)
from tests.test_sprint5_phase9_base_depletion import _movements, _seed

# ═══════════════════════════════════════════════════════════════════════════════
# UNIT — handler.reverse_line (bound-session harness; rolled back, no residue)
# ═══════════════════════════════════════════════════════════════════════════════


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


async def _refunded_flag(db: AsyncSession, tid: str, sli_id: str) -> bool:
    return bool(
        (
            await db.execute(
                text("SELECT is_refunded FROM sale_line_items WHERE id = :s AND tenant_id = :t"),
                {"s": sli_id, "t": tid},
            )
        ).scalar()
    )


@pytest.mark.integration
async def test_reverse_line_reverses_base(db) -> None:
    """A previously-depleted line: reverse_line emits a type-specific negation, nets to zero."""
    s = await _seed(db, mode="recipe_deducted", recipe_qty=2, line_qty=3, yield_q=1.0)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    forward = await _movements(db, s["tid"], s["item_id"])
    assert len(forward) == 1 and Decimal(str(forward[0]["delta"])) == Decimal("-6")

    n = await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert n == 1
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert "sale_depletion_reversal" in {m["movement_type"] for m in mvs}
    assert sum(Decimal(str(m["delta"])) for m in mvs) == Decimal("0")  # -6 + 6


@pytest.mark.integration
async def test_reverse_line_does_not_set_is_refunded(db) -> None:
    """ADR D2: reverse_line is a pure ledger op — it reverses movements but never writes
    is_refunded. The WORKER owns refund detection + that write. Proven here in isolation."""
    s = await _seed(db, recipe_qty=2, line_qty=3, is_refunded=False)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert await _refunded_flag(db, s["tid"], s["sli_id"]) is False  # untouched by reverse_line


@pytest.mark.integration
async def test_reverse_line_is_idempotent(db) -> None:
    """Replay must not double-reverse (D1: idempotent via the reversal key + ON CONFLICT)."""
    s = await _seed(db, recipe_qty=2, line_qty=3)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"])) == 1
    assert await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"])) == 0
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert sum(1 for m in mvs if m["movement_type"] == "sale_depletion_reversal") == 1


@pytest.mark.integration
async def test_reverse_line_no_forward_movement_is_noop(db) -> None:
    """ADR D4: driven by movement existence, not status — a never-depleted line reverses to
    a no-op (0 rows), no error, no status thrash."""
    s = await _seed(db, recipe_qty=2, line_qty=3)
    n = await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert n == 0
    assert await _movements(db, s["tid"], s["item_id"]) == []


@pytest.mark.integration
async def test_reverse_line_reverses_legacy_key_movement(db) -> None:
    """ADR D3: reversal covers BOTH key formats. A legacy sale_line:{sli}:{ii} movement must
    be found (shared prefix) and reversed too."""
    s = await _seed(db, recipe_qty=2, line_qty=3)
    await db.execute(
        text("""
            INSERT INTO inventory_movements
                (id, tenant_id, inventory_item_id, movement_type, delta,
                 source_type, source_id, idempotency_key)
            VALUES (gen_random_uuid(), :t, :iid, 'sale_depletion', -6,
                    'sale_line_item', :sli, :key)
        """),
        {
            "t": s["tid"],
            "iid": s["item_id"],
            "sli": s["sli_id"],
            "key": f"sale_line:{s['sli_id']}:{s['item_id']}",
        },
    )
    assert await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"])) == 1
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert any(m["movement_type"] == "sale_depletion_reversal" for m in mvs)
    assert sum(Decimal(str(m["delta"])) for m in mvs) == Decimal("0")


@pytest.mark.integration
async def test_reverse_line_mode_b_reverses_signal(db) -> None:
    """ADR D7: Mode B (count_anchored) sale_signal → sale_signal_reversal (NOT a generic type;
    Mode B observed-consumption math nets signals against signal-reversals)."""
    s = await _seed(db, mode="count_anchored", recipe_qty=2, line_qty=3)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (await _movements(db, s["tid"], s["item_id"]))[0]["movement_type"] == "sale_signal"
    await handler.reverse_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert any(m["movement_type"] == "sale_signal_reversal" for m in mvs)
    assert sum(Decimal(str(m["delta"])) for m in mvs) == Decimal("0")


# ── gate 20: PARTIALLY_REFUNDED eligibility flip via process_line ──────────────


@pytest.mark.integration
async def test_partial_refunded_non_refunded_line_depletes(db) -> None:
    """PARTIALLY_REFUNDED order, line NOT refunded, gate ON → depletes (gate 20)."""
    s = await _seed(db, payment_state="PARTIALLY_REFUNDED", is_refunded=False)
    status, reason = await handler.process_line(
        db, UUID(s["tid"]), UUID(s["sli_id"]), partial_refunds_enabled=True
    )
    assert (status, reason) == ("depleted", None)
    assert len(await _movements(db, s["tid"], s["item_id"])) == 1


@pytest.mark.integration
async def test_partial_refunded_refunded_line_is_line_refunded(db) -> None:
    """PARTIALLY_REFUNDED order, line refunded, gate ON → failed/line_refunded (gate 20)."""
    s = await _seed(db, payment_state="PARTIALLY_REFUNDED", is_refunded=True)
    status, reason = await handler.process_line(
        db, UUID(s["tid"]), UUID(s["sli_id"]), partial_refunds_enabled=True
    )
    assert (status, reason) == ("failed", "line_refunded")
    assert await _movements(db, s["tid"], s["item_id"]) == []


# ═══════════════════════════════════════════════════════════════════════════════
# E2E — real service_worker role (InboxWorker). The grant-catching layer.
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
async def clean_pending_inbox(admin_conn):
    await admin_conn.execute("""
        UPDATE pos_event_inbox SET state = 'duplicate_ignored'
        WHERE state IN ('pending', 'failed', 'processing')
    """)
    yield


async def _process_followup(admin_conn, seed, payload: dict) -> None:
    """Seed a follow-up inbox event carrying a re-fetched payload (pre-fetched, no second
    Clover mock) and let the worker process it as service_worker."""
    ev = await seed_inbox_event(
        admin_conn, seed, vendor_event_id=seed["vendor_event_id"], fetched_payload=payload
    )
    worker = InboxWorker()
    claimed = await worker.claim_batch(batch_size=1)
    assert claimed and str(claimed[0]["inbox_id"]) == ev["inbox_id"]
    await worker.process_event(claimed[0])


async def _seed_base_sale(admin_conn, *, qty: int = 3, recipe_qty: float = 2.0):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, recipe_qty))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)
    li = _make_li_with_item(pos_item_id, qty=qty)
    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID", line_items=[li]
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])
    return seed, tid, inv_id, li


@pytest.mark.asyncio
@respx.mock
async def test_e2e_refund_after_depletion_reverses_real_role(admin_conn):
    """KEYSTONE (gate 21, fail-gate 15): a depleted line, on a later refund event, is reversed
    AND flagged is_refunded by the WORKER — executed as the real service_worker role, proving
    the 0021 is_refunded grant. A missing grant fails here while unit tests stay green."""
    seed, tid, inv_id, li = await _seed_base_sale(admin_conn)
    fwd = await admin_conn.fetchval(
        "SELECT delta FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2 AND movement_type='sale_depletion'",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert Decimal(str(fwd)) == Decimal("-6")

    refund_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="REFUNDED",
        line_items=[{**li, "refunded": True}],
    )
    await _process_followup(admin_conn, seed, refund_order)

    rev = await admin_conn.fetchval(
        "SELECT delta FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2 AND movement_type='sale_depletion_reversal'",
        uuid.UUID(tid),
        uuid.UUID(inv_id),
    )
    assert Decimal(str(rev)) == Decimal("6")
    assert Decimal(
        str(
            await admin_conn.fetchval(
                "SELECT COALESCE(SUM(delta),0) FROM inventory_movements"
                " WHERE tenant_id=$1 AND inventory_item_id=$2",
                uuid.UUID(tid),
                uuid.UUID(inv_id),
            )
        )
    ) == Decimal("0")
    assert (
        await admin_conn.fetchval(
            "SELECT is_refunded FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
        )
        is True
    )


@pytest.mark.asyncio
@respx.mock
async def test_e2e_refund_after_depletion_reverses_modifier_too(admin_conn):
    """ADR D3: reversal covers base AND modifier movements — both net to zero (real role)."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_id = await _seed_uom(admin_conn, tid)
    base_item = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    mod_item = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (base_item, 2.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    menu_item_id = await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)
    pos_mod_id = await _seed_confirmed_modifier(
        admin_conn, tid, menu_item_id, mod_item, mod_qty=5.0
    )

    li = _make_li_with_item(pos_item_id, qty=3)
    li["modifications"] = {"elements": [{"modifier": {"id": pos_mod_id}, "quantity": 2}]}
    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID", line_items=[li]
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    await _process_followup(
        admin_conn,
        seed,
        make_clover_order(
            order_id=seed["vendor_event_id"],
            state="locked",
            payment_state="REFUNDED",
            line_items=[{**li, "refunded": True}],
        ),
    )

    for item in (base_item, mod_item):
        assert Decimal(
            str(
                await admin_conn.fetchval(
                    "SELECT COALESCE(SUM(delta),0) FROM inventory_movements"
                    " WHERE tenant_id=$1 AND inventory_item_id=$2",
                    uuid.UUID(tid),
                    uuid.UUID(item),
                )
            )
        ) == Decimal("0"), f"item {item} should net to zero"
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1"
            " AND movement_type IN ('sale_depletion_reversal','sale_signal_reversal')",
            uuid.UUID(tid),
        )
        == 2
    )


@pytest.mark.asyncio
@respx.mock
async def test_e2e_refund_before_depletion_fails_line_refunded(admin_conn):
    """Edit 8 path 2: a line that arrives already refunded is failed (line_refunded), no
    ledger writes — eligibility, not reversal, is the mechanism. Real role."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_id = await _seed_uom(admin_conn, tid)
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 2.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    li = {**_make_li_with_item(pos_item_id, qty=3), "refunded": True}
    order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="PARTIALLY_REFUNDED",
        line_items=[li],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    row = await admin_conn.fetchrow(
        "SELECT depletion_status, depletion_reason, is_refunded FROM sale_line_items"
        " WHERE tenant_id=$1",
        uuid.UUID(tid),
    )
    assert (row["depletion_status"], row["depletion_reason"]) == ("failed", "line_refunded")
    assert row["is_refunded"] is True
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1 AND inventory_item_id=$2",
            uuid.UUID(tid),
            uuid.UUID(inv_id),
        )
        == 0
    )


@pytest.mark.asyncio
@respx.mock
async def test_e2e_partially_refunded_mixed_lines(admin_conn):
    """Gate 20 end-to-end: PARTIALLY_REFUNDED order, one refunded + one non-refunded line —
    non-refunded depletes, refunded fails (line_refunded)."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_id = await _seed_uom(admin_conn, tid)
    item_keep = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    item_refund = await _seed_inventory_item(admin_conn, tid, uom_id, mode="recipe_deducted")
    rv_keep = await _seed_recipe(admin_conn, tid, (item_keep, 2.0))
    rv_refund = await _seed_recipe(admin_conn, tid, (item_refund, 2.0))
    pos_keep = f"POS_{uuid.uuid4().hex[:8]}"
    pos_refund = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_keep, recipe_version_id=rv_keep)
    await _seed_menu_item(admin_conn, tid, pos_refund, recipe_version_id=rv_refund)

    li_keep = _make_li_with_item(pos_keep, qty=1)
    li_refund = {**_make_li_with_item(pos_refund, qty=1), "refunded": True}
    order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="PARTIALLY_REFUNDED",
        line_items=[li_keep, li_refund],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    assert Decimal(
        str(
            await admin_conn.fetchval(
                "SELECT delta FROM inventory_movements WHERE tenant_id=$1 AND inventory_item_id=$2",
                uuid.UUID(tid),
                uuid.UUID(item_keep),
            )
        )
    ) == Decimal("-2")
    assert (
        await admin_conn.fetchval(
            "SELECT depletion_status FROM sale_line_items"
            " WHERE tenant_id=$1 AND clover_line_item_id=$2",
            uuid.UUID(tid),
            li_keep["id"],
        )
        == "depleted"
    )
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1 AND inventory_item_id=$2",
            uuid.UUID(tid),
            uuid.UUID(item_refund),
        )
        == 0
    )
    refund_row = await admin_conn.fetchrow(
        "SELECT depletion_status, depletion_reason FROM sale_line_items"
        " WHERE tenant_id=$1 AND clover_line_item_id=$2",
        uuid.UUID(tid),
        li_refund["id"],
    )
    assert (refund_row["depletion_status"], refund_row["depletion_reason"]) == (
        "failed",
        "line_refunded",
    )


@pytest.mark.asyncio
@respx.mock
async def test_e2e_replayed_refund_event_writes_one_reversal(admin_conn):
    """ADR D1, idempotency mirror of forward replay: a DUPLICATE refund event (refund webhooks
    retry) produces exactly ONE reversal, not two; is_refunded stays true; no double anything."""
    seed, tid, inv_id, li = await _seed_base_sale(admin_conn)
    refund_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="REFUNDED",
        line_items=[{**li, "refunded": True}],
    )
    await _process_followup(admin_conn, seed, refund_order)
    await _process_followup(admin_conn, seed, refund_order)  # retried webhook

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1"
            " AND movement_type='sale_depletion_reversal'",
            uuid.UUID(tid),
        )
        == 1
    )
    assert Decimal(
        str(
            await admin_conn.fetchval(
                "SELECT COALESCE(SUM(delta),0) FROM inventory_movements"
                " WHERE tenant_id=$1 AND inventory_item_id=$2",
                uuid.UUID(tid),
                uuid.UUID(inv_id),
            )
        )
    ) == Decimal("0")
    assert (
        await admin_conn.fetchval(
            "SELECT is_refunded FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
        )
        is True
    )


@pytest.mark.asyncio
@respx.mock
async def test_e2e_reversal_is_additive_forward_rows_untouched(admin_conn):
    """Immutability (gate 25/41 extended to reversal, accounting §9): a refund ADDS a canceling
    row; it never mutates the forward movement. Forward row byte-identical after; reversal row
    references it (source_id) with the negated delta."""
    seed, tid, _inv_id, li = await _seed_base_sale(admin_conn)
    forward_before = dict(
        await admin_conn.fetchrow(
            "SELECT id, delta, movement_type, idempotency_key, yield_factor_applied, recorded_at"
            " FROM inventory_movements WHERE tenant_id=$1 AND movement_type='sale_depletion'",
            uuid.UUID(tid),
        )
    )

    await _process_followup(
        admin_conn,
        seed,
        make_clover_order(
            order_id=seed["vendor_event_id"],
            state="locked",
            payment_state="REFUNDED",
            line_items=[{**li, "refunded": True}],
        ),
    )

    forward_after = dict(
        await admin_conn.fetchrow(
            "SELECT id, delta, movement_type, idempotency_key, yield_factor_applied, recorded_at"
            " FROM inventory_movements WHERE tenant_id=$1 AND id=$2",
            uuid.UUID(tid),
            forward_before["id"],
        )
    )
    assert forward_after == forward_before, "reversal must not mutate the forward movement"
    rev = await admin_conn.fetchrow(
        "SELECT delta, source_type, source_id, yield_factor_applied FROM inventory_movements"
        " WHERE tenant_id=$1 AND movement_type='sale_depletion_reversal'",
        uuid.UUID(tid),
    )
    assert Decimal(str(rev["delta"])) == -Decimal(str(forward_before["delta"]))
    assert rev["source_type"] == "reversal"
    assert rev["source_id"] == forward_before["id"]
    # yield_factor preserved (accounting §9 line 374)
    assert rev["yield_factor_applied"] == forward_before["yield_factor_applied"]


@pytest.mark.asyncio
@respx.mock
async def test_e2e_late_void_after_depletion_does_not_reverse(admin_conn):
    """ADR D8: a line voided AFTER it depleted is OUT OF SCOPE for Sprint 5 — it keeps its
    forward movements and 'depleted' status. The refund-reconcile detection keys off the
    refund field ONLY, never the void/exchange field, so a late void does NOT trigger reversal
    (D8b). This is the decided behavior, not an accident."""
    seed, tid, _inv_id, li = await _seed_base_sale(admin_conn)
    # a later VOID event: line exchanged=true (voided), NOT refunded
    void_order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="PAID",
        line_items=[{**li, "exchanged": True}],
    )
    await _process_followup(admin_conn, seed, void_order)

    # no reversal written; forward movement untouched; status still depleted
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1"
            " AND movement_type IN ('sale_depletion_reversal','sale_signal_reversal')",
            uuid.UUID(tid),
        )
        == 0
    )
    assert Decimal(
        str(
            await admin_conn.fetchval(
                "SELECT delta FROM inventory_movements"
                " WHERE tenant_id=$1 AND movement_type='sale_depletion'",
                uuid.UUID(tid),
            )
        )
    ) == Decimal("-6")
    assert (
        await admin_conn.fetchval(
            "SELECT depletion_status FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
        )
        == "depleted"
    )
