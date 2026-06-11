# Sprint 5 — Phase 13 notes (coverage view verification + monitoring diagnostics)

> A verification phase: the `vw_depletion_coverage` view already shipped in migration 0017;
> Phase 13 proves the properties that were load-bearing in the 0017 review, and adds the
> operational-concern-6 refund-pattern diagnostic. No migration, no new behavior.
>
> **Convention (applies forward):** each Sprint 5 verification/infra phase gets a lightweight
> `sprint-5-phase-N-notes.md` like this one; ADRs stay reserved for architecture a future
> reader must re-derive (e.g. ADR 0001's ledger-driven reversal). Phases 14 and 15 follow this
> same convention.
>
> Closes gate 30.

## N1 — Coverage is a three-state matrix; each state catches a different wrong view

Gate 30 reads "returns both `depleted_count_pct` and `depleted_revenue_pct`," but the
load-bearing content is the NULL-vs-zero semantics from the 0017 review. The view's revenue
percentage has exactly three meaningfully-distinct states, and the tests pin all three —
each one fails a *different* wrong implementation:

1. **Zero depleted, nonzero total → `0.00`, NOT NULL** — the alert-correctness case. A
   filtered `SUM` over no matching rows is NULL; the 0017 view wraps the numerator in
   `COALESCE(..., 0)` so this reads `0.00`. If it read NULL, the coverage-collapse alert
   (`pct < threshold`) would evaluate UNKNOWN and **silently never fire for the tenant that
   most needs it.** Guards against a future change dropping the numerator COALESCE.
2. **count% ≠ revenue% → the two columns are independently computed** (factor≠1 at the view
   layer). Test data is weighted so the depleted *count* share differs from the depleted
   *revenue* share (e.g. 2/4 lines = 50% count, but those 2 hold 400/1000 cents = 40%
   revenue). Equal weighting would pass even if one pct were derived from the other's
   numerator.
3. **Zero total revenue → NULL is correct** — the honest no-denominator answer. You cannot
   take a percentage of nothing; `NULLIF(SUM(...), 0)` on the denominator yields NULL rather
   than a divide-by-zero or a fake `0.00`. Guards against a future *over*-COALESCE that turns
   genuine no-data into a misleading 0%.

Together: zero-of-something = `0.00`, something-of-anything = the real ratio, anything-of-zero
= NULL.

## N2 — RLS test: cheap, permanent guard on a high-stakes boundary

The view is `security_invoker = true` (verified in the 0017 DDL review), so it runs with the
querying role's RLS and `app_user` sees only its own tenant's row. This phase adds a cheap
end-to-end test — seed two tenants, query the view as tenant A's `app_user`, assert only A's
row returns — that converts the 0017 review's one-time catch into a continuously-verified
property. The regression it guards is **cross-tenant revenue/coverage visibility**, and the
mechanism (the view's `security_invoker` reloption) could be silently dropped by a future
migration that recreates the view. Same discipline as Phase 1's cross-tenant density test:
verify the boundary end-to-end, don't trust the mechanism.

## N3 — failed_reason_breakdown: NULL → "unknown", and windowed

The refund-pattern monitor (operational-concern 6). Two design decisions:

- **NULL reason → `'unknown'`, not excluded.** The `depletion_status_reason_consistency`
  CHECK makes a failed line with a NULL reason *impossible*. So if one exists, the CHECK was
  violated, bypassed, or the row predates enforcement — a data-integrity anomaly ops should
  see **loudly**. A diagnostic must surface impossible-states-that-exist (the highest-value
  signal it can emit), not filter them into invisibility. `COALESCE(depletion_reason,
  'unknown')`.
- **Windowed on `created_at` (`window_days`, default 30).** Op-concern 6 is about *spikes* —
  "a `line_refunded` spike is normal; a `sale_ineligible` spike may indicate Clover
  state-mapping issues." A spike is a *rate*; an all-time unwindowed count cannot show one,
  because months of accumulated base swamp any recent movement (500 historical `line_refunded`
  + 30 new `sale_ineligible` this week reads as noise all-time, but the 30-in-a-week is the
  signal). The coverage view windows to 30 days for the same reason. Unwindowed, the
  diagnostic would pass its test and only reveal its uselessness months into pilot.

## N4 — No endpoint

Both diagnostics (`stuck_pending_lines`, `failed_reason_breakdown`) are read-only query
helpers in the depletion module, not read endpoints — consistent with F.5 (the coverage
*card* is deferred to Sprint 9; Sprint 5 adds no coverage read endpoint). Import-isolated from
the LLM layer like the rest of `depletion/` (the Phase 14 guard).
