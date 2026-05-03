# ReorderOS Backend

FastAPI modular monolith. Python 3.12, SQLAlchemy 2 async, Alembic, Pydantic v2.

## One-command local start

```bash
make dev
```

This brings up Postgres 16 in Docker, runs migrations, and starts the API at `http://localhost:8000`.

## Manual

```bash
# 1. Create venv and install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Bring up Postgres
docker compose up -d db

# 3. Apply migrations
alembic upgrade head

# 4. Run API
uvicorn app.main:app --reload --port 8000
```

## Health checks

- `GET /health/live` — process is up.
- `GET /health/ready` — process is up **and** Postgres is reachable.

## Project layout

```
backend/
  app/
    main.py                 # FastAPI factory + router wiring
    core/                   # cross-cutting: config, db, logging, security, RLS, idempotency
    modules/                # one folder per bounded context
      auth/ tenants/ users/ rbac/
      pos_clover/ inventory/ recipes/ sales/
      receipts/ purchase_orders/ forecasting/
      dashboard/ stock/ settings/ exports/
      notifications/ outbox/ observability/
  alembic/                  # migrations
  tests/                    # pytest-asyncio
  scripts/                  # operational helpers (export OpenAPI, etc.)
  docker-compose.yml
  Makefile
```

## Testing

```bash
make test       # unit only (in-memory)
make test-int   # integration tests against docker compose Postgres
```

## Module rules

- Modules may depend inward on `app.core`, never on each other's internals.
- Cross-module writes go through service functions with explicit transaction boundaries.
- Tenant-scoped tables enable Postgres RLS keyed on `app.tenant_id`.
- Append-only tables (`inventory_movements`, `outbox_events`, `pos_event_inbox`,
  `admin_audit_log`, `ingredient_prices`, `batch_runs`) reject UPDATE/DELETE at the DB layer.

See `decisions/v1-scope.md` for product locks and `decisions/api-surface-inventory.md` for the
endpoint contract.

