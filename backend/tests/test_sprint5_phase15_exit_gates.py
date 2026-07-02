"""Sprint 5 Phase 15 — exit-gate sweep gap-fills.

Design: backend/docs/sprints/sprint-5-phase-15-notes.md (the traceability matrix). The matrix
audit found 5 fill-now gaps where the would-it-fail standard was not yet met. Each test below
states its gate ID and its one-line failure assertion in the docstring, so the test→gate
mapping survives outside the matrix.

  Gate 19 + fail-14  — CREDITED order → no forward depletion (e2e, the v4 regression).
  Gate 22            — voided line → row lands failed/sale_ineligible (not just no movement).
  Gate 26 + fail-3   — modifier edit after a processed sale does not alter that sale's ledger.
  Gate 10 + fail-11  — confirm on a zero-ingredient draft hits the authoritative EmptyDraft.
  Fail-9             — depleted status + its movements are atomic (injected-rollback).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from app.modules.pos.worker import InboxWorker
from app.modules.recipes import modifiers_repo
from app.modules.recipes import repo as recipes_repo
from tests.helpers.phase7 import make_clover_order, seed_worker_prereqs
from tests.test_e2e_pos_inventory import (
    _make_li_with_item,
    _seed_confirmed_modifier,
    _seed_inventory_item,
    _seed_menu_item,
    _seed_recipe,
    _seed_uom,
)
from tests.test_sprint5_phase12_worker_e2e import _seed_confirmed_recipe_on_pos_item


@pytest.fixture(autouse=True)
async def clean_pending_inbox(admin_conn):
    await admin_conn.execute("""
        UPDATE pos_event_inbox SET state = 'duplicate_ignored'
        WHERE state IN ('pending', 'failed', 'processing')
    """)
    yield


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


async def _event_state(admin_conn, inbox_id) -> str:
    return await admin_conn.fetchval(
        "SELECT state FROM pos_event_inbox WHERE inbox_id = $1", inbox_id
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 19 + fail-14 — CREDITED order does NOT trigger forward depletion (v4 regression)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_credited_order_no_forward_depletion(admin_conn):
    """Gate 19 / fail-14: a CREDITED order through the real worker writes NO ledger movements
    and the line lands failed/sale_ineligible. FAILS IF any movement is written or the line
    isn't failed/sale_ineligible — the v4 bug was end-to-end (a wrong spec everything downstream
    would have faithfully implemented), so the proof spans webhook→ledger, not just the predicate."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom = await _seed_uom(admin_conn, tid)
    inv = await _seed_inventory_item(admin_conn, tid, uom, mode="recipe_deducted")
    rv = await _seed_recipe(admin_conn, tid, (inv, 2.0))
    pos = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos, recipe_version_id=rv)

    order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="CREDITED",
        line_items=[_make_li_with_item(pos, qty=3)],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1", uuid.UUID(tid)
        )
        == 0
    ), "CREDITED must produce NO forward depletion (the v4 regression)"
    row = await admin_conn.fetchrow(
        "SELECT depletion_status, depletion_reason FROM sale_line_items WHERE tenant_id=$1",
        uuid.UUID(tid),
    )
    assert (row["depletion_status"], row["depletion_reason"]) == ("failed", "sale_ineligible")


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 22 — voided line transitions to failed/sale_ineligible (row status, not just no row)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_voided_line_row_is_failed_sale_ineligible(admin_conn):
    """Gate 22: a voided line lands failed/sale_ineligible at the ROW level — the gap the
    parametrized ineligible test left by never varying is_voided. FAILS IF a voided line stays
    pending or carries a different reason (a voided line that wrote no movements but stayed
    pending would pass the existing no-movement test while polluting the stuck-pending monitor)."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom = await _seed_uom(admin_conn, tid)
    inv = await _seed_inventory_item(admin_conn, tid, uom, mode="recipe_deducted")
    rv = await _seed_recipe(admin_conn, tid, (inv, 2.0))
    pos = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos, recipe_version_id=rv)

    li = {**_make_li_with_item(pos, qty=2), "exchanged": True}  # exchanged → is_voided
    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID", line_items=[li]
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    row = await admin_conn.fetchrow(
        "SELECT depletion_status, depletion_reason FROM sale_line_items WHERE tenant_id=$1",
        uuid.UUID(tid),
    )
    assert (row["depletion_status"], row["depletion_reason"]) == ("failed", "sale_ineligible")
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1", uuid.UUID(tid)
        )
        == 0
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 26 + fail-3 (modifier half) — modifier edits don't alter a processed sale's ledger
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_e2e_modifier_edit_does_not_alter_processed_sale_ledger(admin_conn):
    """Gate 26 / fail-3 (modifier half): a real modifier un-confirm after a processed modifier
    sale leaves that sale's modifier movements + frozen slim unchanged. FAILS IF the modifier
    edit alters the historical ledger. Symmetric partner of gate 25 (recipe), with the same
    vacuousness precondition: assert the modifier movement exists with the original
    modifier_version_id in its key BEFORE the edit, so a disconnected fixture can't pass emptily."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uid = await admin_conn.fetchval(
        "SELECT user_id FROM user_tenants WHERE tenant_id=$1 LIMIT 1", uuid.UUID(tid)
    )
    uom = await _seed_uom(admin_conn, tid)
    base_item = await _seed_inventory_item(admin_conn, tid, uom, mode="recipe_deducted")
    mod_item = await _seed_inventory_item(admin_conn, tid, uom, mode="recipe_deducted")
    pos = f"POS_{uuid.uuid4().hex[:8]}"
    mi_id, _rv = await _seed_confirmed_recipe_on_pos_item(admin_conn, tid, pos, base_item, 2.0)
    # _seed_confirmed_recipe_on_pos_item returns a raw asyncpg UUID; _seed_confirmed_modifier
    # does uuid.UUID(menu_item_id) internally → coerce to str.
    pos_mod_id = await _seed_confirmed_modifier(admin_conn, tid, str(mi_id), mod_item, mod_qty=5.0)

    mod_row = await admin_conn.fetchrow(
        "SELECT id, current_version_id FROM modifiers WHERE tenant_id=$1 AND pos_modifier_id=$2",
        uuid.UUID(tid),
        pos_mod_id,
    )
    modifier_id, mv_id = mod_row["id"], mod_row["current_version_id"]

    li = _make_li_with_item(pos, qty=3)
    li["modifications"] = {"elements": [{"modifier": {"id": pos_mod_id}, "quantity": 2}]}
    order = make_clover_order(
        order_id=seed["vendor_event_id"], state="locked", payment_state="PAID", line_items=[li]
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )
    worker = InboxWorker()
    await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    # ── VACUOUSNESS PRECONDITION: the modifier movement exists, keyed by modifier_version v1 ──
    mod_mv = await admin_conn.fetchrow(
        "SELECT id, delta, idempotency_key FROM inventory_movements"
        " WHERE tenant_id=$1 AND inventory_item_id=$2",
        uuid.UUID(tid),
        uuid.UUID(str(mod_item)),
    )
    assert mod_mv is not None, "precondition: the modifier sale must have depleted"
    assert "modifier" in mod_mv["idempotency_key"]
    assert str(mv_id) in mod_mv["idempotency_key"], "precondition: depleted against modifier v1"
    mod_mv_before = dict(mod_mv)
    slim_mv_before = await admin_conn.fetchval(
        "SELECT modifier_version_id FROM sale_line_item_modifiers WHERE tenant_id=$1",
        uuid.UUID(tid),
    )
    mver_before = dict(
        await admin_conn.fetchrow(
            "SELECT id, modifier_id, version_number, yield_quantity FROM modifier_versions WHERE id=$1",
            mv_id,
        )
    )

    # ── real modifier un-confirm (operator path) on a committed session ──
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        await modifiers_repo.unconfirm_modifier(
            session, UUID(tid), UUID(str(mi_id)), UUID(str(modifier_id)), UUID(str(uid))
        )
        await session.flush()
        await trans.commit()

    # pointer cleared (un-confirm semantics)
    assert (
        await admin_conn.fetchval(
            "SELECT current_version_id FROM modifiers WHERE id=$1", modifier_id
        )
        is None
    )

    # the processed sale is UNTOUCHED
    assert (
        dict(
            await admin_conn.fetchrow(
                "SELECT id, delta, idempotency_key FROM inventory_movements"
                " WHERE tenant_id=$1 AND inventory_item_id=$2",
                uuid.UUID(tid),
                uuid.UUID(str(mod_item)),
            )
        )
        == mod_mv_before
    ), "modifier edit must not alter the processed sale's ledger (gate 26)"
    assert (
        await admin_conn.fetchval(
            "SELECT modifier_version_id FROM sale_line_item_modifiers WHERE tenant_id=$1",
            uuid.UUID(tid),
        )
        == slim_mv_before
    ), "frozen slim modifier_version_id must be unchanged"
    assert (
        dict(
            await admin_conn.fetchrow(
                "SELECT id, modifier_id, version_number, yield_quantity FROM modifier_versions WHERE id=$1",
                mv_id,
            )
        )
        == mver_before
    ), "modifier_versions row must be byte-identical (fail-gate 7 modifier half)"


# ═══════════════════════════════════════════════════════════════════════════════
# Gate 10 + fail-11 — confirm on a zero-ingredient draft hits the authoritative EmptyDraft
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_confirm_zero_ingredient_draft_raises_emptydraft(db) -> None:
    """Gate 10 / fail-11: confirm on a draft that EXISTS with zero ingredients hits the
    authoritative post-lock check (repo.py:463 → EmptyDraft → 400), distinct from the API
    fast-path AND from no-draft. FAILS IF confirm creates a version instead of raising. The
    zero-ingredient draft is seeded directly because the API rejects it earlier — the branch
    exists precisely to guard states that bypass the API path."""
    tid = str(uuid7())
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:t,'T',:s)"),
        {"t": tid, "s": f"t-{uuid.uuid4().hex[:8]}"},
    )
    mi_id = (
        await db.execute(
            text("INSERT INTO menu_items (tenant_id, name) VALUES (:t,'mi') RETURNING id"),
            {"t": tid},
        )
    ).scalar_one()
    recipe_id = (
        await db.execute(
            text(
                "INSERT INTO recipes (tenant_id, menu_item_id, status) VALUES (:t,:mi,'draft')"
                " RETURNING id"
            ),
            {"t": tid, "mi": mi_id},
        )
    ).scalar_one()
    await db.execute(
        text(
            "INSERT INTO recipe_drafts (tenant_id, recipe_id, draft_ingredients)"
            " VALUES (:t,:r, CAST('[]' AS jsonb))"
        ),
        {"t": tid, "r": recipe_id},
    )
    await db.flush()

    with pytest.raises(recipes_repo.EmptyDraft):
        await recipes_repo.confirm_recipe(db, UUID(tid), UUID(str(mi_id)))

    # the deep branch raised BEFORE creating anything: no version, draft intact, status unchanged
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM recipe_versions WHERE recipe_id=:r"), {"r": recipe_id}
        )
    ).scalar() == 0, "EmptyDraft must raise before any recipe_versions row is created"
    assert (
        await db.execute(text("SELECT status FROM recipes WHERE id=:r"), {"r": recipe_id})
    ).scalar() == "draft"
    assert (
        await db.execute(
            text("SELECT COUNT(*) FROM recipe_drafts WHERE recipe_id=:r"), {"r": recipe_id}
        )
    ).scalar() == 1, "draft must be intact (not deleted by a partial confirm)"


# ═══════════════════════════════════════════════════════════════════════════════
# Fail-9 — depleted status + its movements are atomic (injected-rollback)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@respx.mock
async def test_depleted_status_and_movements_are_atomic_injected_rollback(admin_conn):
    """Fail-gate 9: a 'depleted' row can NEVER exist without its movements. Inject a failure
    AFTER write_movement but BEFORE the status='depleted' commit; the per-line transaction must
    roll back BOTH — no movement rows, line not depleted. FAILS IF a movement persists or the
    line reads 'depleted'. autospec + assert_called so a future rename/inline of _set_status
    makes the injection fail LOUDLY (AttributeError at patch / never-called) instead of the test
    passing emptily — the vacuousness trap in patch form."""
    seed = await seed_worker_prereqs(admin_conn)
    tid = seed["tenant_id"]
    uom = await _seed_uom(admin_conn, tid)
    inv = await _seed_inventory_item(admin_conn, tid, uom, mode="recipe_deducted")
    rv = await _seed_recipe(admin_conn, tid, (inv, 2.0))
    pos = f"POS_{uuid.uuid4().hex[:8]}"
    await _seed_menu_item(admin_conn, tid, pos, recipe_version_id=rv)

    order = make_clover_order(
        order_id=seed["vendor_event_id"],
        state="locked",
        payment_state="PAID",
        line_items=[_make_li_with_item(pos, qty=2)],
    )
    respx.get(url__regex=f".*/orders/{seed['vendor_event_id']}.*").mock(
        return_value=httpx.Response(200, json=order)
    )

    real_set_status = handler._set_status

    async def flaky(session, tenant_id, sale_line_item_id, status, reason):
        if status == "depleted":  # raise AFTER movements were written, BEFORE status commits
            raise RuntimeError("injected crash between movement write and status commit")
        return await real_set_status(session, tenant_id, sale_line_item_id, status, reason)

    worker = InboxWorker()
    with patch.object(handler, "_set_status", autospec=True, side_effect=flaky) as m:
        await worker.process_event((await worker.claim_batch(batch_size=1))[0])

    m.assert_called()  # the injection point actually fired (guards a future rename/inline)
    # the rollback erased EVERYTHING the txn would have written:
    assert (
        await admin_conn.fetchval(
            "SELECT COUNT(*) FROM inventory_movements WHERE tenant_id=$1", uuid.UUID(tid)
        )
        == 0
    ), "movements written in the same txn must roll back with the failed status"
    assert (
        await admin_conn.fetchval(
            "SELECT depletion_status FROM sale_line_items WHERE tenant_id=$1", uuid.UUID(tid)
        )
        == "pending"
    ), "no depleted-without-movements; the line stays pending and retries"
    assert await _event_state(admin_conn, seed["inbox_id"]) == "failed"
