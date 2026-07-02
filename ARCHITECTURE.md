# ReorderOS — Architecture

Canadian restaurant inventory + reorder platform. iOS-first, Clover-only POS at v1,
bilingual EN/FR. FastAPI async backend + DigitalOcean (App Platform + managed
PostgreSQL 17 + Spaces), Expo React Native app, Clerk JWT auth, Anthropic for recipe
suggestion, Postmark email, Better Stack observability.

This is the map for "where does the bug live." For *how things fail in production*
see [`docs/sprint-failure-catalog.md`](docs/sprint-failure-catalog.md); for *what is
built vs wired right now* see [`STATUS.md`](STATUS.md).

## The core value loop (a real sale → stock goes down)

```
Clover POS                         ReorderOS backend
──────────                         ─────────────────
webhook  ──────────▶  webhook.py            verify static X-Clover-Auth secret
(order LOCKED)        (HTTP, fast)           (hmac.compare_digest), echo verification
                          │                  code, INSERT into pos_event_inbox.
                          │                  NO heavy work inline.  ── inbox pattern ──
                          ▼
                      InboxWorker (pos/worker.py)        runs as service_worker role
                          │  claim_batch:  WITH ... MATERIALIZED ( ... FOR UPDATE
                          │                SKIP LOCKED )  ← MATERIALIZED is load-bearing
                          ▼
                      clover_client.list_orders          fetch the real order JSON
                          │                              (token-bucket rate limited)
                          ▼
                      depletion/handler.py               one transaction per sale line
                          │   resolver  → is this line eligible? (not refunded/voided)
                          │   walker    → base recipe + modifiers → ingredient amounts
                          │   conversions→ recipe unit → stock unit (exact Decimal)
                          │   writer    → append movement, ON CONFLICT (tenant_id,
                          │               idempotency_key) DO NOTHING  ← idempotent
                          ▼
                      inventory_movements  (append-only ledger)
                          │
                          ▼
                      on_hand = SUM(movements)           inventory/services.py
```

Key invariants (do not break these):
- **Depletion is deterministic and LLM-free.** No AI call anywhere under
  `inventory/depletion/`. Enforced by `tools/ci/check_no_llm_in_depletion.py`.
- **Append-only ledger.** Stock is never mutated in place; `on_hand` is a sum of
  movements. Re-processing the same Clover order must not double-count — guaranteed
  by `UNIQUE(tenant_id, idempotency_key)` + `ON CONFLICT DO NOTHING`, not by app gates.
- **Frozen snapshot.** A line depletes against `sale_line_items.recipe_version_id`
  captured at ingest, never the current recipe — past sales are immune to later edits.
- **Inbox pattern.** The webhook only verifies + inserts; all fetch/deplete work is
  the worker's. Webhooks never call Clover or deplete inline.
- **Compute-then-commit per line.** A conversion failure in base *or* modifier fails
  the whole line with zero partial ledger rows; failed lines stay pending and retry.

## Module ownership (`backend/app/modules/`)

| Module | Owns | Notable files |
|---|---|---|
| `auth/` | Clerk JWT verify, principal | `router.py`, `../core/security.py` |
| `tenants/` | tenant + membership; `resolve_principal` (verifies active membership for `X-Tenant-Id`) | `repo.py` |
| `invitations/` | invites, RBAC | `router.py`, `repo.py` |
| `inventory/` | the ledger + the depletion engine | `services.py` (on_hand/movements/idempotency), `depletion/{resolver,walker,conversions,units,handler,writer,diagnostics}.py`, `router.py` (`GET /items`, opening-balance) |
| `recipes/` | recipe + modifier config; LLM suggestion | `repo.py`, `router.py`, `modifiers_{repo,router}.py`, `schemas.py`, `validators.py`, `suggest.py`, `llm_client.py` |
| `pos/` | all Clover I/O | `clover_client.py`, `catalog_sync.py`, `router.py` (OAuth), `worker.py` (inbox), `webhook.py`, `reconciliation.py`, `state_manager.py`, `token_refresh.py` |

`core/`: `config.py`, `database.py` (app_user pool), `service_db.py` (service_worker
pool), `rls.py`, `deps.py`, `encryption.py`, `logging.py`.

## Two DB roles and how tenant isolation actually works

- **`app_user`** — the request path (`get_rls_session` sets `app.tenant_id`/user/role
  GUCs). It is the table-owning role, so **`FORCE ROW LEVEL SECURITY` is what makes
  RLS apply to it** — plain `ENABLE` is bypassed by the owner. Every tenant table is
  `FORCE`'d (guard: `tests/test_rls.py::test_every_tenant_table_is_force_rls`).
- **`service_worker`** — the webhook handler + inbox/depletion worker, a separate
  non-owner role. The cross-tenant `pos_event_inbox` / `tenant_pos_connections` use
  `USING(true)` policies by design (the worker must resolve merchant→tenant before it
  knows the tenant); it sets `app.tenant_id` explicitly before any tenant-scoped write.

## Migrations

Alembic, linear chain, run as `app_user` via `alembic/env.py` (so `app_user` owns the
tables — see the FORCE note above). Sprint 5 spans `0014`–`0022`. New tenant tables
must pair `ENABLE` with `FORCE` ROW LEVEL SECURITY or the guard test fails CI.
