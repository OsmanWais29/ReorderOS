# Inventory Accounting Semantics

**Status**: Active  
**Applies to**: Sprint 3+ inventory system  
**Last updated**: 2026-05-26  

This document defines the accounting philosophy, temporal semantics, and operational
invariants of the ReorderOS inventory system. Every developer touching
`app/modules/inventory/` must read this before making changes. Violating these
invariants corrupts auditable history that cannot be recovered without manual
reconciliation.

---

## 1. The Ledger/Accountant Separation

The system is divided into two distinct layers with no overlap of responsibility.

### The Ledger (database)

The database enforces **immutable structural invariants** — rules whose violation
would permanently invalidate the accounting record. These invariants are enforced
at the DB level because they must hold regardless of which code path writes to the
table.

What the DB enforces:
- **Referential integrity**: FKs, NOT NULL, UNIQUE constraints
- **Enumerated movement types**: CHECK constraint on `movement_type`
- **Append-only `inventory_movements`**: `REVOKE UPDATE, DELETE ON inventory_movements FROM app_user`
- **Opening balance ordering**: `fn_opening_balance_must_be_first()` trigger raises an
  error if `opening_balance` is inserted after other movements exist for the same item.
  This is a cross-row constraint that cannot be expressed as a CHECK — it is a domain
  invariant of the ledger, not business logic.
- **Tenant isolation**: Row Level Security on every tenant-scoped table

What the DB does NOT enforce: quantity calculations, mode branching, depletion
formulas, alert thresholds, reconciliation decisions. Those belong exclusively to
the service layer.

### The Accountant (service layer)

`app/modules/inventory/services.py` is the sole location for accounting decisions.
It owns:
- All quantity calculations (`on_hand`, `theoretical_qty`, `count_adjust` delta)
- All inventory mode branching (Mode A vs Mode B behavior)
- All idempotency key generation
- All monitoring alert thresholds
- All reconciliation logic (watermark capture, late-signal detection)

**Rule**: If you are making a domain decision about inventory — what to write, how
much, in which direction, under what condition — that decision belongs in
`services.py`. If you find yourself encoding a domain decision inside a migration
or trigger, stop and move it to the service layer.

---

## 2. Mode A — Recipe-Deducted (Deterministic)

**Guarantee**: `on_hand()` for a Mode A item is exact and deterministic.

```
on_hand = SUM(delta)
          over all inventory_movements
          WHERE movement_type NOT IN ('sale_signal', 'sale_signal_reversal')
```

Every depletion event writes a ledger row. The current balance is always the sum of
all rows. There is no anchor, no approximation, no reference table dependency.

**What this means operationally**:
- A wrong depletion is corrected by a compensating `count_adjust` or
  `sale_depletion_reversal` — never by deleting or editing the original row.
- Late-arriving events are simply added to the running sum. Ordering does not
  affect the result.
- Historical replay at any point in time is exact: filter movements by
  `recorded_at <= T` and sum.

**Appropriate for**: any ingredient where per-unit tracking matters — proteins,
premium ingredients, controlled stock.

**Sprint 5 sale-depletion formula (v5 §11)** — the per-ingredient quantity is:

```
delta = -1 * line_quantity
           * (recipe_ingredient.quantity / recipe_versions.yield_quantity)
           * unit_conversion_factor
```

where `unit_conversion_factor` converts the recipe ingredient's unit to the inventory
item's storage unit via `unit_conversions` (the `convert()` service). Mode B is identical
with a positive sign (`sale_signal`).

> **`inventory_items.storage_to_recipe_factor` is NOT consulted by Sprint 5 sale
> depletion.** Sprint 3/4 used it (`sale_qty * recipe_qty / factor`); v5 replaces it with
> unit conversion. The column still exists in the schema but is **vestigial for depletion**
> — the walker never reads it. It is intentionally left in place (no cleanup migration in
> Sprint 5; a future migration may drop it). A reader seeing the column must not assume
> depletion uses it.

---

## 3. Mode B — Count-Anchored (Approximation)

**Guarantee**: `on_hand()` for a Mode B item is an approximation, not an exact
accounting figure. The system explicitly models this as an operational estimate.

```
on_hand = last_count_quantity
        + SUM(delta of receive/transfer_in/count_adjust/opening_balance movements)
            WHERE created_at > reconciliation_cutoff_created_at
        - SUM(delta_i × yield_factor_applied_i, per row)
            for each sale_signal and sale_signal_reversal movement i
            WHERE created_at > reconciliation_cutoff_created_at
```

**`opening_balance` in the receipts term**: included to match the implementation
(`services.py` `receipts_since`). In practice an `opening_balance` is written once at item
creation, *before* any count, so it is excluded by `created_at > cutoff` and the term is empty;
it is listed only so a re-initialization edge case (an `opening_balance` after a count) is
counted consistently by the formula and the code.

**Sign convention**: `sale_signal` rows store a positive delta (theoretical units consumed);
`sale_signal_reversal` rows store the arithmetic negation (negative delta). Summing both
without `ABS()` nets them correctly — a reversal cancels its original. Using `ABS()` would
accumulate magnitudes rather than netting them and produces wrong answers when reversals exist.

The filter boundary is `created_at` (system ingestion time), not `recorded_at`
(business event time). See Section 4 for why.

**Why Mode B is approximate and cannot be made exact**:

A physical count at timestamp T says: "inventory is X units at the moment of
counting." What it cannot say is which real-world events — sales, waste, transfers
— the counter physically observed before recording the count. The system knows when
it ingested events, not whether those events were reflected in the counter's
physical observation. This ambiguity is irreducible.

Mode B is the correct model for bulk ingredients, consumables, and items where
frequent physical counts are the primary correction mechanism. For items requiring
exact accounting, use Mode A.

**Appropriate for**: non-recipe items, bulk ingredients, high-volume low-value
consumables.

---

## 4. Three-Timestamp Model

Every `inventory_movements` row carries three timestamps with distinct, non-overlapping
meanings. Never conflate them.

### `recorded_at` — Business Event Time

When the real-world event occurred, according to the source system (Clover POS
timestamp, manual entry, receipt date). Supplied by the caller. Can be in the past.
Can be clock-skewed. **Treated as a business claim, not a verified system fact.**

Used for:
- Human-readable audit trails ("this sale happened at 7:42 PM")
- Historical replay queries filtered by business time
- Analytics and reporting

Not used for: reconciliation boundary calculations in Mode B.

### `created_at` — System Accountability Time

When the database row was inserted. Set by `DEFAULT NOW()` at INSERT. Never
mutated. Protected by `REVOKE UPDATE ON inventory_movements FROM app_user`.
**This is a system fact, not a claim.**

Used for:
- Mode B reconciliation boundary (`created_at > reconciliation_cutoff_created_at`)
- Late-signal detection (`created_at - recorded_at > threshold`)
- Ordering movements by ingestion sequence

The accounting philosophy: the system becomes accountable for an event at the
moment it inserts the row, regardless of when the business event allegedly occurred.
A Clover sale that happened Monday but whose webhook was retried Wednesday is
accounted for on Wednesday. This is defensible because:
1. `created_at` is a verifiable system fact; `recorded_at` is an external claim
2. The late arrival is surfaced as an alert, not silently absorbed
3. Auditors can see both timestamps and understand the gap

### `accounted_at` — Explicit Accounting Acceptance Time

Nullable. When NULL, treat as equal to `created_at`. Reserved for future tooling
where a row is recreated (replay, dead-letter reprocessing, event repair) but the
original accounting acceptance time must be preserved distinct from the new
`created_at` of the recreated row.

**Today**: `accounted_at` is always NULL (equals `created_at`).  
**Future**: replay tooling sets `accounted_at` to the original acceptance time when
recreating rows, allowing `created_at` to reflect the new insertion without losing
the original accounting boundary.

Do not write code today that reads `accounted_at`. It is a reserved column for
future tooling. Document its purpose in comments if you reference it.

---

## 5. Compensating Entries Only — No Mutations, No Deletions

**This is the single most important rule in the system.**

`inventory_movements` rows are permanent accounting facts. They are never:
- `UPDATE`d for any reason
- `DELETE`d for any reason
- Retroactively recalculated
- Silently overwritten

Every correction, reversal, and void is expressed as a **new row** with a delta
that counteracts the original.

### Why this matters

An accounting ledger that allows mutation produces non-reproducible history. If
you can change a depletion row after the fact, you cannot answer the question
"what was on-hand on March 15th?" because the rows that existed on March 15th may
have been edited since.

Append-only ledgers are the foundation of double-entry bookkeeping. They are why
accounting systems survive audits. Treat every movement row as a journal entry
that has been stamped, witnessed, and filed.

### How corrections work

| Scenario | Wrong approach | Correct approach |
|---|---|---|
| Wrong quantity depletion | Edit the original row's delta | Insert `count_adjust` with compensating delta |
| Sale needs to be voided | Delete the `sale_depletion` row | Insert `sale_depletion_reversal` with opposite delta |
| Recipe was wrong at sale time | Recalculate and update old movements | Insert reversal of original + new depletion with correct recipe |
| Physical count finds discrepancy | Adjust the opening balance | Insert `count_adjust` dated at count time |
| Yield factor was wrong | Update old depletion rows | Accept historical rows as-is (they carry `yield_factor_applied`); new depletions use corrected factor |

---

## 6. Idempotency Contract

Every service write is idempotent. Submitting the same logical operation multiple
times produces exactly one row and returns the original result.

**Idempotency key format**: `{operation_type}:{deterministic_identifiers}`

| Operation | Key format |
|---|---|
| Opening balance | `opening_balance:{inventory_item_id}` |
| Sale effect — base (v5) | `sale_line:{sale_line_item_id}:base:{recipe_version_id}:{inventory_item_id}` |
| Sale effect — modifier (v5) | `sale_line:{sale_line_item_id}:modifier:{sale_line_item_modifier_id}:{modifier_version_id}:{inventory_item_id}` |
| Count event | `count:{inventory_item_id}:{counted_at.isoformat()}:{counted_quantity}` |
| Count adjust | `count_adjust:{count_event_id}` |
| Receipt line | `receipt_line:{receipt_line_id}` |
| Sale reversal | `reversal:{original_movement_id}:{inventory_item_id}` |

The unique constraint `UNIQUE (tenant_id, idempotency_key)` on `inventory_movements`
enforces this at the DB level. The Sprint 5 writer uses `INSERT ... ON CONFLICT
(tenant_id, idempotency_key) DO NOTHING` (the constraint is the concurrency arbiter; two
workers racing the same sale cannot both insert).

**Legacy key (superseded).** Sprint 3/4 used `sale_line:{sale_line_item_id}:{inventory_item_id}`
(no `base:`/version segment). v5 supersedes it; no production rows exist in that format. The
writer still performs a **read-only** check for the legacy key before writing the new-format
base movement and skips if present — defense-in-depth against double depletion in dev/legacy
environments. The legacy format is referenced (read) but never written.

**Rule**: Any new write operation added to the service layer must have an
idempotency key. No exceptions. Writes without idempotency keys are not safe to
retry and will produce duplicate ledger entries under any network or process fault.

---

## 7. Count Reconciliation Boundaries

A count event is an **accounting checkpoint** — it establishes a new baseline for
Mode B items. The watermark mechanism ensures that signals ingested after the
checkpoint are correctly attributed to the post-count period.

### Atomicity requirement

`record_count_event()` must execute these steps in strict order, within a single
transaction, with the item row lock held throughout:

1. `SELECT ... FOR UPDATE` on the item row — acquires lock, serializes concurrent counts and signals
2. `cutoff = datetime.now(UTC)` — captures watermark while lock is held
3. `on_hand(session, ..., reconciliation_cutoff=cutoff)` — reads pre-count balance
4. `INSERT INTO inventory_count_events (..., reconciliation_cutoff_created_at=cutoff)`
5. Mode B: `UPDATE inventory_items SET last_count_at=..., last_count_quantity=...`
   Mode A: `INSERT INTO inventory_movements` (count_adjust if drift nonzero)
6. `session.flush()`

The lock must be acquired **before** the watermark is captured. If the watermark is
captured before the lock, a concurrent signal can insert between the two steps with
`created_at < cutoff`, causing it to be excluded even though the system did not
know about it at count time.

### What the watermark means

`reconciliation_cutoff_created_at` on a count event row means:

> "At the time this count was finalized, the system had ingested all signals with
> `created_at <= cutoff`. The physical count is assumed to reflect those signals.
> Signals with `created_at > cutoff` belong to the post-count period and will be
> included in future `on_hand()` calculations."

### Historical rows

Count event rows created before migration `0010` have `reconciliation_cutoff_created_at = NULL`.
`on_hand()` falls back to `recorded_at > last_count_at` for these rows. This is the
pre-watermark behavior and is backwards compatible. New counts always have a cutoff.

---

## 8. Late Event Policy

### Definition

A late event is a movement where `(created_at - recorded_at) > 30 minutes` AND
the movement crosses a count reconciliation boundary (i.e., `recorded_at` is before
a count's cutoff but `created_at` is after it).

### What the system does

1. The signal is processed normally and attributed to the correct period per the
   watermark rule (included in post-count `on_hand()`)
2. A `late_signal_reconciliation` alert fires at ingestion time (not at the next count)
3. Severity: `warn`
4. The alert is operator-visible in `monitoring_alerts`

### What the system does NOT do

The system does not auto-correct. It does not attempt to determine whether the
physical count already reflected this event. That determination requires human
judgment and operator knowledge of how the count was conducted.

### Why alert at ingestion, not at count time

The ambiguity is known the moment the signal arrives — `created_at` is set, the
gap to `recorded_at` is calculable, and the count boundary is queryable. Deferring
the alert to the next count event means operators may not see the flag for hours
or days. Immediate alerting allows same-session review.

### The `recorded_at` vs `created_at` gap as a diagnostic signal

A large gap between `recorded_at` and `created_at` is always worth investigating:
- Clover POS clock skew
- Webhook delivery delay
- Dead-letter queue reprocessing
- Manual backdated entry

The gap is surfaced in the alert payload so operators can distinguish these causes.

---

## 9. Reversal Philosophy

### Movement types are accounting intent, not just mechanics

Movement types encode why a row exists, not just what it does. Auditors, analytics
queries, and anomaly detection tools all read movement types to understand provenance.
Generic types like `adjustment` destroy that provenance — the row becomes a black
box with a delta and no history.

### Explicit reversal types

| Original type | Reversal type |
|---|---|
| `sale_depletion` | `sale_depletion_reversal` |
| `sale_signal` | `sale_signal_reversal` |

Both exist in the `movement_type` CHECK constraint. `sale_depletion_reversal` was
added in migration `0010`. Do not use `adjustment` for reversals.

### Reversal mechanics

A reversal row must:
- Have `delta` equal to the exact arithmetic negation of the original row's `delta`
- Have `source_id` pointing to the original movement's `id`
- Have `source_type = 'reversal'`
- Have idempotency key `reversal:{original_movement_id}:{inventory_item_id}`
- Have `movement_type` matching the table above
- Have `yield_factor_applied` equal to the original row's `yield_factor_applied`

The `yield_factor_applied` requirement is a constraint of reversal arithmetic, not a field
detail. The watermark `on_hand()` path computes `delta * yield_factor_applied` for every
movement in the signal set. For the original and its reversal to net exactly to zero, both
must carry the same `yield_factor_applied`. A reversal with a different or absent value
would not cancel the original, silently corrupting Mode B `on_hand()` calculations.
Do not omit or override this field when writing reversal rows.

A reversal is idempotent. Calling `record_sale_reversal()` twice produces one row.

### What reversals do not do

A reversal does not delete the original row. It does not modify the original row.
The original row remains in the ledger permanently. The net effect of
`original + reversal` is zero delta, which is the correct representation of a voided
transaction in an append-only accounting system.

---

## 10. Snapshot Invariants

These invariants ensure that historical depletion replay never depends on the
current state of any mutable reference table.

### `yield_factor_applied` on `inventory_movements`

Every `sale_depletion` and `sale_signal` row stores the yield factor that was in
effect at write time. Changing `inventory_yield_factors` after a depletion was written
does not affect that depletion's historical record.

**Rule**: `record_sale_inventory_effect()` must always read the current yield factor
and store it in `yield_factor_applied` — never leave this field NULL and never inline
from the reference table after the fact.

If `inventory_yield_factors` has no row for the item, store `1.0` (the identity).

The role of the stored value differs by mode:

- **Mode B (`sale_signal`)**: `on_hand()` applies `yield_factor_applied` at query time
  to each signal row's delta (`delta * yield_factor_applied`). The stored snapshot is
  what makes Mode B on_hand() stable across future yield table changes.
- **Mode A (`sale_depletion`)**: `on_hand()` sums deltas directly (per §2). The delta
  is pre-computed at write time incorporating the yield factor, so `yield_factor_applied`
  is stored as an audit record of the factor that was used — it is not re-applied at
  query time. Applying it again at query time would double-count.

For the reversal-specific requirement — that the reversal row must carry the same
`yield_factor_applied` as its original — see §9. That requirement is a constraint of
cancellation arithmetic and applies regardless of mode.

### `recipe_version_id` on `sale_line_items`

`sale_line_items.recipe_version_id` is set once, at sale processing time, and never
updated. It points to the exact recipe version used to compute the depletion. Future
recipe changes do not affect historical sales.

**Rule**: No code path may UPDATE `sale_line_items.recipe_version_id` after it has
been set. This column is immutable once written.

---

## 10b. Depletion-status reason fidelity (Sprint 5)

The Sprint 5 depletion worker runs as `service_worker`, which cannot read the
operator-owned `recipes` table (least-privilege boundary). Consequently, when a sale line
has no confirmed recipe version (`recipe_version_id` is NULL), the worker records
`depletion_status='unmapped', depletion_reason='no_recipe'` for **all** such cases — it
cannot distinguish *never configured* from *draft* from *operator-skipped*, because that
requires reading `recipes`.

**This conflation is intentional, and the finer breakdown is NOT lost — it is just not
stamped on the depletion row.** Operator-facing reporting (running as `app_user`, which can
read `recipes`) recovers draft-vs-skipped-vs-absent by joining `recipes` on
`menu_items`. A dashboard reading raw `depletion_reason='no_recipe'` coverage must NOT
conclude "nothing was configured" — some of those may be deliberately skipped or
in-draft; query the operator side for the real breakdown.

(Minor: late-signal is detected per written movement, so a single sale line crossing a
count boundary with N movements — base + modifiers — increments the alert's `alert_count`
N times. It is a frequency counter on a single tenant-level alert, not N alert rows.)

---

## 11. Known Fidelity Gaps in Tests

### Deferred constraints

Tests use `make_bound_session(conn)` which wraps service commits in SAVEPOINTs via
`join_transaction_mode="create_savepoint"`. PostgreSQL deferred constraints
(`DEFERRABLE INITIALLY DEFERRED`) only fire at the outer COMMIT, not at SAVEPOINT
RELEASE. Such constraints will not be exercised in the test suite.

Mitigation: the codebase currently has no deferred constraints. If one is ever
added, add a note here and write a separate integration test that uses a real
transaction commit to verify it.

### Migration rollback

`downgrade()` functions in migration files are written but never automatically
tested. Before any production downgrade, manually verify the downgrade on a staging
database.

---

## 12. Future Evolution Paths

These are documented decisions not to build yet — recorded here so the
architecture does not drift toward them accidentally before they are formally
designed.

### `accounted_at` separation

Today `accounted_at` is always NULL (treated as equal to `created_at`). When
event replay tooling is introduced — dead-letter reprocessing, historical event
repair — `accounted_at` will be set to the original acceptance time while
`created_at` reflects the new insertion. This separation allows the accounting
boundary to be preserved across row recreation.

Do not write production code that reads `accounted_at` until this tooling exists.

### Reconciliation periods as first-class entities

The system currently creates implicit accounting periods bounded by count events.
A future `inventory_reconciliation_periods` table would make these explicit:
- Explicit accounting windows with open/closed state
- Variance analysis between periods
- Late-event attribution to a specific period
- Reconciliation reports per period

The watermark approach in `inventory_count_events` is designed to be backwards
compatible with this future model — `reconciliation_cutoff_created_at` becomes
a FK into `inventory_reconciliation_periods` when that table exists.

### Decoupled signal append vs anchor update

`record_count_event()` currently holds `FOR UPDATE` on the item row to serialize
concurrent signals during count finalization. On high-volume Mode B items, this
creates a hot row. The long-term fix is to decouple signal appends (pure inserts,
no lock needed) from anchor updates (lock required). Count finalization becomes the
only lock-requiring operation, and signals run at full throughput between counts.

### Lock contention under high write volume

`FOR UPDATE` on `inventory_items` serializes concurrent count events and concurrent
signals on the same item. At low to moderate volume this is correct and safe. At
high volume (busy restaurant, multiple terminals processing simultaneous sales),
consider batching signals and processing them in micro-batches rather than one
row per HTTP request.

### Reconciliation exception workflows

The current `late_signal_reconciliation` alert surfaces ambiguity but provides no
structured resolution path. A future reconciliation exception queue would allow
operators to mark exceptions as reviewed, resolved, or escalated — turning the
alert from a notification into a workflow.

---

## 13. Glossary

Terms used in this document. Definitions are scoped to this system; equivalent terms in other accounting systems may carry slightly different meanings.

**Accounted-at time** — the value of `accounted_at` on an `inventory_movements` row. Currently always NULL; treated as equal to `created_at`. Reserved for future replay tooling per §12.

**Anchor** — a value or event that establishes a known baseline against which subsequent movements are reconciled. The canonical anchor is the most recent `count_adjust` movement (Mode A) or `inventory_items.last_count_at`/`last_count_quantity` (Mode B). Anchors do not invalidate prior movements; they assert a new reality going forward from a defined point in time.

**Append-only** — the property that `inventory_movements` rows are never UPDATEd or DELETEd. Corrections are expressed as new rows. Enforced at the database level by `REVOKE UPDATE, DELETE ON inventory_movements FROM app_user`.

**Authoritative** — recorded as a row in `inventory_movements`. Authoritative data is the source of truth for what happened. The phrase "the ledger" refers exclusively to authoritative data.

**Business event time** — the timestamp on `inventory_movements.recorded_at`. Reflects when the real-world event occurred according to the source system (POS timestamp, manual entry, receipt date). Supplied by the caller. May be backdated. Treated as a claim, not a verified fact. See §4.

**Compensating entry** — a movement that corrects a prior movement by adding a new row with an opposite-sign `delta`. The compensating entry has `source_id` pointing to the original movement's `id` and `source_type = 'reversal'`. See §9.

**Created-at time** — the timestamp on `inventory_movements.created_at`. Reflects when the database row was inserted. Set by `DEFAULT NOW()` at insert. Never mutated. Treated as a verified system fact. See §4.

**Delta** — the signed quantity change on an `inventory_movements` row. Positive values increase inventory (`receive`, `count_adjust` upward); negative values decrease inventory (`sale_depletion`, `sale_signal`, `waste`, `count_adjust` downward).

**Idempotency key** — the value of `inventory_movements.idempotency_key`. Format is `{operation_type}:{deterministic_identifiers}` per §6. The unique constraint `UNIQUE (tenant_id, idempotency_key)` enforces idempotency at the database level.

**Inferred event** — a movement whose `delta` was computed from a derived source rather than directly observed. The canonical inferred events are `sale_depletion` (Mode A recipe walk against a POS sale) and `sale_signal` (Mode B telemetry derived from a POS sale). Distinguished from observed events because the derivation depends on reference data (recipes, conversions, yield factors) that may be wrong.

**Late event** — a movement where `created_at - recorded_at > 30 minutes` AND `recorded_at` falls before a count's reconciliation cutoff while `created_at` falls after it. See §8.

**Mode A** — the `recipe_deducted` value of `inventory_items.inventory_mode`. Stock state is derived from the sum of all admitted ledger rows. Exact and deterministic. See §2.

**Mode B** — the `count_anchored` value of `inventory_items.inventory_mode`. Stock state is derived from the most recent count anchor plus post-anchor observed events. Sale signals are telemetry, not stock truth. Approximate by design. See §3.

**Movement** — a row in `inventory_movements`. Every change to inventory state is a movement.

**Observed event** — a movement whose `delta` was witnessed directly rather than derived. Examples: `receive` (supplier delivery), `count_adjust` (physical count), `waste` (operator-logged loss), `opening_balance` (initial stock entered by operator). Distinguished from inferred events because the observation does not depend on potentially-wrong reference data.

**Projection** — derived state that is regenerable from authoritative data plus operator inputs. Examples: any cached `on_hand` value, dashboard counters, `inventory_items.last_count_quantity`, Mode B `confidence_state`. Projections are optimizations, not truth. If removing a projection value would corrupt accounting history, that value is not a projection — it is authoritative and belongs in `inventory_movements`.

**Reconciliation cutoff** — the value of `inventory_count_events.reconciliation_cutoff_created_at`. Marks the system-time boundary at which a count event was finalized. Used by Mode B `on_hand()` to determine which movements are post-anchor. See §7.

**Replay** — re-deriving `inventory_movements` rows from their original input sources to verify or reconstruct history. Mode A replay (sum movements filtered by `recorded_at <= T`) is exact within a single application version. See §10.

**Reversal** — a compensating entry with `movement_type` of `sale_depletion_reversal` or `sale_signal_reversal`. Reversal rows have `source_type = 'reversal'`. See §9.

**Snapshot** — a value captured on a movement row at write time that fixes the calculation against a then-current reference value, immune to later changes in the reference table. `yield_factor_applied` and `recipe_version_id` are the two current snapshot columns. See §10.

**Watermark** — synonym for reconciliation cutoff in this document.

**Yield factor** — a multiplier stored in `inventory_yield_factors` that adjusts theoretical recipe quantities to reflect actual depletion. Snapshotted onto each `sale_depletion` and `sale_signal` row at write time via `yield_factor_applied`.
