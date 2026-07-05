"""V7 — Clover payload-conformance (offline): does our parser handle real Clover shapes?

Covers the trickiest parser logic most likely to break against a real merchant:
  • _derive_payment_state across every signal (explicit paymentState, refundTotal, payments array,
    payType, default) and their priority ordering;
  • the modifier multiplier in BOTH representations Clover may use ("×2" as one modification with
    quantity=2, OR two modifications defaulting to 1) → identical depletion.

These run with no Clover account. The items in v7-clover-cert-checklist.md §C still need a captured
sandbox payload to confirm the assumed shapes; this file locks the logic we control.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_sessionmaker
from app.modules.pos.worker import InboxWorker
from tests.depletion_model.run import actual_ledger
from tests.depletion_model.seed import Seeded, seed_world
from tests.depletion_model.sim import drain_concurrent, seed_inbox_payload
from tests.depletion_model.world import MODE_A, Ingredient, Item, Modifier, Recipe, World

D = Decimal
_ps = InboxWorker()._derive_payment_state  # pure; no instance state


# ── Payment-state derivation matrix (pure) ───────────────────────────────────────


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        # explicit paymentState wins
        ({"paymentState": "PAID"}, "PAID"),
        ({"paymentState": "REFUNDED"}, "REFUNDED"),
        ({"paymentState": "PARTIALLY_REFUNDED"}, "PARTIALLY_REFUNDED"),
        ({"paymentState": "CREDITED"}, "CREDITED"),
        # refundTotal vs total
        ({"total": 1000, "refundTotal": 1000}, "REFUNDED"),
        ({"total": 1000, "refundTotal": 400}, "PARTIALLY_REFUNDED"),
        # payments array results
        ({"payments": {"elements": [{"result": "SUCCESS"}]}}, "PAID"),
        ({"payments": {"elements": [{"result": "AUTH"}]}}, "PAID"),
        ({"payments": {"elements": [{"result": "REFUNDED"}]}}, "REFUNDED"),
        (
            {"payments": {"elements": [{"result": "SUCCESS"}, {"result": "REFUNDED"}]}},
            "PARTIALLY_REFUNDED",
        ),
        # payType fallback
        ({"payType": "FULL"}, "PAID"),
        # default
        ({}, "OPEN"),
        # priority: explicit paymentState beats refund signals
        ({"paymentState": "PAID", "total": 1000, "refundTotal": 1000}, "PAID"),
    ],
)
def test_derive_payment_state_matrix(order: dict[str, Any], expected: str) -> None:
    assert _ps(order) == expected


# ── Modifier multiplier: two Clover representations → identical depletion ─────────


@pytest.fixture
async def committed_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


def _latte_world() -> World:
    return World(
        items=[Item("milk", MODE_A, "ml")],
        recipes=[Recipe("latte", D("1"), [Ingredient("milk", D("200"), "ml")])],
        modifiers=[Modifier("extra", "latte", D("1"), [Ingredient("milk", D("50"), "ml")])],
    )


def _order_with_mods(seeded: Seeded, mods: list[dict[str, Any]], created_ms: int) -> dict[str, Any]:
    import uuid

    return {
        "id": f"order_{uuid.uuid4().hex}",
        "state": "locked",
        "paymentState": "PAID",
        "total": 0,
        "createdTime": created_ms,
        "clientCreatedTime": created_ms,
        "lineItems": {
            "elements": [
                {
                    "id": f"li_{uuid.uuid4().hex}",
                    "item": {"id": seeded.pos_item_ids["latte"]},
                    "name": "latte",
                    "unitQty": 1.0,
                    "price": 100,
                    "modifications": {"elements": mods},
                }
            ]
        },
    }


@pytest.mark.integration
async def test_modifier_multiplier_both_representations(committed_session: AsyncSession) -> None:
    """'Extra shot ×2' as ONE modification with quantity=2 vs TWO modifications (no quantity →
    default 1 each) must deplete identically. Run in two tenants and compare ledgers."""
    s = committed_session

    seeded_a = await seed_world(s, _latte_world())
    pos_a = seeded_a.pos_modifier_ids["extra"]
    await seed_inbox_payload(
        s,
        seeded_a,
        _order_with_mods(seeded_a, [{"modifier": {"id": pos_a}, "quantity": 2}], 0),
    )
    await s.commit()
    await drain_concurrent(s, seeded_a.tenant_id, n_workers=1)
    ledger_one_element = await actual_ledger(s, seeded_a)

    seeded_b = await seed_world(s, _latte_world())
    pos_b = seeded_b.pos_modifier_ids["extra"]
    await seed_inbox_payload(
        s,
        seeded_b,
        _order_with_mods(seeded_b, [{"modifier": {"id": pos_b}}, {"modifier": {"id": pos_b}}], 0),
    )
    await s.commit()
    await drain_concurrent(s, seeded_b.tenant_id, n_workers=1)
    ledger_two_elements = await actual_ledger(s, seeded_b)

    # base 200 + modifier 2×50 = 300 → milk -300, both representations
    assert ledger_one_element == {"milk": D("-300")}, ledger_one_element
    assert ledger_two_elements == ledger_one_element, (
        f"modifier representations diverge: one-element={ledger_one_element} "
        f"two-element={ledger_two_elements}"
    )
