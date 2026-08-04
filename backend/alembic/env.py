"""Alembic env. Async-friendly: spins up an async engine for online migrations."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import every modules' models package so autogenerate sees them.
import app.modules.auth.models
import app.modules.invitations.models
import app.modules.tenants.models  # noqa: F401
from alembic import context
from app.core.config import normalize_postgres_url
from app.core.database import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read DATABASE_URL directly — deliberately WITHOUT constructing full Settings. The
# migrate PRE_DEPLOY job runs DDL only; it must not be forced to supply runtime
# request/worker secrets (WorkOS/Clover/Postmark/token/service DSN) just to import.
# `app.core.config`'s lazy `settings` proxy means importing it (via Base above) does not
# trigger the production fail-closed check; here we take only the one field Alembic uses.
_DEFAULT_URL = "postgresql+asyncpg://reorderos:reorderos@localhost:5432/reorderos"


def _dotenv_database_url() -> str | None:
    """Minimal `.env` fallback for the ONE key Alembic uses, preserving the local-dev
    `alembic upgrade head` workflow that Settings' env_file previously provided — still
    without constructing Settings. Real env vars take precedence (same as pydantic).
    Deployed images carry no .env (gitignored), so DO jobs are unaffected."""
    from pathlib import Path

    try:
        text = Path(".env").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATABASE_URL":
            return value.strip().strip("'\"") or None
    return None


_database_url = normalize_postgres_url(
    os.environ.get("DATABASE_URL") or _dotenv_database_url() or _DEFAULT_URL
)
config.set_main_option("sqlalchemy.url", str(_database_url))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _is_production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _run_capability_preflight(connection: Connection) -> None:
    # Basic CREATE-capability preflight (see assert_migration_capability_sync docs
    # for exactly what it does and does NOT prove). MUST run on its own connection,
    # not the migration connection: issuing a query here opens a transaction that
    # Alembic's begin_transaction() would treat as externally-managed and decline to
    # commit — silently rolling back the whole migration. Kept separate so the
    # migration connection's transaction is owned solely by Alembic.
    from app.core.rls_assert import assert_migration_capability_sync

    assert_migration_capability_sync(connection)


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    # Fail-closed capability preflight FIRST, on a throwaway connection, so a role
    # lacking DDL rights aborts before the migration connection is opened. Gated on
    # production so local/CI (superuser) is unaffected.
    if _is_production():
        async with connectable.connect() as check_conn:
            await check_conn.run_sync(_run_capability_preflight)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
