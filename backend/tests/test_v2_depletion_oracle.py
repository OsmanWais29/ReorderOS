"""V2 — depletion correctness vs an INDEPENDENT reference oracle.

Each test builds an in-memory World + order stream, seeds it, drives the REAL engine
(process_line / reverse_line), and asserts the actual per-item ledger AND on-hand match the
oracle (tests/depletion_model/oracle.py), which recomputes them from the v5 §11 formula
WITHOUT importing the depletion module. Whole-stream/aggregation/mode-mix/refund-netting bugs
that the per-case phase tests can't see surface here. Reused at scale by V5.

Distinguishing conditions (the sprint throughline — values that coincide-and-mask at 1):
qty≠1, yield_quantity≠1, conversion factor≠1, modifier multiplier≠1, two ingredient rows →
one item (aggregation), base+modifier → one shared item (key non-collision), Mode A and
Mode B in one world, yield_factor≠1 (the on-hand layer the ledger layer can't catch).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, make_bound_session
from tests.depletion_model import oracle
from tests.depletion_model.run import actual_ledger, actual_on_hand, run_stream
from tests.depletion_model.seed import seed_world
from tests.depletion_model.world import (
    MODE_A,
    MODE_B,
    Conversion,
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
async def db() -> AsyncIterator[AsyncSession]:
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = make_bound_session(conn)
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


async def _assert_world(
    db: AsyncSession, world: World, orders: list[Order], *, partial_refunds_enabled: bool = False
) -> None:
    """Seed → drive the real engine → assert both layers match the oracle. Exact Decimal
    equality (no tolerance: the oracle mirrors the engine's per-ingredient-then-sum order)."""
    seeded = await seed_world(db, world)
    await run_stream(db, world, orders, seeded, partial_refunds_enabled=partial_refunds_enabled)

    exp_ledger = oracle.expected_ledger(
        world, orders, partial_refunds_enabled=partial_refunds_enabled
    )
    act_ledger = await actual_ledger(db, seeded)
    assert act_ledger == exp_ledger, f"ledger δ mismatch\n exp={exp_ledger}\n act={act_ledger}"

    exp_oh = oracle.expected_on_hand(world, orders, partial_refunds_enabled=partial_refunds_enabled)
    act_oh = await actual_on_hand(db, seeded, world)
    assert act_oh == exp_oh, f"on-hand mismatch\n exp={exp_oh}\n act={act_oh}"


@pytest.mark.integration
async def test_mode_a_aggregation_conversion_yield(db: AsyncSession) -> None:
    """Mode A workhorse: two ingredient rows → one item (aggregation), a g→kg conversion
    (factor≠1), yield_quantity≠1, line qty≠1. Exercises the full base formula + per-item sum."""
    world = World(
        items=[
            Item("flour", MODE_A, "kg"),
            Item("sugar", MODE_A, "g"),
        ],
        conversions=[Conversion("g", "kg", D("0.001"))],
        recipes=[
            Recipe(
                "cake",
                yield_quantity=D("2"),
                ingredients=[
                    Ingredient("flour", D("300"), "g"),  # → 0.3 kg
                    Ingredient("flour", D("200"), "g"),  # → 0.2 kg (same item: aggregate)
                    Ingredient("sugar", D("200"), "g"),  # identity
                ],
            ),
        ],
    )
    orders = [Order([Line("cake", D("3"))])]
    # flour: 3*(0.3+0.2)/2 = 0.75 → δ -0.75 ; sugar: 3*200/2 = 300 → δ -300
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_mode_b_signal_applies_yield_factor(db: AsyncSession) -> None:
    """Mode B: positive sale_signal, and on_hand applies yield_factor (≠1) — the layer the raw
    ledger cannot catch. anchor 1000, consume 3*150/3=150 signal, yield 0.8 → on_hand
    1000 - 150*0.8 = 880; ledger δ (pre-yield) = +150."""
    world = World(
        items=[Item("beans", MODE_B, "g", yield_factor=D("0.8"), opening_count=D("1000"))],
        recipes=[
            Recipe(
                "latte", yield_quantity=D("3"), ingredients=[Ingredient("beans", D("150"), "g")]
            ),
        ],
    )
    orders = [Order([Line("latte", D("3"))])]
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_base_plus_modifier_shared_item(db: AsyncSession) -> None:
    """Base AND a modifier deplete the SAME item with distinct keys (both apply, no collision);
    modifier multiplier≠1, modifier yield≠1."""
    world = World(
        items=[Item("milk", MODE_A, "ml")],
        recipes=[
            Recipe(
                "latte", yield_quantity=D("1"), ingredients=[Ingredient("milk", D("200"), "ml")]
            ),
        ],
        modifiers=[
            Modifier(
                "extra_milk",
                menu_item="latte",
                yield_quantity=D("2"),
                ingredients=[Ingredient("milk", D("100"), "ml")],
            ),
        ],
    )
    # base: 2*200/1 = 400 ; modifier: 2*(×3)*100/2 = 300 ; net δ milk = -(400+300) = -700
    orders = [Order([Line("latte", D("2"), modifiers=[("extra_milk", D("3"))])])]
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_mixed_eligibility_stream(db: AsyncSession) -> None:
    """A stream where only some lines deplete: voided / refunded / unpaid / not-locked all
    contribute zero; the eligible PAID+locked lines accumulate."""
    world = World(
        items=[Item("cheese", MODE_A, "g")],
        recipes=[
            Recipe(
                "pizza", yield_quantity=D("1"), ingredients=[Ingredient("cheese", D("50"), "g")]
            ),
        ],
    )
    orders = [
        Order([Line("pizza", D("2"))]),  # eligible → -100
        Order([Line("pizza", D("5"), is_voided=True)]),  # voided → 0
        Order([Line("pizza", D("4"), is_refunded=True)]),  # refunded → 0 (line_refunded)
        Order([Line("pizza", D("9"))], payment_state="OPEN"),  # unpaid order → 0
        Order([Line("pizza", D("7"))], order_state="open"),  # not locked → 0
        Order([Line("pizza", D("3"))]),  # eligible → -150
    ]
    # net cheese = -(100 + 150) = -250
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_refund_after_deplete_nets_to_zero(db: AsyncSession) -> None:
    """A depleted line that is then reversed (refund path) nets to zero — forward + reversal
    cancel. The other eligible line stands."""
    world = World(
        items=[Item("tomato", MODE_A, "g")],
        recipes=[
            Recipe(
                "sauce", yield_quantity=D("1"), ingredients=[Ingredient("tomato", D("80"), "g")]
            ),
        ],
    )
    orders = [
        Order([Line("sauce", D("2"), refund_after_deplete=True)]),  # +(-160) then +160 = 0
        Order([Line("sauce", D("1"))]),  # -80
    ]
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_mixed_modes_one_world(db: AsyncSession) -> None:
    """Mode A and Mode B items in one recipe: A deducts (negative), B signals (positive); the
    on-hand layer applies yield_factor to the B item only."""
    world = World(
        items=[
            Item("dough", MODE_A, "g"),
            Item("oil", MODE_B, "ml", yield_factor=D("1.25"), opening_count=D("500")),
        ],
        recipes=[
            Recipe(
                "flatbread",
                yield_quantity=D("4"),
                ingredients=[
                    Ingredient("dough", D("400"), "g"),
                    Ingredient("oil", D("40"), "ml"),
                ],
            ),
        ],
    )
    orders = [Order([Line("flatbread", D("8"))])]
    # dough: 8*400/4 = 800 → δ -800 ; oil signal: 8*40/4 = 80 → δ +80
    # on_hand oil = 500 - 80*1.25 = 400 ; on_hand dough = -800
    await _assert_world(db, world, orders)


@pytest.mark.integration
async def test_unmapped_and_missing_conversion_contribute_nothing(db: AsyncSession) -> None:
    """A line with no confirmed recipe (unmapped) and a line whose unit can't convert
    (missing_conversion) both write zero movements — the eligible convertible line stands."""
    world = World(
        items=[Item("syrup", MODE_A, "ml"), Item("water", MODE_A, "ml")],
        # NOTE: no g->ml conversion seeded → the 'tea' recipe (g ingredient, ml storage) fails.
        recipes=[
            Recipe("soda", yield_quantity=D("1"), ingredients=[Ingredient("syrup", D("30"), "ml")]),
            Recipe("tea", yield_quantity=D("1"), ingredients=[Ingredient("water", D("10"), "g")]),
            Recipe(
                "juice",
                yield_quantity=D("1"),
                ingredients=[Ingredient("syrup", D("20"), "ml")],
                confirmed=False,
            ),
        ],
    )
    orders = [
        Order([Line("soda", D("2"))]),  # eligible, convertible → syrup -60
        Order([Line("tea", D("5"))]),  # missing g->ml conversion → 0
        Order([Line("juice", D("9"))]),  # unconfirmed recipe → unmapped → 0
    ]
    await _assert_world(db, world, orders)
