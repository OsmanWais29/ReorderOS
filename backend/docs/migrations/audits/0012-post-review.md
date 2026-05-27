# Post-Migration Audit: 0012_menu_item_recipe_link

**Migration:** `0012_menu_item_recipe_link`
**Applied:** prior to the commit of `backend/docs/migration-risk-standard.md` (commit `7d6a7b6`)
**Audit date:** 2026-05-26
**Audit framework:** `backend/docs/migration-risk-standard.md`
**Status:** Applied. Working. Tests passing (420/420). Predates the standard.

## Purpose of this audit

Same purpose as `0013-post-review.md`: 0012 was applied before the migration risk standard was committed. This audit measures it against the standard retrospectively.

## What 0012 changed

Three operations:

1. `menu_items.recipe_version_id UUID REFERENCES recipe_versions(id)` — nullable FK column add
2. `CREATE POLICY menu_items_select_sw ON menu_items FOR SELECT TO service_worker`
3. `GRANT SELECT ON menu_items TO service_worker`

## Standard conformance: gaps

### Gap 1: No risk profile block (§1.2)

Retrospective classification:

| Operation | Data validity | Availability | App compatibility | Data propagation | Reversibility |
|---|---|---|---|---|---|
| 1 (nullable FK add) | LOW — nullable; existing rows get NULL, which is the correct representation of "no recipe mapped" | LOW | **MEDIUM** — worker's `_insert_line_item()` must read `recipe_version_id` at sale time; without 0012 this SELECT would fail | LOW | LOW |
| 2, 3 (policy + GRANT) | LOW | LOW | **MEDIUM** — same reasoning as operation 1; the SELECT cannot succeed without the grant | LOW | LOW |

The MEDIUM app compatibility rating reflects the same pattern as 0011: these operations enable a Sprint 4 code path (`worker.py:_insert_line_item()` reads `menu_items.recipe_version_id`) that does not exist in prior application versions. Existing paths are not broken. The new path fails at runtime without both the column and the grant.

No data-validating operations in this migration. No isolation rule issue.

### Gap 2: No formal §4.5 call-site verification documented

The migration docstring states the reason for each operation informally ("The worker reads menu_items.recipe_version_id inside _insert_line_item()") but includes no grep results. The standard requires explicit verification with documented results.

The retroactive verification (run 2026-05-27 from repo root):

```bash
# Operation 1, 2, 3 — menu_items.recipe_version_id read path + service_worker SELECT requirement
$ grep -rn "menu_items\|recipe_version_id" backend/app --include="*.py" | grep -v "sale_line_items"
```
**Results (selected):**
```
worker.py:425    # Best-effort menu_item_id + recipe_version_id lookup.
worker.py:426    # recipe_version_id is snapshotted at insert time (Section 10 of the accounting ADR)
worker.py:436    SELECT id, recipe_version_id FROM menu_items WHERE pos_item_id = :pid AND tenant_id = :tid
worker.py:445    if mi_row.recipe_version_id is not None:
worker.py:446        recipe_version_id = str(mi_row.recipe_version_id)
worker.py:477    INSERT INTO sale_line_items (..., recipe_version_id, ...)
worker.py:518    ON ri.recipe_version_id = s.recipe_version_id  (ingredient walk join)
services.py:298  ON ri.recipe_version_id = s.recipe_version_id  (record_sale_inventory_effect join)
```
`worker.py:436` executes as `service_worker`. The query selects `recipe_version_id` from `menu_items`. Both the column (operation 1) and the SELECT grant (operations 2–3) are required. The column value is then snapshotted into `sale_line_items.recipe_version_id` at line 477, fulfilling the snapshot invariant documented in `inventory_accounting_semantics.md §10`. Verified.

```bash
# Confirm no other application paths write to menu_items.recipe_version_id
$ grep -rn "UPDATE.*menu_items\|menu_items.*UPDATE" backend/app --include="*.py"
```
**Result: no results.** No application code updates `menu_items` rows. The column is written by operators through the admin interface (not yet implemented in Sprint 4). The migration docstring note — "Set by operators when they map menu items to recipes" — is accurate.

## What 0012 got right

0012 is a clean, single-purpose metadata migration. The nullable FK add is fully backwards-compatible: existing `menu_items` rows receive NULL for `recipe_version_id`, which is the correct representation of "no recipe assigned." The `on_hand()` depletion path handles NULL gracefully — the worker's `_insert_line_item()` only calls `_emit_inventory_effects()` when `recipe_version_id is not None` (worker.py:445). The downgrade reverses in correct dependency order: drop policy, revoke grant, drop column.

The snapshot invariant (recipe_version_id frozen at sale time) is correctly implemented. The column is populated at `_insert_line_item()` time and never updated. The `sale_line_items` immutability rule in `inventory_accounting_semantics.md §10` is satisfied.

## Operational disposition

0012 is accepted as-applied. The call-site verification confirms the single read path is correct and no write path exists that would corrupt the snapshot invariant.
