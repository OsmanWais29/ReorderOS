"""Order-stream transforms for V3 metamorphic relations (and V5 reuse).

Pure functions on list[Order] — each returns a NEW stream (inputs untouched). The metamorphic
tests run the original and a transformed stream through the REAL engine and compare the two
engine outputs to each other (no oracle), so a bug the oracle would share still shows.
"""

from __future__ import annotations

from tests.depletion_model.world import D, Line, Order


def _copy_line(line: Line, *, qty: object | None = None) -> Line:
    return Line(
        menu_item=line.menu_item,
        qty=D(line.qty) if qty is None else D(qty),
        modifiers=list(line.modifiers),
        is_voided=line.is_voided,
        is_refunded=line.is_refunded,
        refund_after_deplete=line.refund_after_deplete,
    )


def _copy_order(order: Order, lines: list[Line]) -> Order:
    return Order(lines=lines, payment_state=order.payment_state, order_state=order.order_state)


def permute(orders: list[Order]) -> list[Order]:
    """Deterministic non-identity reorder: reverse the order list AND the lines within each
    order. Tests commutativity (ledger net is order-independent)."""
    return [_copy_order(o, list(reversed(o.lines))) for o in reversed(orders)]


def scale_qty(orders: list[Order], k: int) -> list[Order]:
    """Multiply every line's qty by integer k (modifiers' slim multipliers unchanged — the
    line-qty factor already scales their magnitude). Tests homogeneity (δ scales by k)."""
    return [
        _copy_order(o, [_copy_line(line, qty=D(line.qty) * k) for line in o.lines])
        for o in orders
    ]


def split_each_line(orders: list[Order]) -> list[Order]:
    """Replace each modifier-free line of integer qty q≥2 with two lines (1 and q-1) in the
    same order. Tests additivity (decomposing a line doesn't change net δ). Lines with
    modifiers or qty<2 are passed through unchanged (splitting a modifier line would double
    its modifiers)."""
    out: list[Order] = []
    for o in orders:
        new_lines: list[Line] = []
        for line in o.lines:
            q = D(line.qty)
            if line.modifiers or q < 2 or q != q.to_integral_value():
                new_lines.append(_copy_line(line))
            else:
                new_lines.append(_copy_line(line, qty=D(1)))
                new_lines.append(_copy_line(line, qty=q - 1))
        out.append(_copy_order(o, new_lines))
    return out
