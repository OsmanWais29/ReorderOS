# ReorderOS — Sprint Failure Catalog

**Purpose:** Canonical reference for how each sprint can fail in production.
Used to build the admin monitoring dashboard and ops runbook.
Each section covers: failure modes, diagnostic queries, alert thresholds,
and where in the codebase/logs to find the root cause.

**Last updated:** 2026-05-27
**Covers:** Sprints 1–5

---

## How to use this document

Each failure mode has:
- **Signal:** what you observe (metric, error, query result)
- **Root cause candidates:** ordered most → least likely
- **Diagnostic query or log search:** exact SQL or log field to check
- **Resolution path:** what to do

Run the monitoring queries against the production database.
All queries are tenant-scoped — replace `:tid` with the affected tenant UUID.
Global queries omit the tenant filter.

---

## Sprint 1 — Platform Skeleton

### F1.1 API returns 502 / app platform unhealthy

**Signal:** `/health/live` returns non-200, or DigitalOcean App Platform health check fails.

**Root cause candidates:**
1. Python process crashed (OOM, unhandled exception at startup)
2. Environment variable missing (`DATABASE_URL`, `SECRET_KEY`, etc.)
3. Alembic migration failed at deploy, leaving DB in partial state
4. DB connection pool exhausted

**Diagnostic:**
```bash
# DigitalOcean App Platform → Runtime Logs tab
# Look for: "ModuleNotFoundError", "KeyError", "sqlalchemy.exc"
# Better Stack: filter log_level=ERROR, service=reorderos-api
```

**Resolution path:** Check DO runtime logs → fix env var or migration → redeploy.

---

### F1.2 Migration fails at deploy

**Signal:** App fails to start, logs show `alembic.exc.CommandError` or `ProgrammingError`.

**Root cause candidates:**
1. Migration touches a column/table that already exists (duplicate migration)
2. NOT NULL constraint added without backfill on a table with existing rows
3. Role `app_user` or `service_worker` does not exist in the target DB

**Diagnostic:**
```sql
-- Check which migrations have been applied
SELECT version_num, executed_at FROM alembic_version;
```

**Resolution path:** Identify the failing migration from logs → fix the DDL → re-run migration on staging first.

---

## Sprint 2 — Tenant, Auth & RBAC

### F2.1 All API requests return 401 or 403

**Signal:** Every authenticated request fails.

**Root cause candidates:**
1. Clerk JWKS cache expired and Clerk is unreachable
2. `CLERK_JWKS_URL` env var missing or wrong
3. JWT `active_tenant_id` claim missing from Clerk metadata (new user, no tenant yet)

**Diagnostic:**
```bash
# Log field to look for: jwt_validation_error, jwks_fetch_failed
# Better Stack: filter logger=app.core.security
```

**Resolution path:** Check Clerk status page → verify `CLERK_JWKS_URL` → check if JWKS TTL in config is too short.

---

### F2.2 Cross-tenant data leak

**Signal:** User sees data from a different restaurant. P1 alert.

**Root cause candidates:**
1. `app.tenant_id` session variable not set before a query runs
2. RLS disabled on a table (missed in migration)
3. A raw query bypasses the RLS context helper

**Diagnostic:**
```sql
-- Verify RLS is enabled on all tenant tables
SELECT tablename, rowsecurity, forcerls
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'inventory_items','inventory_movements','orders',
    'sale_line_items','recipes','recipe_versions',
    'modifiers','menu_items','receipts'
  );
-- All rows should have rowsecurity=true, forcerls=true
```

**Resolution path:** Immediately rotate or invalidate session for affected user → identify which table had RLS disabled → add missing migration → audit all queries touching that table.

---

### F2.3 RBAC bypass — Staff reaches Owner endpoint

**Signal:** Staff user can call `POST /purchase-orders/{id}/approve`.

**Root cause candidates:**
1. `require_role(min_role)` dependency missing from route
2. Role claim in JWT does not match DB `user_tenants.role`

**Diagnostic:**
```sql
SELECT u.email, ut.role
FROM user_tenants ut
JOIN users u ON u.id = ut.user_id
WHERE ut.tenant_id = :tid;
```

**Resolution path:** Add missing role guard to route → verify Clerk metadata role matches `user_tenants.role`.

---

## Sprint 3 — Inventory Ledger

### F3.1 `on_hand()` returns wrong value

**Signal:** Operator sees incorrect stock level. Could be higher or lower than physical reality.

**Root cause candidates:**
1. Mode B: `last_count_quantity` is NULL (count not yet done — on_hand returns NULL, shown as 0 in UI)
2. Mode B: `reconciliation_cutoff_created_at` is NULL on a count event (pre-0010 row — falls back to `recorded_at` filter, may miss signals)
3. Mode A: a `count_adjust` movement was written with wrong sign
4. Late signal crossed a count boundary — visible as `late_signal_reconciliation` alert

**Diagnostic:**
```sql
-- Check count events for the item
SELECT counted_quantity, counted_at, reconciliation_cutoff_created_at
FROM inventory_count_events
WHERE tenant_id = :tid AND inventory_item_id = :iid
ORDER BY counted_at DESC
LIMIT 5;

-- Check monitoring alerts
SELECT monitor_name, severity, trigger_payload, last_seen_at
FROM monitoring_alerts
WHERE tenant_id = :tid AND resolved_at IS NULL;

-- Sum all movements (Mode A verification)
SELECT movement_type, SUM(delta) as total
FROM inventory_movements
WHERE tenant_id = :tid AND inventory_item_id = :iid
GROUP BY movement_type;
```

**Resolution path:** For Mode B null count → operator does a physical count. For wrong count_adjust → insert compensating `count_adjust` with correct delta. Never edit or delete existing rows.

---

### F3.2 Duplicate `opening_balance` movement

**Signal:** PostgreSQL unique index violation `one_opening_balance_per_item` (error 23505) when trying to add opening balance.

**Root cause candidates:**
1. Opening balance called twice for same item (race condition or retry)
2. Item was deleted and recreated with same UUID (should be impossible, UUIDs are random)

**Diagnostic:**
```sql
SELECT id, delta, recorded_at
FROM inventory_movements
WHERE tenant_id = :tid AND inventory_item_id = :iid
  AND movement_type = 'opening_balance';
```

**Resolution path:** If duplicate was not committed (transaction rolled back by constraint), retry is safe. If somehow committed, this is a data integrity incident — escalate.

---

### F3.3 `integrity_drift_high` alert fires

**Signal:** `monitoring_alerts` row with `monitor_name = 'integrity_drift_high'`, severity `warn` or `critical`.

**Root cause candidates:**
1. Legitimate variance — physical waste, spillage, unreported transfers
2. Recipe ingredient quantities set wrong (too high/low depletion)
3. Mode A item with a sale that depleted before recipe was confirmed (no depletion for those sales)

**Diagnostic:**
```sql
-- Get alert detail
SELECT trigger_payload FROM monitoring_alerts
WHERE tenant_id = :tid AND monitor_name = 'integrity_drift_high'
  AND resolved_at IS NULL;

-- Check recent movements
SELECT movement_type, delta, recorded_at, source_type
FROM inventory_movements
WHERE tenant_id = :tid AND inventory_item_id = :iid
ORDER BY created_at DESC
LIMIT 20;
```

**Resolution path:** Verify physical count → if legitimate variance, record `count_adjust` → if recipe error, fix recipe and let future sales use corrected recipe (historical movements are unchanged).

---

## Sprint 4 — Clover Integration

### F4.1 Webhook events silently dropped

**Signal:** Sales visible in Clover but not in `orders` table. Coverage metric in Sprint 5 will also surface this as low `depleted_count_pct`.

**Root cause candidates:**
1. Webhook HMAC verification failing (wrong secret) → endpoint returns 401 without writing inbox
2. Inbox worker not running
3. Worker claiming events but failing silently → rows stuck in `processing` state
4. `vendor_object_type` not 'O' — worker only processes order events
5. Order state not `locked` — worker skips non-locked orders (by design)

**Diagnostic:**
```sql
-- Events stuck in processing beyond claim TTL (5 min)
SELECT inbox_id, state, retry_count, last_error, received_at, claim_expires_at
FROM pos_event_inbox
WHERE state = 'processing'
  AND claim_expires_at < NOW();

-- Dead-lettered events
SELECT inbox_id, last_error, retry_count, received_at
FROM pos_event_inbox
WHERE state = 'dead_letter'
ORDER BY received_at DESC
LIMIT 20;

-- Events by state (last 24h)
SELECT state, COUNT(*)
FROM pos_event_inbox
WHERE received_at > NOW() - INTERVAL '24 hours'
GROUP BY state;
```

**Alert threshold:** >0 dead_letter events in 1 hour → P1. >10 stuck `processing` events → P2.

**Resolution path:** Dead-letter → check `last_error` field → fix root cause → re-queue manually or via reconciliation pull. HMAC failure → verify webhook secret matches Clover dashboard.

---

### F4.2 Duplicate ledger movements from webhook replay

**Signal:** `inventory_movements` has two rows with the same effective content for the same sale. Idempotency breach.

**Diagnostic:**
```sql
-- Check for duplicate idempotency keys (should return 0 rows always)
SELECT idempotency_key, COUNT(*)
FROM inventory_movements
WHERE tenant_id = :tid
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY idempotency_key
HAVING COUNT(*) > 1;
```

**Alert threshold:** Any row returned here is a P1 — data integrity breach. Escalate immediately.

**Resolution path:** Identify which movement is the duplicate → write a `count_adjust` compensating entry to cancel it → investigate why idempotency key uniqueness was bypassed → never delete the original rows.

---

### F4.3 Token refresh failure — Clover API calls fail

**Signal:** Worker logs show `TokenExpiredError`, inbox rows stuck in `failed` state.

**Diagnostic:**
```sql
SELECT state, refresh_failure_count, access_token_expires_at, last_token_refresh_at
FROM tenant_pos_connections
WHERE tenant_id = :tid AND vendor = 'clover';
```

**Alert threshold:** `refresh_failure_count > 3` → P2. Worker cannot fetch orders until token is refreshed.

**Resolution path:** Re-initiate Clover OAuth flow for the tenant → token refresh job will pick it up once a valid refresh token exists.

---

## Sprint 5 — Recipe Configuration and Inventory Depletion

### F5.1 Pending depletion rows older than 5 minutes

**Signal:** Sale line items stuck in `depletion_status = 'pending'` state. Indicates the depletion engine crashed mid-run or the worker loop stopped.

**Diagnostic:**
```sql
-- Global: all tenants
SELECT tenant_id, COUNT(*) AS stuck_pending
FROM sale_line_items
WHERE depletion_status = 'pending'
  AND created_at < NOW() - INTERVAL '5 minutes'
GROUP BY tenant_id;

-- Per tenant: what are these lines
SELECT id, name_at_sale, created_at, menu_item_id
FROM sale_line_items
WHERE tenant_id = :tid
  AND depletion_status = 'pending'
  AND created_at < NOW() - INTERVAL '5 minutes'
ORDER BY created_at ASC;
```

**Alert threshold:** Any row with pending > 5 min → P1. The depletion engine should process each line within seconds of the inbox worker completing.

**Root cause candidates:**
1. Worker process crashed between INSERT (pending) and depletion transaction commit
2. Unhandled exception in `handler.py` that swallowed the error without transitioning status
3. DB deadlock or lock timeout in the depletion transaction
4. New recipe ingredient references a `unit` not in the conversion table → `missing_conversion` failure that wasn't handled correctly

**Resolution path:** Check worker logs for exceptions at the time the rows were created → fix the root cause → rows at `pending` are idempotently reprocessable — restarting the worker will retry them.

---

### F5.2 Coverage collapse — `depleted_count_pct` drops below expected threshold

**Signal:** `vw_depletion_coverage` shows `depleted_count_pct < 50` for a restaurant that has confirmed recipes.

**Diagnostic:**
```sql
-- Coverage view
SELECT * FROM vw_depletion_coverage WHERE tenant_id = :tid;

-- Failure reason distribution (most important diagnostic query)
SELECT depletion_reason, COUNT(*) AS count
FROM sale_line_items
WHERE tenant_id = :tid
  AND depletion_status IN ('failed', 'unmapped', 'skipped')
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY depletion_reason
ORDER BY count DESC;

-- How many menu items have confirmed recipes
SELECT s.status, COUNT(*) AS count
FROM recipes s
WHERE tenant_id = :tid
GROUP BY s.status;
```

**Failure reason interpretations:**

| `depletion_reason` | Meaning | Action |
|---|---|---|
| `no_recipe` | Menu item has no recipe at all | Operator needs to confirm recipes in onboarding |
| `recipe_draft` | Recipe exists but not confirmed | Operator needs to confirm |
| `recipe_skipped` | Operator explicitly skipped | Expected — skipped items don't deplete |
| `missing_conversion` | Unit conversion not seeded for this unit pair | Check `unit_conversions` seed data |
| `invalid_recipe` | Recipe version has zero ingredients | Data integrity issue — investigate confirmation atomicity |
| `sale_ineligible` | Order not in PAID/PARTIALLY_REFUNDED + locked state | Expected for refunds/voids — high rate indicates Clover state mapping issue |
| `line_refunded` | This specific line was refunded | Expected for refunded line items |
| `computation_error` | Depletion math threw an exception | Check worker logs — Python traceback |

**Alert threshold:** `depleted_count_pct < 50` and `total_count > 20` → P2 (low coverage after enough data to judge). `depleted_count_pct < 10` → P1.

---

### F5.3 Duplicate ledger movements (depletion-specific)

**Signal:** Same ingredient depleted twice for the same sale. Idempotency breach.

**Diagnostic:**
```sql
-- Check for duplicate depletion keys (Sprint 5 key format)
SELECT idempotency_key, COUNT(*)
FROM inventory_movements
WHERE tenant_id = :tid
  AND movement_type IN ('sale_depletion', 'sale_signal')
  AND idempotency_key LIKE 'sale_line:%:base:%'
  AND created_at > NOW() - INTERVAL '24 hours'
GROUP BY idempotency_key
HAVING COUNT(*) > 1;

-- Also check modifier keys
SELECT idempotency_key, COUNT(*)
FROM inventory_movements
WHERE tenant_id = :tid
  AND idempotency_key LIKE 'sale_line:%:modifier:%'
GROUP BY idempotency_key
HAVING COUNT(*) > 1;
```

**Alert threshold:** Any row returned is P1.

**Resolution path:** Write compensating `count_adjust` for duplicate → investigate which code path bypassed idempotency check → this should be impossible given the UNIQUE constraint on `(tenant_id, idempotency_key)`.

---

### F5.4 CREDITED order incorrectly depleted (v4 regression guard)

**Signal:** `sale_line_items` rows with `depletion_status = 'depleted'` where the parent order has `payment_state = 'CREDITED'`.

**Diagnostic:**
```sql
SELECT sli.id, sli.depletion_status, o.payment_state, o.state
FROM sale_line_items sli
JOIN orders o ON o.id = sli.order_id
WHERE sli.tenant_id = :tid
  AND sli.depletion_status = 'depleted'
  AND o.payment_state = 'CREDITED';
```

**Alert threshold:** Any row returned is a fail gate violation. CREDITED orders must never forward-deplete.

**Resolution path:** Write `sale_depletion_reversal` (Mode A) or `sale_signal_reversal` (Mode B) compensating entries via `record_sale_reversal()` for each affected line → update `depletion_status = 'failed'`, `depletion_reason = 'sale_ineligible'` → investigate eligibility check in `resolver.py`.

---

### F5.5 Line refund did not trigger reversal

**Signal:** A sale line has `is_refunded = true` but still has `depletion_status = 'depleted'` with no corresponding `sale_depletion_reversal` movement.

**Diagnostic:**
```sql
-- Lines that are refunded but still show as depleted (should be 0)
SELECT sli.id, sli.name_at_sale, sli.is_refunded, sli.depletion_status, sli.created_at
FROM sale_line_items sli
WHERE sli.tenant_id = :tid
  AND sli.is_refunded = true
  AND sli.depletion_status = 'depleted';

-- Verify if a reversal movement exists for a specific line
SELECT im.movement_type, im.delta, im.idempotency_key, im.created_at
FROM inventory_movements im
WHERE im.tenant_id = :tid
  AND im.idempotency_key LIKE 'reversal:%'
  AND im.source_id IN (
    SELECT im2.id FROM inventory_movements im2
    WHERE im2.tenant_id = :tid
      AND im2.idempotency_key LIKE 'sale_line:{:sli_id}:base:%'
  );
```

**Alert threshold:** Any row from first query is a data integrity issue (physical inventory inflated vs actual).

**Resolution path:** Manually trigger `record_sale_reversal()` for affected movements → investigate refund detection logic in worker.

---

### F5.6 `sale_line_items` row depleted but no ledger movements

**Signal:** Fail gate #9 — `depletion_status = 'depleted'` with no `inventory_movements` rows for that sale line.

**Diagnostic:**
```sql
-- Lines marked depleted but no corresponding movements (should return 0 rows)
SELECT sli.id, sli.name_at_sale, sli.created_at
FROM sale_line_items sli
WHERE sli.tenant_id = :tid
  AND sli.depletion_status = 'depleted'
  AND NOT EXISTS (
    SELECT 1 FROM inventory_movements im
    WHERE im.tenant_id = :tid
      AND im.idempotency_key LIKE 'sale_line:' || sli.id::text || ':%'
  );
```

**Alert threshold:** Any row returned is P1.

**Resolution path:** This indicates the status was written without the ledger row committing (transaction split). Investigate transaction boundary in `depletion/writer.py` → both must commit together.

---

### F5.7 LLM inference in the depletion path (CI guard failure)

**Signal:** CI check `tools/ci/check_no_llm_in_depletion.py` fails. Or at runtime: depletion latency spikes >10x normal (LLM calls are slow).

**Diagnostic:**
```bash
# CI output names the file with the illegal import
python tools/ci/check_no_llm_in_depletion.py

# Runtime: look for anthropic client init in depletion module logs
# Log field: logger=app.modules.inventory.depletion.*
```

**Alert threshold:** CI failure = block merge. Runtime latency spike in depletion path = P1.

**Resolution path:** Remove LLM import from depletion module → LLM inference belongs only in `app/modules/inventory/inference/` or equivalent.

---

### F5.8 Recipe confirmation not atomic — partial confirms

**Signal:** `recipe_versions` row exists with no corresponding `recipe_ingredients` rows. Or `menu_items.recipe_version_id` points to a version not owned by any recipe.

**Diagnostic:**
```sql
-- Recipe versions with no ingredients (should be 0 in production)
SELECT rv.id, rv.version_number, rv.created_at
FROM recipe_versions rv
WHERE rv.tenant_id = :tid
  AND NOT EXISTS (
    SELECT 1 FROM recipe_ingredients ri WHERE ri.recipe_version_id = rv.id
  );

-- Menu items pointing at orphaned recipe versions
SELECT mi.id, mi.name, mi.recipe_version_id
FROM menu_items mi
WHERE mi.tenant_id = :tid
  AND mi.recipe_version_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM recipe_versions rv WHERE rv.id = mi.recipe_version_id
  );
```

**Alert threshold:** Any row returned is P1 — depletion will fail with `invalid_recipe` for affected menu items.

**Resolution path:** If partial confirm happened → fix the recipe via the unconfirm → re-confirm flow (never direct DB edits) → investigate atomicity in confirmation transaction.

---

### F5.9 Unit conversion missing — `missing_conversion` failures spike

**Signal:** `depletion_reason = 'missing_conversion'` appears repeatedly in failure distribution query (F5.2 diagnostic).

**Diagnostic:**
```sql
-- What unit pairs are failing
SELECT
  ri.unit AS recipe_unit,
  ii.storage_unit_id,
  uom.abbreviation AS storage_unit,
  COUNT(*) AS fail_count
FROM sale_line_items sli
JOIN recipe_versions rv ON rv.id = sli.recipe_version_id
JOIN recipe_ingredients ri ON ri.recipe_version_id = rv.id
JOIN inventory_items ii ON ii.id = ri.inventory_item_id
JOIN units_of_measure uom ON uom.id = ii.storage_unit_id
WHERE sli.tenant_id = :tid
  AND sli.depletion_status = 'failed'
  AND sli.depletion_reason = 'missing_conversion'
GROUP BY ri.unit, ii.storage_unit_id, uom.abbreviation;

-- Check what's in unit_conversions
SELECT from_unit, to_unit, factor FROM unit_conversions
WHERE tenant_id IS NULL  -- global seeds
ORDER BY from_unit, to_unit;
```

**Root cause:** Recipe uses a unit that has no conversion path to the inventory item's storage unit.

**Resolution path:** Add the missing row to `unit_conversions` seed data → re-run depletion for affected `pending` rows (they retry on restart).

---

### F5.10 Menu sync failed — Recipes screen empty

**Signal:** Operator completes Clover OAuth but Recipes screen shows no menu items.

**Diagnostic:**
```sql
-- Are there menu items for this tenant?
SELECT COUNT(*) FROM menu_items WHERE tenant_id = :tid AND active = true;

-- Was there a sync attempt?
SELECT last_reconciliation_at, state
FROM tenant_pos_connections
WHERE tenant_id = :tid AND vendor = 'clover';
```

**Root cause candidates:**
1. Clover API call at OAuth callback failed (token not yet active, API timeout)
2. `POST /pos/clover/sync-menu` was never called after OAuth
3. Clover catalog is empty in sandbox (expected during testing)

**Resolution path:** Operator hits "Refresh" / calls `POST /pos/clover/sync-menu` → check Clover API response in logs for error → verify access token is valid.

---

### F5.11 Un-confirm mutated a `recipe_versions` row

**Signal:** Fail gate #7. A `recipe_versions` row was updated after creation (should never happen).

**Diagnostic:**
```sql
-- recipe_versions rows have no updated_at (append-only)
-- Check for any UPDATE operations via pg_audit or application logs
-- Log field: action=recipe_version_update (should never appear)

-- Verify recipe versions are append-only by checking movement count
SELECT rv.id, rv.version_number, rv.created_at,
       COUNT(sli.id) AS sale_lines_using_this_version
FROM recipe_versions rv
LEFT JOIN sale_line_items sli ON sli.recipe_version_id = rv.id
WHERE rv.tenant_id = :tid
GROUP BY rv.id, rv.version_number, rv.created_at
ORDER BY rv.created_at DESC;
```

**Alert threshold:** Any UPDATE to `recipe_versions` is P1 — historical sale depletions referencing this version are now unverifiable.

**Resolution path:** Restore original version data from PITR backup for that row → investigate un-confirm transaction code to ensure it writes `recipe_drafts` + updates `recipes.status` only, never touches `recipe_versions`.

---

## Cross-sprint diagnostic queries (run regularly)

### Daily ops health check

```sql
-- 1. Pending depletions (should be 0 older than 5 min)
SELECT tenant_id, COUNT(*) AS stuck
FROM sale_line_items
WHERE depletion_status = 'pending'
  AND created_at < NOW() - INTERVAL '5 minutes'
GROUP BY tenant_id;

-- 2. Dead-lettered inbox events (should be 0)
SELECT tenant_id, COUNT(*) AS dead_count
FROM pos_event_inbox
WHERE state = 'dead_letter'
  AND received_at > NOW() - INTERVAL '24 hours'
GROUP BY tenant_id;

-- 3. Coverage by tenant
SELECT tenant_id, depleted_count_pct, depleted_revenue_pct, total_count
FROM vw_depletion_coverage
ORDER BY depleted_count_pct ASC;

-- 4. Unresolved monitoring alerts
SELECT tenant_id, monitor_name, severity, alert_count, last_seen_at
FROM monitoring_alerts
WHERE resolved_at IS NULL
ORDER BY severity DESC, last_seen_at DESC;

-- 5. Duplicate idempotency keys (must be 0 always)
SELECT idempotency_key, COUNT(*)
FROM inventory_movements
WHERE created_at > NOW() - INTERVAL '24 hours'
GROUP BY idempotency_key
HAVING COUNT(*) > 1;
```

### Weekly recipe health check

```sql
-- Restaurants with low recipe coverage (many unconfirmed menu items)
SELECT
  r.tenant_id,
  COUNT(*) FILTER (WHERE r.status = 'confirmed') AS confirmed,
  COUNT(*) FILTER (WHERE r.status = 'draft') AS draft,
  COUNT(*) FILTER (WHERE r.status = 'skipped') AS skipped,
  COUNT(*) AS total
FROM recipes r
GROUP BY r.tenant_id
HAVING COUNT(*) FILTER (WHERE r.status = 'confirmed') < COUNT(*) / 2;

-- CREDITED orders that produced forward depletions (v4 regression — must be 0)
SELECT sli.tenant_id, COUNT(*) AS bad_depletions
FROM sale_line_items sli
JOIN orders o ON o.id = sli.order_id
WHERE sli.depletion_status = 'depleted'
  AND o.payment_state = 'CREDITED'
GROUP BY sli.tenant_id;

-- Refunded lines that are still marked depleted (no reversal) — must be 0
SELECT sli.tenant_id, COUNT(*) AS missing_reversals
FROM sale_line_items sli
WHERE sli.is_refunded = true
  AND sli.depletion_status = 'depleted'
GROUP BY sli.tenant_id;
```

---

## Admin dashboard widget map

These queries map directly to dashboard widgets for the admin monitoring panel:

| Widget | Query | Alert threshold |
|---|---|---|
| Stuck depletions | F5.1 diagnostic | >0 → P1 |
| Dead-letter inbox | F4.1 diagnostic | >0 in 1h → P1 |
| Coverage by tenant | `vw_depletion_coverage` | <50% after 20+ sales → P2; <10% → P1 |
| Failure reason heatmap | F5.2 reason distribution | `missing_conversion` spike → check unit seeds |
| Duplicate movements | F4.2 / F5.3 | Any row → P1 |
| CREDITED depletion guard | F5.4 | Any row → P1 |
| Refund reversal gap | F5.5 | Any row → P2 |
| Partial confirm guard | F5.8 | Any row → P1 |
| Active monitoring alerts | Cross-sprint daily #4 | critical → P1; warn → P2 |
| Token refresh health | F4.3 | `refresh_failure_count > 3` → P2 |
| Recipe coverage by tenant | Weekly #1 | <50% confirmed → notify founder |

---

## Severity definitions

| Level | Response | Examples |
|---|---|---|
| P1 | Page founder immediately | Data integrity breach, duplicate movements, CREDITED depletion, cross-tenant data |
| P2 | Review next business day | Coverage collapse, dead-letter spike, token refresh failure, stuck pending |
| Info | Track, no immediate action | Normal refund rate, low draft recipe count on new tenants |
