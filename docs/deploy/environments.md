# ReorderOS environments — production vs staging

A deliberate separation so **production stays clean and easy to reason about**, and all risky
first-time testing happens in **staging**.

## The two environments

| | **Production** | **Staging** |
|---|---|---|
| DO App | `reorderos-api` (`6a6930d9-…`) | `reorderos-staging` (`70e351c1-…`) |
| DO Project | **ReOrderOS** | **ReOrderOS** |
| Deploys branch | **`main`** | **`sprint-5-recipe-depletion`** (the in-flight branch) |
| App spec | `.do/app.yaml` | `.do/staging.app.yaml` |
| Database | `reorderos-dev-pg` (prod data) | `reorderos-staging-pg` (throwaway) |
| URL | https://reorderos-api-7d4et.ondigitalocean.app | https://reorderos-staging-3ez2g.ondigitalocean.app |
| Clover | the real merchant integration | sandbox test merchant |
| Purpose | **production-grade — keep clean** | **testing — break things here first** |

## Rules of the distinction (from now on)

1. **Test in staging first.** First-time integrations, migrations, Clover sandbox, parser-shape
   discovery — all happen on staging. Production is only touched once a change is proven on staging.
2. **Production is promoted, not experimented on.** Changes reach prod by: merge the proven branch →
   `main`, deploy, run migrations — following a release checklist (see `v7-clover-cert-checklist.md`
   and the prod-release section below). No live trial-and-error on prod.
3. **Never put secret values in files or chat.** Secrets are set only in the DO **console** (encrypted),
   per environment. The committed specs (`.do/*.yaml`) declare secrets as `type: SECRET` with **no
   values**. (A staging secret leak happened once via inline-in-file editing — don't repeat it.)
4. **Each secret in exactly one place per component.** Either app-level (inherited by all components)
   OR per-component — not both (a dangling/duplicate state caused hours of staging debugging).
5. **Keep prod's config minimal and legible.** Prefer app-level for genuinely shared config; only the
   handful of api-only vars live on the api component. The worker carries only what it uniquely needs.

## Hard-won lessons to apply when setting up / cleaning prod

- **Migrations need a `CREATEROLE` DB user.** Migration 0002 does `CREATE ROLE app_user/service_worker`.
  The DO-bound *restricted* app user can't create roles → migrations fail. `DATABASE_URL` must be a
  user with `CREATEROLE` (doadmin) — which also matches prod's "app connects as doadmin" reality.
- **`APP_ENV=production` is required for the DB-SSL path.** `database.py`/`service_db.py` only attach the
  SSL context when `is_production`; DO managed Postgres requires SSL. (Staging uses `production` too, for
  faithfulness + SSL.)
- **All 7 F1.3-required secrets must be reachable by EVERY `production` process** (api AND worker), or
  the component crashes at config validation: `TOKEN_ENCRYPTION_KEY`, `SERVICE_DATABASE_URL`,
  `WORKOS_CLIENT_ID`, `WORKOS_JWKS_URL`, `CLOVER_APP_ID`, `CLOVER_APP_SECRET`, `CLOVER_WEBHOOK_AUTH_CODE`.
- **Clover webhook auth code is Clover-generated** — copy it from the Clover dashboard into
  `CLOVER_WEBHOOK_AUTH_CODE`; you don't invent it.
- **Two DB roles by design:** both `DATABASE_URL` and `SERVICE_DATABASE_URL` are connection strings to
  the *same* DB — they differ only in the **role (username)** they log in as. `DATABASE_URL` = `doadmin`
  (admin; runs migrations incl. `CREATE ROLE`; bypasses RLS). `SERVICE_DATABASE_URL` = `service_worker`
  (least-privilege; only the grants migrations gave it; subject to RLS) — used by worker + webhook.
  `service_worker` is created by migration `0006` (already `LOGIN`), so the URL is just the doadmin URI
  with `doadmin:<pw>` swapped for `service_worker:<pw>` (same host/port/db/`?sslmode=require`). Staging
  may run with `SERVICE_DATABASE_URL`=doadmin as a fallback — when it does, the worker bypasses RLS, so
  a Tier-2 sim won't exercise `service_worker` grants; switch to the `service_worker` role to close that.

## ⚠️ PROD-RELEASE CHECKLIST items (do NOT inherit staging shortcuts)
- **`service_worker` password:** migration `0006` hardcodes `CREATE ROLE service_worker LOGIN PASSWORD
  'service_worker'` — a known weak default. Acceptable for the isolated staging testbed; **for prod, run
  `ALTER ROLE service_worker WITH PASSWORD '<strong-secret>'`** and use that strong password in prod's
  `SERVICE_DATABASE_URL`. Never ship the hardcoded `'service_worker'` password to production.
- **`SERVICE_DATABASE_URL` must be the `service_worker` role in prod** (not the doadmin fallback) so the
  worker runs under real least-privilege + RLS.
- `DATABASE_URL` in prod must be a `CREATEROLE` user (doadmin) so migrations can create roles.
- Set all 7 F1.3 secrets (console only) reachable by every process; `APP_ENV=production`.

## Current staging status (2026-06-17)
api + inbox-worker both ACTIVE/healthy; DB at migration head `0021`; ready for the V7 Clover-sandbox
afternoon. Known minor untidiness: `CLOVER_APP_SECRET`/`CLOVER_WEBHOOK_AUTH_CODE` declared at both
app-level and component-level (working via the component value; tidy in a later clean spec pass).
