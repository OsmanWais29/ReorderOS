"""The independent reference calculator (V2 oracle).

Pure Python/Decimal. Imports NOTHING from app.modules.inventory.depletion — that structural
isolation is what makes it an independent check rather than a copy of the engine. It encodes
the v5 §11 formula directly from the spec, not the engine's implementation.

Two layers (founder chose both):
  expected_ledger(world, orders)  → {item_key: net δ}    raw movement deltas (yield_factor NOT
                                    applied — mirrors the engine's ledger layer)
  expected_on_hand(world, orders) → {item_key: qty|None} physical stock per spec (yield_factor
                                    IS applied for Mode B) — the number the café reorders from

Deliberate scope choices that keep it trustworthy (not a re-derivation of engine internals):
  • conversions are global-tier only and trivial (identity or one factor) — precedence is NOT
    re-implemented here (owned by test_sprint5_phase1_conversions).
  • it predicts net δ + a coarse per-line outcome (contributes / net-zero), NOT engine
    reason-strings (those are engine labels, not spec truth).
  • Decimal arithmetic mirrors the engine's divide-per-ingredient-then-sum ORDER so the
    comparison is exact (no tolerance — a tolerance would mask real bugs).
"""

from __future__ import annotations

from decimal import Decimal

from tests.depletion_model.world import MODE_A, D, Line, Order, World


class OracleMissingConversion(Exception):
    """The oracle's trivial conversion has no path — mirrors the engine failing the whole
    line (no movements). Used internally to zero out a line's contribution."""


def _convert(qty: Decimal, from_unit: str, to_unit: str, world: World) -> Decimal:
    """Trivial global-tier conversion. Identity short-circuits (matches engine). Raises
    OracleMissingConversion when no global factor exists (⇒ line contributes nothing)."""
    if from_unit == to_unit:
        return qty
    for c in world.conversions:
        if c.from_unit == from_unit and c.to_unit == to_unit:
            return qty * D(c.factor)
    raise OracleMissingConversion(f"{from_unit}->{to_unit}")


def _eligible(order: Order, line: Line, *, partial_refunds_enabled: bool) -> bool:
    """v5 §11 eligibility (order dominates line). Spec predicate, not the engine's resolver —
    kept minimal; exhaustive eligibility/reason correctness is owned by test_sprint5_phase8."""
    ok_payments = {"PAID"}
    if partial_refunds_enabled:
        ok_payments = ok_payments | {"PARTIALLY_REFUNDED"}
    if order.payment_state not in ok_payments:
        return False
    if order.order_state != "locked":
        return False
    if line.is_voided:
        return False
    return not line.is_refunded


def _line_deltas(world: World, order: Order, line: Line) -> dict[str, Decimal] | None:
    """Signed δ per item for ONE eligible line (base + modifiers), or None if the line does
    not deplete (no recipe / no confirmed version / a conversion fails — engine writes 0
    movements in all three, so the line contributes nothing). δ is per-ingredient-then-summed,
    aggregated per item, sign by item mode (Mode A negative, Mode B positive). yield_factor is
    NOT applied here — this is the ledger layer."""
    recipe = world.recipe(line.menu_item)
    if recipe is None or not recipe.confirmed or not recipe.ingredients:
        return None  # unmapped/no_recipe or invalid_recipe → no movements

    out: dict[str, Decimal] = {}
    try:
        # base
        for ing in recipe.ingredients:
            it = world.item(ing.item)
            storage_qty = _convert(D(ing.qty), ing.unit, it.storage_unit, world)
            magnitude = D(line.qty) * storage_qty / D(recipe.yield_quantity)
            signed = -magnitude if it.mode == MODE_A else magnitude
            out[ing.item] = out.get(ing.item, Decimal("0")) + signed
        # modifiers
        for mod_key, slim_qty in line.modifiers:
            mod = world.modifier(mod_key)
            for ing in mod.ingredients:
                it = world.item(ing.item)
                storage_qty = _convert(D(ing.qty), ing.unit, it.storage_unit, world)
                magnitude = (
                    D(line.qty) * D(slim_qty) * storage_qty / D(mod.yield_quantity)
                )
                signed = -magnitude if it.mode == MODE_A else magnitude
                out[ing.item] = out.get(ing.item, Decimal("0")) + signed
    except OracleMissingConversion:
        return None  # any conversion failure fails the WHOLE line (base+modifier atomic)
    return out


def expected_ledger(
    world: World, orders: list[Order], *, partial_refunds_enabled: bool = False
) -> dict[str, Decimal]:
    """Net δ per item across the whole stream (raw ledger layer). A refund_after_deplete line
    depletes then reverses → net zero contribution. Items with no movement are absent."""
    net: dict[str, Decimal] = {}
    for order in orders:
        for line in order.lines:
            if not _eligible(order, line, partial_refunds_enabled=partial_refunds_enabled):
                continue
            deltas = _line_deltas(world, order, line)
            if deltas is None:
                continue
            if line.refund_after_deplete:
                continue  # forward + reversal net to zero
            for item_key, d in deltas.items():
                net[item_key] = net.get(item_key, Decimal("0")) + d
    return net


def expected_on_hand(
    world: World, orders: list[Order], *, partial_refunds_enabled: bool = False
) -> dict[str, Decimal | None]:
    """Physical on-hand per item per spec (the number the café reorders from).

    Mode A: Σ(non-signal deltas) = the same as the ledger net for Mode A (Mode A produces only
            sale_depletion, a non-signal movement). yield_factor irrelevant.
    Mode B: last_count_quantity − Σ(sale_signal magnitudes) × yield_factor.
            None when opening_count is None (count not done) — matches on_hand() returning None.

    V2 keeps the Mode B boundary trivial (the anchor precedes all sales, no post-count
    receipts), so on-hand = anchor − consumed×yield_factor. This is where yield_factor bites
    and the ledger layer cannot — a yield bug shows here, not in expected_ledger."""
    # Accumulate signed magnitudes per item (pre-yield), respecting refund netting.
    signed: dict[str, Decimal] = {}
    for order in orders:
        for line in order.lines:
            if not _eligible(order, line, partial_refunds_enabled=partial_refunds_enabled):
                continue
            deltas = _line_deltas(world, order, line)
            if deltas is None or line.refund_after_deplete:
                continue
            for item_key, d in deltas.items():
                signed[item_key] = signed.get(item_key, Decimal("0")) + d

    out: dict[str, Decimal | None] = {}
    for it in world.items:
        s = signed.get(it.key, Decimal("0"))
        if it.mode == MODE_A:
            # Mode A on_hand = Σ(non-signal deltas) ONLY — the engine ignores last_count_quantity
            # for Mode A (it's a Mode B anchor). Opening stock for Mode A would need a seeded
            # opening_balance movement (out of V2 scope), so base is 0 here.
            out[it.key] = s
        else:  # MODE_B
            if it.opening_count is None:
                out[it.key] = None  # no count yet → on_hand() returns None
            else:
                # s is the positive Σ(signal magnitudes); consumed reduces on-hand, weighted
                # by yield_factor (the engine's Mode B: anchor − signals×yield_factor).
                out[it.key] = D(it.opening_count) - s * D(it.yield_factor)
    return out
