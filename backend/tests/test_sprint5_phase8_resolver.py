"""Sprint 5 Phase 8 — sale-line eligibility resolver (v5 §11).

Pure unit tests: resolve_eligibility takes scalars and returns a verdict, so every case
is asserted on return values alone — no DB, no fixtures, no monkeypatching. The
partial-refund gate is exercised by passing the parameter (not mutating module state),
which is what keeps the resolver a pure function.

The reasons asserted (sale_ineligible, line_refunded) are exactly the two eligibility
members of the depletion_reason CHECK; the handler (Phase 9) maps an ineligible verdict to
depletion_status='failed'.
"""

from __future__ import annotations

import pytest

from app.modules.inventory.depletion.resolver import (
    LINE_REFUNDED,
    SALE_INELIGIBLE,
    resolve_eligibility,
)


def _r(
    payment_state: str = "PAID",
    order_state: str = "locked",
    *,
    is_voided: bool = False,
    is_refunded: bool = False,
    partial_refunds_enabled: bool = False,
):
    return resolve_eligibility(
        payment_state=payment_state,
        order_state=order_state,
        is_voided=is_voided,
        is_refunded=is_refunded,
        partial_refunds_enabled=partial_refunds_enabled,
    )


# ── eligible ─────────────────────────────────────────────────────────────────


def test_paid_locked_clean_is_eligible() -> None:
    res = _r()
    assert res.eligible is True
    assert res.reason is None


# ── order-level ineligibility → sale_ineligible ──────────────────────────────


def test_open_payment_ineligible() -> None:
    res = _r(payment_state="OPEN")
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_not_locked_ineligible() -> None:
    res = _r(order_state="open")
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_refunded_order_ineligible() -> None:
    res = _r(payment_state="REFUNDED")
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_credited_not_eligible_v4_regression() -> None:
    """Gate #19: a CREDITED order must NOT forward-deplete (CREDITED == refunded per
    Clover; forward depletion would double-count the physical loss)."""
    res = _r(payment_state="CREDITED")
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


# ── line-level ───────────────────────────────────────────────────────────────


def test_voided_line_ineligible() -> None:
    res = _r(is_voided=True)
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_refunded_line_in_eligible_order_is_line_refunded() -> None:
    res = _r(is_refunded=True)
    assert res.eligible is False and res.reason == LINE_REFUNDED


# ── precedence: order dominates line ─────────────────────────────────────────


def test_refunded_line_in_refunded_order_is_sale_ineligible_not_line_refunded() -> None:
    """Order-level domination: a refunded line in a fully-REFUNDED order resolves to
    sale_ineligible (order check fails first), NOT line_refunded."""
    res = _r(payment_state="REFUNDED", is_refunded=True)
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_voided_precedes_refunded_reason() -> None:
    res = _r(is_voided=True, is_refunded=True)
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


# ── PARTIALLY_REFUNDED — built-but-latent, gated by the parameter ────────────


def test_partial_refund_gated_off_is_ineligible() -> None:
    """Conservative pre-Phase-11 default: PARTIALLY_REFUNDED is ineligible while the gate
    is off (is_refunded not yet reliably populated)."""
    res = _r(payment_state="PARTIALLY_REFUNDED", partial_refunds_enabled=False)
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_partial_refund_gated_on_nonrefunded_line_eligible() -> None:
    res = _r(payment_state="PARTIALLY_REFUNDED", is_refunded=False, partial_refunds_enabled=True)
    assert res.eligible is True and res.reason is None


def test_partial_refund_gated_on_refunded_line_is_line_refunded() -> None:
    res = _r(payment_state="PARTIALLY_REFUNDED", is_refunded=True, partial_refunds_enabled=True)
    assert res.eligible is False and res.reason == LINE_REFUNDED


@pytest.mark.parametrize("payment_state", ["OPEN", "REFUNDED", "CREDITED"])
def test_gate_on_does_not_rescue_other_states(payment_state: str) -> None:
    """Enabling partial refunds must not make OPEN/REFUNDED/CREDITED eligible."""
    res = _r(payment_state=payment_state, partial_refunds_enabled=True)
    assert res.eligible is False and res.reason == SALE_INELIGIBLE


def test_gate_on_does_not_disturb_normal_paid_path() -> None:
    """Turning the gate on must leave a clean PAID line eligible (no regression)."""
    res = _r(partial_refunds_enabled=True)
    assert res.eligible is True and res.reason is None


def test_null_order_fields_resolve_safe() -> None:
    """orders.payment_state / state are nullable text — a partially-ingested order can
    pass None. The safe verdict is sale_ineligible (not eligible-by-accident)."""
    res = resolve_eligibility(
        payment_state=None,  # type: ignore[arg-type]
        order_state=None,  # type: ignore[arg-type]
        is_voided=False,
        is_refunded=False,
    )
    assert res.eligible is False and res.reason == SALE_INELIGIBLE
