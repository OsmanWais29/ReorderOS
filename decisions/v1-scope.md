# V1 Scope Lock

Status: **frozen for v1**. Any change requires explicit founder sign-off and a new ADR.

## Platforms

- iOS (Expo) and Android (Expo). No PWA, no web app for v1.
- Backend: Python 3.12, FastAPI, modular monolith, single deployable.
- Hosting: DigitalOcean App Platform + Managed PostgreSQL 16 + Spaces.
- Auth: Clerk (JWT + JWKS).
- POS: Clover only at launch. Square / Toast / Lightspeed / others route to waitlist.

## Roles & Permissions (RBAC)

Three roles only:

| Role    | Can do                                                                          | Cannot do                                  |
|---------|---------------------------------------------------------------------------------|--------------------------------------------|
| Owner   | everything Manager + Staff can; approve POs, create manual POs, send POs, billing, team invites | n/a                                       |
| Manager | counts, receipts, waste, recipes, suppliers/items                               | approve POs, send POs, billing, invites    |
| Staff   | counts, receipt photo upload + correction draft, waste logs                     | recipes, suppliers, POs, billing, invites  |

- Staff = individual login accounts with their own Clerk identity.
- No shared in-store PIN login mode in v1.
- `ops_lead`, `line_cook`, `accountant` roles are **deferred** (not implemented in v1).

## Money Actions Are Owner-Only

All four PO money actions are owner-only and audit-logged:

1. Create manual PO
2. Approve PO
3. Send PO
4. Cancel PO

PO send channel is **email only** in v1 via Postmark. SMS is **deferred**.

## Receipts (Receiving Inventory)

- Receipt photo upload to DigitalOcean Spaces.
- Anthropic does the extraction.
- Anthropic output is a **draft**. Never auto-committed.
- Manual entry fallback exists for failed extraction.
- Commit transaction writes: `receipts`, `receipt_lines`, `inventory_movements`, `ingredient_prices` atomically.

## Inventory Truth Model

- Append-only ledger: `inventory_movements`. No app role can update or delete rows.
- Materialized state: `inventory_items.current_quantity`. Rebuildable from ledger.
- Movement types: `receipt`, `sale_depletion`, `manual_adjust`, `waste`, `count_correction`.
- Idempotency keys required on all user-triggered writes.
- Nightly integrity check: `sum(movements) == current_quantity`. Mismatch raises P1.

## Tenant Isolation

- Every tenant-scoped table has `tenant_id NOT NULL`.
- Postgres RLS enabled on every tenant-scoped table.
- Session vars: `app.tenant_id`, `app.user_role`. Set per-request from the validated Clerk JWT.
- Missing tenant context returns zero rows, never errors that leak schema.

## Suppliers Model

Hybrid:

- `suppliers_master` (public, curated; cross-tenant read-only).
- `tenant_suppliers` (private, tenant-scoped; tenant-owned overrides + ad-hoc suppliers).

## Cross-Tenant Data Sharing

- **Disabled in v1.** No cross-tenant price comparison, no cross-tenant menu insights.
- Re-evaluated only at 50–100 paying restaurants, after legal review and explicit opt-in surface.

## Pricing & Billing

- Billing UI **hidden during pilot**.
- Pilot pricing: first 10 pilot restaurants pay **USD 99/month/location for life**.
- Public post-pilot price target: **USD 149/month/location**.
- Billing provider: Stripe (integration deferred until pilot exit).

## Languages

- English and French on every customer-facing surface from day one.
- App-level `LangProvider` controls switch; persisted per-user.
- Server returns localized strings only for owner/operator notifications (push, email).
  Errors and validation messages are returned as stable codes; UI translates them.

## Realtime

- No realtime / websockets / push-from-server-to-client subscriptions in v1.
- Foreground polling only (e.g., dashboard refresh on screen focus).
- Server-to-device push notifications via Expo push tokens for events only (PO state changes, P1 alerts to owner). Not for data sync.

## Background Jobs (Trigger.dev or equivalent worker)

V1 task set:

| id                          | trigger                  |
|-----------------------------|--------------------------|
| `clover.inbox.process`      | continuous / polled      |
| `clover.reconcile`          | scheduled                |
| `inventory.integrity_check` | nightly                  |
| `forecast.nightly`          | tenant-local 02:00       |
| `receipt.extract`           | on upload                |
| `outbox.dispatch`           | continuous / polled      |
| `exports.generate`          | on demand                |
| `dr.restore_smoke_test`     | pre-launch + quarterly   |

Deferred: `sms.dispatch`, `price.market_aggregate`, `accounting.push`.

## Observability & Ops

- Better Stack for logs + alert routing.
- Structured JSON logs; PII and tokens redacted at the logger layer.
- P1: pages founder. P2: queues for next-day review.
- Postgres PITR enabled. Restore drill must pass before first pilot.

## What Is Explicitly Out For V1

- SMS sending (any channel)
- Cross-tenant price aggregation
- Direct accounting push (QBO/Xero)
- Square / Toast / Lightspeed POS
- Shared PIN login
- PWA / web dashboard
- Realtime websocket subscriptions
- Self-serve role customization

## Sign-Off

This document is the canonical source for v1 scope. Each sprint PR must reference the section it implements. Any deviation requires a new ADR under `decisions/adr/` and explicit founder approval.

| Item                                  | Accepted | Date       |
|---------------------------------------|----------|------------|
| Clover-only POS at launch             | [ ]      |            |
| Cross-tenant price comparison off     | [ ]      |            |
| SMS deferred                          | [ ]      |            |
| Billing hidden during pilot           | [ ]      |            |
| RBAC = Owner / Manager / Staff only   | [ ]      |            |
| Staff = individual accounts only      | [ ]      |            |
| Anthropic = draft only, no auto-commit| [ ]      |            |
| iOS + Android only (no PWA)           | [ ]      |            |
| PostgreSQL on DO Managed (no Supabase)| [ ]      |            |
| No realtime in v1                     | [ ]      |            |
| Hybrid suppliers model                | [ ]      |            |
