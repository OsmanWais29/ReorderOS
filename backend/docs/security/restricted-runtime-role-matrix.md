# Restricted Runtime Role — Exact Privilege Matrix

Security PR (migration `0035_restricted_runtime_role`). This matrix is the ground-truth
authorization model the PR targets. Every row is tagged with its **evidence class**:

| Tag | Meaning |
|---|---|
| **[LOCAL]** | Verified against the LOCAL dev DB (`information_schema.role_table_grants`, `role_usage_grants`, `role_routine_grants`, `pg_policies`, `pg_roles`, `pg_auth_members`). This is target-state, not staging. |
| **[MIGRATION]** | Defined by migration `0035` (and prior migrations) — what the DB SQL applies. |
| **[STAGING]** | Actual live staging evidence. **NONE YET** — the DO cutover has not run (see §7, `NOT RUN`). |
| **[PROD]** | Future production state after a separate, approved production cutover. Not in scope here. |

Reproduce the [LOCAL] rows with the queries in the appendix. Do **not** read any [LOCAL] or
[MIGRATION] row as proof of staging: `reorderos_app` as an actual DigitalOcean **LOGIN** role
does not exist yet, so nothing here is [STAGING]-verified.

> **Threat model this PR closes.** Before this PR both runtime pools connected as
> `doadmin`, which has `rolbypassrls = TRUE`. Normal API and worker traffic therefore
> **bypassed every RLS policy** — tenant isolation was enforced only by application
> `WHERE` clauses, not by the database. In an environment that completes the cutover
> (staging first), the request path connects as `reorderos_app` and the worker path as
> `service_worker`, **both** `rolsuper = false` and `rolbypassrls = false`, so RLS is the
> enforced control; there `doadmin` is retained for migrations only (a capability that
> genuinely needs DDL/ownership).
>
> **Enforcement is gated on `RESTRICTED_RUNTIME_ROLES_ENABLED`** (Settings flag, default
> `false`): the fail-closed startup role assertions (api request pool = `reorderos_app`,
> service pools = `service_worker`) run ONLY where the flag is `true`. The staging
> candidate spec sets it `true`; the committed production spec sets it `false` —
> production keeps its current admin-bound `DATABASE_URL` and Dockerfile default
> (`alembic upgrade head && exec uvicorn`) unchanged until its own separately approved
> cutover. `APP_ENV=production` alone never activates the assertions. The flag does NOT
> touch the RLS policies themselves — those are always active per 0022/0035; it only
> controls the cutover-time startup assertion.

---

## 0. Component dependency matrix (code-backed; the source of `app/core/component_requirements.py`)

Which component consumes which secret, with the consuming code as evidence. **Do not widen
a set without new file:line evidence.** Executable proof: the boot matrix in
`tests/test_component_requirements.py` (each component boots with EXACTLY its declared
set; every single key removed → named fail-closed failure) plus the feature-path
fake-provider suites cited per row. `APP_COMPONENT` selects the set at runtime;
production (flag off, `APP_COMPONENT` unset) uses the `legacy` profile = the pre-cutover
global behavior, pinned key-by-key in `tests/test_phase0_config.py`.

| Secret | api | inbox-worker | reconciliation-worker | receipt-extraction-worker | inbound-email-worker | migrate job | Consuming code (evidence) | Feature-path fake-provider tests |
|---|---|---|---|---|---|---|---|---|
| `DATABASE_URL` | ✔ (request pool) | — | — | — | — | ✔ (only secret) | `app/core/database.py`; `alembic/env.py` (reads env directly) | whole HTTP suite; migration round-trip |
| `SERVICE_DATABASE_URL` | ✔ | ✔ | ✔ | ✔ | ✔ | — | `pos/webhook.py`, `receipts/inbound_webhook.py`, `pos/catalog_sync.py` (api); each worker's session factory | POS webhook/e2e suites; worker suites |
| `TOKEN_ENCRYPTION_KEY` | ✔ | ✔ | ✔ | — | — | — | `pos/router.py` (store), `pos/catalog_sync.py` (api); `pos/worker.py` (inbox); `pos/reconciliation.py` + `pos/token_refresh.py` (reconciliation) | `test_phase5_oauth.py`, `test_phase8_reconciliation.py`, POS e2e |
| `WORKOS_SECRET_KEY` | ✔ | — | — | — | — | — | `core/security.py:226,233`; `auth/router.py:271,279,310` | auth respx suites (`test_auth_refresh.py` et al.) |
| `ANTHROPIC_API_KEY` | ✔ | — | — | ✔ | — | — | `recipes/router.py:38-40` (suggestions); `workers/receipt_extraction_worker.py:51,80` | Sprint-5 phase-5 fake-LLM suite; `test_sprint6_phase3_extraction.py` (fake client) |
| `CLOVER_APP_ID`¹ | ✔ | — | ✔ | — | — | — | `pos/router.py:102-169` (OAuth); `pos/token_refresh.py:76` (refresh `client_id`) | `test_phase5_oauth.py:373,405` (respx refresh) |
| `CLOVER_APP_SECRET` | ✔ | — | — | — | — | — | `pos/router.py:170` — the ONLY consumer (OAuth code exchange) | `test_phase5_oauth.py` |
| `CLOVER_WEBHOOK_AUTH_CODE` | ✔ | — | — | — | — | — | `pos/webhook.py:81` — the ONLY consumer | POS webhook suites |
| `DO_SPACES_KEY`/`SECRET` (+endpoint/region/bucket config) | ✔ | — | — | ✔ | — | — | `receipts/services.py`, `receipts/router.py`, `receipts/inbound_webhook.py`, `observability/router.py` (api); `receipts/extraction_worker.py` (worker) | `test_sprint6_phase2_upload.py` (monkeypatched storage); phase3b |
| `POSTMARK_WEBHOOK_USER`/`PASSWORD` | ✔ | — | — | — | — | — | `receipts/inbound_webhook.py:89,118-119` — webhook Basic Auth (the fan-out worker reads inbox rows only) | `test_sprint6_phase3b_postmark.py` |
| `POSTMARK_INBOUND_ADDRESS` (configuration, NOT a credential) | ✔ (optional) | — | — | — | — | — | `receipts/inbound_admin.py:166,200` (reports `configured:false` without it) | inbound-admin tests |
| `TOKEN_ENCRYPTION_KEY_PREVIOUS` (optional, rotation) | when present | when present | when present | — | — | — | `core/encryption.py` | encryption rotation tests |

¹ Non-secret public identifier; listed because its **requirement** is component-scoped
(`when=CLOVER_ENABLED`). Historical overexposures this matrix removed: `CLOVER_APP_SECRET`
was in the inbox-worker's profile (no consumer — least-privilege violation), the api
profile lacked `ANTHROPIC_API_KEY` (real consumer at `recipes/router.py`), and
reconciliation-worker had **no profile and no boot `check_env` gate at all** (both added).

---

## 1. Role attributes

**[LOCAL]** for the local dev DB (where `reorderos_app` is a test LOGIN role). **[STAGING]:
NOT VERIFIED** — the DO `reorderos_app` LOGIN role does not exist until the runbook runs.

| Role | LOGIN | SUPERUSER | BYPASSRLS | INHERIT | Purpose | Provisioned by |
|---|---|---|---|---|---|---|
| `reorderos_app` | staging/prod: **out-of-band (NOT YET)**; local test: yes | **false** | **false** | yes | API request path (`DATABASE_URL`) | Out-of-band runbook (LOGIN+password); membership by fixtures/runbook — **[MIGRATION] does NOT create it** |
| `app_user` | no | false | false | yes | NOLOGIN group holding all request-path grants + RLS policies | Migration `0006`… **[MIGRATION]** |
| `service_worker` | yes | **false** | **false** | yes | Worker path (`SERVICE_DATABASE_URL`) — cross-tenant jobs | Migration `0006`; password rotated out-of-band |
| `doadmin` | yes | false¹ | **true** | yes | Cut-over env: migrations only (staging `migrate` PRE_DEPLOY job). Production (flag `false`): still ALL pools — its current, unchanged state | DigitalOcean managed cluster |

¹ On DO managed PG `doadmin` is not a true superuser (cannot `SET session_replication_role`)
but **owns the tables and has `rolbypassrls = TRUE`** — which is exactly why it must not
be a runtime connection role in a cut-over environment.

**Memberships [LOCAL]:** `reorderos_app` → member of `app_user`. `service_worker`
is **not** a member of `app_user` (its grants are direct and deliberately narrower on the
request-path tables). No other memberships into these groups.

**Why membership, not direct grants, for the request path:** `reorderos_app` inherits the
entire `app_user` grant set + RLS policies by being a member. The login role carries **zero**
direct table privileges of its own; rotating or replacing it changes nothing about the
authorization surface. This keeps "what the request path may do" defined in exactly one
place (`app_user`).

---

## 2. RLS policies changed by this PR **[MIGRATION]** (also **[LOCAL]**-verified)

Only two policies change. Everything else is untouched.

> **No `rls_mode` carve-out — deliberately.** An earlier draft added an
> `OR app.rls_mode IN ('register','accept_invite')` arm so the bootstrap
> `INSERT … RETURNING` could read its own new row. That arm does **not reference the
> row** — when the mode is set it evaluates true for *every* row, so any read of
> `tenants`/`user_tenants` while a bootstrap mode is live would return **all customers'
> rows**. That is the same anti-pattern as BYPASSRLS, just narrower, and it contradicts
> the PR's thesis. It was removed. Instead the two bootstrap handlers **bind the RLS
> context precisely** (below), so the existing row-scoped arms cover the RETURNING. Every
> arm of both policies references the row or the caller's own membership at all times.
> Regression-guarded by `test_register_mode_stays_row_scoped_no_foreign_leak` and
> `test_accept_invite_mode_stays_row_scoped_no_foreign_leak` (both **fail** against the
> carve-out version, **pass** against precise context).

### `tenants`
| Policy | CMD | Roles | Predicate (post-PR) |
|---|---|---|---|
| `tenant_select` | SELECT | **`app_user`** | `id = app.tenant_id` **OR** `EXISTS(active user_tenants for app.user_id)` |
| `tenant_insert` | INSERT | public | `WITH CHECK (app.rls_mode = 'register')` *(unchanged)* |
| `tenant_update` | UPDATE | public | `id = app.tenant_id` *(unchanged)* |

- **Pre-PR** `tenant_select` was `TO public` with predicate `id = app.tenant_id` only.
- **Only widening:** the membership arm (`EXISTS … user_tenants … app.user_id … active`).
  `/auth/me` and `GET /tenants` list a user's memberships with **no single active tenant** set;
  under `doadmin` these returned rows via BYPASSRLS, under a restricted role they returned
  **zero**. The arm restores exactly the caller's own tenants — and no more. It is row-scoped
  (references `tenants.id`) and the subquery is itself bounded by `user_tenants`' own RLS.
- **Scoped `TO app_user`** so the widening only reaches request-path members
  (`reorderos_app`). `service_worker` has **no SELECT grant on `tenants`**, so it is unaffected.
- **register RETURNING** is covered by `id = app.tenant_id`: `register_tenant` client-generates
  the tenant id and binds `app.tenant_id` to it **before** the `INSERT … RETURNING`
  (`tenants.repo.register_tenant`). `tenant_insert`'s WITH CHECK still gates the INSERT on
  `rls_mode = 'register'`.

### `user_tenants`
| Policy | CMD | Roles | Predicate (post-PR) |
|---|---|---|---|
| `user_tenant_select` | SELECT | public | `user_id = app.user_id` **OR** `tenant_id = app.tenant_id` |
| `user_tenant_insert` | INSERT | public | `WITH CHECK (app.rls_mode ∈ {register, accept_invite})` *(unchanged)* |
| `user_tenant_update` | UPDATE | public | `tenant_id = app.tenant_id` *(unchanged)* |

- **Byte-identical to the pre-0035 (0002) policy** — recreated by the migration only for
  explicitness; no widening.
- **accept-invite RETURNING** is covered by `user_id = app.user_id`: `accept_invitation` binds
  `app.user_id` to the invitee **after upserting the user, before** the membership
  `INSERT … RETURNING` (`invitations.repo.accept_invitation`). `register` binds `app.user_id`
  to the owner. `user_tenant_insert`'s WITH CHECK still gates the INSERT on the bootstrap modes.

**Downgrade** restores both predicates to their exact pre-0035 form (`tenant_select` back to
`TO public`, `id = app.tenant_id` only; `user_tenant_select` unchanged) and revokes the
`alembic_version` grant. It touches **no runtime role** (login roles are infra, not migration
state).

---

## 3. Grant delta introduced by this PR

Exactly **one** new grant:

| Object | Privilege | Grantee | Why (call site) |
|---|---|---|---|
| `alembic_version` | SELECT | `app_user` | API startup schema-head check (`_assert_schema_at_head`) reads `alembic_version` **on the request pool**. Without this, startup 500s the moment `DATABASE_URL` stops using `doadmin`. Also exercised by `test_schema_head_readable_under_reorderos_app`. |

No other grants are added, and **none are removed**, by this PR. The tables below are the
**pre-existing** grant surface (unchanged by this PR) included so the matrix is complete and
auditable.

---

## 4. Request-path grant surface — `app_user` (inherited by `reorderos_app`)

**[LOCAL]** (local dev DB). Legend: S=SELECT, I=INSERT, U=UPDATE, D=DELETE.

| Table | S | I | U | D |
|---|:-:|:-:|:-:|:-:|
| `alembic_version` | ✔ | | | | ← **new this PR** |
| `idempotency_keys` | ✔ | ✔ | ✔ | |
| `inbound_email_attachments` | ✔ | | | |
| `inbound_email_inbox` | ✔ | | | |
| `ingredient_cost_snapshots` | ✔ | ✔ | | | ← app_user-only (see §6) |
| `ingredients_master` | ✔ | ✔ | ✔ | ✔ |
| `inventory_count_events` | ✔ | ✔ | | |
| `inventory_items` | ✔ | ✔ | ✔ | ✔ |
| `inventory_movements` | ✔ | ✔ | | |
| `inventory_yield_factors` | ✔ | ✔ | ✔ | ✔ |
| `invitations` | ✔ | ✔ | ✔ | ✔ |
| `menu_items` | ✔ | ✔ | ✔ | |
| `modifier_drafts` | ✔ | ✔ | ✔ | ✔ |
| `modifier_ingredients` | ✔ | ✔ | | |
| `modifier_llm_suggestions` | ✔ | ✔ | | |
| `modifier_versions` | ✔ | ✔ | | |
| `modifiers` | ✔ | ✔ | ✔ | |
| `monitoring_alerts` | ✔ | ✔ | ✔ | ✔ |
| `oauth_states` | ✔ | ✔ | | ✔ |
| `orders` | ✔ | | | |
| `pos_event_inbox` | ✔ | | | |
| `pos_waitlist` | | ✔ | | |
| `receipt_adjustments` | ✔ | ✔ | | |
| `receipt_extraction_jobs` | ✔ | ✔ | | |
| `receipt_lines` | ✔ | ✔ | ✔ | ✔ |
| `receipts` | ✔ | ✔ | ✔ | ✔ |
| `recipe_drafts` | ✔ | ✔ | ✔ | ✔ |
| `recipe_ingredients` | ✔ | ✔ | ✔ | ✔ |
| `recipe_llm_suggestions` | ✔ | ✔ | | |
| `recipe_versions` | ✔ | ✔ | ✔ | ✔ |
| `recipes` | ✔ | ✔ | ✔ | |
| `sale_line_item_modifiers` | ✔ | | | |
| `sale_line_items` | ✔ | | | |
| `storage_zones` | ✔ | ✔ | ✔ | ✔ |
| `tenant_active_email_channel` | ✔ | ✔ | ✔ | ✔ |
| `tenant_extraction_rate_limits` | ✔ | | | |
| `tenant_gmail_connections` | ✔ | ✔ | ✔ | ✔ |
| `tenant_inbound_email_tokens` | ✔ | ✔ | ✔ | |
| `tenant_inbound_webhook_tokens` | ✔ | ✔ | ✔ | |
| `tenant_invoice_senders` | ✔ | ✔ | | ✔ |
| `tenant_item_purchase_conversions` | ✔ | ✔ | ✔ | |
| `tenant_pos_connections` | ✔ | ✔ | ✔ | |
| `tenants` | ✔ | ✔ | ✔ | |
| `unit_conversions` | ✔ | | | |
| `units_of_measure` | ✔ | ✔ | ✔ | ✔ |
| `user_tenants` | ✔ | ✔ | ✔ | ✔ |
| `users` | ✔ | ✔ | ✔ | |
| `vw_depletion_coverage` (view) | ✔ | | | |

---

## 5. Worker-path grant surface — `service_worker` (direct grants; NOT a member of `app_user`)

**[LOCAL]** (local dev DB). Deliberately narrower and intentionally cross-tenant. Workers
authorize via **role-scoped `USING(true)` policies granted `TO service_worker`** (e.g.
`tpc_service_access`, `inbox_service_access`) — a broad, whole-table policy tied to the
`service_worker` role itself, NOT a `app.rls_mode='service'` session flag (no such mechanism
exists). Worker isolation is by design (cross-tenant jobs) plus the narrower grant set below;
request-scoped RLS does not apply to this role.

| Table | S | I | U | D |
|---|:-:|:-:|:-:|:-:|
| `inbound_email_attachments` | ✔ | ✔ | ✔ | |
| `inbound_email_inbox` | ✔ | ✔ | ✔ | |
| `inventory_count_events` | ✔ | | | |
| `inventory_items` | ✔ | | | |
| `inventory_movements` | ✔ | ✔ | | |
| `inventory_yield_factors` | ✔ | | | |
| `menu_items` | ✔ | ✔ | | |
| `modifier_ingredients` | ✔ | | | |
| `modifier_versions` | ✔ | | | |
| `modifiers` | ✔ | ✔ | | |
| `monitoring_alerts` | ✔ | ✔ | ✔ | |
| `oauth_states` | ✔ | ✔ | | ✔ |
| `orders` | ✔ | ✔ | ✔ | |
| `pos_event_inbox` | ✔ | ✔ | ✔ | |
| `receipt_extraction_jobs` | ✔ | ✔ | ✔ | |
| `receipt_lines` | ✔ | ✔ | | |
| `receipts` | ✔ | ✔ | | |
| `recipe_ingredients` | ✔ | | | |
| `recipe_versions` | ✔ | | | |
| `sale_line_item_modifiers` | ✔ | ✔ | | |
| `sale_line_items` | ✔ | ✔ | | |
| `tenant_extraction_rate_limits` | ✔ | ✔ | ✔ | |
| `tenant_gmail_connections` | ✔ | | ✔ | |
| `tenant_inbound_email_tokens` | ✔ | | | |
| `tenant_inbound_webhook_tokens` | ✔ | | | |
| `tenant_invoice_senders` | ✔ | | | |
| `tenant_pos_connections` | ✔ | | ✔ | |
| `unit_conversions` | ✔ | | | |
| `units_of_measure` | ✔ | | | |

---

## 6. Sequences, functions, and deliberate exclusions

- **Sequences:** `role_usage_grants` returns **0 sequence grants** for both roles. All PKs are
  UUID (`gen_random_uuid()`), so there are no sequences to grant. Nothing to add; nothing
  missing. *(If a future table uses an identity/serial column, that sequence will need
  `USAGE` — flagged here so it isn't silently forgotten.)*
- **Function `EXECUTE`:** exactly **one** non-default grant each —
  `lookup_tenant_by_merchant` to **both** `app_user` and `service_worker`. No others. (All
  other functions rely on the default `PUBLIC` EXECUTE and are not part of the restricted-role
  surface.)
- **Deliberate exclusion — `ingredient_cost_snapshots` is NOT granted to `service_worker`.**
  [LOCAL]: `service_worker` has no privilege on this table. Cost snapshots are written
  on the **request path** (`commit_receipt` runs under `app_user`), so a worker grant would be
  privilege with no call site. Left ungranted by design.

### Privileges present but not tied to a verified call site (flagged, not changed)

These are pre-existing (not touched by this PR) and are called out for audit follow-up — none
block this PR:

- `app_user` has `DELETE` on `oauth_states` and `pos_waitlist` has only `INSERT` for
  `app_user` — verify these still match live call sites during the next grant audit.
- `service_worker` `INSERT` on `sale_line_items` / `sale_line_item_modifiers` / `orders` is POS
  ingest; confirm the reconciliation worker still writes these post-refactor.

These are **observations for a future least-privilege audit**, not part of the security PR's
change set.

---

## Appendix — reproduce this matrix

```sql
-- role attributes
SELECT rolname,rolsuper,rolbypassrls,rolcanlogin,rolinherit FROM pg_roles
 WHERE rolname IN ('app_user','service_worker','reorderos_app','doadmin');
-- memberships
SELECT m.rolname member, g.rolname grp FROM pg_auth_members am
 JOIN pg_roles m ON m.oid=am.member JOIN pg_roles g ON g.oid=am.roleid
 WHERE g.rolname IN ('app_user','service_worker');
-- table grants
SELECT grantee,table_name,string_agg(privilege_type,',' ORDER BY privilege_type)
 FROM information_schema.role_table_grants
 WHERE grantee IN ('app_user','service_worker') AND table_schema='public'
 GROUP BY grantee,table_name ORDER BY table_name,grantee;
-- sequences / functions
SELECT grantee,object_name FROM information_schema.role_usage_grants
 WHERE grantee IN ('app_user','service_worker') AND object_type='SEQUENCE';
SELECT grantee,routine_name FROM information_schema.role_routine_grants
 WHERE grantee IN ('app_user','service_worker') AND privilege_type='EXECUTE'
   AND specific_schema='public';
-- policies
SELECT tablename,policyname,cmd,roles,qual,with_check FROM pg_policies
 WHERE tablename IN ('tenants','user_tenants');
```

---

## 7. Application-code correctness required by the restricted role **[LOCAL]**

Moving the request pool off `doadmin` (bypassrls) to `reorderos_app` exposes code paths that
only worked because RLS was bypassed. These are fixed and regression-tested in this PR.

- **Post-commit RLS context reversion (fail-closed regression).** `get_rls_session` sets context
  via `SET LOCAL`, which reverts at the first `commit()`. Three handlers did tenant-scoped DB
  work *after* their first commit with empty context — harmless under bypassrls, but a **500 /
  blocked write** under `reorderos_app`: `inventory.router.create_count_event` (post-commit
  read-back), `create_opening_balance` (idempotency write), `create_receipt_endpoint`
  (post-commit read-back). Fixed by re-establishing `set_rls_context` after the commit (the
  `/auth/me` idiom). Proven fail-before/pass-after by
  `test_inventory_writes_work_under_restricted_role` + `test_receipts_read_scoped_and_denied`.
- **Test-harness vacuity (found + fixed).** The restricted-role harness first used `SET ROLE
  reorderos_app` on a superuser connection via a `connect` event. `SET ROLE` is lost when a
  pooled connection is **reused**, so any request that opened a 2nd session (resolve_principal
  then the handler) silently ran the handler as the **bypassrls superuser** — making HTTP tests
  pass vacuously. Fixed: the harness now connects **directly** as a real `reorderos_app` LOGIN
  role, so every connection is genuinely restricted. `test_test_connection_is_reorderos_app`
  pins `current_user=reorderos_app`, not super, not bypass.
- **Migration silent-rollback (found + fixed).** The `assert_migration_capability_sync` preflight
  queried the migration connection, opening a transaction Alembic declined to commit → the
  migration logged success and exited 0 while rolling back. Fixed by running the preflight on a
  separate connection. Guarded by `test_migration_persists_under_production_env`.
- **Capability preflight — scope narrowed.** `assert_migration_capability_sync` checks CREATE on
  the database + `public` schema only. It is a **basic CREATE-capability smoke test**, NOT proof
  of ownership or ALTER/DROP over every migrated object. Documented as such in the code; a
  migration that ALTERs an object owned by another role can still fail after this passes
  (transactional DDL rolls back cleanly).

### Session-context lint — what it detects, and what it does NOT prove

- **Post-commit lint — checked in:** `tests/test_post_commit_rls_audit.py` is an **AST lint run
  in CI**. Precise claim: *it detects the enumerated direct post-commit session operations* — for
  a function that establishes request RLS context (depends on `get_rls_session` or calls a
  context setter), after `await <s>.commit()` and before that session's context is
  re-established, it flags `await <s>.{execute,scalar,scalars,flush,refresh,get,delete,merge}(…)`
  (including nested forms like `(await <s>.execute(…)).scalar_one()`) and any
  `await helper(… <s> …)` that passes the committed session. It tracks context **per session**
  (resetting session A does not clear a committed session B). Verified fail-before/pass-after:
  against the pre-fix tree it flags exactly `inventory/router.py:107, :236, :264`; fixed tree
  passes; the four enumerated bad patterns are biting self-tests.
- **This lint does NOT prove** that every tenant-scoped request restores context. It is not
  path-sensitive (branches flattened), does not resolve aliasing, scopes out service/worker
  paths (`service_worker` + `USING(true)` policies, no request SET-LOCAL context), and treats
  session-passing helper calls conservatively (may over-flag). **The primary evidence is the
  restricted-role HTTP/integration tests** (§7 above, run as genuine `reorderos_app`); the lint
  is a regression tripwire for the enumerated shapes, not a proof of the invariant.
- *Follow-up (not this PR):* centralizing context restoration in the session infrastructure so
  correctness does not depend on a manual re-set after every commit.
- **Bootstrap-mode setters — enumerated [LOCAL]:** the only setters of a bootstrap `app.rls_mode`
  are `register_tenant` (register) and `accept_invitation` (accept_invite), each a single call
  site, both precisely bound (see §2). Reproduce:
  `grep -rn "set_identity_context\|set_accept_invite_context\|set_config('app.rls_mode'" app`.

**POS request-path RLS — corrected [LOCAL].** The earlier draft conflated two policies. The live
per-role policies on `tenant_pos_connections` and `pos_event_inbox` are:

| Table | Policy | CMD | Role | USING |
|---|---|---|---|---|
| `tenant_pos_connections` | `tpc_tenant_isolation` | ALL | **`app_user`** | `tenant_id = app.tenant_id` |
| `tenant_pos_connections` | `tpc_service_access` | ALL | `service_worker` | `true` |
| `pos_event_inbox` | `inbox_tenant_isolation` | ALL | **`app_user`** | `tenant_id = app.tenant_id` |
| `pos_event_inbox` | `inbox_service_access` | ALL | `service_worker` | `true` |

So the **request path (`reorderos_app`/`app_user`) is RLS-tenant-scoped** on these tables —
`USING(true)` belongs to the **`service_worker`** policy, not the request path, and there is **no
`app.rls_mode='service'` mechanism** (authorization is by role-scoped policy). The POS handlers
set only `app.tenant_id` on a raw session, which is exactly (and only) what the `app_user`
policy keys on, so the read is correctly isolated by RLS; cross-tenant access is additionally
denied at principal resolution (403). Behaviorally covered by
`test_pos_status_under_restricted_role`.

### Function EXECUTE — PUBLIC default vs explicit grants, and SECURITY DEFINER ACLs [LOCAL]

- **Default `PUBLIC EXECUTE`:** ordinary SQL functions with `proacl IS NULL` are executable by
  `PUBLIC` (hence by `reorderos_app`/`service_worker`) — this is Postgres default and is NOT an
  explicit grant. It is called out so the explicit-grant list is not mistaken for the complete
  callable surface.
- **Explicit EXECUTE grant:** exactly one — `lookup_tenant_by_merchant` to `app_user` +
  `service_worker` (used by the POS webhook to resolve a tenant by merchant id).
- **SECURITY DEFINER ACL proof [LOCAL]:** exactly **one** SECURITY DEFINER function exists —
  `lookup_tenant_by_merchant`, owned by `reorderos` (superuser). Its ACL is **explicit and
  non-PUBLIC**: `{reorderos=X, app_user=X, service_worker=X}` — PUBLIC has no EXECUTE. So the one
  privilege-escalating function is intentionally ACL'd to exactly the two runtime roles that call
  it, and cannot be invoked by an unprivileged/PUBLIC caller. Reproduce:
  `SELECT proname, proacl, prosecdef FROM pg_proc WHERE prosecdef AND pronamespace='public'::regnamespace`.

### Scope boundary — what this cutover does and does NOT constrain

This PR constrains the **ordinary request pool** (`api.DATABASE_URL` → `reorderos_app`, RLS
enforced). It is **not** a process-compromise / RCE boundary: the `api` process still holds
`SERVICE_DATABASE_URL` (the `service_worker` DSN, cross-tenant) because some endpoints legitimately
use the service session. Code that can execute arbitrary Python in the `api` process can still open
the service pool. The value delivered is: normal request-path SQL is now RLS-isolated instead of
running as a bypassrls superuser — defense against SQL-scoping bugs and missing `WHERE` clauses,
not against full process compromise. Narrowing/removing the service DSN from the `api` component is
a separate, larger change (out of scope here).

---

## 8. Staging certification — **NOT RUN**

This section stays `NOT RUN` until the approved DO cutover (runbook §4). It records only
**non-secret** evidence. **No [STAGING] evidence exists yet** — nothing below is filled in.

| Item | Expected | Recorded |
|---|---|---|
| Deployment SHA | (git SHA of the deployed branch) | `NOT RUN` |
| `api` request pool `current_user` | `reorderos_app` | `NOT RUN` |
| `api` service pool `current_user` | `service_worker` | `NOT RUN` |
| each worker `current_user` | `service_worker` | `NOT RUN` |
| `rolsuper` (all runtime pools) | `false` | `NOT RUN` |
| `rolbypassrls` (all runtime pools) | `false` | `NOT RUN` |
| `reorderos_app` ∈ `app_user` | `true` | `NOT RUN` |
| migration head | `0035_restricted_runtime_role` | `NOT RUN` |
| API `/health/ready` | `200` | `NOT RUN` |
| authenticated own-tenant smoke (`/auth/me` tenants) | `>= 1` | `NOT RUN` |
| foreign-tenant denial (`X-Tenant-Id` non-member) | `403` | `NOT RUN` |
| each enabled worker reached `<worker>.starting` after its role gate | yes (all 4) | `NOT RUN` |
