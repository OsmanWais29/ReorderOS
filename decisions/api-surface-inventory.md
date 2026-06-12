# API Surface Inventory (v1)

Maps every Expo screen and every background task to the FastAPI endpoint(s) they need. This is the contract the OpenAPI schema must cover before Sprint 10 (Expo rebuild) starts.

Conventions:
- Base path: `/api/v1`
- All requests carry `Authorization: Bearer <Clerk JWT>` and (after onboarding) `X-Tenant-Id` header.
- All write endpoints accept `Idempotency-Key` header.
- All `*_id` are UUIDs.
- Error format: `{ "code": "STABLE_ERROR_CODE", "message": "human-readable", "details": {...} }`.
- Roles in this table are the **minimum** role required server-side. UI may hide more.

Legend: O = Owner, M = Manager, S = Staff, P = Public (no auth).

---

## Onboarding (`frontend/app/onboarding/*`)

| Screen              | Endpoint                                            | Method | Role | Notes                                                                  |
|---------------------|-----------------------------------------------------|--------|------|------------------------------------------------------------------------|
| `welcome`           | (no API)                                            | —      | P    | Marketing copy only.                                                   |
| `sign-in`           | (Clerk SDK; no FastAPI call)                        | —      | P    | Clerk owns auth surface.                                               |
| `account`           | `/tenants`                                          | POST   | O    | Creates tenant after Clerk sign-up; first user becomes Owner.          |
| `push`              | `/users/me/push-tokens`                             | POST   | S    | Registers Expo push token.                                             |
| `pos-picker`        | `/onboarding/pos-options`                           | GET    | O    | Returns supported list (`clover` only) + waitlist options.             |
| `pos-picker` (waitlist) | `/onboarding/waitlist`                          | POST   | O    | Captures unsupported POS provider name.                                |
| `connecting`        | `/integrations/clover/oauth/start`                  | GET    | O    | Returns Clover OAuth URL + state.                                      |
| `connecting` (callback) | `/integrations/clover/oauth/callback`           | POST   | O    | Exchanges code, persists `tenant_pos_connections`.                     |
| `connecting`        | `/integrations/clover/initial-sync`                 | POST   | O    | Kicks off async first sync; returns `batch_run_id`.                    |
| `connecting`        | `/batch-runs/{id}`                                  | GET    | O    | Polled by app while waiting.                                           |
| `found-summary`     | `/onboarding/found-summary`                         | GET    | O    | Counts of items pulled, sales window, restaurant identity.             |
| `cleanup`           | `/menu-items?uncategorized=true`                    | GET    | M    | Paginated.                                                             |
| `cleanup`           | `/menu-items/{id}/category`                         | PUT    | M    | Sets category.                                                         |
| `manual-menu`       | `/menu-items`                                       | POST   | M    | Manual menu entry path.                                                |
| `suppliers`         | `/suppliers`                                        | GET    | M    | Hybrid: master + tenant suppliers, deduped by source.                  |
| `suppliers`         | `/suppliers`                                        | POST   | M    | Adds tenant-scoped supplier.                                           |
| `par-levels`        | `/inventory-items?needs_par=true`                   | GET    | M    | Items missing par level.                                               |
| `par-levels`        | `/inventory-items/{id}/par`                         | PUT    | M    | Sets `par_min`, `par_max`.                                             |
| `team`              | `/users/invitations`                                | POST   | O    | Creates Clerk invitation + role binding.                               |
| `pin`               | (local only via `expo-secure-store`)                | —      | —    | No server PIN in v1; Clerk session is the auth.                        |
| `biometric`         | (local only via `expo-local-authentication`)        | —      | —    | Device-side gate.                                                      |
| `billing`           | (hidden during pilot)                               | —      | O    | Stripe deferred; UI shows trial pill only.                             |
| `done`              | `/users/me/onboarding/complete`                     | POST   | O    | Marks onboarding done; returns home payload.                           |

---

## Tabs (`frontend/app/(app)/*`)

### `home.tsx` — Home dashboard

| Endpoint                     | Method | Role | Returns                                                                  |
|------------------------------|--------|------|--------------------------------------------------------------------------|
| `/dashboard/home`            | GET    | S    | Greeting context, today's sales, food cost %, open POs, AI suggestions.  |
| `/dashboard/suggestions`     | GET    | S    | Latest forecast-derived owner suggestions (e.g. "order ground beef").    |
| `/dashboard/refresh`         | POST   | S    | Forces re-aggregation cache refresh (no batch trigger).                  |

### `stock.tsx` — Inventory list

| Endpoint                                | Method | Role | Returns                                                  |
|-----------------------------------------|--------|------|----------------------------------------------------------|
| `/inventory-items`                      | GET    | S    | Paginated list with `current_quantity`, `par_min/max`, status. |
| `/inventory-items/{id}`                 | GET    | S    | Item detail incl. last 30 movements summary.             |
| `/inventory-items/{id}/movements`       | GET    | S    | Append-only ledger view, paginated.                      |
| `/inventory-items/{id}/count`           | POST   | S    | Submits a physical count → `count_correction` movement.  |
| `/inventory-items/{id}/waste`           | POST   | S    | Submits waste log → `waste` movement.                    |
| `/inventory-items/{id}/par`             | PUT    | M    | Updates par levels.                                      |
| `/stock/variance`                       | GET    | M    | Variance vs ledger expectation; coverage flag included.  |

### `orders.tsx` — Purchase Orders

| Endpoint                                  | Method | Role | Notes                                                       |
|-------------------------------------------|--------|------|-------------------------------------------------------------|
| `/purchase-orders`                        | GET    | M    | List by status (`draft`, `approved`, `dispatched`, ...).     |
| `/purchase-orders`                        | POST   | O    | Create manual PO. Owner-only.                               |
| `/purchase-orders/{id}`                   | GET    | M    | Detail incl. lines, supplier, audit trail.                  |
| `/purchase-orders/{id}/lines`             | PUT    | O    | Edit lines while in `draft`.                                |
| `/purchase-orders/{id}/approve`           | POST   | O    | `draft` → `approved`. Owner-only. Audit-logged.             |
| `/purchase-orders/{id}/send`              | POST   | O    | `approved` → `dispatched`. Triggers outbox email. Owner-only.|
| `/purchase-orders/{id}/cancel`            | POST   | O    | Allowed in `draft` and `approved`. Owner-only.              |
| `/purchase-orders/{id}/mark-failed`       | POST   | O    | `dispatched` → `failed_delivery`.                           |
| `/receipts`                               | POST   | M    | Initiate a receipt (used to close PO upon receiving).       |

### `sales.tsx` — Sales view

| Endpoint                  | Method | Role | Returns                                            |
|---------------------------|--------|------|----------------------------------------------------|
| `/sales/overview`         | GET    | M    | Daily/weekly revenue, AOV, covers (if available).  |
| `/sales/breakdown`        | GET    | M    | By category / menu item / hour.                    |
| `/sales/feed`             | GET    | M    | Recent sales (debug + reconciliation visibility).  |

### `more.tsx` — Settings & account

| Endpoint                          | Method | Role | Notes                                                  |
|-----------------------------------|--------|------|--------------------------------------------------------|
| `/users/me`                       | GET    | S    | Profile + role + tenant.                               |
| `/users/me/language`              | PUT    | S    | `en` or `fr`.                                          |
| `/tenants/me`                     | GET    | M    | Restaurant settings.                                   |
| `/tenants/me`                     | PUT    | O    | Update restaurant settings.                            |
| `/users` (team list)              | GET    | O    | Lists members.                                         |
| `/users/invitations`              | POST   | O    | Invites member.                                        |
| `/users/{id}/role`                | PUT    | O    | Reassigns role (within v1 set).                        |
| `/users/{id}`                     | DELETE | O    | Removes member.                                        |
| `/suppliers`                      | GET/POST/PUT/DELETE | M | Tenant supplier CRUD; master suppliers are read-only. |
| `/billing/portal`                 | GET    | O    | Stripe portal link. **Hidden in pilot.**               |
| `/auth/sign-out`                  | POST   | S    | Revokes session (Clerk).                               |

---

## Receipts module (used from Stock + Orders flows)

| Endpoint                                  | Method | Role | Notes                                                      |
|-------------------------------------------|--------|------|------------------------------------------------------------|
| `/receipts/uploads`                       | POST   | S    | Returns signed Spaces URL for photo upload + `receipt_id`. |
| `/receipts/{id}/extract`                  | POST   | S    | Triggers `receipt.extract` task.                           |
| `/receipts/{id}`                          | GET    | S    | Returns extraction draft + per-line confidence.            |
| `/receipts/{id}/lines/{line_id}`          | PUT    | S    | Edit a draft line before commit.                           |
| `/receipts/{id}/commit`                   | POST   | M    | Atomic commit. Requires `confirmed_at` + idempotency key.  |
| `/receipts/{id}/cancel`                   | POST   | S    | Discards a draft.                                          |

---

## System

| Endpoint                  | Method | Role | Purpose                                              |
|---------------------------|--------|------|------------------------------------------------------|
| `/health/live`            | GET    | P    | Liveness probe.                                      |
| `/health/ready`           | GET    | P    | Readiness probe (DB reachable).                      |
| `/version`                | GET    | P    | Build SHA.                                           |
| `/webhooks/pos/clover`    | POST   | P*   | Clover signed webhook. *Verified by HMAC, not JWT.   |
| `/exports`                | POST   | O    | Kicks off `exports.generate`.                        |
| `/exports/{id}`           | GET    | O    | Status + signed download URL when ready.             |
| `/admin/audit-log`        | GET    | O    | Paginated audit log for the tenant.                  |

---

## Background Tasks (no HTTP surface; consumes IDs from DB / queues)

| Task id                     | Reads                              | Writes                                    |
|-----------------------------|------------------------------------|-------------------------------------------|
| `clover.inbox.process`      | `pos_event_inbox` pending          | `sales`, `inventory_movements`            |
| `clover.reconcile`          | Clover API                         | `pos_event_inbox`                         |
| `inventory.integrity_check` | `inventory_movements`              | `admin_audit_log`, alerts via outbox      |
| `forecast.nightly`          | sales history, current quantities  | `forecast_runs`, draft `purchase_orders`  |
| `receipt.extract`           | Spaces image                       | `receipts` (extraction draft)             |
| `outbox.dispatch`           | `outbox_events`                    | external (Postmark, Expo push)            |
| `exports.generate`          | tenant data                        | `exports`, signed URL                     |
| `dr.restore_smoke_test`     | backup snapshot                    | drill report row                          |

---

## Surface Volume

- 14 onboarding screens → **~12 endpoints** (some local-only).
- 5 tabs → **~30 endpoints**.
- Receipts cross-cutting → **6 endpoints**.
- System + admin → **~7 endpoints**.

Total v1 HTTP surface: **~55 endpoints**, plus 1 webhook ingress.

CI must keep the generated TypeScript client at `frontend/api-client/` in sync with this surface (Sprint 10).

## Audit notes

- **2026-06-11 (Sprint 5 Phase 16):** added `GET /onboarding/recipes/{menu_item_id}/modifiers/{modifier_id}` (read-only modifier detail). The modifier config surface had write routes (PATCH/confirm/skip) but **no read** for a single modifier's ingredients — a concrete instance of the **consumer-less-API completeness class**: an endpoint set looks complete until a consumer (here the modifier-config UI) needs the symmetric read, and its absence forces a write-as-read anti-pattern (PATCH would implicitly unskip / 409). The recipe surface had the read (`GET /recipes/{menu_item_id}`); the modifier surface didn't. Worth a sweep of the matrix for other write-without-read asymmetries before the Sprint 10 client-gen.
