# V7 — Create a staging environment (first-time, click-by-click)

Goal: a SECOND DigitalOcean app + a SEPARATE non-production database, running the
`sprint-5-recipe-depletion` branch, where the Clover sandbox afternoon happens — so prod is never
where you discover a parser mismatch or run these migrations for the first time.

Mirrors the real prod spec (`.do/app.yaml`): two components — an `api` web service and an
`inbox-worker` — both built from `backend/Dockerfile`, plus a managed Postgres DB. The web
Dockerfile runs `alembic upgrade head` on boot, so **migrations apply automatically on first
deploy** (no separate migration step).

---

## ⚠️ READ FIRST — F1.3 fail-closed: set these 7 or the app won't boot

We set staging to `APP_ENV=production` (deliberate — see "Why APP_ENV=production" below). That turns on
the fail-closed validator: **if any of these 7 are missing, the app crashes on startup** (you'd see the
deploy fail / container restart-loop, not a helpful error). Set all 7 BEFORE you deploy:

| # | Key | Type | Staging value / where to get it |
|---|---|---|---|
| 1 | `TOKEN_ENCRYPTION_KEY` | SECRET | a Fernet key — generate: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| 2 | `SERVICE_DATABASE_URL` | SECRET | connection string to the staging DB as the `service_worker` role (see Step 3c) |
| 3 | `WORKOS_CLIENT_ID` | value | reuse prod: `client_01KQT0CRCZP8W8AMAYE5Y1F9SP` |
| 4 | `WORKOS_JWKS_URL` | value | `https://api.workos.com/sso/jwks/client_01KQT0CRCZP8W8AMAYE5Y1F9SP` |
| 5 | `CLOVER_APP_ID` | value | reuse prod: `DJFFAT14DS7QM` (or your own sandbox app id) |
| 6 | `CLOVER_APP_SECRET` | SECRET | from the Clover dev dashboard (App Settings) |
| 7 | `CLOVER_WEBHOOK_AUTH_CODE` | SECRET | a string YOU choose; set the same value in the Clover webhook config |

(Plus the non-required envs in Step 3 — DATABASE_URL is auto-bound; the Clover base URLs, WorkOS issuer,
CORS, etc.)

**Why `APP_ENV=production` on staging** (not `staging`): two reasons — (1) it makes staging *faithful*
(F1.3 enforced, same code paths as prod), and (2) the DB-SSL handling in `database.py` only applies its
SSL context when `is_production` is true; DO managed Postgres requires SSL, so `APP_ENV=staging` would
likely fail to connect. Staging is a *separate app + separate DB*, so "production" semantics here are
safe.

---

## Step 1 — Create the staging database (non-production)

1. DO console → **Databases** → **Create Database Cluster**.
2. Engine **PostgreSQL**, version **17**, region **tor1** (match prod). Smallest plan is fine
   (Basic, 1 GB). Name it **`reorderos-staging-pg`**.
3. **Create** and wait ~5 min for it to provision.
4. Open the cluster → **Connection Details** → keep this tab; you'll need the host/port/user/password
   and the `doadmin` connection string.

## Step 2 — Create the staging app from the branch

1. DO console → **Apps** → **Create App**.
2. Source: **GitHub** → repo **`OsmanWais29/ReorderOS`** → branch **`sprint-5-recipe-depletion`**
   (NOT main). Leave "Autodeploy on push" ON (optional) so re-pushes redeploy staging.
3. DO will detect the Dockerfile. Set **Source Directory = `backend`** and confirm it uses
   `backend/Dockerfile`. This first component is your **web service** — rename it **`api`**.
   - HTTP port **8080**.
   - Health check path **`/health/ready`** (important — `/health` alone 404s).
   - Plan: Basic (basic-xxs is what prod uses).
4. **Add the worker component:** in the app's Components, **Create Component → Worker** (or "Add
   Resource → Worker"), same repo/branch, **Source Directory `backend`**, same `backend/Dockerfile`,
   and **Run Command:** `python -m app.workers.inbox_worker`. Name it **`inbox-worker`**.
5. **Attach the database:** add the existing managed DB `reorderos-staging-pg` to the app (Create
   Component → Database → use existing, or "Add Resource → Database"). DO will expose it to components
   as `${reorderos-staging-pg.DATABASE_URL}`.

## Step 3 — Set environment variables (BEFORE first deploy)

Set these on **both** the `api` and `inbox-worker` components (App → Settings → each component → Env
Vars). Mark the SECRET ones as encrypted.

**a) Required by F1.3 (the 7 above)** — set all 7. For `DATABASE_URL`, bind the DB:
```
DATABASE_URL = ${reorderos-staging-pg.DATABASE_URL}      # both components
APP_ENV = production                                      # both (RUN_AND_BUILD_TIME)
```
**b) Clover + WorkOS non-secrets** (reuse prod values, sandbox):
```
CLOVER_ENVIRONMENT       = sandbox
CLOVER_API_BASE_URL      = https://apisandbox.dev.clover.com
CLOVER_OAUTH_BASE_URL    = https://sandbox.dev.clover.com
WORKOS_ISSUER            = https://api.workos.com/user_management/client_01KQT0CRCZP8W8AMAYE5Y1F9SP
WORKOS_VERIFY_AUDIENCE   = false
CORS_ORIGINS             = ["http://localhost:8081"]
CLOVER_POST_CONNECT_REDIRECT = http://localhost:8081/onboarding/found-summary
```
`CLOVER_OAUTH_CALLBACK_URL` — you don't know the staging URL yet; **leave it for Step 5** (set it once
DO assigns the app URL), then redeploy.

**c) `SERVICE_DATABASE_URL`** (the worker connects as the `service_worker` role). The migrations create
that role, but you must give it a password to log in:
- After the first deploy runs migrations (Step 4), open the DB (DO console DB → or `psql` with the
  `doadmin` string) and run:
  `ALTER ROLE service_worker WITH LOGIN PASSWORD '<choose-a-password>';`
- Build the URL like the `doadmin` one but with `service_worker` + that password:
  `postgresql://service_worker:<password>@<host>:<port>/<db>?sslmode=require`
- Set that as `SERVICE_DATABASE_URL` (SECRET) on both components, then redeploy.
- *Quick-but-less-faithful fallback to unblock the afternoon:* set `SERVICE_DATABASE_URL` = the
  `doadmin` URL. The pipeline works, but the worker runs as admin so it won't catch a missing-grant
  bug. Prefer the real `service_worker` role; note the trade-off if you use the fallback.

> Chicken-and-egg note: `SERVICE_DATABASE_URL` is one of the F1.3-required 7, so the very first boot
> needs *a* value. Set it to the `doadmin` URL to get the first deploy/migrations up, then switch it to
> the `service_worker` URL once that role has a password, and redeploy.

## Step 4 — Deploy → migrations run automatically

1. Click **Deploy** (or it deploys on create). Watch the build + deploy logs.
2. On boot the web container runs `alembic upgrade head` → applies `0014 … 0021` to the **staging** DB
   (which starts empty, so the data-validating migrations validate against zero rows).
3. ✅ Success = the `api` component goes **healthy** (`/health/ready` passing) and `inbox-worker` shows
   **running**. ❌ If the deploy fails on boot, the usual cause is a missing F1.3 secret (Step 3a) or a
   bad `SERVICE_DATABASE_URL` — check the deploy logs for the config error.

Confirm migrations landed (psql with the `doadmin` string):
```sql
SELECT version_num FROM alembic_version;   -- expect 0021 (current head)
```

## Step 5 — Wire Clover to the staging URL

1. Copy the staging app URL DO assigned (e.g. `https://reorderos-staging-xxxxx.ondigitalocean.app`).
2. Set `CLOVER_OAUTH_CALLBACK_URL = https://<staging-url>/api/v1/pos/clover/callback` on the `api`
   component → redeploy.
3. In the **Clover dev dashboard** (App Settings), point the **redirect URL** to that exact callback,
   and the **webhook URL** to `https://<staging-url>/api/v1/webhooks/pos/clover`. (Same Clover sandbox
   app is fine since prod was never connected; or use a separate sandbox app for staging.)
4. Reachability check against staging:
   ```
   curl -sS -X POST https://<staging-url>/api/v1/webhooks/pos/clover \
        -H 'Content-Type: application/json' -d '{"verificationCode":"staging-ping"}'
   ```
   ✅ echoes `staging-ping`.

## Step 6 — Run the afternoon against staging

Now follow `v7-clover-sandbox-walkthrough.md` with `API=https://<staging-url>`. Confirm at Step 4.3 that
`connect-url` returns **`/oauth/v2/authorize`** (the fix is on this branch, so staging has it — unlike
prod). Find/fix whatever the §C shapes and the token-host flag surface, here, safely.

## Only then — production
Once one real sale is green end-to-end on staging and §C is closed, we prepare the **production release
checklist** (merge `sprint-5-recipe-depletion` → `main`, prod-version pre-check, the same 7 secrets on
prod, smoke test, rollback) and promote the proven version. Not before.

---

### Alternative fast path (if you prefer the CLI to clicking)
Everything above can instead be one command from a `.do/staging.app.yaml` (a copy of `.do/app.yaml`
with `name: reorderos-staging`, `branch: sprint-5-recipe-depletion`, the staging DB cluster, and
`APP_ENV=production`): `doctl apps create --spec .do/staging.app.yaml` (after `doctl auth init`). Ask
and I'll generate that spec file.
