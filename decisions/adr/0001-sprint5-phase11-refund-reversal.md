# ADR 0001 — Sprint 5 Phase 11: refund/reversal wiring + PARTIALLY_REFUNDED activation

**Status:** Accepted (2026-06-10)
**Sprint / phase:** Sprint 5, Phase 11 (`sprint-5-phase-map.md`)
**Implements:** v5 §11 "Refund event processing"; closes gates 20, 21; fail-gate 15.
**Supersedes / amends:** none. Records the design decisions resolved during a read-first
review before implementation. No v1-scope lock is changed by this ADR.

---

## Context

Phase 11 wires line-level refunds to inventory reversal and activates partial-refund
depletion. The forward depletion engine (Phases 9/10) already writes `sale_depletion` /
`sale_signal` movements against a recipe/modifier version frozen at sale time. The reversal
primitive `record_sale_reversal` exists (moved verbatim in Phase 7) but has **no app caller**
— Phase 11 is its first. `resolver.resolve_eligibility` already gates PARTIALLY_REFUNDED
behind a `partial_refunds_enabled` parameter (default `False`) pending reliable per-line
`is_refunded`.

This phase looks like "wire up a function that already exists," but reversal-of-prior-state
is where two opposite failures both lurk: **reverse too much** (double-credit via duplicate
webhooks, or recompute-from-current-state mismatch) or **reverse too little** (miss modifier
movements, or let the reversal path get swallowed by the terminal-status no-op). The
decisions below exist to make the behavior correct *by design*, not correct-by-accident.

## The guardrail (the whole architecture of the phase)

**Reversal is a LEDGER operation, not a depletion operation.** It reads the forward
`inventory_movements` rows that actually exist for a sale line and negates them. It **never**
walks recipes/modifiers to recompute.

Why this is non-negotiable: the forward movements were written against the version frozen at
sale time. The recipe may have changed since (un-confirm → edit → re-confirm produces a new
version). Recomputing "what to give back" from the *current* recipe would reverse the wrong
version's quantities (sale depleted v1; recipe is now v3) and leave the ledger permanently
off. Reading the actual forward rows and negating them is self-consistent: you give back
exactly what those specific rows took, regardless of what the recipe says now. This is gate
#25 immutability (recipe edits don't alter a past sale's ledger) extended to reversal.
Authority: `inventory_accounting_semantics.md` §9 ("a reversal does not delete… does not
modify the original row… net effect of original + reversal is zero").

## Decisions

### D1 — Reversal idempotency key + mechanism (LOCKED-doc format)

Key: **`reversal:{original_movement_id}:{inventory_item_id}`** — the format pinned by the
canonical contract (`inventory_accounting_semantics.md` §6 line 246, §9 line 372). It is
deterministic from the forward movement (via its id), distinct from forward keys, and
unique-constrained. (An earlier review example used a `:reversal`-suffixed forward key; that
was illustrative. The locked doc format satisfies the same three required properties and is
what `record_sale_reversal` already emits, so no deviation/ADR-for-the-key is warranted.)

Mechanism: **`INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING RETURNING id`**,
returning `None` on replay. This is doc-mandated (§6 line 249), not a judgment call. The
current `record_sale_reversal` uses check-then-insert (a TOCTOU pattern); under retried/
concurrent refund webhooks two reversals could both pass the SELECT and the second INSERT
throw a unique violation. The constraint is the concurrency arbiter; **a replayed refund
event writes exactly one reversal, never two.** No "WHERE not already reversed" pre-filter is
needed — re-attempting an already-reversed row is a constraint-level no-op at per-line row
counts.

### D2 — Refund detection ownership: the worker, not `reverse_line`

`reverse_line` is a **pure ledger operation**: it reverses the forward movements of a line
the caller has *already determined* is refunded. It does not decide whether a line is
refunded.

Ownership split:
- **Worker detects** the refund (it has the Clover payload; the `li["refunded"]` field).
- **Worker marks** `is_refunded = true` (it owns that write via the 0021 grant).
- **`reverse_line` reverses** the ledger movements.

Coupling `is_refunded` ownership into `reverse_line` would mean any future caller (e.g. a
manual ops correction) flips `is_refunded` as a side effect whether wanted or not.

**Crash safety:** the `is_refunded = true` write and the reversal writes happen in the **same
per-line transaction**, so a crash between them can never leave a line marked-but-unreversed
or reversed-but-unmarked.

### D3 — Both base and modifier reversed, atomically per line

`reverse_line` reads **all** forward movements for the line — base **and** modifier — via the
shared `sale_line:{sli}:` idempotency-key prefix (also catches legacy keys), filtered to
`movement_type IN ('sale_depletion','sale_signal')`, and writes all reversals in **one
transaction**. No partial reversal (some ingredients credited, others not = corrupted state).
Same per-line atomicity as forward depletion.

### D4 — "No forward movement" and non-depleted statuses are the same case

Reversal is driven by **movement existence, not line status**. A line has forward movements
only if it reached `depleted`; unmapped/skipped/failed/pending lines have none, so reversal is
a **no-op by construction** — no error, no status thrash. This unifies all non-depleted
statuses without special-casing each (v5 §11 edit 5: "if no forward movement exists → no-op").

### D5 — Status after reversal, and the terminal-no-op collision

A refunded `depleted` line **stays `depleted`** (it *was* depleted — historical fact, the
movements exist) and `is_refunded` flips true; the reversal movements net it to zero
arithmetically. Changing status to a "reversed" value would lose the depleted-then-refunded
vs never-depleted distinction.

This requires reversal to be a **separate path from `process_line`**: `process_line` no-ops on
terminal (`depleted`) lines, but reversal legitimately operates on exactly those lines. The
worker runs the refund-reconcile step **before** the T2 depletion pass, so reversal is never
gated by the terminal-no-op.

### D6 — `partial_refunds_enabled` activation is lockstep

Within Phase 11, in order: (1) wire `is_refunded` population from Clover refund events,
(2) prove it populates (test), (3) **then** flip `partial_refunds_enabled=True` at the
worker's `process_line` call site — same change. Flag-true-without-wiring would trust an
unpopulated `is_refunded` and wrongly deplete refunded lines on PARTIALLY_REFUNDED orders.

### D7 — Movement-type taxonomy AND source linkage are preserved (two different fields)

Reversal rows carry both:
- **`movement_type`** = the type-specific reversal: `sale_depletion_reversal` (Mode A) /
  `sale_signal_reversal` (Mode B). This is accounting semantics — Mode B's observed-
  consumption math nets signals against signal-reversals, not against depletions. Collapsing
  to a generic "reversal" type would corrupt that.
- **`source_type = 'reversal'` + `source_id = <original movement id>`** = audit linkage.
- **`yield_factor_applied`** = equal to the original's (§9 line 374) — required for Mode B
  `on_hand()` to net to zero.

The Phase 11 change touches only the insert *mechanism* (→ ON CONFLICT). It must NOT regress
this taxonomy.

### D8 — Late voids are OUT OF SCOPE for Sprint 5 (decided, not left to fall out)

A line voided **after** it has depleted keeps its forward movements and its `depleted` status.
**Only the refund path (`is_refunded`) drives reversal in Sprint 5.**

Rationale: a late void's physical semantics are genuinely ambiguous — was the dish made and
then wasted (depletion should stand), or never made (should reverse)? This is the same
ambiguity that `inventory_accounting_semantics`/v5 known-limitation #4 documents for refunds
("refund-as-waste vs refund-as-return is not operator-distinguishable — a refunded latte was
still consumed"), arguably worse. Auto-reversing a late void would return to stock ingredients
that may have physically gone into a made-and-discarded dish, overstating inventory. Not
reversing errs toward understating stock (conservative for reorder). Neither is universally
right — so it defers to the same future operator-facing waste-vs-return flag as the refund
case.

Requirements this imposes on the implementation:
- **(a)** Documented as a known limitation alongside refund-as-waste (#4).
- **(b)** The refund-reconcile detection keys off the **refund field only** (`li["refunded"]`),
  **never** the void/exchange field (`li["exchanged"]`) — a late void must not accidentally
  trigger the refund/reversal path.
- **(c)** Tested: a previously-depleted line + a void event → no reversal, movements untouched.

New voided lines (never depleted) are unchanged: the resolver fails them `sale_ineligible`,
no movements — already correct from Phase 8.

## The `is_refunded` write grant

Migration 0017 granted `service_worker` column-scoped UPDATE on `sale_line_items` limited to
`(depletion_status, depletion_reason)` and **deliberately excluded `is_refunded`** (only
INSERT set it). Phase 11 is the first code to mutate `is_refunded` post-insert (D2), so the
grant must widen by exactly one column. **Migration 0021** (`GRANT UPDATE (is_refunded)`) is a
**metadata** migration (Migration Risk Standard §2.1: permissions) and carries the §1.2 risk
block + §4.5 application-impact (call-site) verification. This is a privilege boundary, so it
must be proven under the **real `service_worker` role** — superuser unit tests structurally
cannot catch a missing grant (the Slice B/C keystone lesson).

## Implementation sequence

1. Migration **0021** — `GRANT UPDATE (is_refunded)` (metadata; round-trip on local pg15).
2. `writer.py` — `record_sale_reversal` → ON CONFLICT DO NOTHING RETURNING (D1); taxonomy
   (D7) unchanged.
3. `handler.reverse_line` — pure ledger reverse of all forward movements for a line (D3, D4);
   does not own `is_refunded` (D2).
4. `worker.py` — refund-reconcile step **before** T2 (D5): per refunded line (detection by
   `li["refunded"]` only — D8b), in one per-line transaction, set `is_refunded=true` then
   `reverse_line` (D2 crash-safety). Then flip `process_line(..., partial_refunds_enabled=True)`
   (D6).

## Tests

Full refund reverses (base); partial-refund mixed lines (non-refunded depletes / refunded →
`line_refunded`, gate 20); **replayed refund event → exactly one reversal** + `is_refunded`/
side-effects don't re-fire (the idempotency mirror); modifier reversal (base+modifier net
zero, D3); **reverse-with-no-forward-movements → no-op, no error** (D4); **immutability** —
forward rows byte-identical after reverse, reversal additive with `source_id`=forward id
(gate 25/41); **late void after depletion → no reversal, movements untouched** (D8c); both
refund-arrival paths (edit 8); and the **real-`service_worker`-role e2e** proving the 0021
grant.

## References

- `backend/docs/sprints/sprint-5-unified-spec-v5-LOCKED.md` §11; known-limitation #4.
- `backend/docs/inventory_accounting_semantics.md` §6 (idempotency), §9 (reversal philosophy).
- `backend/docs/sprints/sprint-5-phase-map.md` Phase 11; edits 4, 5, 8.
- `backend/docs/migration-risk-standard.md` §1.2, §2.1, §4.5.
- v1-scope.md Inventory Truth Model (append-only ledger; compensating entries only).
