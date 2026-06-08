"""Sprint 5 Phase 9 — base-recipe depletion (Walker + Writer + handler.process_line).

Sub-step 1: the new compute-then-commit base path, tested directly against seeded data
via the bound-transaction harness (one connection/transaction, rolled back — no residue).
The live-worker switch + old-writer retirement is sub-step 2; modifier depletion + worker
modifier-extraction is sub-step 3; all committed together as the 9/10 deploy gate.

Proves the keystone properties: Mode A/B types+signs, eligibility wired (ineligible →
failed, no rows), the no-confirmed-recipe reasons, missing_conversion → failed/no rows,
ATOMIC idempotent replay (re-run → one movement, via ON CONFLICT), the legacy-key guard,
and per-item aggregation (two ingredient rows → one summed movement).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from tests.helpers.sprint5 import seed_recipe_version_session


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


async def _seed(
    db: AsyncSession,
    *,
    mode: str = "recipe_deducted",
    recipe_qty: float = 2,
    line_qty: float = 3,
    yield_q: float = 1.0,
    payment_state: str = "PAID",
    order_state: str = "locked",
    is_refunded: bool = False,
    is_voided: bool = False,
    recipe_status: str = "confirmed",
    snapshot_rv: bool = True,
    recipe_unit: str = "g",
    storage_unit: str = "g",
    factor: float = 1.0,
    extra_ingredient_same_item: float | None = None,
) -> dict[str, Any]:
    tid = str(uuid7())
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:t,'T',:s)"),
        {"t": tid, "s": f"t-{uuid.uuid4().hex[:8]}"},
    )
    su_id = (
        await db.execute(
            text(
                "INSERT INTO units_of_measure (tenant_id, name, abbreviation, unit_type)"
                " VALUES (:t,:n,:n,'weight') RETURNING id"
            ),
            {"t": tid, "n": storage_unit},
        )
    ).scalar_one()
    # count_anchored requires cadence+grace (cadence_coherence CHECK)
    cadence = 7 if mode == "count_anchored" else None
    item_id = (
        await db.execute(
            text("""
                INSERT INTO inventory_items
                    (tenant_id, name, inventory_mode, storage_unit_id, recipe_unit_id,
                     storage_to_recipe_factor, count_cadence_days, count_grace_days)
                VALUES (:t, :n, :mode, :su, :su, :factor, :cad, :cad) RETURNING id
            """),
            {"t": tid, "n": f"it-{uuid.uuid4().hex[:6]}", "mode": mode, "su": su_id,
             "cad": cadence, "factor": factor},
        )
    ).scalar_one()

    ingredients = [(item_id, recipe_qty, recipe_unit)]
    if extra_ingredient_same_item is not None:
        ingredients.append((item_id, extra_ingredient_same_item, recipe_unit))
    seeded = await seed_recipe_version_session(
        db, tid, ingredients=ingredients, yield_quantity=yield_q, status=recipe_status
    )

    inbox_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO pos_event_inbox
            (inbox_id, tenant_id, vendor, vendor_event_id, vendor_object_type,
             vendor_event_type, vendor_ts, raw_payload, signature_verified, source)
            VALUES (:iid,:t,'clover','O:p9','O','UPDATE',0,'{}',false,'webhook')
        """),
        {"iid": inbox_id, "t": tid},
    )
    order_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO orders
            (id, tenant_id, pos_event_inbox_id, clover_order_id, total_amount_cents,
             state, payment_state, processed_at)
            VALUES (:oid,:t,:iid,'p9_order',0,:st,:ps,now())
        """),
        {"oid": order_id, "t": tid, "iid": inbox_id, "st": order_state, "ps": payment_state},
    )
    sli_id = str(uuid.uuid4())
    await db.execute(
        text("""
            INSERT INTO sale_line_items
            (id, tenant_id, order_id, clover_line_item_id, menu_item_id, name_at_sale,
             quantity, price_cents_at_sale, net_revenue_cents, is_refunded, is_voided,
             recipe_version_id, depletion_status)
            VALUES (:id,:t,:oid,:cli,:mid,'Item',:qty,0,0,:ref,:void,:rv,'pending')
        """),
        {
            "id": sli_id, "t": tid, "oid": order_id, "cli": f"cli_{uuid.uuid4().hex[:8]}",
            "mid": seeded.menu_item_id, "qty": line_qty, "ref": is_refunded, "void": is_voided,
            "rv": seeded.recipe_version_id if snapshot_rv else None,
        },
    )
    await db.flush()
    return {"tid": tid, "sli_id": sli_id, "item_id": str(item_id), "seeded": seeded}


async def _movements(db: AsyncSession, tid: str, item_id: str) -> list[Any]:
    return (
        await db.execute(
            text(
                "SELECT movement_type, delta, idempotency_key FROM inventory_movements"
                " WHERE tenant_id = :t AND inventory_item_id = :i ORDER BY idempotency_key"
            ),
            {"t": tid, "i": item_id},
        )
    ).mappings().all()


# ── Mode A / Mode B ──────────────────────────────────────────────────────────


@pytest.mark.integration
async def test_mode_a_depletes_negative(db) -> None:
    s = await _seed(db, mode="recipe_deducted", recipe_qty=2, line_qty=3, yield_q=1.0)
    status, reason = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, reason) == ("depleted", None)
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert len(mvs) == 1
    assert mvs[0]["movement_type"] == "sale_depletion"
    assert Decimal(str(mvs[0]["delta"])) == Decimal("-6")  # -(3*2/1)
    assert mvs[0]["idempotency_key"] == f"sale_line:{s['sli_id']}:base:{s['seeded'].recipe_version_id}:{s['item_id']}"


@pytest.mark.integration
async def test_mode_b_signals_positive(db) -> None:
    s = await _seed(db, mode="count_anchored", recipe_qty=2, line_qty=3)
    status, reason = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, reason) == ("depleted", None)
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert mvs[0]["movement_type"] == "sale_signal"
    assert Decimal(str(mvs[0]["delta"])) == Decimal("6")


@pytest.mark.integration
async def test_yield_quantity_divides(db) -> None:
    s = await _seed(db, recipe_qty=10, line_qty=2, yield_q=4.0)  # -(2*10/4) = -5
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert Decimal(str(mvs[0]["delta"])) == Decimal("-5")


# ── eligibility wired ────────────────────────────────────────────────────────


@pytest.mark.integration
@pytest.mark.parametrize(
    "payment_state,order_state,reason",
    [("OPEN", "locked", "sale_ineligible"), ("PAID", "open", "sale_ineligible"),
     ("REFUNDED", "locked", "sale_ineligible"), ("CREDITED", "locked", "sale_ineligible")],
)
async def test_ineligible_fails_no_rows(db, payment_state, order_state, reason) -> None:
    s = await _seed(db, payment_state=payment_state, order_state=order_state)
    status, r = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, r) == ("failed", reason)
    assert await _movements(db, s["tid"], s["item_id"]) == []


@pytest.mark.integration
async def test_refunded_line_is_line_refunded_no_rows(db) -> None:
    s = await _seed(db, is_refunded=True)
    status, r = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, r) == ("failed", "line_refunded")
    assert await _movements(db, s["tid"], s["item_id"]) == []


# ── no-confirmed-recipe (NULL frozen snapshot) → unmapped/no_recipe ──────────


@pytest.mark.integration
@pytest.mark.parametrize("recipe_status", ["draft", "skipped", "confirmed"])
async def test_null_snapshot_is_unmapped_no_recipe(db, recipe_status) -> None:
    """A NULL frozen recipe_version_id → unmapped/no_recipe, REGARDLESS of the recipe's
    current state. The worker (service_worker) cannot read the operator-owned recipes
    table, so it deliberately does not distinguish never-created / draft / skipped — that
    finer breakdown is a reporting concern. (Proves non-consultation of recipes: the same
    verdict for draft, skipped, and confirmed-but-unsnapshotted.)"""
    s = await _seed(db, snapshot_rv=False, recipe_status=recipe_status)
    status, r = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, r) == ("unmapped", "no_recipe")
    assert await _movements(db, s["tid"], s["item_id"]) == []


# ── storage_to_recipe_factor is vestigial — NOT consulted by depletion ───────


@pytest.mark.integration
async def test_storage_to_recipe_factor_is_not_consulted(db) -> None:
    """v5 §11 replaces storage_to_recipe_factor with unit conversion. With factor=0.5 and
    identity units (g→g), the delta must be -(line·recipe/yield), the SAME as factor=1 —
    i.e. the factor is IGNORED. Pinned at factor≠1 deliberately: a factor-1 test couldn't
    prove non-consultation (old and new models agree at 1). If the code still divided by
    the factor, the delta would be -12 (= -6/0.5), not -6."""
    s = await _seed(db, recipe_qty=2, line_qty=3, yield_q=1.0, factor=0.5)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert Decimal(str(mvs[0]["delta"])) == Decimal("-6")  # -(3*2/1), factor 0.5 ignored


# ── missing conversion ───────────────────────────────────────────────────────


@pytest.mark.integration
async def test_missing_conversion_fails_no_rows(db) -> None:
    # recipe unit 'ml' (volume) vs storage 'g' (weight): cross-dimension, no item density
    s = await _seed(db, recipe_unit="ml", storage_unit="g")
    status, r = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, r) == ("failed", "missing_conversion")
    assert await _movements(db, s["tid"], s["item_id"]) == []


# ── atomic idempotent replay + legacy guard ──────────────────────────────────


@pytest.mark.integration
async def test_replay_is_idempotent(db) -> None:
    s = await _seed(db)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))  # re-run
    assert len(await _movements(db, s["tid"], s["item_id"])) == 1  # ON CONFLICT, no dup


@pytest.mark.integration
async def test_legacy_key_guard_skips_new_write(db) -> None:
    s = await _seed(db)
    # a legacy-format movement already exists for this (sli, item)
    await db.execute(
        text("""
            INSERT INTO inventory_movements
                (tenant_id, inventory_item_id, movement_type, delta, source_type,
                 source_id, idempotency_key)
            VALUES (:t,:i,'sale_depletion',-6,'sale_line_item',:sid,:k)
        """),
        {"t": s["tid"], "i": s["item_id"], "sid": s["sli_id"],
         "k": f"sale_line:{s['sli_id']}:{s['item_id']}"},
    )
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert len(mvs) == 1  # only the legacy row; new-key write was skipped
    assert mvs[0]["idempotency_key"] == f"sale_line:{s['sli_id']}:{s['item_id']}"


# ── terminal-status no-op on replay (process once, freeze outcome) ───────────


@pytest.mark.integration
async def test_terminal_status_is_noop_on_replay(db) -> None:
    """A line already in a terminal status is frozen: process_line short-circuits before
    eligibility/walk, so the recorded (status, reason) and movements never change — even
    if fresh processing would now produce a different outcome. This is what makes the
    confirmed-drift reason an acceptable one-time capture rather than a live bug."""
    s = await _seed(db)  # a fully depletable PAID line
    # pretend a prior run recorded a (contradictory) terminal outcome
    await db.execute(
        text(
            "UPDATE sale_line_items SET depletion_status='unmapped',"
            " depletion_reason='recipe_draft' WHERE id = :id"
        ),
        {"id": s["sli_id"]},
    )
    status, reason = await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    assert (status, reason) == ("unmapped", "recipe_draft")  # frozen, not recomputed
    assert await _movements(db, s["tid"], s["item_id"]) == []  # walk skipped, no deplete


# ── aggregation: two ingredient rows → one summed movement ───────────────────


@pytest.mark.integration
async def test_duplicate_item_ingredients_aggregate(db) -> None:
    s = await _seed(db, recipe_qty=2, extra_ingredient_same_item=5, line_qty=1, yield_q=1.0)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]))
    mvs = await _movements(db, s["tid"], s["item_id"])
    assert len(mvs) == 1  # one movement per (item, key), not two colliding
    assert Decimal(str(mvs[0]["delta"])) == Decimal("-7")  # -(1*(2+5)/1)


# ── late-signal detection (ported from H0.9/H0.10 to the new handler path) ───


async def _count_event(db: AsyncSession, tid: str, item_id: str) -> None:
    # counted_at defaults now() → a reconciliation boundary at ≈ now
    await db.execute(
        text(
            "INSERT INTO inventory_count_events (tenant_id, inventory_item_id, counted_quantity)"
            " VALUES (:t, :i, 500)"
        ),
        {"t": tid, "i": item_id},
    )


async def _active_late_alert(db: AsyncSession, tid: str) -> Any:
    return (
        await db.execute(
            text(
                "SELECT severity, alert_count FROM monitoring_alerts"
                " WHERE tenant_id = :t AND monitor_name = 'late_signal_reconciliation'"
                "   AND resolved_at IS NULL"
            ),
            {"t": tid},
        )
    ).mappings().fetchone()


@pytest.mark.integration
async def test_late_signal_fires_on_boundary_cross(db) -> None:
    """A newly-written movement whose recorded_at is >30min stale AND crosses a count
    boundary raises a late_signal_reconciliation warn alert (ported from H0.9)."""
    s = await _seed(db, mode="count_anchored")  # eligible PAID line, Mode B
    await _count_event(db, s["tid"], s["item_id"])
    late = datetime.now(UTC) - timedelta(hours=2)
    status, _ = await handler.process_line(
        db, UUID(s["tid"]), UUID(s["sli_id"]), recorded_at=late
    )
    assert status == "depleted"
    alert = await _active_late_alert(db, s["tid"])
    assert alert is not None and alert["severity"] == "warn"


@pytest.mark.integration
async def test_no_late_signal_without_boundary(db) -> None:
    """No count boundary in the window → no alert, even with a stale recorded_at (H0.10)."""
    s = await _seed(db, mode="count_anchored")
    late = datetime.now(UTC) - timedelta(hours=2)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]), recorded_at=late)
    assert await _active_late_alert(db, s["tid"]) is None


@pytest.mark.integration
async def test_late_signal_does_not_refire_on_replay(db) -> None:
    """The new replay surface the old coupled version didn't expose: a replay must not
    re-fire late-signal. The terminal no-op short-circuits before the write, so alert_count
    is not bumped (and the partial-unique guarantees no second active row)."""
    s = await _seed(db, mode="count_anchored")
    await _count_event(db, s["tid"], s["item_id"])
    late = datetime.now(UTC) - timedelta(hours=2)
    await handler.process_line(db, UUID(s["tid"]), UUID(s["sli_id"]), recorded_at=late)
    a1 = await _active_late_alert(db, s["tid"])
    assert a1 is not None and a1["alert_count"] == 1
    # replay: line is terminal (depleted) → process_line short-circuits → no re-fire
    status, _ = await handler.process_line(
        db, UUID(s["tid"]), UUID(s["sli_id"]), recorded_at=late
    )
    assert status == "depleted"
    a2 = await _active_late_alert(db, s["tid"])
    assert a2["alert_count"] == 1  # not bumped — late-signal did not re-fire
