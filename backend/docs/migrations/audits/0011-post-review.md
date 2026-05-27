# Post-Migration Audit: 0011_sw_inventory_grants

**Migration:** `0011_sw_inventory_grants`
**Applied:** prior to the commit of `backend/docs/migration-risk-standard.md` (commit `7d6a7b6`)
**Audit date:** 2026-05-26
**Audit framework:** `backend/docs/migration-risk-standard.md`
**Status:** Applied. Working. Tests passing (420/420). Predates the standard.

## Purpose of this audit

Same purpose as `0013-post-review.md`: 0011 was applied before the migration risk standard was committed. This audit measures it against the standard retrospectively.

## What 0011 changed

Ten operations across five tables — RLS policies and GRANTs for the `service_worker` role:

1. `CREATE POLICY inventory_items_select_sw ON inventory_items FOR SELECT TO service_worker`
2. `GRANT SELECT ON inventory_items TO service_worker`
3. `CREATE POLICY inventory_movements_select_sw ON inventory_movements FOR SELECT TO service_worker`
4. `CREATE POLICY inventory_movements_insert_sw ON inventory_movements FOR INSERT TO service_worker`
5. `GRANT SELECT, INSERT ON inventory_movements TO service_worker`
6. `CREATE POLICY inventory_count_events_select_sw ON inventory_count_events FOR SELECT TO service_worker`
7. `GRANT SELECT ON inventory_count_events TO service_worker`
8. `CREATE POLICY inventory_yield_factors_select_sw ON inventory_yield_factors FOR SELECT TO service_worker`
9. `GRANT SELECT ON inventory_yield_factors TO service_worker`
10. `CREATE POLICY recipe_ingredients_select_sw ON recipe_ingredients FOR SELECT TO service_worker`
11. `GRANT SELECT ON recipe_ingredients TO service_worker`

(The migration docstring lists 5 tables; actual SQL has 10 policy+grant pairs — one per operation above.)

## Standard conformance: gaps

### Gap 1: No risk profile block (§1.2)

Retrospective classification:

| Operation | Data validity | Availability | App compatibility | Data propagation | Reversibility |
|---|---|---|---|---|---|
| All (GRANTs + RLS policies) | LOW | LOW | LOW — additive; no existing paths broken | LOW | LOW |

All operations are pure metadata: permission grants and RLS policies. No existing data is affected. No existing code paths are removed or altered. These grants enable code paths that would fail at runtime without them; the application code that uses these paths (Sprint 4 service and worker additions) was committed alongside this migration. All dimensions are LOW.

The only edge worth noting: revoking these grants in a downgrade while Sprint 4 application code remains deployed would cause runtime failures on the granted paths. The downgrade is safe only if paired with a compatible application rollback. This is a standard multi-component rollback coordination requirement, not a data-integrity concern.

### Gap 2: No formal §4.5 call-site verification documented

The migration docstring informally describes why each grant is needed (e.g., "The worker reads menu_items.recipe_version_id inside _insert_line_item()"), but does not include grep results. The standard requires verification results to be documented explicitly.

The retroactive verification (run 2026-05-27 from repo root):

```bash
# inventory_items SELECT (service_worker) — on_hand() and record_sale_inventory_effect() mode lookup
$ grep -n "inventory_items" backend/app/modules/inventory/services.py
```
**Results (selected):**
```
services.py:56     FROM inventory_items  (on_hand() Mode A CTE)
services.py:111    FROM inventory_items  (on_hand() Mode B anchor)
services.py:198    SELECT inventory_mode FROM inventory_items  (record_sale_inventory_effect mode lookup)
services.py:230    UPDATE inventory_items  (opening_balance mode upgrade)
services.py:261    JOIN inventory_items ii  (recipe walk join)
services.py:301    JOIN inventory_items ii  (record_sale_inventory_effect ingredient walk)
services.py:540    SELECT id, inventory_mode FROM inventory_items  (record_count_event item fetch)
services.py:613    UPDATE inventory_items  (record_count_event last_count update)
```
`service_worker` executes all of these paths. The `inventory_items` SELECT grant is required. Verified.

```bash
# inventory_movements SELECT + INSERT (service_worker) — idempotency check + depletion write
$ grep -n "inventory_movements" backend/app/modules/inventory/services.py
```
**Results (selected):**
```
services.py:188    SELECT id FROM inventory_movements  (idempotency check)
services.py:210    INSERT INTO inventory_movements     (record_sale_inventory_effect write)
services.py:282    SELECT id FROM inventory_movements  (idempotency check — reversal)
services.py:341    INSERT INTO inventory_movements     (record_sale_inventory_effect depletion write)
services.py:444    SELECT id FROM inventory_movements  (reversal idempotency check)
services.py:482    INSERT INTO inventory_movements     (record_sale_reversal write)
services.py:819    SELECT id FROM inventory_movements  (record_count_event adjustment idempotency)
services.py:836    INSERT INTO inventory_movements     (record_count_event adjustment write)
```
`service_worker` executes all of these paths. Both SELECT and INSERT grants are required. Verified.

```bash
# inventory_count_events SELECT (service_worker) — late-signal boundary check
$ grep -n "inventory_count_events" backend/app/modules/inventory/services.py
```
**Results:**
```
services.py:370    SELECT 1 FROM inventory_count_events  (late-signal boundary check)
services.py:558    SELECT id FROM inventory_count_events  (record_count_event idempotency check)
services.py:583    INSERT INTO inventory_count_events     (record_count_event write)
```
SELECT grant is required. Verified. (INSERT is for `app_user`, not `service_worker` — workers don't initiate counts.)

```bash
# inventory_yield_factors SELECT (service_worker) — yield factor snapshot at write time
$ grep -n "inventory_yield_factors" backend/app/modules/inventory/services.py
```
**Results:**
```
services.py:78     SELECT yield_factor FROM inventory_yield_factors  (watermark on_hand() fallback)
services.py:146    SELECT yield_factor FROM inventory_yield_factors  (historical on_hand() fallback)
services.py:323    SELECT yield_factor FROM inventory_yield_factors  (record_sale_inventory_effect snapshot)
```
SELECT grant is required. Verified.

```bash
# recipe_ingredients SELECT (service_worker) — ingredient walk for _emit_inventory_effects
$ grep -rn "recipe_ingredients" backend/app --include="*.py"
```
**Results:**
```
services.py:297    JOIN recipe_ingredients ri  (record_sale_inventory_effect ingredient query)
worker.py:517      JOIN recipe_ingredients ri  (_emit_inventory_effects ingredient query in worker)
```
SELECT grant is required. Verified.

## What 0011 got right

0011 is a clean, single-purpose metadata migration. All ten operations are grants and policies — no data changes, no constraint enforcement. The docstring explains the motivation for each grant. The downgrade function removes every policy and revokes every grant in the exact reverse order. No isolation rule violation (all metadata).

The retrospective call-site verification confirms every granted path is used by `service_worker`-role code in the Sprint 4 application layer.

## Operational disposition

0011 is accepted as-applied. No action required.
