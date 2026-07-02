"""V3 — metamorphic relations on the depletion engine.

Where V2 asks "is the number right?" (engine vs independent oracle), V3 asks "does the engine
obey invariants that must hold REGARDLESS of the number?" Most relations here compare two REAL
engine runs to each other (original vs transformed stream) with NO oracle — so a bug the oracle
would share still shows.

No-tolerance discipline (from V2): relations that re-divide (split/scale/batch) use
evenly-divisible quantities and the engine's multiply-then-divide order, so they are EXACT, not
approximate. Even divisibility is a deliberate constraint stated per test, not a hidden cap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from tests.depletion_model.run import actual_ledger
from tests.depletion_model.seed import SALE_AT, seed_orders, seed_world
from tests.depletion_model.transforms import permute, scale_qty, split_each_line
from tests.depletion_model.world import (
    MODE_A,
    Conversion,
    Ingredient,
    Item,
    Line,
    Order,
    Recipe,
    World,
)

D = Decimal


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


async def _run(db: AsyncSession, world: World, orders: list[Order]) -> dict[str, Decimal]:
    """Seed the world in a FRESH tenant, drive the real engine over the stream, return net δ
    per item key (keys are stable across seedings, so two runs are directly comparable)."""
    seeded = await seed_world(db, world)
    lines = await seed_orders(db, world, orders, seeded)
    tid = UUID(seeded.tenant_id)
    for sli_id, line in lines:
        await handler.process_line(db, tid, UUID(sli_id), recorded_at=SALE_AT)
        if line.refund_after_deplete:
            await handler.reverse_line(db, tid, UUID(sli_id))
    return await actual_ledger(db, seeded)


# ── a small reusable world (Mode A, single ingredient, evenly-divisible) ──────────
def _world() -> World:
    return World(
        items=[Item("flour", MODE_A, "g")],
        recipes=[
            Recipe("bun", yield_quantity=D("2"), ingredients=[Ingredient("flour", D("100"), "g")]),
        ],
    )


# ── Tier 1: EXACT, engine-vs-engine ─────────────────────────────────────────────


@pytest.mark.integration
async def test_commutativity_order_independent(db: AsyncSession) -> None:
    """Permuting order/line processing order leaves net δ identical (sums commute)."""
    stream = [
        Order([Line("bun", D("3")), Line("bun", D("5"))]),
        Order([Line("bun", D("2"))]),
    ]
    base = await _run(db, _world(), stream)
    perm = await _run(db, _world(), permute(stream))
    assert perm == base, f"commutativity broken\n base={base}\n perm={perm}"


@pytest.mark.integration
async def test_eligibility_isolation(db: AsyncSession) -> None:
    """Inserting ineligible lines (voided / refunded / unpaid order / not-locked) does not
    change net δ."""
    stream = [Order([Line("bun", D("4"))])]
    augmented = [
        Order([Line("bun", D("4"))]),
        Order([Line("bun", D("9"), is_voided=True)]),
        Order([Line("bun", D("7"), is_refunded=True)]),
        Order([Line("bun", D("8"))], payment_state="OPEN"),
        Order([Line("bun", D("6"))], order_state="open"),
    ]
    assert await _run(db, _world(), augmented) == await _run(db, _world(), stream)


@pytest.mark.integration
async def test_inert_isolation_unmapped_line(db: AsyncSession) -> None:
    """A line for a menu item with no confirmed recipe (unmapped) contributes nothing."""
    world = World(
        items=[Item("flour", MODE_A, "g")],
        recipes=[
            Recipe("bun", yield_quantity=D("2"), ingredients=[Ingredient("flour", D("100"), "g")]),
            Recipe(
                "special",
                yield_quantity=D("1"),
                ingredients=[Ingredient("flour", D("50"), "g")],
                confirmed=False,
            ),
        ],
    )
    stream = [Order([Line("bun", D("4"))])]
    augmented = [Order([Line("bun", D("4")), Line("special", D("99"))])]
    assert await _run(db, world, augmented) == await _run(db, world, stream)


@pytest.mark.integration
async def test_tenant_isolation(db: AsyncSession) -> None:
    """Running the SAME stream in a second tenant does not change the first tenant's ledger
    (no cross-tenant leakage). t1-alone == t2-with-t3-also-running."""
    stream = [Order([Line("bun", D("3")), Line("bun", D("5"))])]
    alone = await _run(db, _world(), stream)  # tenant t1
    with_neighbor_self = await _run(db, _world(), stream)  # t2
    _neighbor = await _run(db, _world(), stream)  # t3 runs in the same session
    assert with_neighbor_self == alone, "tenant isolation broken (neighbor affected ledger)"


@pytest.mark.integration
async def test_replay_idempotency(db: AsyncSession) -> None:
    """Re-processing the same sale lines writes zero new movements; net δ unchanged."""
    world = _world()
    seeded = await seed_world(db, world)
    stream = [Order([Line("bun", D("3")), Line("bun", D("5"))])]
    lines = await seed_orders(db, world, stream, seeded)
    tid = UUID(seeded.tenant_id)
    for sli_id, _ in lines:
        await handler.process_line(db, tid, UUID(sli_id), recorded_at=SALE_AT)
    first = await actual_ledger(db, seeded)
    for sli_id, _ in lines:  # replay
        await handler.process_line(db, tid, UUID(sli_id), recorded_at=SALE_AT)
    assert await actual_ledger(db, seeded) == first, "replay changed the ledger"


@pytest.mark.integration
async def test_refund_symmetry_nets_to_zero(db: AsyncSession) -> None:
    """A depleted-then-reversed line nets to zero — a stream with it equals the stream without
    it."""
    base = [Order([Line("bun", D("4"))])]
    with_refunded = [
        Order([Line("bun", D("4")), Line("bun", D("6"), refund_after_deplete=True)]),
    ]
    assert await _run(db, _world(), with_refunded) == await _run(db, _world(), base)


# ── Tier 2: re-division relations, made EXACT by even divisibility ────────────────


@pytest.mark.integration
async def test_additivity_split_line(db: AsyncSession) -> None:
    """Splitting a line of qty q into (1, q-1) does not change net δ. Q=100, yield=2 → per-unit
    50 (exact); any integer split sums to the same total."""
    stream = [Order([Line("bun", D("6"))]), Order([Line("bun", D("4"))])]
    assert await _run(db, _world(), split_each_line(stream)) == await _run(db, _world(), stream)


@pytest.mark.integration
async def test_homogeneity_scale_by_k(db: AsyncSession) -> None:
    """Scaling all qtys by integer k scales every δ by exactly k (multiply-then-divide order +
    even divisibility keep it exact)."""
    stream = [Order([Line("bun", D("3")), Line("bun", D("2"))])]
    k = 4
    base = await _run(db, _world(), stream)
    scaled = await _run(db, _world(), scale_qty(stream, k))
    expected = {item: d * k for item, d in base.items()}
    assert scaled == expected, f"homogeneity broken: base x{k} != scaled\n {base}\n {scaled}"


# ── Batch-yield relation (the production-untested path; founder-requested) ────────


@pytest.mark.integration
async def test_batch_ratio_per_serving(db: AsyncSession) -> None:
    """A recipe with yield=N depletes exactly Q/N of an ingredient per serving. Q=12 divisible
    by N=3 → 4 per serving; selling 1 serving depletes 4."""
    world = World(
        items=[Item("dough", MODE_A, "g")],
        recipes=[
            Recipe("pizza", yield_quantity=D("3"), ingredients=[Ingredient("dough", D("12"), "g")]),
        ],
    )
    ledger = await _run(db, world, [Order([Line("pizza", D("1"))])])
    assert ledger == {"dough": D("-4")}, f"per-serving batch ratio wrong: {ledger}"


@pytest.mark.integration
async def test_batch_ratio_invariant_across_yield(db: AsyncSession) -> None:
    """Selling N servings of a yield-N recipe depletes exactly one batch (Q), invariant across
    N — (N×Q)/N = Q exactly (multiply-then-divide, no rounding). This is the batch-cooking case
    V2 found untested in production (yield hardcoded to 1)."""
    batch = D("600")
    results = []
    for n in ("1", "3", "5", "8"):
        world = World(
            items=[Item("stock", MODE_A, "ml")],
            recipes=[
                Recipe("soup", yield_quantity=D(n), ingredients=[Ingredient("stock", batch, "ml")]),
            ],
        )
        ledger = await _run(db, world, [Order([Line("soup", D(n))])])  # sell N servings
        results.append((n, ledger))
    for n, ledger in results:
        assert ledger == {"stock": -batch}, f"batch invariance broken at yield={n}: {ledger}"


# ── Conversion invariance (the one oracle-free correctness pair) ──────────────────


@pytest.mark.integration
async def test_conversion_invariance_unit_relabel(db: AsyncSession) -> None:
    """Expressing an ingredient as 1000 g (identity to a g-stored item) vs 1 kg (+ a kg→g
    conversion) yields identical δ — conversion correctness as a metamorphic relation."""
    grams = World(
        items=[Item("milk", MODE_A, "g")],
        recipes=[
            Recipe(
                "latte", yield_quantity=D("1"), ingredients=[Ingredient("milk", D("1000"), "g")]
            ),
        ],
    )
    kilos = World(
        items=[Item("milk", MODE_A, "g")],
        conversions=[Conversion("kg", "g", D("1000"))],
        recipes=[
            Recipe("latte", yield_quantity=D("1"), ingredients=[Ingredient("milk", D("1"), "kg")]),
        ],
    )
    stream = [Order([Line("latte", D("2"))])]
    assert await _run(db, kilos, stream) == await _run(db, grams, stream)  # both → milk -2000
