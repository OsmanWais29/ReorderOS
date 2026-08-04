"""Fail-closed runtime-role assertions.

Each runtime pool must run as a role that is NEITHER superuser NOR bypassrls, so
RLS is the enforced tenant-isolation control. These are pure (session in, raise or
role name out), so the startup gates are directly unit-testable. Log/raise carry
role names + booleans only — never DSNs or credentials.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

_FACTS = text(
    "SELECT current_user,"
    " (SELECT rolsuper FROM pg_roles WHERE rolname=current_user),"
    " (SELECT rolbypassrls FROM pg_roles WHERE rolname=current_user)"
)


async def _facts(session: Any) -> tuple[str, bool, bool]:
    cu, is_super, bypass = (await session.execute(_FACTS)).one()
    return str(cu), bool(is_super), bool(bypass)


async def assert_request_role_session(session: Any, *, expected: str = "reorderos_app") -> str:
    """Request pool must be exactly `expected`, a MEMBER of app_user, non-super,
    non-bypassrls. Returns current_user; raises RuntimeError otherwise."""
    cu, is_super, bypass = await _facts(session)
    is_member = bool(
        (
            await session.execute(text("SELECT pg_has_role(current_user,'app_user','MEMBER')"))
        ).scalar_one()
    )
    if cu != expected or not is_member or is_super or bypass:
        raise RuntimeError(
            f"request-pool role invalid: current_user={cu} expected={expected} "
            f"app_user_member={is_member} rolsuper={is_super} rolbypassrls={bypass}"
        )
    return cu


async def assert_service_role_session(session: Any, *, expected: str | None = None) -> str:
    """Service/worker pool must be exactly `expected`, non-super, non-bypassrls.
    `expected` defaults to Settings.service_role_name ("service_worker"; a versioned
    replacement role during an outage-safe credential rotation — runbook Phase B-alt)."""
    if expected is None:
        from app.core.config import get_settings

        expected = get_settings().service_role_name
    cu, is_super, bypass = await _facts(session)
    if cu != expected or is_super or bypass:
        raise RuntimeError(
            f"service-pool role invalid: current_user={cu} expected={expected} "
            f"rolsuper={is_super} rolbypassrls={bypass}"
        )
    return cu


async def assert_service_pool_role() -> str:
    """Worker startup gate: open a service session and assert its role."""
    from app.core.service_db import get_service_sessionmaker

    async with get_service_sessionmaker()() as s:
        return await assert_service_role_session(s)


async def assert_service_pool_role_if_enabled() -> str | None:
    """Worker startup gate, gated on RESTRICTED_RUNTIME_ROLES_ENABLED (default false).

    APP_ENV=production alone must NOT activate the role assertion — production
    currently runs its service pool on an admin DSN and must keep booting unchanged
    until its own approved cutover. When the flag IS true the assertion is mandatory
    and fail-closed: a wrong or missing role raises and prevents worker startup.
    Returns the asserted role name, or None when the flag is off (nothing checked —
    no service session is opened)."""
    from app.core.config import get_settings

    if not get_settings().restricted_runtime_roles_enabled:
        return None
    return await assert_service_pool_role()


def assert_migration_capability_sync(sync_conn: Any) -> str:
    """Migration-job preflight — BASIC CREATE-capability check only.

    Proves the connected role has CREATE on the target database AND CREATE on the
    public schema (so it can create new objects), keyed on real privileges rather
    than an inference from the username. Returns current_user; raises otherwise.

    Scope (be precise — this is NOT a full Alembic-DDL proof):
      - It does NOT prove ownership of, or ALTER/DROP rights over, every object an
        individual migration touches (policies, existing tables, sequences, grants).
        A migration that ALTERs an object owned by another role can still fail after
        this check passes.
      - It is a fail-closed smoke test to catch the gross misconfiguration — a runtime
        role (e.g. reorderos_app/service_worker, no CREATE) accidentally wired as the
        migrate DSN — before Alembic runs. Ownership/ALTER coverage is left to the
        migration actually executing (transactional DDL rolls back cleanly on failure).

    MUST be run on a connection separate from the migration connection (see
    alembic/env.py): issuing this query on the migration connection opens a
    transaction Alembic will not commit, silently rolling back the migration."""
    row = sync_conn.exec_driver_sql(
        "SELECT current_user, "
        "has_database_privilege(current_user, current_database(), 'CREATE'), "
        "has_schema_privilege(current_user, 'public', 'CREATE')"
    ).one()
    cu, db_create, schema_create = str(row[0]), bool(row[1]), bool(row[2])
    if not (db_create and schema_create):
        raise RuntimeError(
            f"migration role lacks basic CREATE capability: current_user={cu} "
            f"db_create={db_create} schema_create={schema_create}"
        )
    return cu
