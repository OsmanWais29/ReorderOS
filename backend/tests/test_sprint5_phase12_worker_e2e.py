"""Sprint 5 Phase 12 — worker end-to-end: pending lifecycle + line-level granularity.

Design: backend/docs/sprints/sprint-5-phase-12-notes.md. Phase 12 builds NO new behavior;
it proves properties Phases 9/10/11 established, with tests sensitive to the conditions where
the bug would show:

  test_line_failure_isolation_graceful (N1/N2.1, gate 23)
      A line that fails GRACEFULLY (failed/missing_conversion — a DATA state) does not block
      the other line; the event is marked PROCESSED (retry would only re-derive the terminal).

  test_crash_mid_line_survives_and_recovers (N2.2, fail-gate 13, §39)
      A line that RAISES (a SYSTEM state) is isolated: the good line persists, the crashed
      line is observable-pending, the event is FAILED/retryable — and on RERUN with the fault
      removed, the pending line completes with no duplication. Survival AND recovery.

  test_unconfirm_preserves_historical_ledger (N3, gates 25 + 41)
      Real unconfirm_recipe over a real depleted sale, with a vacuousness guard (assert the
      sale actually depleted against v1 BEFORE un-confirming).

  test_stuck_pending_lines_queryable (N4, gate 29)
      The stuck-pending diagnostic surfaces NULL/pending lines older than the threshold by
      created_at (ingested-but-unprocessed), not recorded_at.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
import respx

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import diagnostics, handler
from app.modules.pos.worker import InboxWorker
from app.modules.recipes import repo as recipes_repo
from tests.helpers.phase7 import make_clover_order, seed_inbox_event, seed_worker_prereqs
from tests.test_e2e_pos_inventory import (
    _make_li_with_item,
    _seed_inventory_item,
    _seed_menu_item,
    _seed_recipe,
    _seed_uom,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def clean_pending_inbox(admin_conn):
    await admin_conn.execute("""
        UPDATE pos_event_inbox SET state = 'duplicate_ignored'
        WHERE state IN ('pending', 'failed', 'processing')
    """)
    yield


async def _event_state(admin_conn, inbox_id) -> str:
    return await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", inbox_id
    )


# ═══════════════════════════════════════════════════════════════════════════════
# N1/N2.1 — graceful failure: DATA state → other line depletes, event processed
# ═══════════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_line_failure_isolation_graceful(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_g = await _seed_uom(admin_conn, tid, name="g")
    uom_ml = await _seed_uom(admin_conn, tid, name="ml")  # no g↔ml conversion seeded
    inv_good = await _seed_inventory_item(admin_conn, tid, uom_g, mode="recipe_deducted")
    inv_bad = await _seed_inventory_item(admin_conn, tid, uom_ml, mode="recipe_deducted")
    rv_good = await _seed_recipe(admin_conn, tid, (inv_good, 2.0))
    rv_bad = await _seed_recipe(admin_conn, tid, (inv_bad, 2.0))  # unit 'g' → 'ml' missing
    pos_good, pos_bad = f"POS_{uuid.uuid4().hex[:8]}", f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_good, recipe_version_id=rv_good)
    await _seed_menu_item(admin_conn, tid, pos_bad, recipe_version_id=rv_bad)

    li_good, li_bad = _make_li_with_item(pos_good, qty=1), _make_li_with_item(pos_bad, qty=1)
    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID",
        line_items=[li_good, li_bad],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    assert await admin_conn.fetchval(
        "SELECT depletion_status FROM sale_line_items"
        " WHERE tenant_id=$1 AND clover_line_item_id=$2", uuid.UUID(tid), li_good["id"],
    ) == "depleted"
    bad = await admin_conn.fetchrow(
        "SELECT depletion_status, depletion_reason FROM sale_line_items"
        " WHERE tenant_id=$1 AND clover_line_item_id=$2", uuid.UUID(tid), li_bad["id"],
    )
    assert (bad["depletion_status"], bad["depletion_reason"]) == ("failed", "missing_conversion")
    # N1: a graceful (data) failure does NOT fail the event — remediation is operator action
    assert await _event_state(admin_conn, seed["inbox_id"]) == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# N2.2 — crash mid-line: SYSTEM state → isolate, retry, recover (4 assertions)
# ═══════════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_crash_mid_line_survives_and_recovers(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_g = await _seed_uom(admin_conn, tid, name="g")
    inv_a = await _seed_inventory_item(admin_conn, tid, uom_g, mode="recipe_deducted")
    inv_b = await _seed_inventory_item(admin_conn, tid, uom_g, mode="recipe_deducted")
    rv_a = await _seed_recipe(admin_conn, tid, (inv_a, 2.0))
    rv_b = await _seed_recipe(admin_conn, tid, (inv_b, 2.0))
    pos_a, pos_b = f"POS_{uuid.uuid4().hex[:8]}", f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos_a, recipe_version_id=rv_a)
    await _seed_menu_item(admin_conn, tid, pos_b, recipe_version_id=rv_b)

    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID",
        line_items=[_make_li_with_item(pos_a, qty=1), _make_li_with_item(pos_b, qty=1)],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )

    # Inject a crash on the FIRST per-line process_line call, then delegate to the real one.
    real_process_line = handler.process_line
    state = {"crash": True}

    async def flaky(session, tenant_id, sli_id, **kwargs):
        if state["crash"]:
            state["crash"] = False
            raise RuntimeError("injected crash mid-line")
        return await real_process_line(session, tenant_id, sli_id, **kwargs)

    worker = InboxWorker()
    with patch.object(handler, "process_line", flaky):
        await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    # (a) the good line persisted; (b) the crashed line is observable-pending
    depleted = await admin_conn.fetch(
        "SELECT id FROM sale_line_items WHERE tenant_id=$1 AND depletion_status='depleted'",
        uuid.UUID(tid),
    )
    pending = await admin_conn.fetch(
        "SELECT id FROM sale_line_items WHERE tenant_id=$1 AND depletion_status='pending'",
        uuid.UUID(tid),
    )
    assert len(depleted) == 1, "exactly one line should have committed past the crash"
    assert len(pending) == 1, "the crashed line must be left observable-pending"
    movements_after_crash = await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1", uuid.UUID(tid)
    )
    assert movements_after_crash == 1
    # (c) the event is failed/retryable
    assert await _event_state(admin_conn, seed["inbox_id"]) == "failed"

    # (d) RERUN with the fault removed → pending line completes, no duplication
    ev = await seed_inbox_event(
        admin_conn, seed, vendor_event_id=seed["vendor_event_id"], fetched_payload=order
    )
    worker2 = InboxWorker()
    await worker2.process_event((await worker2.claim_batch(batch_size=1))[0])

    assert await admin_conn.fetchval(
        "SELECT COUNT(*) FROM sale_line_items WHERE tenant_id=$1 AND depletion_status='depleted'",
        uuid.UUID(tid),
    ) == 2
    # idempotency: exactly one movement per item, no duplicate from the crashed partial attempt
    assert await admin_conn.fetchval(
        "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1", uuid.UUID(tid)
    ) == 2
    assert await _event_state(admin_conn, ev["inbox_id"]) == "processed"


# ═══════════════════════════════════════════════════════════════════════════════
# N3 — historical-pointer: real un-confirm + vacuousness guard (gates 25 + 41)
# ═══════════════════════════════════════════════════════════════════════════════


async def _seed_confirmed_recipe_on_pos_item(
    admin_conn, tid: str, pos_item_id: str, inv_id: str, qty: float
) -> tuple[uuid.UUID, uuid.UUID]:
    """ONE menu_item that BOTH carries pos_item_id (worker maps the sale) AND parents a
    confirmed recipe chain (unconfirm_recipe can re-open it). Avoids the silent-vacuous trap
    of the per-recipe helper. Returns (menu_item_id, recipe_version_id)."""
    t = uuid.UUID(tid)
    mi_id = await admin_conn.fetchval(
        "INSERT INTO menu_items (tenant_id, pos_item_id, name) VALUES ($1,$2,$3) RETURNING id",
        t, pos_item_id, f"menu-{uuid.uuid4().hex[:6]}",
    )
    recipe_id = await admin_conn.fetchval(
        "INSERT INTO recipes (tenant_id, menu_item_id, status) VALUES ($1,$2,'confirmed')"
        " RETURNING id", t, mi_id,
    )
    rv_id = await admin_conn.fetchval(
        "INSERT INTO recipe_versions (tenant_id, recipe_id, version_number, yield_quantity, name)"
        " VALUES ($1,$2,1,1.0,$3) RETURNING id", t, recipe_id, f"rv-{uuid.uuid4().hex[:6]}",
    )
    await admin_conn.execute(
        "INSERT INTO recipe_ingredients (tenant_id, recipe_version_id, inventory_item_id,"
        " quantity, unit) VALUES ($1,$2,$3,$4,'g')", t, rv_id, uuid.UUID(inv_id), qty,
    )
    await admin_conn.execute(
        "UPDATE menu_items SET recipe_version_id=$1 WHERE id=$2", rv_id, mi_id
    )
    return mi_id, rv_id


@respx.mock
async def test_unconfirm_preserves_historical_ledger(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uid = await admin_conn.fetchval(
        "SELECT user_id FROM user_tenants WHERE tenant_id=$1 LIMIT 1", uuid.UUID(tid)
    )
    uom_g = await _seed_uom(admin_conn, tid, name="g")
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_g, mode="recipe_deducted")
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    mi_id, rv_id = await _seed_confirmed_recipe_on_pos_item(admin_conn, tid, pos_item_id, inv_id, 2.0)

    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID",
        line_items=[_make_li_with_item(pos_item_id, qty=3)],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    # ── VACUOUSNESS GUARD: the sale must actually have depleted against v1 ──────────────
    # (movements exist AND v1 appears in their idempotency keys). If the setup ever regressed
    # to the disconnected-helper trap, this fails LOUDLY instead of the test passing emptily.
    mv = await admin_conn.fetchrow(
        "SELECT delta, idempotency_key FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2", uuid.UUID(tid), uuid.UUID(inv_id),
    )
    assert mv is not None, "precondition: the sale must have depleted (chain wired)"
    assert str(rv_id) in mv["idempotency_key"], "precondition: depleted against v1"
    assert Decimal(str(mv["delta"])) == Decimal("-6")

    # capture pre-unconfirm state
    sli_rv_before = await admin_conn.fetchval(
        "SELECT recipe_version_id FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
    )
    rv_before = dict(await admin_conn.fetchrow(
        "SELECT id, recipe_id, version_number, yield_quantity, name FROM recipe_versions"
        " WHERE id=$1", rv_id,
    ))
    ri_before = [dict(r) for r in await admin_conn.fetch(
        "SELECT inventory_item_id, quantity, unit FROM recipe_ingredients"
        " WHERE recipe_version_id=$1 ORDER BY inventory_item_id", rv_id,
    )]
    mv_before = [dict(r) for r in await admin_conn.fetch(
        "SELECT id, delta, idempotency_key FROM inventory_movements"
        " WHERE tenant_id=$1 ORDER BY idempotency_key", uuid.UUID(tid),
    )]

    # ── run the REAL un-confirm (Manager+ operator path) on a committed session ─────────
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        await recipes_repo.unconfirm_recipe(session, uuid.UUID(tid), mi_id, uid)
        await session.flush()
        await trans.commit()

    # pointer cleared + recipe re-opened + draft correctly parented at v1
    assert await admin_conn.fetchval(
        "SELECT recipe_version_id FROM menu_items WHERE id=$1", mi_id
    ) is None
    assert await admin_conn.fetchval(
        "SELECT status FROM recipes WHERE menu_item_id=$1", mi_id
    ) == "draft"
    assert await admin_conn.fetchval(
        "SELECT parent_recipe_version_id FROM recipe_drafts WHERE tenant_id=$1", uuid.UUID(tid)
    ) == rv_id, "un-confirm must produce a draft parented at v1, not just clear the pointer"

    # the processed sale is UNTOUCHED — frozen pointer, byte-identical version, same ledger
    assert sli_rv_before == rv_id
    assert await admin_conn.fetchval(
        "SELECT recipe_version_id FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
    ) == rv_id
    assert dict(await admin_conn.fetchrow(
        "SELECT id, recipe_id, version_number, yield_quantity, name FROM recipe_versions"
        " WHERE id=$1", rv_id,
    )) == rv_before, "un-confirm must not mutate recipe_versions (fail-gate 7)"
    assert [dict(r) for r in await admin_conn.fetch(
        "SELECT inventory_item_id, quantity, unit FROM recipe_ingredients"
        " WHERE recipe_version_id=$1 ORDER BY inventory_item_id", rv_id,
    )] == ri_before
    assert [dict(r) for r in await admin_conn.fetch(
        "SELECT id, delta, idempotency_key FROM inventory_movements"
        " WHERE tenant_id=$1 ORDER BY idempotency_key", uuid.UUID(tid),
    )] == mv_before, "ledger rows must be unchanged (gate 25, fail-gate 3)"


# ═══════════════════════════════════════════════════════════════════════════════
# N4 — stuck-pending diagnostic (gate 29): NULL+pending, anchored on created_at
# ═══════════════════════════════════════════════════════════════════════════════


@respx.mock
async def test_stuck_pending_lines_queryable(admin_conn):
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom_g = await _seed_uom(admin_conn, tid, name="g")
    inv_id = await _seed_inventory_item(admin_conn, tid, uom_g, mode="recipe_deducted")
    rv_id = await _seed_recipe(admin_conn, tid, (inv_id, 1.0))
    pos_item_id = f"POS_{uuid.uuid4().hex[:8]}"
    mi_id = await _seed_menu_item(admin_conn, tid, pos_item_id, recipe_version_id=rv_id)

    async def _line(age_min: int, status, cli: str) -> None:
        oid = await admin_conn.fetchval(
            "INSERT INTO orders (id, tenant_id, pos_event_inbox_id, clover_order_id,"
            " total_amount_cents, state, payment_state, processed_at)"
            " VALUES (gen_random_uuid(),$1,$2,$3,0,'locked','PAID',now()) RETURNING id",
            uuid.UUID(tid), uuid.UUID(seed["inbox_id"]), f"ord_{uuid.uuid4().hex[:8]}",
        )
        await admin_conn.execute(
            "INSERT INTO sale_line_items (id, tenant_id, order_id, clover_line_item_id,"
            " menu_item_id, name_at_sale, quantity, price_cents_at_sale, net_revenue_cents,"
            " recipe_version_id, depletion_status, created_at)"
            " VALUES (gen_random_uuid(),$1,$2,$3,$4,'Item',1,0,0,$5,$6,"
            " now() - make_interval(mins => $7))",
            uuid.UUID(tid), oid, cli, uuid.UUID(mi_id), uuid.UUID(rv_id), status, age_min,
        )

    await _line(10, "pending", f"cli_p_{uuid.uuid4().hex[:6]}")  # old + pending → stuck
    await _line(10, None, f"cli_n_{uuid.uuid4().hex[:6]}")       # old + NULL → also stuck (N4)
    await _line(0, "pending", f"cli_f_{uuid.uuid4().hex[:6]}")   # fresh pending → not stuck
    await _line(10, "depleted", f"cli_d_{uuid.uuid4().hex[:6]}")  # old but terminal → not stuck

    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        stuck = await diagnostics.stuck_pending_lines(
            session, tenant_id=uuid.UUID(tid), older_than_minutes=5
        )
        await trans.rollback()

    assert len(stuck) == 2, f"old pending AND old NULL should surface, got {len(stuck)}"
    assert all(s.age_seconds >= 300 for s in stuck)
