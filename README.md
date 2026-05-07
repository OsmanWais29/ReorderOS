# ReOrderOS

Canadian restaurant inventory and reorder platform. iOS-first, Clover-native, bilingual (EN/FR).

## Repository layout

```
ReOrderOS/
├── backend/          FastAPI monolith — auth, tenants, invitations (Sprint 2 complete)
├── frontend/         React Native (Expo) mobile app
├── decisions/        Architecture decision records — read before changing anything
├── docs/             Deployment guides, environment variables reference
│   └── archive/      Stale design docs kept for historical reference
├── .do/              DigitalOcean App Platform spec
└── .github/          CI (backend-ci.yml) and deploy workflows
```

## Sprints

| Sprint | Scope | Status |
|--------|-------|--------|
| 1 | Platform skeleton — health, version, DO deploy | ✅ Done |
| 2 | WorkOS JWT auth, multi-tenant RLS, invitations, RBAC | ✅ Done |
| 3 | Clover POS sync, inventory, stock levels, purchase orders | 🔜 Next |
| 4 | Sales analytics, forecasting, supplier management | Planned |
| 5 | Onboarding flow, billing (Stripe), notifications | Planned |

## Quick start

```bash
# Backend
cd backend
make install       # create venv + install deps
make up            # start local Postgres (Docker)
make migrate       # apply Alembic migrations
make dev           # run FastAPI on :8000

# Frontend
cd frontend
npm install
npx expo start --web --port 3000
```

## Key references

- [Decisions index](decisions/README.md) — v1 scope, API surface, risk register
- [Environment variables](docs/ENVIRONMENT_VARIABLES.md) — every env var explained
- [DO deployment](docs/deploy-digitalocean.md) — DigitalOcean App Platform setup
- [Backend README](backend/README.md) — module rules, testing guide, migration workflow
- [Product bible](docs/archive/ReOrderos/bible.txt) — full PRD (4 761 lines)

## Live environment

- **API**: https://reorderos-api-7d4et.ondigitalocean.app
- **Health**: https://reorderos-api-7d4et.ondigitalocean.app/health/ready
- **Region**: Toronto (tor1)
- **DB**: Managed PostgreSQL 17 — `reorderos-dev-pg`
