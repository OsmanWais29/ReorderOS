# ReorderOS — Sprint Checklists

> **Working stack**: FastAPI (Python async) · DigitalOcean App Platform + Managed PostgreSQL + Spaces · Expo React Native (iOS + Android) · Clerk auth · Clover POS · Anthropic receipt extraction · Postmark email · Better Stack observability
>
> **Rule**: each sprint must close its own exit gates before the next sprint begins.

---

## Sprint 0 — Scope Freeze & Contradiction Cleanup

**Goal**: Lock every v1 decision in writing so no sprint re-litigates scope.

### Documents & Decisions
- [ ] Create `decisions/v1-scope.md` listing all product locks (below)
- [ ] Create `decisions/contradiction-register.md`
- [ ] Create `decisions/release-risk-register.md`
- [ ] Build API surface inventory from the existing frontend mock screens (ScreenHome, ScreenSales, ScreenOrders, ScreenStock, ScreenMore)
- [ ] Compile bilingual string inventory (EN + FR) for every customer-facing surface

### V1 Locks to Confirm in Writing
- [ ] Clover-only POS at launch; unsupported POS routes to waitlist
- [ ] Cross-tenant price comparison disabled (deferred until 50-100 restaurants + legal)
- [ ] SMS deferred — email-only PO sending in v1
- [ ] Billing hidden for pilot; first 10 pilots get lifetime $99 CAD/month
- [ ] RBAC v1: Owner / Manager / Staff only (no ops_lead / line_cook / accountant in v1)
- [ ] Staff = individual login accounts; no shared PIN login mode
- [ ] Anthropic receipt extraction = draft only, no auto-commit
- [ ] iOS + Android launch (Expo); no PWA for v1
- [ ] PostgreSQL on DigitalOcean Managed Database; Supabase explicitly out
- [ ] No realtime subscriptions in v1 — foreground polling only
- [ ] Suppliers table: hybrid master+tenant model (suppliers_master public + tenant_suppliers private)

### Exit Gates
- [ ] All locks above accepted in writing by founder
- [ ] No unresolved legal/compliance question touches live customer data sharing
- [ ] Contradiction register is empty or all items resolved
- [ ] API surface inventory documents every screen → endpoint mapping

### Fail Gate
- Any unresolved legal question touches cross-tenant data

---

## Sprint 1 — Platform Skeleton

**Goal**: Deployable FastAPI modular monolith with database and CI pipeline.

### Project Scaffold
- [ ] Initialize Python project with `pyproject.toml` (uv or poetry)
- [ ] FastAPI app entry point (`app/main.py`)
- [ ] Module directory structure:
  ```
  app/
    main.py
    core/config.py, logging.py, security.py, database.py, rls.py, idempotency.py
    modules/auth/, tenants/, users/, rbac/, pos_clover/, inventory/, recipes/,
             sales/, receipts/, purchase_orders/, forecasting/, dashboard/,
             stock/, settings/, exports/, notifications/, outbox/, observability/
  ```
- [ ] SQLAlchemy 2 async engine setup (`app/core/database.py`)
- [ ] Alembic configuration for async migrations
- [ ] Pydantic v2 base models and request/response schemas
- [ ] OpenAPI schema auto-generation enabled

### Health Endpoints
- [ ] `GET /health/live` → `{"status": "ok"}`
- [ ] `GET /health/ready` → checks DB connection, returns `{"status": "ok"}`

### Local Development
- [ ] `docker-compose.yml` with PostgreSQL 16 service
- [ ] `.env.example` with all required env vars documented
- [ ] Single-command local start (`make dev` or `docker compose up`)
- [ ] First empty Alembic migration applies and rolls back cleanly

### CI Pipeline
- [ ] Linting (ruff)
- [ ] Type checking (mypy or pyright)
- [ ] Unit test runner (pytest-asyncio)
- [ ] Migration check (alembic upgrade head on fresh DB)
- [ ] OpenAPI schema generated and saved as artifact

### Exit Gates
- [ ] `GET /health/live` and `GET /health/ready` work locally
- [ ] Empty migration applies and rolls back in disposable local DB
- [ ] OpenAPI schema generated in CI without errors
- [ ] First DigitalOcean deployment target documented (App Platform spec)

### Fail Gate
- Backend cannot be started locally with a single command

---

## Sprint 2 — Tenant, Auth & RBAC Foundation

**Goal**: Every request is tenant-aware and role-aware before any business logic runs.

### Clerk JWT Integration
- [ ] JWKS URL cache with configurable TTL (`app/core/security.py`)
- [ ] JWT decode and validation middleware
- [ ] `active_tenant_id` claim extraction from JWT metadata
- [ ] Clerk outage behavior: serve cached JWKS until expiry, documented

### Database Schema — Migrations
- [ ] `tenants` table: `id`, `name`, `slug`, `tenant_timezone`, `locale`, `trial_ends_at`, `pilot_pricing`, `created_at`
- [ ] `users` table: `id`, `clerk_id`, `email`, `name`, `locale_preference`, `created_at`
- [ ] `user_tenants` join table: `user_id`, `tenant_id`, `role` (owner/manager/staff), `invited_by`, `joined_at`, `revoked_at`
- [ ] RLS enabled on `user_tenants` and `tenants`

### RLS Middleware
- [ ] `SET LOCAL app.tenant_id = '{uuid}'` on every DB connection from request context
- [ ] `SET LOCAL app.user_role = '{role}'` on every DB connection
- [ ] Missing tenant context → query returns zero rows (verified by test)
- [ ] Cross-tenant access test: user A cannot read tenant B's rows

### RBAC Permission Guards
- [ ] `require_role(min_role)` dependency for FastAPI routes
- [ ] Owner-only guard decorator
- [ ] Manager-and-above guard decorator
- [ ] Staff-and-above guard decorator (all authenticated users)

### Invite Skeleton
- [ ] `POST /invites` — create invite record (full email flow deferred to Sprint 7)
- [ ] `POST /invites/{token}/accept` — accept invite, create user_tenants row

### API Endpoints
- [ ] `POST /auth/register-tenant` — create tenant + owner user
- [ ] `GET /auth/me` — current user + active tenant context
- [ ] `PATCH /auth/me/active-tenant` — switch active tenant (multi-location)

### Exit Gates
- [ ] RLS returns zero rows when `app.tenant_id` is not set
- [ ] Cross-tenant access test fails safely (no data leak)
- [ ] Owner-only endpoints reject Manager and Staff (403)
- [ ] Staff cannot call Manager endpoints (403)
- [ ] Clerk outage behavior documented and tested with cached JWKS

### Fail Gate
- Any tenant-scoped query executes without `app.tenant_id` being set

---

## Sprint 3 — Inventory Ledger Core

**Goal**: Inventory movements are the append-only source of truth; current quantity is derived.

### Database Schema — Migrations
- [ ] `inventory_items` table: `id`, `tenant_id`, `name`, `sku`, `unit`, `category`, `par_level`, `current_quantity`, `reorder_threshold`, `supplier_id`, `created_at`, `updated_at`
- [ ] `inventory_movements` append-only table: `id`, `tenant_id`, `item_id`, `movement_type`, `quantity_delta`, `unit`, `reference_id`, `reference_type`, `idempotency_key`, `notes`, `created_at`, `created_by`
- [ ] `movement_type` enum: `receipt`, `sale_depletion`, `manual_adjust`, `waste`, `count_correction`
- [ ] DB constraint: no UPDATE or DELETE on `inventory_movements` for app roles
- [ ] RLS on both tables

### Inventory Service
- [ ] `create_movement(item_id, type, delta, idempotency_key)` — writes movement + updates `current_quantity` in one transaction
- [ ] Idempotency: duplicate key returns original result without double-write
- [ ] `get_ledger_sum(item_id)` — sum of all movements for an item
- [ ] Integrity check: `ledger_sum == current_quantity` (if mismatch → write `admin_alerts` row)

### Categories & Units
- [ ] `item_categories` master table
- [ ] `units` master table with conversion factors
- [ ] Unit conversion service (`convert(qty, from_unit, to_unit)`)

### API Endpoints
- [ ] `GET /inventory/items` — list with pagination, filter by category/supplier
- [ ] `POST /inventory/items` — create item (Manager+)
- [ ] `PATCH /inventory/items/{id}` — update item metadata (Manager+)
- [ ] `GET /inventory/items/{id}/movements` — movement history
- [ ] `POST /inventory/items/{id}/adjust` — manual adjustment (Manager+, with idempotency key)
- [ ] `POST /inventory/waste` — log waste event (Staff+)
- [ ] `GET /inventory/categories` — list categories
- [ ] `GET /inventory/units` — list units with conversion table

### Exit Gates
- [ ] No app role can UPDATE or DELETE `inventory_movements`
- [ ] `ledger_sum == current_quantity` after every operation
- [ ] Duplicate idempotency key returns original result, no double movement
- [ ] Integrity mismatch creates `admin_alerts` row
- [ ] All endpoints return 403 for insufficient role

### Fail Gate
- Any handler changes inventory without writing to `inventory_movements`

---

## Sprint 4 — Clover Integration MVP

**Goal**: Reliably ingest Clover sales with duplicate-proof inbox processing.

### Clover OAuth
- [ ] `tenant_pos_connections` table: `id`, `tenant_id`, `pos_type`, `access_token`, `merchant_id`, `connected_at`, `last_synced_at`, `status`
- [ ] Clover OAuth sandbox flow (authorization code → token exchange)
- [ ] Token storage encrypted at rest
- [ ] `GET /pos/clover/connect` — initiate OAuth
- [ ] `GET /pos/clover/callback` — handle OAuth callback, store tokens

### Webhook Endpoint
- [ ] `POST /webhooks/pos/clover` — HMAC/signature verification
- [ ] Verify signature before any DB write
- [ ] Insert into `pos_event_inbox` with vendor event ID (unique constraint)
- [ ] Return `200 OK` immediately (before any processing)
- [ ] `pos_event_inbox` table: `id`, `tenant_id`, `vendor_event_id` (unique), `event_type`, `raw_payload`, `status`, `claimed_at`, `processed_at`, `retry_count`, `error`

### Inbox Worker
- [ ] Claim pending events (pessimistic lock / `FOR UPDATE SKIP LOCKED`)
- [ ] Normalize raw Clover payload → `SaleEvent` model
- [ ] Insert into `sales` table
- [ ] Mark inbox row as `processed`
- [ ] Idempotent retry: already-processed event is a no-op
- [ ] `sales` table: `id`, `tenant_id`, `pos_event_id`, `pos_sale_id` (unique per tenant), `sale_time`, `channel`, `subtotal_cents`, `tax_cents`, `total_cents`, `line_items` (JSONB), `raw_payload`

### Reconciliation Pull
- [ ] `GET /pos/clover/reconcile` — pull missed sales from Clover API for a date range
- [ ] Deduplicate against existing `pos_event_inbox` rows by vendor event ID
- [ ] Fixture set: normalized Clover sale events for testing

### Unsupported POS Waitlist
- [ ] `POST /pos/waitlist` — capture email + POS type for unsupported systems
- [ ] `waitlist_entries` table: `id`, `email`, `pos_type`, `notes`, `created_at`

### Exit Gates
- [ ] Webhook handler stores event and returns 200 within 200ms (measured)
- [ ] Duplicate webhook event is silently ignored (unique constraint)
- [ ] Worker replays inbox event idempotently
- [ ] Clover sandbox sale appears in `sales` table end-to-end
- [ ] Reconciliation pull backfills a missed sale
- [ ] Invalid HMAC signature returns 401, nothing written

### Fail Gate
- Webhook processing triggers forecast math, receipt logic, or any slow operation inline

---

## Sprint 5 — Recipe Walk & Sale Depletion

**Goal**: Clover sales deterministically deplete the correct inventory items.

### Database Schema — Migrations
- [ ] `menu_items` table: `id`, `tenant_id`, `pos_item_id`, `name`, `price_cents`, `category`, `active`
- [ ] `recipes` table: `id`, `tenant_id`, `menu_item_id`, `version`, `valid_from`, `valid_to`, `created_by`
- [ ] `recipe_ingredients` table: `id`, `recipe_id`, `inventory_item_id`, `quantity`, `unit`, `notes`
- [ ] `recipe_sub_recipes` table: `id`, `recipe_id`, `child_recipe_id`, `quantity` (for sub-recipe support)
- [ ] `pos_modifiers` table: `id`, `tenant_id`, `pos_modifier_id`, `name`, `modifier_type` (additive/substitution)
- [ ] `recipe_modifiers` table: `id`, `recipe_id`, `modifier_id`, `ingredient_delta` (JSONB)
- [ ] `ingredient_yields` table: `id`, `tenant_id`, `item_id`, `yield_factor`, `prep_method`

### Sale Depletion Service
- [ ] `handle_sale_depletion(sale_event)` function
  - [ ] Look up active recipe version at `sale_time`
  - [ ] Walk recipe ingredients (with sub-recipe recursion)
  - [ ] Apply modifier deltas from sale line items
  - [ ] Apply yield factors
  - [ ] Convert units via unit conversion service
  - [ ] Write one `inventory_movements` row per affected ingredient (`movement_type=sale_depletion`)
  - [ ] Idempotency: `reference_id=sale_id`, `reference_type=sale` — duplicate sale never double-depletes
- [ ] Recipe version selection: use recipe valid at `sale_time`, not current recipe
- [ ] Deterministic: same inputs → same movements, every run

### Unit Conversion
- [ ] Full conversion table: g↔kg, mL↔L, oz↔lb, pieces
- [ ] Cross-category conversion error (g→mL) raises domain error

### API Endpoints
- [ ] `GET /recipes` — list recipes with current version
- [ ] `POST /recipes` — create recipe for a menu item (Manager+)
- [ ] `PATCH /recipes/{id}` — update recipe, auto-versions (Manager+)
- [ ] `GET /menu-items` — list menu items synced from POS
- [ ] `POST /menu-items/{id}/recipe` — assign recipe to menu item

### Tests
- [ ] Fixture: one Clover sandwich sale → correct depletion of bread (2), cheese (30g), lettuce (50g)
- [ ] Fixture: duplicate sale ID → no second depletion
- [ ] Fixture: recipe version change → old sale uses old recipe version
- [ ] Fixture: sub-recipe walk (sauce made of ingredients)
- [ ] Fixture: modifier (extra cheese +20g)

### Exit Gates
- [ ] One sandbox Clover sale depletes correct inventory items end-to-end
- [ ] Duplicate sale ID never double-depletes (idempotency key test)
- [ ] Recipe version changes do not alter depletion of already-processed sales
- [ ] Handler is fully deterministic (same fixtures → same movements)
- [ ] No LLM in any part of this pipeline

### Fail Gate
- Any LLM call in the sale depletion path

---

## Sprint 6 — Receipts & Photo Extraction

**Goal**: Receive supplier orders and capture prices with human-reviewed AI extraction.

### Database Schema — Migrations
- [ ] `receipts` table: `id`, `tenant_id`, `supplier_id`, `receipt_date`, `status` (draft/committed), `total_cents`, `photo_url`, `extraction_confidence`, `created_by`, `committed_at`
- [ ] `receipt_lines` table: `id`, `receipt_id`, `inventory_item_id`, `quantity`, `unit`, `unit_price_cents`, `total_cents`, `extracted_name`, `confidence`, `manually_corrected`
- [ ] `ingredient_prices` append-only: `id`, `tenant_id`, `item_id`, `supplier_id`, `unit_price_cents`, `unit`, `effective_date`, `source` (receipt/manual), `receipt_id`
- [ ] `receipt_adjustments` table: for correction rows after initial commit

### Photo Upload & Storage
- [ ] Presigned upload URL generation to DigitalOcean Spaces
- [ ] Signed read URL for receipt photo display (time-limited)
- [ ] `POST /receipts/upload-url` — generate presigned S3-compatible upload URL
- [ ] No raw receipt content or tokens ever appear in logs (enforced)

### Anthropic Extraction
- [ ] Extraction task: send photo to Anthropic Claude, structured output only
- [ ] Output schema: `{lines: [{name, qty, unit, unit_price, confidence}], supplier_name, receipt_date, total}`
- [ ] Confidence field per line (0.0–1.0)
- [ ] Extraction failure → return empty draft + `manual_entry_required: true` flag
- [ ] Log extraction cost and success/failure (no content in logs)
- [ ] Extraction runs async (not blocking the HTTP response)

### Receipt Review & Commit Flow
- [ ] `POST /receipts` — create draft receipt record, kick off extraction task
- [ ] `GET /receipts/{id}` — return draft with extracted lines for human review
- [ ] `PATCH /receipts/{id}/lines` — user corrects extracted lines
- [ ] `POST /receipts/{id}/commit` — atomic transaction:
  - [ ] Mark receipt `committed`
  - [ ] Insert all `receipt_lines`
  - [ ] Insert `inventory_movements` (type=`receipt`) for each line
  - [ ] Insert `ingredient_prices` row for each line
  - [ ] Cannot commit without at least one reviewed line

### Correction Path
- [ ] `POST /receipts/{id}/adjust` — post-commit correction via `receipt_adjustments` + new movement rows

### API Endpoints
- [ ] `GET /receipts` — list receipts (Manager+)
- [ ] `DELETE /receipts/{id}` — delete uncommitted draft only

### Exit Gates
- [ ] Failed Anthropic extraction falls back to manual entry flow
- [ ] No extracted value is committed without user review step
- [ ] Receipt commit is fully atomic (all tables or none)
- [ ] Photo storage uses signed URLs (no public access)
- [ ] Extraction cost and failure are logged; content is not
- [ ] Raw receipt bytes never appear in structured logs

### Fail Gate
- Receipt photo content or Anthropic tokens appear in any log output

---

## Sprint 7 — Purchase Orders

**Goal**: Owners can create, approve, and send POs; email sends only after DB commit.

### Database Schema — Migrations
- [ ] `purchase_orders` table: `id`, `tenant_id`, `supplier_id`, `status`, `created_by`, `approved_by`, `sent_at`, `expected_delivery_date`, `notes`, `total_estimated_cents`, `idempotency_key`
- [ ] `po_line_items` table: `id`, `po_id`, `inventory_item_id`, `quantity`, `unit`, `estimated_unit_price_cents`
- [ ] `po_status` enum: `draft`, `approved`, `dispatched`, `awaiting_receipt`, `received`, `partially_received`, `canceled`, `failed_delivery`
- [ ] `admin_audit_log` table: `id`, `tenant_id`, `user_id`, `action`, `entity_type`, `entity_id`, `payload` (JSONB), `created_at`
- [ ] `outbox_events` table: `id`, `tenant_id`, `event_type`, `payload` (JSONB), `status`, `created_at`, `processed_at`, `retry_count`, `dead_lettered_at`

### PO State Machine
- [ ] Server-side state transition validation (no invalid transitions accepted)
- [ ] `draft → approved` — owner only
- [ ] `approved → dispatched` — owner only (triggers outbox email event)
- [ ] `dispatched → awaiting_receipt` — auto on expected date
- [ ] `awaiting_receipt → received` — on receipt commit
- [ ] `awaiting_receipt → partially_received` — on partial receipt commit
- [ ] `draft/approved → canceled` — owner only
- [ ] `dispatched → failed_delivery` — owner marks

### Email Send via Outbox
- [ ] `POST /purchase-orders/{id}/send` → insert `outbox_events` row in same transaction as status update
- [ ] Outbox dispatcher: pick up pending events, call Postmark, mark processed
- [ ] Failed email retries with exponential backoff
- [ ] Dead-lettered events create `admin_alerts` row
- [ ] Email sends after DB commit — never before

### Access Control
- [ ] Create PO: Owner only
- [ ] Approve PO: Owner only
- [ ] Send PO: Owner only
- [ ] View PO list: Manager+
- [ ] Manager/Staff cannot create, send, or approve (403)

### Audit Logging
- [ ] Every PO state transition writes `admin_audit_log` row
- [ ] Audit log includes: who, what, when, from_state, to_state

### API Endpoints
- [ ] `GET /purchase-orders` — list POs with status filter (Manager+)
- [ ] `POST /purchase-orders` — create draft PO (Owner only)
- [ ] `GET /purchase-orders/{id}` — PO detail with line items
- [ ] `PATCH /purchase-orders/{id}` — update draft (Owner only)
- [ ] `POST /purchase-orders/{id}/approve` — Owner only
- [ ] `POST /purchase-orders/{id}/send` — Owner only, triggers outbox
- [ ] `POST /purchase-orders/{id}/cancel` — Owner only
- [ ] `POST /purchase-orders/{id}/mark-failed` — Owner only

### Supplier Management
- [ ] `suppliers_master` table (public, read-only by all)
- [ ] `tenant_suppliers` table (tenant-scoped)
- [ ] `GET /suppliers` — merged master + tenant view
- [ ] `POST /suppliers` — create tenant-private supplier (Manager+)
- [ ] `PATCH /suppliers/{id}` — update tenant supplier (Manager+)

### Exit Gates
- [ ] Manager and Staff cannot create, send, or approve POs (403)
- [ ] Email send event written to outbox in same transaction as status update
- [ ] Failed email retries and eventually dead-letters
- [ ] Dead-lettered event creates `admin_alerts` row
- [ ] Every PO action has `admin_audit_log` entry

### Fail Gate
- A PO can be sent without writing to `admin_audit_log`

---

## Sprint 8 — Nightly Batch & Forecast V1

**Goal**: Deterministic nightly agent loop — no LLMs, runs exactly once per tenant per day.

### Database Schema — Migrations
- [ ] `batch_runs` table: `id`, `tenant_id`, `batch_type`, `local_date` (date), `status`, `started_at`, `completed_at`, `error` — unique constraint on `(tenant_id, batch_type, local_date)`
- [ ] `forecast_items` table: `id`, `tenant_id`, `item_id`, `forecast_date`, `predicted_consumption`, `confidence`, `method`, `batch_run_id`
- [ ] `at_risk_items` table: `id`, `tenant_id`, `item_id`, `estimated_runout_date`, `current_days_remaining`, `batch_run_id`
- [ ] `draft_pos` table (for system-generated drafts, distinct from manual POs)

### Batch Dispatcher
- [ ] Tenant-local 2am scheduler (respects `tenants.tenant_timezone`)
- [ ] DST guard: use `pytz` or `zoneinfo`, test spring-forward/fall-back
- [ ] `batch_runs` insert-or-skip guard: if `(tenant_id, batch_type, local_date)` exists → skip
- [ ] Batch types: `forecast_nightly`, `clover_reconcile`, `inventory_integrity`, `price_integrity`

### Forecast Engine (Method C — no LLM)
- [ ] `consumption(t) = trend(t) + weekly_seasonality(t) + holiday_adjustment(t)`
- [ ] 14-day horizon per ingredient
- [ ] EWMA trend component
- [ ] Day-of-week seasonality weights (7 values per item)
- [ ] Canadian statutory holiday calendar (ON + QC)
- [ ] At-risk item detection: `estimated_runout_date` ≤ lead_time + safety_days
- [ ] Forecast output: deterministic from same inputs (no randomness)

### Draft PO Generation
- [ ] At-risk items grouped by supplier
- [ ] Draft PO quantity = `(forecast_consumption * safety_multiplier) - current_quantity`
- [ ] MOQ (minimum order quantity) respected per supplier
- [ ] Draft POs written to `purchase_orders` with `status=draft`, `created_by=system`
- [ ] No duplicate draft POs for same tenant/supplier/date (guard)
- [ ] Draft PO generation runs asynchronously (never in synchronous handler)

### Observability
- [ ] `batch_runs` row marks start time, status (`running/completed/failed`), end time
- [ ] Failed batch creates `admin_alerts` row
- [ ] Forecast output reproducible: same `batch_run_id` → same results

### Exit Gates
- [ ] Batch runs exactly once per tenant per local day (insert conflict = skip)
- [ ] DST guard test passes for spring-forward and fall-back
- [ ] Forecast output is reproducible given same inputs
- [ ] Draft PO generation never happens in synchronous request handlers
- [ ] No LLM call anywhere in this sprint

### Fail Gate
- Batch can generate duplicate POs for same tenant/day/supplier

---

## Sprint 9 — Dashboard, Sales & Stock Visibility

**Goal**: Expose the operational insights that justify the product.

### Dashboard API (Posture C — hybrid pre-computed + live)
- [ ] `GET /dashboard` endpoint
  - [ ] Today's revenue (live query against `sales`)
  - [ ] This week vs last week revenue (live)
  - [ ] At-risk ingredient count (from latest `at_risk_items` batch)
  - [ ] Pending PO count
  - [ ] Pending receipt count
  - [ ] Trial clock status
- [ ] Dashboard payload cached with short TTL (5 min for batch-sourced data)
- [ ] "Stale data" flag if batch hasn't run in >26 hours

### Sales API — Four Tabs
- [ ] `GET /sales/overview` — revenue pacing, this-week vs last, channel split (dine_in/takeout/doordash/ubereats/skip/other)
- [ ] `GET /sales/menu` — per-menu-item revenue + BCG quadrant classification (Kasavana-Smith Method 3, 70% popularity threshold)
- [ ] `GET /sales/dayparts` — 12-week trailing heatmap per (day_of_week, hour_of_day), outlier-excluded (>2σ)
- [ ] `GET /sales/moves` — six opportunity generators:
  - [ ] Time-block promo (capture rate 0.40)
  - [ ] Pricing change (price elasticity, 3-tier fallback)
  - [ ] Weather campaign (capture rate 0.55)
  - [ ] Winback (capture rate 0.22)
  - [ ] Bundling (capture rate 0.30)
  - [ ] Dead-hour promo (capture rate 0.65)

### Stock API — Three Tabs
- [ ] `GET /stock/variance` — period selector, M2 inventory-flow method actual COGS, five-bar waterfall (theoretical → waste → yield loss → shrink → comps → actual)
- [ ] `GET /stock/variance/top-offenders` — ranked items with 11-template explanation catalog
- [ ] `GET /stock/waste` — waste log with reason taxonomy
- [ ] `POST /stock/waste` — log waste event (Staff+)
- [ ] `GET /stock/suppliers` — supplier price trends, alert matrix

### Performance Requirements
- [ ] All read endpoints have documented p95 latency targets (≤500ms)
- [ ] Query plans reviewed for all joins over `inventory_movements` and `sales`
- [ ] Variance math has `coverage_honesty_flag` (insufficient data disclosed)
- [ ] Anthropic-generated content is structured output only, never opinion prose

### Exit Gates
- [ ] Restaurant can see revenue, sales patterns, inventory movement, waste, and variance
- [ ] Variance math discloses coverage confidence
- [ ] All read APIs have p95 targets and query plans reviewed
- [ ] Backend makes no claims without visible source data

### Fail Gate
- Any endpoint returns an insight without disclosing the data behind it

---

## Sprint 10 — Expo App Rebuild & API Integration

**Goal**: Replace static mock data with real mobile flows on iOS and Android.

### Expo Project Setup
- [ ] Expo SDK 51 (or latest stable) project initialized
- [ ] Expo Router v3 file-based navigation
- [ ] Generated TypeScript API client from FastAPI OpenAPI schema (`openapi-typescript` or `orval`)
- [ ] CI: TypeScript type drift from OpenAPI schema fails build
- [ ] Clerk Expo SDK integration for auth session

### Navigation & Auth
- [ ] Onboarding stack (linear, hard-gated, 7 steps):
  - [ ] Step 1: Welcome (EN/FR selection)
  - [ ] Step 2: Account creation (Clerk)
  - [ ] Step 3A: POS picker
  - [ ] Step 3B: Clover OAuth (ASWebAuthenticationSession)
  - [ ] Step 3C: Menu confirmation
  - [ ] Step 4: Recipe placeholders
  - [ ] Step 5A: Suppliers & ingredients setup
  - [ ] Step 5B: Ingredient list + supplier assignment
  - [ ] Step 6: About your restaurant + daily count setup
  - [ ] Step 7: PIN setup ($500 default approval threshold)
  - [ ] Post-onboarding: completion screen + 5-slide tour
- [ ] Tab navigator: Home / Sales / Orders / Stock / More
- [ ] Role-aware navigation (Staff sees subset of screens)
- [ ] Unsupported POS waitlist flow

### Screen Implementations (replace static Data.jsx)
- [ ] **Home/Dashboard** — live dashboard API, trial clock, at-risk items, pending POs
- [ ] **Sales** — four-tab: Overview / Menu / Dayparts / Moves
- [ ] **Orders** — three-tab: At-Risk / Drafts / History; PO detail; PIN approval; send PO
- [ ] **Stock** — three-tab: Variance / Waste / Suppliers; waste log sheet
- [ ] **Settings** — 12-section IA: Account, Restaurant, Team, Suppliers, Items & Recipes, Operations, Notifications, Integrations, Billing (hidden pilot), Privacy & Data, Activity, Help & About

### State Handling (every screen)
- [ ] Loading skeleton
- [ ] Error state with retry
- [ ] Empty state with CTA
- [ ] Stale/offline state with "last updated" timestamp
- [ ] Pull-to-refresh

### Internationalisation
- [ ] `expo-localization` + i18n library
- [ ] EN string file complete
- [ ] FR string file complete
- [ ] EN/FR toggle works at runtime (no restart required)
- [ ] All date/currency formats respect locale

### Push Notifications
- [ ] Push token registration on app open
- [ ] `POST /notifications/register-token` endpoint
- [ ] Notification handling for: at-risk alerts, PO drafts ready, receipt needed

### Exit Gates
- [ ] App runs on iOS simulator and Android emulator locally
- [ ] Every primary screen handles loading, error, empty, and stale states
- [ ] EN/FR language switch works without app restart
- [ ] TypeScript client drift from OpenAPI schema fails CI
- [ ] No screen uses static demo `DATA` object for production behavior

### Fail Gate
- Any screen in production flow relies on hardcoded static data

---

## Sprint 11 — Operations, Security & Disaster Recovery

**Goal**: System is operable by one founder; restore path tested before first customer.

### Structured Logging
- [ ] All logs use structured JSON format (no string interpolation of PII)
- [ ] PII redaction middleware: email, phone, tokens never appear in logs
- [ ] Request ID propagated through all log lines
- [ ] Better Stack integration (log drain configured)

### Alerting
- [ ] P1 alerts (page founder immediately): DB down, restore drill failure, RLS bypass, integrity mismatch, Clover webhook failure >15min
- [ ] P2 alerts (notify, no page): batch missed, Anthropic extraction failure spike, dead-letter queue growing
- [ ] P1 alert tested end-to-end: fires and reaches founder's phone
- [ ] Better Stack uptime checks on `/health/ready`

### Backup & PITR
- [ ] DigitalOcean Managed PostgreSQL PITR enabled (7-day minimum retention)
- [ ] DigitalOcean Spaces backup bucket configured
- [ ] Backup cross-region replication configured (if available)
- [ ] RPO target: ≤1 hour; RTO target: ≤4 hours documented

### Restore Drill (must complete before first pilot customer)
- [ ] Written runbook for restore procedure
- [ ] Restore drill executed against staging environment
- [ ] Restore drill result recorded in `dr.restore_smoke_test` operational log
- [ ] Drill passes: data intact, app functional, ≤RTO target

### Security Posture
- [ ] Secrets rotation procedure documented (Clerk keys, DO API keys, Postmark, Anthropic)
- [ ] No secrets in environment files committed to git
- [ ] `.env.example` only, with all real secrets in DO App Platform env vars
- [ ] SQL injection audit on all raw queries
- [ ] RLS bypass test: confirm no endpoint returns cross-tenant data

### Data Export Pipeline
- [ ] `GET /exports/compliance` — async raw CSV-per-table ZIP, 7-day signed link
- [ ] `GET /exports/bookkeeper` — async 11-report bundle, QuickBooks IIF / Xero CSV / generic GL
- [ ] Export jobs write to `batch_runs` for exactly-once guard
- [ ] Signed link expires after 7 days

### Incident Response
- [ ] Incident template: P1 template, P2 template
- [ ] Breach notification preparation (PIPEDA 72-hour requirement documented)
- [ ] Data minimization audit: confirm no unnecessary PII stored

### Exit Gates
- [ ] Restore drill succeeds before any pilot customer onboards
- [ ] P1 alert reaches founder (tested)
- [ ] Logs contain no PII or tokens
- [ ] Data export works for a full tenant dataset
- [ ] Production runbook exists and is readable

### Fail Gate
- There is no tested restore path before first customer

---

## Sprint 12 — Pilot Hardening & App Store Launch

**Goal**: Prove readiness before real restaurant pilots; ship to TestFlight and Play Store.

### End-to-End Test Suite
- [ ] Synthetic full day replay: onboarding → Clover sale → depletion → nightly batch → dashboard → PO draft → approve → send → receipt commit
- [ ] All critical paths tested (not just happy path)
- [ ] POS outage scenario: app continues with stale labels
- [ ] Anthropic outage scenario: manual receipt entry fallback
- [ ] Postmark outage scenario: email retries and dead-letters
- [ ] Clover webhook duplicate: no double depletion
- [ ] Restore drill: confirmed passing (from Sprint 11)

### Mobile Builds
- [ ] TestFlight build submitted and approved (iOS)
- [ ] Google Play internal test build submitted (Android)
- [ ] App icons, splash screens, screenshots prepared
- [ ] App Store metadata: EN + FR descriptions
- [ ] Play Store metadata: EN + FR descriptions
- [ ] Privacy policy URL live
- [ ] Terms of service URL live
- [ ] Legal EN/FR documents reviewed

### Pilot Operations
- [ ] 10-pilot limit enforced operationally (flag in `tenants` table: `pilot_cohort`)
- [ ] Billing hidden for pilot (`billing_status = 'pilot_exempt'`)
- [ ] Pilot onboarding checklist (for founder to walk each restaurant through)
- [ ] Kill switch / feature flags: mechanism to disable individual features per tenant
- [ ] Pilot support workflow: how founder handles bugs from pilot restaurants

### Pre-Launch Gates (from system-design.md)
- [ ] Clover sandbox end-to-end pass
- [ ] RLS bypass test pass
- [ ] Receipt extraction manual-review pass
- [ ] PO email send pass
- [ ] Restore drill pass
- [ ] EN/FR legal and UI string review pass
- [ ] App Store/TestFlight build pass
- [ ] Google Android build pass
- [ ] P1 alert route tested

### Exit Gates
- [ ] One full synthetic day replayed end-to-end without errors
- [ ] App review builds pass on both platforms
- [ ] 10-pilot limit enforced
- [ ] Billing hidden for pilot cohort
- [ ] All 9 production readiness gates above checked

### Fail Gate
- Any critical workflow works only in the happy path

---

## Background Task Registry (Trigger.dev or equivalent worker)

| Task | Trigger | Sprint | Status |
|---|---|---|---|
| `clover.inbox.process` | continuous poll | Sprint 4 | |
| `clover.reconcile` | nightly scheduled | Sprint 4 | |
| `inventory.integrity_check` | nightly | Sprint 3 | |
| `receipt.extract` | on upload | Sprint 6 | |
| `forecast.nightly` | tenant-local 2am | Sprint 8 | |
| `outbox.dispatch` | continuous poll | Sprint 7 | |
| `exports.generate` | on demand | Sprint 11 | |
| `dr.restore_smoke_test` | pre-launch manual | Sprint 11 | |
| `sms.dispatch` | DEFERRED | — | |
| `price.market_aggregate` | DEFERRED | — | |
| `accounting.push` | DEFERRED | — | |

---

## Human Proofreading Checklist (required on every sprint PR)

- [ ] Which v1 lock does this PR implement?
- [ ] Which architecture lock does it supersede, if any?
- [ ] Which failure mode was tested?
- [ ] Which data can leave ReorderOS infrastructure?
- [ ] Which tables are tenant-scoped?
- [ ] Which RLS policy applies?
- [ ] Which user role can call each endpoint?
- [ ] What happens on retry?
- [ ] What happens when the external provider is down?

---

## DigitalOcean Setup Checklist (Pre-Sprint 1)

- [ ] Create DigitalOcean account / project: `ReorderOS`
- [ ] Provision Managed PostgreSQL 16 cluster (smallest plan for dev)
- [ ] Create `reorderos_dev` and `reorderos_staging` databases
- [ ] Create Spaces bucket: `reorderos-receipts` (private, CORS configured)
- [ ] Create App Platform app spec for FastAPI backend
- [ ] Configure environment variables in App Platform
- [ ] Enable PITR on PostgreSQL cluster
- [ ] Set up DigitalOcean Spaces access keys (stored in App Platform env vars)
- [ ] Create `reorderos-backups` Spaces bucket for DB exports
