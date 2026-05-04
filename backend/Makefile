SHELL := /bin/bash
PY := python3
VENV := .venv
ACT := source $(VENV)/bin/activate
COMPOSE := docker compose

.PHONY: help venv install dev up down db-wait migrate downgrade revision lint typecheck test test-int openapi clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

venv: ## Create local virtualenv
	$(PY) -m venv $(VENV)

install: venv ## Install dev deps into venv
	$(ACT) && pip install -U pip && pip install -e ".[dev]"

up: ## Start Postgres
	$(COMPOSE) up -d db

down: ## Stop Postgres
	$(COMPOSE) down

db-wait: ## Block until Postgres is healthy
	@until $(COMPOSE) exec -T db pg_isready -U reorderos -d reorderos >/dev/null 2>&1; do \
	  echo "waiting for db..."; sleep 1; \
	done

migrate: db-wait ## Apply Alembic migrations
	$(ACT) && alembic upgrade head

downgrade: ## Roll back one migration
	$(ACT) && alembic downgrade -1

revision: ## New autogenerate migration: make revision m="add foo"
	$(ACT) && alembic revision --autogenerate -m "$(m)"

dev: up migrate ## One-command local start
	$(ACT) && uvicorn app.main:app --reload --port $${APP_PORT:-8000}

lint: ## Ruff lint + format check
	$(ACT) && ruff check . && ruff format --check .

typecheck: ## mypy strict
	$(ACT) && mypy app

test: ## Unit tests (no DB)
	$(ACT) && pytest -m "not integration"

test-int: up migrate ## Integration tests against compose Postgres
	$(ACT) && pytest -m integration

openapi: ## Dump OpenAPI schema to openapi.json
	$(ACT) && python scripts/export_openapi.py

clean:
	rm -rf $(VENV) .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov openapi.json
