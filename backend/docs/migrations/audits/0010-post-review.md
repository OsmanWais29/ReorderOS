# Post-Migration Audit: 0010_accounting_hardening

**Migration:** `0010_accounting_hardening`
**Applied:** prior to the commit of `backend/docs/migration-risk-standard.md` (commit `7d6a7b6`)
**Audit date:** 2026-05-26
**Audit framework:** `backend/docs/migration-risk-standard.md`
**Status:** Applied. Working. Tests passing (420/420). Predates the standard.

## Purpose of this audit

Same purpose as `0013-post-review.md`: 0010 was applied before the migration risk standard was committed. This audit measures it against the standard retrospectively, records the preflight and call-site verification that should have been documented, and establishes the permanent record.

## What 0010 changed

Five changes in a single migration file:

1. `inventory_movements.yield_factor_applied NUMERIC` (nullable column add)
2. `inventory_movements.accounted_at TIMESTAMPTZ` (nullable column add, reserved)
3. `inventory_count_events.reconciliation_cutoff_created_at TIMESTAMPTZ` (nullable column add)
4. `inventory_movements.movement_type` CHECK constraint drop + recreate, adding `sale_depletion_reversal`
5. `CREATE INDEX idx_inv_movements_created_at ON inventory_movements (tenant_id, inventory_item_id, created_at DESC)`

## Standard conformance: gaps

### Gap 1: No risk profile block (§1.2)

The standard requires a five-dimension risk classification in every migration. 0010 has no risk profile block. Retrospective classification:

| Operation | Data validity | Availability | App compatibility | Data propagation | Reversibility |
|---|---|---|---|---|---|
| 1, 2 (nullable column adds) | LOW | LOW | **MEDIUM** — new write path required (see §4.5 below) | LOW | LOW |
| 3 (nullable column add) | LOW | LOW | **MEDIUM** — new write path required (see §4.5 below) | LOW | LOW |
| 4 (CHECK expansion) | LOW — expansion adds an allowed value; existing rows not affected | LOW | LOW — new type is opt-in; no existing path used `sale_depletion_reversal` before the migration | LOW | LOW |
| 5 (index) | LOW | LOW | LOW | LOW | LOW |

The MEDIUM app compatibility ratings on operations 1–3 reflect the fact that adding a nullable column to a table that application code writes to requires the write path to be updated to populate the new column. Unlike dropping or renaming a column, the application won't break if it doesn't write the column — but correctness of the new feature depends on the write path being present. See Gap 2 and Gap 3 for verification.

### Gap 2: No §4.5 call-site verification documented

The standard requires explicit verification that application call sites handle the new schema. For 0010, three columns have application write paths that must be verified.

The retroactive verification (run 2026-05-27 from repo root):

```bash
# Operations 1, 2 — yield_factor_applied and accounted_at write path
$ grep -rn "yield_factor_applied" backend/app --include="*.py"
```
**Results:**
```
backend/app/modules/inventory/services.py:47     (docstring — on_hand() uses per-row yield_factor_applied)
backend/app/modules/inventory/services.py:77     COALESCE(m.yield_factor_applied, ...) in watermark on_hand() query
backend/app/modules/inventory/services.py:266    (docstring — yield_factor_applied snapshotted at write time)
backend/app/modules/inventory/services.py:344    INSERT INTO inventory_movements (..., yield_factor_applied, ...)
backend/app/modules/inventory/services.py:454    SELECT delta, movement_type, yield_factor_applied — reversal read path
backend/app/modules/inventory/services.py:484    INSERT INTO inventory_movements (..., yield_factor_applied) — reversal write path
```
`record_sale_inventory_effect()` writes `yield_factor_applied` at line 344. `record_sale_reversal()` reads it from the original row (line 454) and carries it into the reversal INSERT (line 484). The watermark `on_hand()` query uses `COALESCE(yield_factor_applied, live_lookup)` at line 77 — backwards-compatible with pre-0010 rows that have NULL. `accounted_at` is intentionally not written by any application code (reserved for future tooling); grep returned no write sites. This is correct per the migration docstring.

```bash
# Operation 3 — reconciliation_cutoff_created_at write path
$ grep -rn "reconciliation_cutoff_created_at" backend/app --include="*.py"
```
**Results:**
```
backend/app/modules/inventory/services.py:374    read path — on_hand() watermark filter
backend/app/modules/inventory/services.py:375    read path — continued
backend/app/modules/inventory/services.py:376    read path — continued
backend/app/modules/inventory/services.py:377    read path — NULL fallback for pre-0010 rows
backend/app/modules/inventory/services.py:587    INSERT INTO inventory_count_events (..., reconciliation_cutoff_created_at)
```
`record_count_event()` writes `reconciliation_cutoff_created_at` at line 587. The `on_hand()` watermark path reads it at lines 374–377, with NULL fallback for pre-0010 count rows. Both paths are present.

```bash
# Operation 4 — sale_depletion_reversal usage
$ grep -rn "sale_depletion_reversal" backend/app --include="*.py"
```
**Results:**
```
backend/app/modules/inventory/services.py:432    (docstring — record_sale_reversal emits sale_depletion_reversal or sale_signal_reversal)
backend/app/modules/inventory/services.py:470    reversal_type = "sale_depletion_reversal"
```
`record_sale_reversal()` maps `sale_depletion` → `sale_depletion_reversal` at line 470. The CHECK constraint expanded in operation 4 permits this value. Verified.

### Gap 3: No risk profile documented for app compatibility impact

The MEDIUM app compatibility ratings above were not documented at apply time. The implicit assumption was "nullable column adds don't break the application." This is true for availability — the application won't crash. But correctness of `yield_factor_applied` and `reconciliation_cutoff_created_at` depends entirely on the write paths in `services.py` being in place. The write paths were committed as Sprint 4 code (uncommitted at audit time but verified present). The standard would have required this verification to be explicit in the migration record.

## What 0010 got right

All operations are metadata or metadata-equivalent. The nullable column adds ensure backwards compatibility: rows written before 0010 have NULL in the new columns, and `on_hand()` handles this via `COALESCE(yield_factor_applied, live_lookup)` at line 77. The CHECK constraint expansion is strictly additive — existing rows are not invalidated. The downgrade function correctly reverses every operation in dependency order, including removing the `sale_depletion_reversal` value from the constraint.

No isolation rule violation: all five operations are metadata (no data-validating operations in this migration).

## Operational disposition

0010 is accepted as-applied. The retrospective call-site verification confirms both write paths exist and are correct. The NULL fallback in `on_hand()` provides backwards compatibility for pre-0010 rows.

## Implications for the next migration

0010 is the first of the Sprint 4 batch. The pattern of nullable column adds paired with service-layer write paths is sound. Future migrations that follow this pattern should document the write path file and line number in the migration docstring at the time of writing, not retroactively.
