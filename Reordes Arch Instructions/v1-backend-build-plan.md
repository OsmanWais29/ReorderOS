# ReorderOS V1 Backend Build Plan

Status: planning baseline for Clover-only App Store launch.

## V1 Product Locks

- Platforms: iOS and Android.
- Mobile: rebuild as an Expo React Native app using generated API clients.
- Backend: Python, FastAPI, modular monolith.
- Infrastructure: DigitalOcean App Platform, Managed PostgreSQL, Spaces.
- POS launch scope: Clover only; unsupported POS routes to waitlist.
- Languages: English and French on every customer-facing surface.
- Roles: Owner, Manager, Staff.
- Staff accounts: individual login accounts; no shared in-store PIN mode.
- Owner-only v1 actions: approve POs, create manual POs, send POs, billing, team invites.
- Manager v1 actions: counts, receipts, waste, recipes, suppliers/items.
- Staff v1 actions: counts, receipt photo upload/correction draft, waste logs.
- PO sending: email-only in v1. SMS later.
- Receipt photo extraction: Anthropic-only in v1, with human review before commit.
- Billing: hidden during pilot.
- Pilot pricing: first 10 pilot restaurants get lifetime USD 99/month/location.
- Public post-pilot price target: USD 149/month/location.
- Cross-tenant price intelligence: deferred until 50-100 restaurants and legal review.

## Engineering Principle

Build the smallest complete operating loop first:

`Clover sale -> durable inbox -> normalized sale -> inventory movement -> variance/sales insight -> owner-visible action`.

Do not start by building every future architecture lock. Start by proving that restaurant operators can see variance, understand sales, and trust the data.

## LLM Sprint Rule

Each sprint must be packaged into a context bundle under 100k tokens:

- sprint goal
- relevant locks only
- API contracts touched
- schema migrations touched
- test plan
- failure modes
- human proofread checklist

Do not load every architecture document into one sprint. That will reduce precision and increase contradiction risk.

## Sprint Gates

### Sprint 0 - Scope Freeze And Contradiction Cleanup

Goal: convert planning decisions into non-negotiable implementation scope.

Entry:
- Current architecture docs extracted.
- V1 locks above accepted.

Build:
- `decisions/v1-scope.md`
- contradiction register
- release risk register
- API surface inventory from frontend mock
- bilingual string inventory

Hard exit gate:
- Clover-only v1 accepted in writing.
- Cross-tenant price comparison disabled for v1.
- RBAC v1 permissions accepted.
- SMS deferred.
- Billing hidden for pilot.
- Unsupported POS waitlist accepted.

Fail gate:
- Any unresolved legal/compliance question touches live customer data sharing.

### Sprint 1 - Platform Skeleton

Goal: create a deployable FastAPI modular monolith with database and CI.

Build:
- FastAPI app scaffold.
- SQLAlchemy 2 async.
- Alembic migrations.
- Pydantic request/response models.
- OpenAPI generation.
- Health endpoints.
- Local Docker Compose for Postgres.
- CI: lint, typecheck, unit tests, migration check.

Hard exit gate:
- `GET /health/live` and `GET /health/ready` work locally.
- Empty Alembic migration applies and rolls back in disposable local DB.
- OpenAPI schema generated in CI.
- First deployment target documented.

Fail gate:
- Backend cannot be run locally by one command.

### Sprint 2 - Tenant, Auth, And RBAC Foundation

Goal: make every request tenant-aware and role-aware.

Build:
- Clerk JWT validation with JWKS cache.
- `tenants`, `users`, `user_tenants`.
- active tenant claim handling.
- RLS session variable: `app.tenant_id`.
- app user role session variable: `app.user_role`.
- Owner/Manager/Staff permission guard.
- invite skeleton for later email flow.

Hard exit gate:
- RLS returns zero rows when tenant context is missing.
- Cross-tenant access test fails safely.
- Owner-only endpoints reject Manager and Staff.
- Clerk outage behavior documented and tested with cached JWKS.

Fail gate:
- Any tenant-scoped query works without `app.tenant_id`.

### Sprint 3 - Inventory Ledger Core

Goal: make inventory movements the source of truth.

Build:
- `inventory_items`.
- `inventory_movements` append-only table.
- current quantity materialized on item.
- movement types: receipt, sale_depletion, manual_adjust, waste, count_correction.
- idempotency keys for user-triggered writes.
- inventory integrity check.
- basic item/unit/category APIs.

Hard exit gate:
- No app role can update/delete `inventory_movements`.
- Ledger sum equals `inventory_items.current_quantity`.
- Duplicate idempotency key returns the original result.
- Integrity mismatch raises admin alert row.

Fail gate:
- Any handler changes inventory without writing a movement.

### Sprint 4 - Clover Integration MVP

Goal: reliably ingest Clover sales without risking duplicated depletion.

Build:
- Clover OAuth sandbox connection.
- `tenant_pos_connections`.
- POS webhook endpoint.
- HMAC/signature verification.
- durable `pos_event_inbox`.
- normalized `sales`.
- normalized sale event fixtures.
- inbox worker.
- reconciliation pull for missed sales.
- unsupported POS waitlist endpoint.

Hard exit gate:
- Webhook handler stores event and returns quickly.
- Duplicate event is ignored by database unique constraint.
- Worker can replay inbox event idempotently.
- Clover sandbox sale appears in `sales`.
- Reconciliation can backfill missed sale.

Fail gate:
- Clover webhook processing depends on forecast math or long-running receipt/order logic.

### Sprint 5 - Recipe Walk And Sale Depletion

Goal: turn Clover sales into deterministic inventory depletion.

Build:
- menu items.
- recipes and recipe ingredients.
- unit conversions.
- yield handling.
- sale-to-depletion handler.
- modifier support for additive modifiers.
- recipe version selection at sale time.

Hard exit gate:
- One sandbox Clover sale decrements the correct inventory items.
- Duplicate sale never double-depletes.
- Recipe version changes do not alter old sale depletion.
- Handler is deterministic and fixture-tested.

Fail gate:
- LLM is used in sale depletion.

### Sprint 6 - Receipts And Photo Extraction

Goal: receive orders and capture prices with photo extraction.

Build:
- receipt photo upload to Spaces.
- Anthropic receipt extraction surface.
- extraction confidence fields.
- manual correction before commit.
- `receipts`, `receipt_lines`.
- receipt commit transaction writes receipt, lines, inventory movements, price rows.
- receipt edit/correction path via adjustment rows.

Hard exit gate:
- Failed Anthropic extraction falls back to manual entry.
- No extracted value commits without user review.
- Receipt commit is atomic across all tables.
- Receipt photo storage has signed URL access.
- Extraction cost and failure are logged.

Fail gate:
- Receipt photo raw content or tokens appear in logs.

### Sprint 7 - Purchase Orders

Goal: let owners create, approve, and send POs.

Build:
- `purchase_orders`, `po_line_items`.
- 8-state PO machine trimmed to v1.
- owner-only manual PO.
- owner-only approval.
- owner-only email send.
- Postmark email dispatch via outbox.
- PO history.
- audit log for owner money actions.

Hard exit gate:
- Manager/Staff cannot create/send/approve PO.
- Email send only fires after DB commit.
- Failed email retries and eventually dead-letters.
- PO state transitions are validated server-side.

Fail gate:
- A PO can be sent without audit logging.

### Sprint 8 - Nightly Batch And Forecast V1

Goal: create the deterministic agent loop without LLMs.

Build:
- `batch_runs` guard.
- tenant-local nightly dispatcher.
- basic forecast run record.
- at-risk SKU computation.
- supplier draft PO generation.
- stale reason flags.
- external signal placeholder hooks.
- batch observability.

Hard exit gate:
- Batch runs exactly once per tenant local day.
- DST guard test passes.
- Forecast output is reproducible from same inputs.
- PO drafting never happens in synchronous request handlers.

Fail gate:
- Batch can generate duplicate POs for same tenant/day/supplier.

### Sprint 9 - Dashboard, Sales, And Variance Visibility

Goal: expose the insights that justify the product.

Build:
- Dashboard read API.
- Sales overview read API.
- Stock variance read API.
- Waste log handler.
- basic supplier price tracking from receipts.
- cache only where needed.

Hard exit gate:
- Restaurant can see revenue, sales patterns, inventory movement, waste, and variance.
- Variance math has a coverage honesty flag.
- All read APIs have p95 targets and query plans.

Fail gate:
- Backend makes unverifiable claims without visible input data.

### Sprint 10 - Expo App Rebuild And API Integration

Goal: replace static mock data with real mobile flows.

Build:
- Expo project.
- generated TypeScript API client from FastAPI OpenAPI.
- auth session handling.
- EN/FR translations.
- role-aware navigation.
- loading/empty/error/offline/stale states.
- push-token registration.
- Clover waitlist flow.

Hard exit gate:
- App runs on iOS and Android locally.
- Every primary screen handles loading, error, empty, stale.
- EN/FR switch works.
- API type drift fails CI.

Fail gate:
- Any screen relies on static demo `DATA` for production behavior.

### Sprint 11 - Operations, Security, And Disaster Recovery

Goal: make the system operable by one founder.

Build:
- structured logging.
- Better Stack integration.
- P1/P2 alert categories.
- backup/PITR runbook.
- restore drill before first customer.
- incident templates.
- data export pipeline.
- breach-notification preparation.
- secrets rotation procedure.

Hard exit gate:
- Restore drill succeeds before first customer.
- P1 alert reaches founder.
- Logs redact PII and tokens.
- Data export can be generated for a tenant.
- Production runbook exists.

Fail gate:
- There is no tested restore path.

### Sprint 12 - Pilot Hardening And Store Launch

Goal: prove readiness before real restaurant pilots.

Build:
- Clover sandbox end-to-end test suite.
- TestFlight build.
- Google closed/internal test build.
- App Store/Play Store metadata.
- pilot onboarding checklist.
- kill switches/feature flags.
- pilot support workflow.

Hard exit gate:
- One full synthetic day can be replayed end-to-end.
- App review builds pass.
- 10-pilot limit enforced operationally.
- Billing hidden for pilot.
- Legal EN/FR documents ready.

Fail gate:
- Any critical workflow works only in ideal happy path.

## Human Proofreading Framework

Every sprint PR must include:

- Which v1 lock it implements.
- Which architecture lock it supersedes, if any.
- Which failure mode was tested.
- Which data can leave ReorderOS infrastructure.
- Which tables are tenant-scoped.
- Which RLS policy applies.
- Which user role can call each endpoint.
- What happens on retry.
- What happens when the external provider is down.

Code comments should explain domain invariants, not obvious syntax. Example: comment why `pos_event_inbox` returns fast before processing; do not comment that a variable is assigned.

