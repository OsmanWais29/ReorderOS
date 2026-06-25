"""V7 load-matrix GAP tests (pre-Sprint-N+1).

Most of the realistic-load matrix is already proven elsewhere:
  - qty>1               -> phase9 test_mode_a_depletes_negative (line_qty=3 -> x3),
                           V3 test_homogeneity_scale_by_k
  - concurrency         -> phase9 test_write_movement_concurrent_arbiter, V5b sim
  - refund nets to zero -> V3 test_refund_symmetry_nets_to_zero, phase11
  - conversion (relabel)-> V3 test_conversion_invariance_unit_relabel

These are the genuine gaps, engine-vs-oracle (exact, no tolerance):
  (2) multiple DISTINCT line items in one order (burger + fries + coke)
  (3) a 20-ingredient recipe (loop/limit check)
  (5) a NON-IDENTITY unit conversion (recipe g, storage kg) — the live cert was identity-only
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, make_bound_session
from app.modules.inventory.depletion import handler
from tests.depletion_model.oracle import expected_ledger
from tests.depletion_model.run import actual_ledger
from tests.depletion_model.seed import SALE_AT, seed_orders, seed_world
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
    seeded = await seed_world(db, world)
    lines = await seed_orders(db, world, orders, seeded)
    tid = UUID(seeded.tenant_id)
    for sli_id, line in lines:
        await handler.process_line(db, tid, UUID(sli_id), recorded_at=SALE_AT)
        if line.refund_after_deplete:
            await handler.reverse_line(db, tid, UUID(sli_id))
    return await actual_ledger(db, seeded)


# (2) multiple distinct line items in ONE order ──────────────────────────────────
@pytest.mark.integration
async def test_multi_distinct_line_items_one_order(db: AsyncSession) -> None:
    world = World(
        items=[Item("beef", MODE_A, "g"), Item("potato", MODE_A, "g"), Item("syrup", MODE_A, "ml")],
        recipes=[
            Recipe("burger", D("1"), [Ingredient("beef", D("150"), "g")]),
            Recipe("fries", D("1"), [Ingredient("potato", D("200"), "g")]),
            Recipe("coke", D("1"), [Ingredient("syrup", D("50"), "ml")]),
        ],
    )
    order = Order(lines=[Line("burger", D("1")), Line("fries", D("1")), Line("coke", D("1"))])
    got = await _run(db, world, [order])
    exp = expected_ledger(world, [order])
    assert got == exp, f"engine != oracle: {got} vs {exp}"
    assert got == {"beef": D("-150"), "potato": D("-200"), "syrup": D("-50")}, got


# (3) 20-ingredient recipe ───────────────────────────────────────────────────────
@pytest.mark.integration
async def test_twenty_ingredient_recipe(db: AsyncSession) -> None:
    items = [Item(f"ing{i}", MODE_A, "g") for i in range(20)]
    # distinct qty 1..20 so a dropped/duplicated/miscounted ingredient is visible
    ingredients = [Ingredient(f"ing{i}", D(str(i + 1)), "g") for i in range(20)]
    world = World(items=items, recipes=[Recipe("mega", D("1"), ingredients)])
    order = Order(lines=[Line("mega", D("1"))])
    got = await _run(db, world, [order])
    exp = expected_ledger(world, [order])
    assert len(got) == 20, f"expected 20 ingredient movements, got {len(got)}"
    assert got == exp, f"engine != oracle: {got} vs {exp}"
    assert got["ing0"] == D("-1") and got["ing19"] == D("-20"), got


# (5) NON-IDENTITY conversion: recipe in g, storage in kg ─────────────────────────
@pytest.mark.integration
async def test_nonidentity_conversion_g_to_kg(db: AsyncSession) -> None:
    world = World(
        items=[Item("flour", MODE_A, "kg")],  # inventory stored/counted in kg
        recipes=[Recipe("loaf", D("1"), [Ingredient("flour", D("500"), "g")])],  # recipe in g
        conversions=[Conversion("g", "kg", "0.001")],  # 1 g = 0.001 kg
    )
    order = Order(lines=[Line("loaf", D("2"))])  # 2 loaves
    got = await _run(db, world, [order])
    exp = expected_ledger(world, [order])
    assert got == exp, f"engine != oracle: {got} vs {exp}"
    # 2 loaves * 500 g / yield 1 = 1000 g = 1.0 kg, recorded in the storage unit (kg)
    assert got["flour"] == D("-1"), f"expected -1 kg, got {got['flour']}"
