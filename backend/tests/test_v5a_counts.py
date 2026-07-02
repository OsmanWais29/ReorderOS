"""V5a — count / re-anchor / watermark math, hardened and tested at SMALL scale FIRST.

Before the 2,000-order concurrent run, prove the oracle and the engine agree on the realistic
restaurant-day count path in isolation — so if V5b breaks, we know it's concurrency, not the
count math (isolate-the-variable). Exercises:
  • Mode B re-anchor (count resets last_count_quantity; on_hand counts only signals AFTER it),
  • Mode B yield_factor on the post-count window,
  • Mode A count_adjust on drift (ledger reconciles to physical; on_hand == counted + post),
  • Mode A no-drift count emits no movement,
  • a mixed timeline counting both an A and a B item.

The historical on_hand path (recorded_at > last_count_at) and the watermark predicted path
(inside record_count_event) are both driven here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine, make_bound_session
from tests.depletion_model import oracle
from tests.depletion_model.run import actual_ledger, actual_on_hand, run_timeline
from tests.depletion_model.seed import seed_world
from tests.depletion_model.world import (
    MODE_A,
    MODE_B,
    Count,
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


async def _assert_timeline(db: AsyncSession, world: World, timeline: list[object]) -> None:
    seeded = await seed_world(db, world)
    await run_timeline(db, world, timeline, seeded)
    exp_ledger, exp_oh = oracle.expected_timeline(world, timeline)
    act_ledger = await actual_ledger(db, seeded)
    act_oh = await actual_on_hand(db, seeded, world)
    assert act_ledger == exp_ledger, f"ledger mismatch\n exp={exp_ledger}\n act={act_ledger}"
    assert act_oh == exp_oh, f"on-hand mismatch\n exp={exp_oh}\n act={act_oh}"


@pytest.mark.integration
async def test_mode_b_reanchor_window(db: AsyncSession) -> None:
    """Count re-anchors; on_hand counts only signals AFTER the count. anchor 1000, sell +100
    (pre-count, absorbed), count→800, sell +150 (post) → 800-150=650. Ledger keeps all signals."""
    world = World(
        items=[Item("beans", MODE_B, "g", opening_count=D("1000"))],
        recipes=[Recipe("latte", D("1"), [Ingredient("beans", D("50"), "g")])],
    )
    timeline = [
        Order([Line("latte", D("2"))]),  # +100 signal (pre-count)
        Count("beans", D("800")),  # re-anchor
        Order([Line("latte", D("3"))]),  # +150 signal (post-count)
    ]
    await _assert_timeline(db, world, timeline)  # on_hand 650, ledger {beans:+250}


@pytest.mark.integration
async def test_mode_b_reanchor_with_yield_factor(db: AsyncSession) -> None:
    """Post-count window is yield-weighted: anchor 900, post signals +250, yield 0.8 →
    900-250*0.8=700."""
    world = World(
        items=[Item("beans", MODE_B, "g", yield_factor=D("0.8"), opening_count=D("1000"))],
        recipes=[Recipe("latte", D("1"), [Ingredient("beans", D("50"), "g")])],
    )
    timeline = [
        Order([Line("latte", D("2"))]),  # +100 pre
        Count("beans", D("900")),
        Order([Line("latte", D("5"))]),  # +250 post
    ]
    await _assert_timeline(db, world, timeline)  # on_hand 700


@pytest.mark.integration
async def test_mode_a_count_adjust_on_drift(db: AsyncSession) -> None:
    """Mode A count emits count_adjust = counted − predicted so on_hand reconciles to physical.
    sell -300 (predicted -300), count→20 → adjust +320 (on_hand 20), sell -100 → on_hand -80."""
    world = World(
        items=[Item("flour", MODE_A, "g")],
        recipes=[Recipe("bun", D("1"), [Ingredient("flour", D("100"), "g")])],
    )
    timeline = [
        Order([Line("bun", D("3"))]),  # -300
        Count("flour", D("20")),  # adjust +320 → running 20
        Order([Line("bun", D("1"))]),  # -100 → running -80
    ]
    await _assert_timeline(db, world, timeline)  # on_hand -80, ledger {flour:-80}


@pytest.mark.integration
async def test_mode_a_no_drift_emits_no_movement(db: AsyncSession) -> None:
    """A count with zero drift (counted == predicted) emits no count_adjust. Count an empty bin
    at 0 before any sale (predicted 0), then sell → ledger is depletions only, no adjust row."""
    world = World(
        items=[Item("flour", MODE_A, "g")],
        recipes=[Recipe("bun", D("1"), [Ingredient("flour", D("100"), "g")])],
    )
    timeline = [
        Count("flour", D("0")),  # predicted 0, counted 0 → no adjust
        Order([Line("bun", D("2"))]),  # -200
    ]
    await _assert_timeline(db, world, timeline)  # on_hand -200, ledger {flour:-200}


@pytest.mark.integration
async def test_mixed_modes_counted_together(db: AsyncSession) -> None:
    """One timeline counts both a Mode A item (adjust) and a Mode B item (re-anchor)."""
    world = World(
        items=[
            Item("flour", MODE_A, "g"),
            Item("oil", MODE_B, "ml", opening_count=D("500")),
        ],
        recipes=[
            Recipe(
                "flatbread",
                D("2"),
                [
                    Ingredient("flour", D("400"), "g"),
                    Ingredient("oil", D("40"), "ml"),
                ],
            ),
        ],
    )
    timeline = [
        Order([Line("flatbread", D("4"))]),  # flour -800 ; oil +80
        Count("flour", D("100")),  # adjust +900 → flour running 100
        Count("oil", D("300")),  # re-anchor oil to 300, reset window
        Order([Line("flatbread", D("2"))]),  # flour -400 → -300 ; oil +40 (post)
    ]
    # on_hand flour -300 ; oil 300-40=260. ledger flour -300 ; oil +120 (all signals)
    await _assert_timeline(db, world, timeline)
