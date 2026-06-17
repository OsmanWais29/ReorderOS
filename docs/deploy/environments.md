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
- **Two DB roles by design:** `DATABASE_URL` = app (api + migrations); `SERVICE_DATABASE_URL` = worker +
  webhook (intended role `service_worker`, least-privilege; staging currently uses doadmin for both —
  prod should use the real `service_worker` role for the worker to exercise least-privilege grants).

## Current staging status (2026-06-17)
api + inbox-worker both ACTIVE/healthy; DB at migration head `0021`; ready for the V7 Clover-sandbox
afternoon. Known minor untidiness: `CLOVER_APP_SECRET`/`CLOVER_WEBHOOK_AUTH_CODE` declared at both
app-level and component-level (working via the component value; tidy in a later clean spec pass).
