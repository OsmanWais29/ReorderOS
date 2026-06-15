"""In-memory ground-truth model of a tenant's depletion-relevant world + order stream.

These are plain dataclasses authored by a test (V2) or generated (V5). They are the SOLE
input to both the oracle (expected) and the seeder (what the real engine runs against), so
the two can never silently disagree about what the world contains. Ids are assigned by the
seeder; the model refers to entities by their logical string ``key``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

# Modes (mirror inventory_items.inventory_mode CHECK).
MODE_A = "recipe_deducted"  # → sale_depletion, negative δ
MODE_B = "count_anchored"  # → sale_signal, positive δ


def D(v: object) -> Decimal:
    """Coerce to Decimal via str (never via float — float would inject rounding)."""
    return v if isinstance(v, Decimal) else Decimal(str(v))


@dataclass
class Item:
    key: str
    mode: str  # MODE_A | MODE_B
    storage_unit: str  # canonical unit (the unit movements are recorded in)
    yield_factor: Decimal = Decimal("1")
    # Mode B anchor (count_anchored on-hand baseline). None ⇒ on_hand() returns None.
    opening_count: Decimal | None = None  # → inventory_items.last_count_quantity


@dataclass
class Ingredient:
    item: str  # Item.key
    qty: Decimal
    unit: str  # recipe/modifier unit (canonical)


@dataclass
class Recipe:
    menu_item: str  # logical menu-item key
    yield_quantity: Decimal
    ingredients: list[Ingredient]
    confirmed: bool = True  # if False, no recipe_version is snapshotted on the sale line


@dataclass
class Modifier:
    key: str
    menu_item: str  # the menu item this modifier attaches to
    yield_quantity: Decimal
    ingredients: list[Ingredient]


@dataclass
class Conversion:
    """Global-tier conversion only (V2 keeps conversions trivial so the oracle never has to
    re-derive the 3-tier item>tenant>global precedence — that is owned by
    test_sprint5_phase1_conversions). from==to identity is implicit, never listed."""

    from_unit: str
    to_unit: str
    factor: Decimal


@dataclass
class World:
    items: list[Item] = field(default_factory=list)
    recipes: list[Recipe] = field(default_factory=list)
    modifiers: list[Modifier] = field(default_factory=list)
    conversions: list[Conversion] = field(default_factory=list)

    def item(self, key: str) -> Item:
        for it in self.items:
            if it.key == key:
                return it
        raise KeyError(f"no item {key!r} in world")

    def recipe(self, menu_item: str) -> Recipe | None:
        for r in self.recipes:
            if r.menu_item == menu_item:
                return r
        return None

    def modifier(self, key: str) -> Modifier:
        for m in self.modifiers:
            if m.key == key:
                return m
        raise KeyError(f"no modifier {key!r} in world")


@dataclass
class Line:
    menu_item: str
    qty: Decimal
    # (modifier key, slim quantity) — the modifier multiplier applied at sale time.
    modifiers: list[tuple[str, Decimal]] = field(default_factory=list)
    # line-level eligibility flags (order-level payment_state/order_state live on Order)
    is_voided: bool = False
    is_refunded: bool = False
    # If True, the driver depletes the line then calls reverse_line (refund-after-deplete):
    # the forward movements are written, then negated → net δ zero. Distinct from is_refunded
    # (which makes the line ineligible up front so it never depletes at all).
    refund_after_deplete: bool = False


@dataclass
class Order:
    lines: list[Line]
    payment_state: str = "PAID"  # order-level (orders.payment_state)
    order_state: str = "locked"  # order-level (orders.state)
