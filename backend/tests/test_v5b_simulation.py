"""V5b — restaurant-day simulation: the real worker, 3 concurrent terminals, counts, vs oracle.

The restaurant-facing proof ("this handles your busiest day"). Seeds Clover events into
pos_event_inbox and drains them with 3 concurrent InboxWorker loops (real claim_batch contention
= the live load test of the F4 MATERIALIZED fix), with operator counts at barriers between waves,
then asserts the final per-ingredient ledger AND on-hand match the independent oracle.

Two sizes: a fast default (runs in the suite) and the full 2,000-order/3-terminal run gated
behind RUN_V5_SIM=1 (heavy; run explicitly for the headline proof).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_sessionmaker
from tests.depletion_model import oracle
from tests.depletion_model.run import actual_ledger, actual_on_hand
from tests.depletion_model.seed import seed_world
from tests.depletion_model.sim import (
    _wave_created_ms,
    build_clover_order,
    drain_concurrent,
    run_count,
    seed_inbox_payload,
    seed_inbox_wave,
)
from tests.depletion_model.world import (
    MODE_A,
    MODE_B,
    Conversion,
    Count,
    Ingredient,
    Item,
    Line,
    Modifier,
    Order,
    Recipe,
    World,
)

D = Decimal


@pytest.fixture
async def committed_session() -> AsyncIterator[AsyncSession]:
    """A real (committing) session — the worker commits on its own connections, so V5b cannot
    use the rollback harness. The autoclean fixture wipes the fresh tenant afterward."""
    async with get_sessionmaker()() as s:
        yield s


def _sim_world() -> World:
    """A mixed kitchen: Mode A + Mode B items, a shared item across base+modifier, a g→kg
    conversion, a batch-yield recipe (yield≠1), and an unmapped item."""
    return World(
        items=[
            Item("flour", MODE_A, "kg"),
            Item("milk", MODE_A, "ml"),
            Item("beans", MODE_B, "g", yield_factor=D("0.9"), opening_count=D("100000")),
            Item("stock", MODE_A, "ml"),
        ],
        conversions=[Conversion("g", "kg", D("0.001"))],
        recipes=[
            Recipe("bread", D("2"), [Ingredient("flour", D("500"), "g")]),  # g→kg + yield
            Recipe(
                "latte",
                D("1"),
                [Ingredient("milk", D("200"), "ml"), Ingredient("beans", D("18"), "g")],
            ),  # Mode A + Mode B
            Recipe("soup", D("10"), [Ingredient("stock", D("5000"), "ml")]),  # batch yield 10
            Recipe(
                "special", D("1"), [Ingredient("flour", D("50"), "g")], confirmed=False
            ),  # unmapped
        ],
        modifiers=[
            Modifier("extra_milk", "latte", D("1"), [Ingredient("milk", D("50"), "ml")]),
        ],
    )


def _build_waves(world: World, per_wave: int, n_waves: int) -> list[list[Order]]:
    """Deterministic order stream (no RNG): cycle menu items, vary qty + modifiers + a few
    ineligible lines by index, so every code path is hit at scale."""
    menus = ["bread", "latte", "soup", "special"]
    waves: list[list[Order]] = []
    for w in range(n_waves):
        orders: list[Order] = []
        for i in range(per_wave):
            mi = menus[i % len(menus)]
            qty = D(1 + (i % 4))  # 1..4
            mods = [("extra_milk", D(1 + (i % 2)))] if (mi == "latte" and i % 3 == 0) else []
            line = Line(mi, qty, modifiers=mods)
            if i % 11 == 0:  # ~9% voided
                line.is_voided = True
            order = Order([line])
            if i % 13 == 0:  # ~8% unpaid → ingested-but-ineligible (still must net to nothing)
                order.payment_state = "OPEN"
            orders.append(order)
        waves.append(orders)
    return waves


async def _simulate(
    s: AsyncSession,
    world: World,
    waves: list[list[Order]],
    counts_by_wave: dict[int, list[Count]],
    *,
    n_workers: int = 3,
) -> None:
    seeded = await seed_world(s, world)
    await s.commit()

    timeline: list[object] = []
    for w, orders in enumerate(waves):
        await seed_inbox_wave(s, seeded, orders, _wave_created_ms(w))
        await s.commit()
        await drain_concurrent(s, seeded.tenant_id, n_workers=n_workers)
        timeline.extend(orders)
        for count in counts_by_wave.get(w, []):
            await run_count(s, seeded, count, w)
            await s.commit()
            timeline.append(count)

    exp_ledger, exp_oh = oracle.expected_timeline(world, timeline, partial_refunds_enabled=True)
    act_ledger = await actual_ledger(s, seeded)
    act_oh = await actual_on_hand(s, seeded, world)
    assert act_ledger == exp_ledger, (
        f"LEDGER mismatch ({len(timeline)} events)\n exp={exp_ledger}\n act={act_ledger}"
    )
    assert act_oh == exp_oh, f"ON-HAND mismatch\n exp={exp_oh}\n act={act_oh}"


@pytest.mark.integration
async def test_v5b_simulation_fast(committed_session: AsyncSession) -> None:
    """Fast default: 2 waves x 20 orders, 3 terminals, a barrier count (Mode B re-anchor +
    Mode A drift) between waves."""
    world = _sim_world()
    waves = _build_waves(world, per_wave=20, n_waves=2)
    counts = {0: [Count("beans", D("90000")), Count("stock", D("12345"))]}
    await _simulate(committed_session, world, waves, counts)


@pytest.mark.integration
async def test_v5b_duplicate_delivery_idempotent(committed_session: AsyncSession) -> None:
    """Idempotency under duplicate webhook delivery (a real busiest-day event: Clover re-sends an
    order). The SAME order payload is delivered TWICE (two distinct inbox rows, identical
    vendor_event_id + line ids) and drained by 3 terminals → the ledger must NOT double. This is
    the positive, load-bearing idempotency claim (distinct from M-mat / F4 over-claim)."""
    s = committed_session
    world = _sim_world()
    seeded = await seed_world(s, world)
    await s.commit()

    order = Order([Line("latte", D("3"), modifiers=[("extra_milk", D("2"))])])
    payload = build_clover_order(seeded, order, _wave_created_ms(0))
    # Same order, two webhook deliveries (CREATE then UPDATE — distinct rows past the inbox dedup,
    # identical clover order + line ids). uq_sli_clover must dedup the lines → no double depletion.
    await seed_inbox_payload(s, seeded, payload, vendor_event_type="CREATE")
    await seed_inbox_payload(s, seeded, payload, vendor_event_type="UPDATE")
    await s.commit()
    await drain_concurrent(s, seeded.tenant_id, n_workers=3)

    exp_ledger, exp_oh = oracle.expected_timeline(world, [order], partial_refunds_enabled=True)
    assert await actual_ledger(s, seeded) == exp_ledger, "duplicate delivery doubled the ledger"
    assert await actual_on_hand(s, seeded, world) == exp_oh


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_V5_SIM") != "1", reason="heavy 2000-order sim; set RUN_V5_SIM=1")
async def test_v5b_simulation_2000_orders(committed_session: AsyncSession) -> None:
    """The headline: ~2,000 orders across the day, 3 concurrent terminals, counts at every wave
    barrier. Real-world load test of claim_batch / the F4 MATERIALIZED fix under contention."""
    world = _sim_world()
    waves = _build_waves(world, per_wave=250, n_waves=8)  # 2,000 orders
    counts = {
        w: [Count("beans", D(95000 - w * 1000)), Count("stock", D(10000 + w * 100))]
        for w in range(7)
    }  # a count after each wave except the last
    await _simulate(committed_session, world, waves, counts, n_workers=3)
