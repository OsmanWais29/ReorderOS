"""Sale-line eligibility resolver (Sprint 5 Phase 8, v5 §11).

PURE FUNCTION — takes the eligibility-relevant scalars and returns a verdict. It reads
no database and writes nothing (not even depletion_status): the Phase 9 handler does the
sale_line_items⋈orders SELECT, calls this, and — when ineligible — writes
status='failed', reason=<result.reason> inside the depletion transaction. Keeping the
resolver pure makes it testable purely on return values, and keeps every write in the
handler's one transaction.

Eligibility (v5 §11) — depletion is eligible when ALL of:
  1. payment_state ∈ {PAID, PARTIALLY_REFUNDED*}   (* gated, see below)
  2. order_state = 'locked'
  3. not is_voided
  4. not is_refunded

CREDITED is NOT eligible (v4 regression, gate #19): per Clover, CREDITED means the order
was refunded; forward-depleting it double-counts the physical loss. REFUNDED likewise.

Precedence (deterministic). Order-level checks DOMINATE line-level, so 'line_refunded'
only ever arises inside an otherwise-eligible order — a refunded line in a fully-REFUNDED
order is 'sale_ineligible' (the order check fails first):
  1. payment_state not eligible (OPEN/REFUNDED/CREDITED, or PARTIALLY_REFUNDED while
     gated off) → sale_ineligible
  2. order_state != 'locked'                                → sale_ineligible
  3. is_voided                                              → sale_ineligible
  4. is_refunded                                            → line_refunded
  5. otherwise                                              → eligible

partial_refunds_enabled gate: PARTIALLY_REFUNDED depletes the non-refunded lines (v5 §11),
but the per-line is_refunded discriminator is only reliable once Phase 11 wires line-refund
detection. Until then is_refunded is always false, so a PARTIALLY_REFUNDED order would
wrongly deplete refunded lines. The flag defaults False (conservative: PARTIALLY_REFUNDED →
sale_ineligible). It is a CALL-SITE LITERAL, not config: Phase 11 passes True in the same
change that wires is_refunded — the gate and its precondition move together, and it is never
an operational toggle. The full line-discriminator logic is built and tested for both states.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reasons (a subset of the depletion_reason CHECK; the rest are Phase 9 walker reasons).
SALE_INELIGIBLE = "sale_ineligible"
LINE_REFUNDED = "line_refunded"

_ELIGIBLE_PAYMENT_BASE = frozenset({"PAID"})


@dataclass(frozen=True)
class EligibilityResult:
    """Verdict only — no status lifecycle. reason is None iff eligible; otherwise it is
    'sale_ineligible' or 'line_refunded' and the handler maps the line to 'failed'."""

    eligible: bool
    reason: str | None


def resolve_eligibility(
    *,
    payment_state: str,
    order_state: str,
    is_voided: bool,
    is_refunded: bool,
    partial_refunds_enabled: bool = False,
) -> EligibilityResult:
    """Decide whether one sale line is depletion-eligible (v5 §11). Pure; writes nothing.

    partial_refunds_enabled: pass False until Phase 11 wires reliable is_refunded
    detection, then pass True at the same call site (a literal, not config)."""
    eligible_payments = _ELIGIBLE_PAYMENT_BASE
    if partial_refunds_enabled:
        eligible_payments = eligible_payments | {"PARTIALLY_REFUNDED"}

    # ── order-level (dominates) ───────────────────────────────────────────────
    if payment_state not in eligible_payments:
        return EligibilityResult(False, SALE_INELIGIBLE)
    if order_state != "locked":
        return EligibilityResult(False, SALE_INELIGIBLE)

    # ── line-level (only reached inside an eligible, locked order) ─────────────
    if is_voided:
        return EligibilityResult(False, SALE_INELIGIBLE)
    if is_refunded:
        return EligibilityResult(False, LINE_REFUNDED)

    return EligibilityResult(True, None)
