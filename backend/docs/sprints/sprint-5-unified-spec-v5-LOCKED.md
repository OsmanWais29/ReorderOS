# Sprint 5 — Recipe Configuration and Inventory Depletion (v5 — LOCKED)

> **Canonical reference.** Committed 2026-05-31. This is the authoritative Sprint 5 spec; all
> "what does Sprint 5 require?" questions resolve by reading this file. Sections 1–13, the hard
> exit gate, fail gates, fidelity limitations, and scope lists below are the founder-locked v5 text,
> reproduced verbatim. **Appendix F — Frontend Integration** was added per founder request to
> operationalize the frontend sections (§1, §3, §13) and the coverage view into concrete Expo wiring;
> it is implementation detail, not a scope change, and defers to the locked body wherever they touch.

**Goal:** Build the complete inventory configuration and depletion foundation. Operators complete onboarding with confirmed recipes. POS sales drive deterministic inventory depletion for both Mode A and Mode B ingredients, with correct handling of refunds, voids, and partial refunds. The foundation supports all downstream sprints without further architectural work on the depletion path.

**Estimated effort:** 6–7 weeks single-developer.

**Version 5 changes from v4:**
- Resolved 6 hard schema conflicts (table existence, missing columns, view column reference, doc path)
- Corrected sale eligibility rule (CREDITED is non-eligible per Clover docs; eligibility now per-line not just per-order)
- Added explicit partial-refund handling via Sprint 4 reversal mechanism
- Added per-line `is_refunded` tracking (if not already in schema)
- Added explicit menu sync separation from LLM inference, with retry endpoint
- Added `GET /onboarding/recipes` list endpoint
- Specified reorganization vs rewrite commit sequencing
- Added explicit Clover modifier worker extension as in-scope work

**This version is locked.** Implementation proceeds from v5. Subsequent revisions only for issues discovered during implementation, not for scope changes.

---

## Pre-implementation: doc supersession

Before any implementation begins, add a supersession note at the top of `docs/archive/v1-backend-build-plan.md` (the actual repo path; v4 incorrectly referenced `docs/v1-backend-build-plan.md`):

> **Supersession notice (2026-05-27):** Statements in this document referring to "current quantity materialized on item" or "ledger sum equals inventory_items.current_quantity" are obsolete as of migration 0009. The current accounting model is documented in `backend/docs/inventory_accounting_semantics.md`, which is the canonical source for inventory semantics. Where this document and the semantics doc conflict, the semantics doc wins.

Single commit. Must happen before Sprint 5 implementation work.

---

## What gets built

### 1. React Native Recipes screen

The onboarding step matching the design mockup:

- Shows **all menu items** synced from POS (top-20 framing dropped)
- Sorted by sales volume descending (informational)
- Each menu item is an accordion row showing: name, volume context, ingredient count, expand/collapse affordance
- Expanded row shows ingredients with quantity + unit + confidence badge
- "Add ingredient" affordance per item
- Inline editing of quantity and unit (canonical allowlist enforced)
- "Confirm recipe" button per item, disabled when draft has zero ingredients
- Progress counter: `confirmed / (total - skipped)` at top
- Three states: `draft` | `confirmed` | `skipped`
- Skipped items hidden by default with "Show skipped" toggle
- "Scan your menu" affordance present but disabled — ships in Sprint 6

**Auto-save:** Field-blur auto-save as draft. App close preserves draft state.

**Un-confirm mechanics:** Three-step atomic transaction:
1. Read currently-confirmed `recipe_versions` row
2. Insert new `recipe_drafts` row with `parent_recipe_version_id` pointing at the confirmed version
3. Update `recipes.status` to `'draft'`

All succeed or rollback. Original `recipe_versions` immutable. Historical `sale_line_items` continue pointing at it.

### 2. LLM ingredient inference service

Per-menu-item LLM call producing base recipe AND modifier suggestions in one call.

**Inputs:** Menu item name, restaurant name, cuisine type, full menu (cuisine context), known modifiers for the item.

**Output:**
```json
{
  "menu_item_id": "uuid",
  "base_recipe": { "ingredients": [...] },
  "modifiers": [
    { "modifier_id": "uuid", "name": "Extra shot", "ingredients": [...] }
  ],
  "model_version": "<captured at runtime>",
  "inferred_at": "2026-05-27T..."
}
```

**Provider:** Claude. Schema via tool use.
**Confidence:** `confident | likely | uncertain` self-reported.
**Storage:** `recipe_llm_suggestions` for base recipes (append-only); `modifier_llm_suggestions` for modifier suggestions (append-only).
**Operational rule:** Never at sale time. Depletion engine never calls it.
**Truth model:** LLM output is never read by depletion. Depletion reads only operator-confirmed data.
**Precondition:** Menu items must exist in `menu_items` table (populated via menu sync — see §4).

### 3. Modifier configuration UI

Sub-section per recipe. Operator sees POS-detected modifiers with LLM-inferred ingredients, edits quantities and units, confirms via same draft/confirmed pattern (≥1 ingredient required).

**Scope limit:** Additive modifiers only. Subtractive/substitution shown as "Not yet supported."

### 4. POS menu sync (new — was implicit in v4)

Menu synchronization is a separate concern from LLM inference. The Recipes screen requires `menu_items` populated before it renders meaningfully.

**Trigger paths:**
1. **Initial sync at Clover OAuth callback.** Immediately after successful OAuth, fetch menu items from Clover API and populate `menu_items` table.
2. **Explicit retry endpoint:** `POST /pos/clover/sync-menu` for operator-triggered re-sync (e.g., after adding new items in Clover).

**Worker extension:** The Clover sync must also detect and populate `modifiers` from each menu item's modifier groups. Each Clover modifier with `pos_modifier_id` creates a `modifiers` row with `status = 'draft'`. The partial unique index on `(tenant_id, menu_item_id, pos_modifier_id)` prevents duplicates on re-sync.

**Behavior on conflict:** When a re-sync finds an existing `menu_items` row by Clover ID, update name/category/active status. Existing recipe associations are preserved (`recipe_version_id` reference is not cleared by re-sync).

**Behavior on removal:** When a re-sync finds that a previously-synced Clover item no longer exists, mark `menu_items.is_active = false`. Recipe data is preserved for historical reference. New sales referencing a deactivated menu item should not happen, but if they do, depletion proceeds normally per the existing recipe.

### 5. Backend endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/onboarding/recipes` | GET | List all menu items with recipe state, volume, ingredient count, modifier summary |
| `/onboarding/recipes/suggest` | POST | Trigger LLM inference for synced menu items |
| `/onboarding/recipes/{menu_item_id}` | GET | Fetch single recipe state |
| `/onboarding/recipes/{menu_item_id}` | PATCH | Auto-save edits; **409 if currently confirmed** |
| `/onboarding/recipes/{menu_item_id}/confirm` | POST | Move draft → confirmed; **400 if zero ingredients** |
| `/onboarding/recipes/{menu_item_id}/unconfirm` | POST | Confirmed → draft (atomic three-step) |
| `/onboarding/recipes/{menu_item_id}/skip` | POST | Move to skipped |
| `/onboarding/recipes/{menu_item_id}/modifiers/{modifier_id}` | PATCH | Edit modifier draft |
| `/onboarding/recipes/{menu_item_id}/modifiers/{modifier_id}/confirm` | POST | Confirm modifier (atomic) |
| `/onboarding/progress` | GET | Counter state for progress bar |
| `/pos/clover/sync-menu` | POST | Trigger menu re-sync from Clover |

All tenant-scoped via existing RLS. State transitions check current state and return 409 on invalid transitions.

### 6. Schema additions

#### New table: `recipes`

The `recipes` table does not exist in current schema. Create from scratch:

```sql
CREATE TABLE recipes (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    menu_item_id UUID NOT NULL REFERENCES menu_items(id),
    status TEXT NOT NULL DEFAULT 'draft'
      CHECK (status IN ('draft', 'confirmed', 'skipped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, menu_item_id)
);
```

UNIQUE constraint enforces "one confirmed recipe per menu item per tenant."

RLS policies follow the existing tenant-scoped pattern.

#### Modified existing tables

**`recipe_versions`** (stub from migration 0003 — currently has `id`, `tenant_id`, `name`, `created_at`):

Evolve per migration discipline (nullable first, backfill/verify, then NOT NULL):

Migration N (additive):
```sql
ALTER TABLE recipe_versions ADD COLUMN recipe_id UUID REFERENCES recipes(id);
ALTER TABLE recipe_versions ADD COLUMN version_number INTEGER;
ALTER TABLE recipe_versions ADD COLUMN yield_quantity NUMERIC(14,4) DEFAULT 1
  CHECK (yield_quantity > 0);
```

Migration N+1 (data-validating after backfill verification):
```sql
ALTER TABLE recipe_versions ALTER COLUMN recipe_id SET NOT NULL;
ALTER TABLE recipe_versions ALTER COLUMN version_number SET NOT NULL;
ALTER TABLE recipe_versions ALTER COLUMN yield_quantity SET NOT NULL;
ALTER TABLE recipe_versions ADD CONSTRAINT recipe_versions_unique_version
  UNIQUE (recipe_id, version_number);
```

**Existing `name` column:** Made nullable. Not dropped. Implementation should not rely on it.

```sql
ALTER TABLE recipe_versions ALTER COLUMN name DROP NOT NULL;
```

If implementation proves `name` is unreferenced anywhere in production code, a future cleanup migration can drop it. Sprint 5 does not.

**`recipe_ingredients`** (stub from migration 0003 — has `id`, `tenant_id`, `recipe_version_id`, `inventory_item_id`, `quantity`, `created_at`):

Add `unit` column following discipline:

Migration N:
```sql
ALTER TABLE recipe_ingredients ADD COLUMN unit TEXT;
```

Migration N+1 (after verification):
```sql
ALTER TABLE recipe_ingredients ALTER COLUMN unit SET NOT NULL;
ALTER TABLE recipe_ingredients ADD CONSTRAINT recipe_ingredients_unit_canonical
  CHECK (unit IN ('g','kg','oz_weight','lb','ml','L','fl_oz','cup','tsp','tbsp','ea','dozen'));
```

**`sale_line_items`** — depletion status tracking AND refund tracking:

Migration N:
```sql
ALTER TABLE sale_line_items ADD COLUMN depletion_status TEXT DEFAULT 'pending'
  CHECK (depletion_status IN ('pending', 'depleted', 'unmapped', 'skipped', 'failed'));

ALTER TABLE sale_line_items ADD COLUMN depletion_reason TEXT
  CHECK (
    depletion_reason IS NULL OR depletion_reason IN (
      'recipe_draft', 'recipe_skipped', 'no_recipe',
      'invalid_recipe', 'missing_conversion', 'computation_error',
      'sale_ineligible', 'line_refunded'
    )
  );

ALTER TABLE sale_line_items ADD COLUMN is_refunded BOOLEAN DEFAULT FALSE;
```

Migration N+1:
```sql
ALTER TABLE sale_line_items ALTER COLUMN depletion_status SET NOT NULL;
ALTER TABLE sale_line_items ALTER COLUMN is_refunded SET NOT NULL;
ALTER TABLE sale_line_items ADD CONSTRAINT depletion_status_reason_consistency CHECK (
  (depletion_status IN ('pending', 'depleted') AND depletion_reason IS NULL)
  OR
  (depletion_status IN ('unmapped', 'skipped', 'failed') AND depletion_reason IS NOT NULL)
);
```

**Note on `is_refunded`:** This column captures per-line refund state. Some Clover refund events refund specific line items, not the whole order. The worker must extract per-line refund status from Clover events when present. If Clover only reports order-level refund state (no line-level data), `is_refunded` defaults to FALSE on initial insert and gets updated by refund event processing.

**If `is_refunded` already exists on sale_line_items in the actual schema:** skip the ADD COLUMN. Claude Code verifies at implementation time. *(Verified 2026-05-31: `sale_line_items` already has `is_refunded` from migration 0006 — the ADD COLUMN for it is skipped; the NOT NULL tightening and refund-event wiring still apply.)*

#### Coverage view

```sql
CREATE VIEW vw_depletion_coverage AS
SELECT
  tenant_id,
  COUNT(*) FILTER (WHERE depletion_status = 'depleted') AS depleted_count,
  COUNT(*) AS total_count,
  ROUND(
    100.0 * COUNT(*) FILTER (WHERE depletion_status = 'depleted') / NULLIF(COUNT(*), 0),
    2
  ) AS depleted_count_pct,
  SUM(net_revenue_cents) FILTER (WHERE depletion_status = 'depleted') AS depleted_revenue_cents,
  SUM(net_revenue_cents) AS total_revenue_cents,
  ROUND(
    100.0 * SUM(net_revenue_cents) FILTER (WHERE depletion_status = 'depleted') / NULLIF(SUM(net_revenue_cents), 0),
    2
  ) AS depleted_revenue_pct
FROM sale_line_items
WHERE created_at > NOW() - INTERVAL '30 days'
GROUP BY tenant_id;
```

Uses actual schema column `net_revenue_cents`. Application layer converts to display currency by dividing by 100.

#### Canonical unit allowlist

Defined in `app/modules/inventory/depletion/units.py`. Referenced by API validators, UI dropdowns, DB CHECK constraints on `recipe_ingredients.unit`, `modifier_ingredients.unit`, `unit_conversions.from_unit`, `unit_conversions.to_unit`, and `unit_conversions` seed data.

| Dimension | Canonical units |
|---|---|
| Weight | `g`, `kg`, `oz_weight`, `lb` |
| Volume | `ml`, `L`, `fl_oz`, `cup`, `tsp`, `tbsp` |
| Count | `ea`, `dozen` |

`oz` rejected; use `oz_weight` or `fl_oz`.

#### Other new tables

`recipe_drafts`, `recipe_llm_suggestions`, `modifiers`, `modifier_versions`, `modifier_ingredients`, `modifier_drafts`, `modifier_llm_suggestions`, `sale_line_item_modifiers`, `unit_conversions` — as specified in v4 with DB-level unit CHECK constraints on all unit-bearing columns. Full DDL in implementation appendix.

`sale_line_item_modifiers` is created from scratch (table does not currently exist):

```sql
CREATE TABLE sale_line_item_modifiers (
    id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    sale_line_item_id UUID NOT NULL REFERENCES sale_line_items(id),
    modifier_id UUID NOT NULL REFERENCES modifiers(id),
    modifier_version_id UUID NOT NULL REFERENCES modifier_versions(id),
    quantity NUMERIC(14,4) NOT NULL DEFAULT 1 CHECK (quantity > 0),
    pos_modifier_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX sale_line_item_modifiers_sli_idx
  ON sale_line_item_modifiers (sale_line_item_id);
```

#### Migration ordering

All new columns nullable initially per migration risk standard. NOT NULL applied in follow-up migrations after backfill. Preflight blocks for data-validating operations. Isolation rule: one data-validating migration per deploy window.

### 7. Recipe confirmation atomicity

**Precondition:** Draft must have ≥1 ingredient. Endpoint returns 400 Bad Request if zero, before transaction begins.

Confirmation transaction (atomic):
1. For each ingredient in draft:
   a. Case-insensitive name match in `inventory_items` within tenant
   b. If not found, create with `inventory_mode = 'recipe_deducted'` (Mode A default)
2. Create new `recipe_versions` row (immutable; new version_number)
3. Insert `recipe_ingredients` linked to new recipe_version_id
4. Update `menu_items.recipe_version_id` to point at new version
5. Update `recipes.status` to `'confirmed'`
6. Delete `recipe_drafts` row

All succeed or rollback. Modifier confirmation follows identical pattern with `modifier_versions`/`modifier_ingredients`/`modifier_drafts` and same ≥1-ingredient precondition.

**Un-confirm transaction** (per §1): three steps atomic. Read confirmed version → insert recipe_draft → update recipes.status.

### 8. Inventory items auto-create

When recipe is confirmed with an ingredient not in `inventory_items`:
- Case-insensitive name match within tenant
- If match exists, link to existing row
- If no match, create new row with Mode A default

**Mode A as default:** Conservative — produces exact depletion. Mode B would silently produce sale_signal-only data not affecting on_hand.

**Mode assignment is founder-led at pilot start.** No operator UI for changing mode in Sprint 5.

### 9. Depletion code reorganization

**Two-commit sequence to preserve test continuity:**

**Commit A — Pure refactor:** Move existing code to `app/modules/inventory/depletion/` with identical behavior. Tests pass before and after. No functional changes. Files moved:
- `_emit_inventory_effects` (from worker.py) → `depletion/handler.py`
- `record_sale_inventory_effect`, `record_sale_reversal` (from services.py) → `depletion/writer.py`

Stub files created for later use: `depletion/walker.py`, `depletion/resolver.py`, `depletion/conversions.py`, `depletion/units.py`.

This commit leaves `writer.py` in a transitional state — behaviorally identical to old services.py but destined for replacement.

**Commit B — Behavioral rewrite:** Replace handler.py and writer.py with new logic per §11. New walker, resolver, conversion, and unit modules implement Sprint 5 semantics. New tests cover new behavior. Old tests that depend on the old formula are updated or removed.

The reorganization → rewrite sequencing prevents the "tests broken across multiple commits" pattern.

### 10. CI guard for no LLM in depletion

Pre-commit hook or CI step failing if any file under `app/modules/inventory/depletion/` directly or transitively imports `anthropic`, `openai`, or any module that imports them.

Implementation: `tools/ci/check_no_llm_in_depletion.py`. Two layers: direct grep and transitive import graph analysis.

### 11. Recipe walk and depletion engine

Core logic. Lives in `app/modules/inventory/depletion/`. No LLM imports.

#### Sale eligibility (corrected from v4)

Per sale line item, depletion is eligible when ALL of:
1. `orders.payment_state = 'PAID'` OR `orders.payment_state = 'PARTIALLY_REFUNDED'`
2. `orders.state = 'locked'`
3. `sale_line_items.is_voided = false`
4. `sale_line_items.is_refunded = false`

**Critical correction:** `CREDITED` is NOT eligible. Per Clover documentation, `CREDITED` indicates the order has been refunded. Forward depletion on `CREDITED` would double-count physical loss (once for original sale, once for refund's reversal).

**Partial refund handling:** Orders in `PARTIALLY_REFUNDED` state can still have lines depleted forward — but only the lines that are themselves not refunded. The line-level `is_refunded` check is the discriminator.

Non-eligible lines transition to `depletion_status = 'failed'`:
- Refunded line: `reason = 'line_refunded'`
- Voided line: `reason = 'sale_ineligible'`
- Open/pending payment: `reason = 'sale_ineligible'`
- Fully refunded order (`REFUNDED` or `CREDITED`): all lines `reason = 'sale_ineligible'`

#### Refund event processing

When Clover reports a line refund (line transitions from `is_refunded = false` to `is_refunded = true`):

**If the line was previously eligible and depleted:**
- Existing Sprint 4 `record_sale_reversal()` triggers, generating `sale_depletion_reversal` (Mode A) or `sale_signal_reversal` (Mode B) movements
- Original ledger rows remain immutable; reversal rows cancel them arithmetically per §9 of accounting semantics doc
- Update `sale_line_items.is_refunded = true`

**If the line was not yet processed:**
- Mark `is_refunded = true` before depletion runs
- When depletion runs, the eligibility check fails and line transitions to `depletion_status = 'failed'` / `'line_refunded'`

#### Failure granularity

Line-level, not sale-level. One failed depletion does not block other lines in same order. Each line's depletion attempt is independent.

#### Trigger flow

For each sale line item:
1. INSERT with `depletion_status = 'pending'`
2. Check eligibility (per above). If ineligible: transition `pending → failed` with appropriate reason, no ledger writes, commit, continue.
3. Look up menu item
4. Resolve recipe version (via `sale_line_items.recipe_version_id` snapshot)
5. If no confirmed recipe: transition status appropriately, no ledger rows, commit, continue
6. Walk recipe ingredients
7. Walk confirmed additive modifiers (via `sale_line_item_modifiers` join)
8. For each ingredient:
   a. Compute theoretical quantity using formulas below
   b. If unit conversion missing: transition `pending → failed`, `reason = 'missing_conversion'`, roll back this line's ledger writes
   c. Determine movement type from `inventory_items.inventory_mode`:
      - Mode A → `sale_depletion` with negative delta
      - Mode B → `sale_signal` with positive delta
   d. Write ledger row with `yield_factor_applied` snapshot, version reference, idempotency key
9. Transition `pending → depleted` in same transaction as ledger writes

Worker crash leaves row at `pending`. Reprocessing is idempotent.

#### Idempotency key formats

Base recipe:
```
sale_line:{sale_line_item_id}:base:{recipe_version_id}:{inventory_item_id}
```

Modifier:
```
sale_line:{sale_line_item_id}:modifier:{sale_line_item_modifier_id}:{modifier_version_id}:{inventory_item_id}
```

**`inventory_accounting_semantics.md` §6 must be updated to reflect these new formats.** Old format (`sale_line:{id}:{ii}`) is superseded. No production data exists with old format (Sprint 4 uncommitted), so no data migration needed. *(Verified 2026-05-31 against the prod cluster `reorderos-dev-pg`: `inventory_movements` has 0 rows. Per phase-map edit 3, the writer still performs a cheap legacy-key existence check (`sale_line:{sli}:{ii}`) before writing the new-format movement — defense-in-depth for dev/future environments where legacy rows could exist. No data migration, just a read-check at write time.)*

#### Formulas

Base recipe (Mode A):
```
recipe_delta = -1
             * line_quantity
             * (recipe_ingredient.quantity / recipe_versions.yield_quantity)
             * unit_conversion_factor
```

Modifier (Mode A):
```
modifier_delta = -1
               * line_quantity
               * sale_line_item_modifiers.quantity
               * (modifier_ingredient.quantity / modifier_versions.yield_quantity)
               * unit_conversion_factor
```

Mode B: identical, positive sign.

`yield_factor_applied` snapshotted from `inventory_yield_factors` or default 1.0.

### 12. Unmapped/skipped/failed depletion policy

Normal operation when configuration is incomplete or sale is ineligible/refunded. Coverage metric (`vw_depletion_coverage`) is the operator-facing signal.

### 13. Post-onboarding recipe editing

After onboarding, Settings → Recipes provides access to the same UI with same draft/confirmed/un-confirm semantics.

---

## Hard exit gate

**Configuration path:**

1. Operator can complete the Recipes step with at least one confirmed recipe
2. Menu sync triggers at OAuth callback and via `/pos/clover/sync-menu`
3. `GET /onboarding/recipes` returns the full menu list with state info
4. Draft auto-save works
5. Confirm/un-confirm/skip transitions work
6. Un-confirm preserves prior `recipe_versions` as immutable; operation is atomic
7. LLM inference produces structured suggestions (base + modifiers in one call)
8. Inventory items auto-create on recipe confirmation with case-insensitive dedup
9. Recipe confirmation atomic across all 6 transaction steps
10. Confirmation rejected with 400 if zero ingredients
11. PATCH on confirmed recipe rejected with 409 Conflict
12. Non-canonical unit values rejected at API with 400
13. POS modifier sync populates `modifiers` and `sale_line_item_modifiers` correctly

**Depletion path:**

14. Eligible Clover sale (PAID + locked + not voided + not refunded) with confirmed recipe produces correct ledger movements
15. Same sale with additive modifier produces additional correct movements via distinct modifier idempotency key
16. Modifier quantity multiplier correctly applied (test: "Extra shot x2" produces 2x depletion)
17. Mode A → `sale_depletion` negative delta
18. Mode B → `sale_signal` positive delta
19. **CREDITED order does NOT trigger forward depletion** (regression test for v4 bug)
20. PARTIALLY_REFUNDED order: non-refunded lines deplete forward, refunded lines transition to `failed` / `line_refunded`
21. Line refund event triggers `record_sale_reversal()` for previously-depleted line
22. Voided line transitions to `failed` / `sale_ineligible`
23. Line-level failure granularity: one failed line does not block other lines
24. Duplicate sale events produce no duplicate ledger rows
25. **Recipe edits after a sale line has been processed do not alter that sale's ledger rows** (precise guarantee; sale-time correctness for webhook-delayed sales is documented limitation)
26. Modifier edits after a sale line has been processed do not alter that sale's ledger rows
27. Unmapped/skipped/failed sales correctly populate status/reason with no ledger rows
28. `depletion_status_reason_consistency` CHECK passes for all rows
29. `pending` rows older than 5 minutes are queryable for monitoring
30. `vw_depletion_coverage` returns both `depleted_count_pct` and `depleted_revenue_pct`

**Architecture:**

31. All depletion code under `app/modules/inventory/depletion/`
32. Reorganization (Commit A) and behavioral rewrite (Commit B) are separate commits with intermediate test pass
33. CI check fails on any LLM import (direct or transitive)
34. Migration risk standard followed for all new migrations
35. Supersession note added to `docs/archive/v1-backend-build-plan.md`
36. Canonical unit allowlist enforced at API, UI, AND DB layers (recipe_ingredients, modifier_ingredients, unit_conversions)
37. Modifier uniqueness from POS sync prevented by partial unique index
38. `inventory_accounting_semantics.md` §6 updated with new idempotency key formats

**Test coverage:**

39. Fixture tests cover: mapped sale, unmapped sale, duplicate sale, modifier sale with multiplier > 1, both modes, CREDITED rejection, PARTIALLY_REFUNDED partial depletion, line refund reversal, voided line, recipe edit non-effect, modifier edit non-effect, unit conversion correctness, idempotency under replay, worker crash mid-depletion, confirmation with zero ingredients (rejected)
40. End-to-end test: synthetic Clover sale through inbox → worker → depletion → ledger
41. Un-confirm round-trip test
42. Menu sync test: Clover catalog pull populates menu_items and modifiers correctly

---

## Fail gates

1. LLM is called anywhere in the depletion path
2. A sale produces duplicate ledger movements under any retry pattern
3. Recipe or modifier version changes alter previously-written ledger rows
4. Mode A and Mode B confuse movement-type semantics
5. UI accepts invalid units, negative quantities, or empty ingredient names
6. Any new migration violates the migration risk standard
7. Un-confirm mutates a `recipe_versions` or `modifier_versions` row
8. PATCH succeeds on a confirmed recipe
9. `sale_line_items` row has status `'depleted'` but no corresponding ledger movements
10. Any unit value outside the canonical allowlist appears in unit-bearing columns
11. Recipe or modifier confirmed with zero ingredients
12. Duplicate POS modifier rows can exist for same `(tenant_id, menu_item_id, pos_modifier_id)`
13. Sale-level failure (one bad line blocks all other lines)
14. `CREDITED` order triggers forward depletion (the v4 bug)
15. Line refund does not trigger reversal for previously-depleted line
16. Reorganization commit (Commit A) changes behavior (must be pure refactor)

---

## Known fidelity limitations (documented, not blocking)

1. **Webhook-delayed sales processed after recipe edits use post-edit recipe version, not sale-time version.** Fix requires `menu_item_recipe_assignments` history table, deferred.

2. **Modifier version resolution has same webhook-delay limitation.**

3. **LLM-suggested recipes stored only at suggestion time.** No regeneration if a future model produces better suggestions.

4. **Refund-as-waste vs refund-as-return is not operator-distinguishable.** Sprint 5 treats line refund as reversal (returning inventory to stock). In reality, many refunds don't return physical inventory (a refunded latte was still consumed). Operator-facing "this was actually waste" flag deferred to future sprint. Until then, operators concerned about this can manually record waste events.

5. **Pre-refund webhook-arrival edge case:** If a sale webhook arrives after the refund webhook (out-of-order), the sale may be processed as ineligible when it should have been depleted-then-reversed. Coverage metric will surface this pattern. Out-of-order webhook handling is deferred.

6. **Order-level refund granularity:** If Clover sends a refund event without per-line detail, the worker may not be able to determine which specific lines are refunded. In that case, the worker falls back to order-level state (`REFUNDED` or `CREDITED` → all lines failed). Per-line refund extraction is best-effort.

---

## Documentation discipline

**Avoid future "sale-time recipe selection" wording.** Use precise: "recipe version active at sale processing time."

---

## Explicitly out of scope

- "Scan your menu" → Sprint 6
- Inventory items management UI → Sprint 6+
- Mode B yield_factor tuning UI → Sprint 9
- Subtractive and substitution modifiers
- Pattern-matching across restaurants → Sprint 7+
- Recipe yield UI
- `menu_item_recipe_assignments` history table
- `sale_ineligible` automatic reprocessing
- Refund-as-waste operator flag
- Out-of-order webhook reconciliation

---

## Operational concerns

1. **No operator-facing communication when depletion activates.** Brief note in pilot onboarding.

2. **Mode assignment per ingredient is founder-led at pilot start.**

3. **LLM inference cost logged but not budgeted.** Probably <$5 per restaurant.

4. **Recipe yield_quantity defaults to 1.** Batch yields via DB edits.

5. **Pending depletion_status monitoring.** Ops alert on `pending` rows >5 min old exceeding threshold.

6. **Refund pattern monitoring.** New diagnostic query: `SELECT depletion_reason, COUNT(*) FROM sale_line_items WHERE depletion_status = 'failed' GROUP BY depletion_reason`. Spike in `line_refunded` is normal (real refunds happen). Spike in `sale_ineligible` may indicate Clover state mapping issues.

7. **Sprint plan amendment.** Original Sprint 5 in `docs/archive/v1-backend-build-plan.md` superseded.

8. **Documentation discipline.** Precise processing-time wording per discipline section.

9. **Reorganization commit discipline.** Commit A is pure refactor. Commit B is behavioral. Reviewer should verify Commit A behavioral identity by running tests before and after.

---

## What ships at the end of Sprint 5

A new beta restaurant can:

1. Connect Clover POS
2. Complete onboarding including Recipes step (with bundled modifier inference)
3. Start operating normally with confidence that:
   - Eligible sales deplete inventory correctly
   - Refunded lines correctly reverse (not double-count)
   - CREDITED orders do not produce phantom depletion
   - Voided lines never deplete
4. See inventory ledger accumulate correct movements
5. See both count and revenue coverage percentages
6. Edit recipes after onboarding without breaking historical depletion
7. Trust deterministic depletion math for confirmed items, acknowledged-incomplete for unconfirmed

Downstream sprints unblocked:
- Sprint 6 (Receipts): `inventory_items` populated, unit conversions available
- Sprint 7 (Purchase Orders): supplier-linkable inventory items
- Sprint 8 (Forecasting): depletion data available
- Sprint 9 (Dashboards): movement data, coverage metrics

---

## Spec status

**Locked.** Implementation proceeds from v5.

**Resolved at lock time:**
- ✅ Modifier inference shape: bundled per-menu-item LLM call
- ✅ Confirmation precondition: ≥1 ingredient
- ✅ Sale eligibility: PAID + locked + not voided + not refunded (CREDITED rejected)
- ✅ Refund handling: line-level via existing Sprint 4 reversal mechanism
- ✅ Partial refund: per-line eligibility allows non-refunded lines to deplete
- ✅ POS modifier uniqueness: partial unique index
- ✅ Unit allowlist DB enforcement: recipe_ingredients, modifier_ingredients, unit_conversions
- ✅ Failure granularity: line-level explicit
- ✅ Menu sync: separate concern with explicit endpoint
- ✅ List endpoint: `GET /onboarding/recipes`
- ✅ Reorganization sequencing: two commits with intermediate test pass
- ✅ All six hard schema conflicts resolved
- ✅ Idempotency key documentation update in accounting semantics §6

**Open at lock time (operational, not architectural):**
- Recipe LLM suggestion freshness policy (operator returns after months)
- Refund-as-waste vs refund-as-return operator distinction (future sprint)

---

# Appendix F — Frontend Integration (Expo / React Native)

> **Status:** Added 2026-05-31 per founder request ("make sure the front end integration we talked
> about is also added in v5"). This operationalizes the locked frontend sections — §1 (Recipes
> screen), §3 (Modifier config UI), §13 (post-onboarding editing) — into concrete Expo Router files,
> components, an API client, i18n, and frontend acceptance gates. It is implementation detail layered
> on the locked body, not a scope change. Where this appendix and §1/§3/§5/§13 touch, the locked body
> wins. Existing frontend is Expo SDK 51 + Expo Router v3 under `frontend/`.

## F.1 Screens (Expo Router)

| Path | Status | Implements | Notes |
|---|---|---|---|
| `frontend/app/onboarding/recipes.tsx` | **NEW** | §1 | The Recipes onboarding step — accordion of **all** synced menu items, per-item confirm/skip, progress counter, field-blur auto-save. Replaces the legacy `cleanup.tsx` "categorize" stub in the flow (v5 has no categorization step). |
| `frontend/app/onboarding/connecting.tsx` | exists | §4 | After OAuth success, shows menu-sync progress (initial sync at callback per §4); routes to `recipes` when `menu_items` are populated. Empty-state → "retry sync" hitting `POST /pos/clover/sync-menu`. |
| `frontend/app/(app)/recipes/index.tsx` | **NEW** | §13 | Post-onboarding Recipes list (Settings → Recipes). Same draft/confirmed/skipped semantics. |
| `frontend/app/(app)/recipes/[menuItemId].tsx` | **NEW** | §13, §3 | Recipe detail + modifier sub-section; edit / confirm / un-confirm. |
| `frontend/app/(app)/more.tsx` | exists | §13 | Add a live "Recipes" row → pushes `/(app)/recipes`, value = confirmed/total summary. |

**Onboarding flow placement:** `found-summary → connecting (menu sync) → recipes (§1) → suppliers → …`. The legacy `cleanup.tsx` / `manual-menu.tsx` / `par-levels.tsx` stubs are not part of the v5 Recipes path; `cleanup.tsx` is dropped from the flow.

## F.2 Components (`frontend/src/components/`)

| Component | Purpose |
|---|---|
| `MenuItemAccordion` | Collapsible row: name, sales-volume context, ingredient count, status badge, expand/collapse (§1). |
| `IngredientRow` | Ingredient name + numeric quantity input + **unit picker restricted to the canonical allowlist** (§6: `g,kg,oz_weight,lb,ml,L,fl_oz,cup,tsp,tbsp,ea,dozen`). Client-side rejects invalid units/negative qty/empty name (mirrors fail gate #5). |
| `ModifierAccordion` / `ModifierRow` | Modifier sub-section (§3); additive modifiers editable; subtractive/substitution shown disabled as "Not yet supported". |
| `RecipeStatusBadge` | `draft` (amber) / `confirmed` (green) / `skipped` (grey). |
| `ConfidenceBadge` | `confident` / `likely` / `uncertain` per §2 LLM output. |
| `ProgressCounter` | `confirmed / (total − skipped)` (§1), backed by `GET /onboarding/progress`. |
| `CoverageBar` | Thin % bar — used only if coverage display lands (see F.5). |
| `RecipeSkeleton` | Loading placeholder. |

## F.3 API client (`frontend/src/api/recipes.ts`)

> **Errata (2026-06-11, Phase 16):** the `suggest()` row below lists `POST
> /onboarding/recipes/suggest`, but the **implemented** route is per-menu-item
> `POST /onboarding/recipes/{menu_item_id}/suggest` — a deliberate Phase 5 design choice (the
> bundled per-item base+modifier LLM call; see `app/modules/recipes/router.py:159`). The client
> (`frontend/src/api/recipes.ts`) follows the implemented route. Build against the per-menu-item
> path; the listing below is stale on this one row.

Thin typed wrappers over the §5 endpoints (no new endpoints invented here):
`getRecipes()` → `GET /onboarding/recipes`; `suggest()` → `POST /onboarding/recipes/suggest`;
`getRecipe(id)`, `patchRecipe(id)` (handle **409** when confirmed), `confirm(id)` (handle **400** on zero ingredients), `unconfirm(id)`, `skip(id)`; modifier `patch`/`confirm`; `getProgress()`; `syncMenu()` → `POST /pos/clover/sync-menu`. All carry the auth/tenant headers already used by `src/auth/api.ts`.

## F.4 State handling & i18n (every new screen)

Per `SPRINTS.md:548–553`: loading skeleton, error + retry, empty + CTA, stale "last updated", pull-to-refresh. All user-facing strings added to `frontend/src/i18n/strings.ts` in **EN and FR** (`v1-scope.md:82` — bilingual on every customer-facing surface); validation errors rendered from stable codes per `v1-scope.md:85`.

## F.5 Coverage display — RESOLVED: deferred to Sprint 9

**Decision (2026-06-01, founder):** Option (a). The `vw_depletion_coverage` **view ships in Sprint 5**, but the operator-facing **coverage card** that renders it is deferred to Sprint 9 dashboards (`SPRINTS.md:466,489`). No coverage read endpoint is added in Sprint 5 — the §5 API surface stays as locked.

Rationale: during onboarding there are no sales yet, so a coverage card would read 0% regardless; it is only meaningful post-sales. The Sprint 5 onboarding status surface is the **progress counter** (`confirmed / (total − skipped)`, §1, `GET /onboarding/progress`), not coverage. Coverage % is a post-sales operational metric and belongs with Sprint 9.

## F.6 Frontend acceptance gates (extends §39 to the UI)

- **FE-1** `GET /onboarding/recipes` renders the accordion list with correct status badges.
- **FE-2** Confirm is disabled at zero ingredients; a 400 surfaces as a validation message.
- **FE-3** PATCH on a confirmed recipe → 409 surfaces as an "un-confirm first" prompt.
- **FE-4** Unit picker offers only canonical units; a server 400 on a bad unit is handled.
- **FE-5** Field-blur auto-save persists draft; app close preserves draft state (§1).
- **FE-6** Un-confirm round-trips in the UI (§1 mechanics).
- **FE-7** "Extra shot ×2" modifier shows the multiplier; subtractive modifiers are disabled.
- **FE-8** EN/FR toggle covers every recipe/modifier string with no missing keys.
- **FE-9** Unauthenticated requests → 401 with no ghost data rendered.
