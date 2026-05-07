# Environment Variables

All variables required to run ReOrderOS. Backend vars live in `backend/.env` locally
and in the DigitalOcean App Platform console / `app.yaml` in production.

---

## Backend (`backend/.env`)

### App

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `APP_ENV` | `local` | No | `local` / `ci` / `staging` / `production` |
| `APP_LOG_LEVEL` | `INFO` | No | `DEBUG` / `INFO` / `WARNING` |
| `APP_PORT` | `8000` | No | Uvicorn listen port (local only) |

### Database

| Variable | Example | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://reorderos:reorderos@localhost:5433/reorderos` | **Yes** | Async SQLAlchemy DSN. DO App Platform injects `postgresql://` automatically — the app upgrades it to `+asyncpg` at startup. |

### WorkOS Auth (Sprint 2+)

| Variable | Example | Required | Description |
|----------|---------|----------|-------------|
| `WORKOS_CLIENT_ID` | `client_01KQT0CRCZP8W8AMAYE5Y1F9SP` | **Yes** | WorkOS app client ID (non-secret, safe to commit) |
| `WORKOS_JWKS_URL` | `https://api.workos.com/sso/jwks/client_01KQT0...` | **Yes** | JWKS endpoint for RS256 key verification. Format: `/sso/jwks/{client_id}` |
| `WORKOS_ISSUER` | `https://api.workos.com/user_management/client_01KQT0...` | **Yes** | Must match the `iss` claim in WorkOS User Management JWTs exactly. Format: `https://api.workos.com/user_management/{client_id}` |
| `WORKOS_SECRET_KEY` | `sk_test_...` or `sk_live_...` | **Yes** | WorkOS API secret key. Used to exchange PKCE codes for tokens and fetch user profiles. **Never commit. Set via DO console as SECRET.** |
| `WORKOS_VERIFY_AUDIENCE` | `false` | No | Set `true` in production to validate the `aud` claim against `WORKOS_CLIENT_ID` |

### Object Storage — DigitalOcean Spaces (Sprint 6+)

| Variable | Example | Required | Description |
|----------|---------|----------|-------------|
| `DO_SPACES_ENDPOINT` | `https://nyc3.digitaloceanspaces.com` | No | Spaces regional endpoint |
| `DO_SPACES_REGION` | `nyc3` | No | Spaces region (Spaces not available in tor1) |
| `DO_SPACES_BUCKET` | `reorderos-receipts` | No | Bucket name for receipt photo uploads |
| `DO_SPACES_KEY` | `DO00...` | No | Spaces access key ID. **Set via DO console as SECRET.** |
| `DO_SPACES_SECRET` | `...` | No | Spaces secret access key. **Set via DO console as SECRET.** |

---

## Frontend (`frontend/src/auth/config.ts`)

These are build-time constants (not `.env` vars) — safe to commit since WorkOS client IDs are not secret.

| Constant | Value | Description |
|----------|-------|-------------|
| `WORKOS_CLIENT_ID` | `client_01KQT0CRCZP8W8AMAYE5Y1F9SP` | WorkOS app client ID |
| `WORKOS_AUTH_URL` | `https://api.workos.com/user_management/authorize` | WorkOS authorization endpoint |
| `API_BASE` | `https://reorderos-api-7d4et.ondigitalocean.app` | Backend base URL |
| `REDIRECT_URI` | `http://localhost:3000/callback` | OAuth callback (registered in WorkOS Dashboard → Authentication → Redirects) |

---

## CI (`github/workflows/backend-ci.yml`)

Fake values used so Settings validation passes in CI — no real WorkOS calls are made.

| Variable | CI Value |
|----------|----------|
| `WORKOS_CLIENT_ID` | `client_ci_fake` |
| `WORKOS_JWKS_URL` | `https://api.workos.com/sso/jwks/client_ci_fake` |
| `WORKOS_ISSUER` | `https://api.workos.com/user_management/client_ci_fake` |
| `WORKOS_VERIFY_AUDIENCE` | `false` |

---

## DigitalOcean App Platform

Set these in the DO console (not in `app.yaml` to avoid overwriting secrets):

| Variable | How to set | Notes |
|----------|-----------|-------|
| `DATABASE_URL` | Auto-injected by DO when the managed DB is bound | Uses `${reorderos-dev-pg.DATABASE_URL}` interpolation |
| `WORKOS_SECRET_KEY` | **DO console → App → Settings → Components → api → Environment Variables** | Mark as **Secret** |
| `DO_SPACES_KEY` | DO console, mark as Secret | Sprint 6+ |
| `DO_SPACES_SECRET` | DO console, mark as Secret | Sprint 6+ |

---

## Local dev minimal `.env`

```env
DATABASE_URL=postgresql+asyncpg://reorderos:reorderos@localhost:5433/reorderos
WORKOS_CLIENT_ID=client_01KQT0CRCZP8W8AMAYE5Y1F9SP
WORKOS_JWKS_URL=https://api.workos.com/sso/jwks/client_01KQT0CRCZP8W8AMAYE5Y1F9SP
WORKOS_ISSUER=https://api.workos.com/user_management/client_01KQT0CRCZP8W8AMAYE5Y1F9SP
WORKOS_SECRET_KEY=sk_test_...
WORKOS_VERIFY_AUDIENCE=false
```
